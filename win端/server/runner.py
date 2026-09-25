"""任务执行器：串行运行账号任务，复用核心发送引擎并汇总结果。

- 全局异步锁保证同一时刻只运行一个账号（避免环境变量相互污染）；
- 复用 app.main.run()（已通过原项目 140 项测试）；
- 执行过程通过 douyin_sender logger 自动广播到 WebSocket；
- 结果写入 data/runs.json 并同步回账号的 last_run。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from server import core

LOGGER = logging.getLogger("douyin_server")


def build_env(data_dir: Path, account: dict[str, Any]) -> dict[str, str]:
    """构造账号的运行环境变量（全部绝对路径，避免工作目录影响）。"""
    account_id = account["id"]
    env = {
        "TASK_CONFIG": str(data_dir / "tasks" / f"{account_id}.json"),
        "ARTIFACTS_DIR": str(data_dir / "artifacts" / account_id),
        "HEADLESS": "true" if account.get("headless", True) else "false",
        "TRACE": "false",  # 默认关闭 trace，避免 artifacts 留存完整页面快照；调试时再开
        # 持久化浏览器用户目录：让每次任务在同一个浏览器环境中运行，
        # 保留 localStorage/IndexedDB，降低"全新访客+固定登录态"的机器特征。
        "DOUYIN_PROFILE_DIR": str(data_dir / "profiles" / account_id),
    }
    credential_type = account.get("credential_type", "storage")
    credential = account.get("credential", "")
    if credential_type == "storage" and credential:
        env["DOUYIN_STORAGE_STATE"] = str(core.resolve_path(data_dir, credential))
    elif credential_type == "cookie":
        if credential:
            env["DOUYIN_COOKIE"] = str(core.resolve_path(data_dir, credential))
        elif account.get("cookie_raw"):
            env["DOUYIN_COOKIE"] = account["cookie_raw"]
    return env


def credential_summary(env: dict[str, str]) -> str:
    if env.get("DOUYIN_STORAGE_STATE"):
        return f"登录态文件 {env['DOUYIN_STORAGE_STATE']}"
    if env.get("DOUYIN_COOKIE"):
        return "Cookie"
    return "未配置凭证"


class Runner:
    def __init__(self, cfg: dict[str, Any], data_dir: Path) -> None:
        self.cfg = cfg
        self.data_dir = data_dir
        self.runs: list[dict[str, Any]] = core.load_runs(data_dir)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- 查询

    def is_busy(self) -> bool:
        return self._lock.locked()

    def running_accounts(self) -> list[str]:
        return [r.get("account_id", "") for r in self.runs if r.get("running") and r.get("account_id")]

    def account_status(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = account["id"]
        recent = [r for r in self.runs if r.get("account_id") == account_id][-1:]
        base = {
            "id": account_id,
            "running": any(r.get("running") for r in self.runs if r.get("account_id") == account_id and r.get("running")),
        }
        if recent:
            run = recent[-1]
            base.update(
                status=run.get("status"),
                finished_at=run.get("finished_at"),
                total=run.get("total", 0),
                success=run.get("success", 0),
                failed=run.get("failed", 0),
                sent=run.get("sent", 0),
                failures=run.get("failures", []),
            )
        return base

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(run) for run in self.runs[-limit:]]

    # ------------------------------------------------------------- 执行

    async def run_account(self, account: dict[str, Any], dry_run: bool = False, force: bool = False) -> dict[str, Any]:
        """执行单个账号任务；返回结果记录。同一时刻仅允许一个任务。

        force=True 时绕过当日防重复（手动运行/补发，保证消息真实发送）。
        """
        async with self._lock:
            return await self._run_one(account, dry_run, force)

    async def _run_one(self, account: dict[str, Any], dry_run: bool, force: bool) -> dict[str, Any]:
        from app.main import _configure_logging, run as core_run

        account_id = account["id"]
        LOGGER.info("════ [%s] 开始执行（%s%s）════",
                    account_id,
                    "试运行" if dry_run else "正式发送",
                    "，强制发送" if force else "")
        record: dict[str, Any] = {
            "account_id": account_id,
            "group": account.get("group", "默认"),
            "mode": "dry" if dry_run else ("manual" if force else "auto"),
            "force": force,
            "started_at": datetime.now().astimezone().isoformat(),
            "running": True,
            "status": "unknown",
            "total": 0, "success": 0, "failed": 0, "sent": 0,
            "failures": [],
        }
        await asyncio.to_thread(core.save_run, self.data_dir, self.runs, record)

        try:
            cooldown_hours = _cooldown_hours(account)
            if cooldown_hours > 0:
                LOGGER.info("════ [%s] 风控冷却中（剩余约 %.1f 小时），跳过本次执行 ════", account_id, cooldown_hours)
                record["status"] = "cooldown"
                record["failures"] = [{
                    "friend": "",
                    "error": f"账号处于风控冷却期（剩余约 {cooldown_hours:.1f} 小时），已自动跳过，请明天再试",
                }]
                record["failed"] = 1
            else:
                env = build_env(self.data_dir, account)
                if not env.get("DOUYIN_STORAGE_STATE") and not env.get("DOUYIN_COOKIE"):
                    raise ValueError("账号未配置登录凭证，请先扫码登录或导入 Cookie")
                if env.get("DOUYIN_STORAGE_STATE") and not Path(env["DOUYIN_STORAGE_STATE"]).is_file():
                    raise ValueError(f"登录态文件不存在: {env['DOUYIN_STORAGE_STATE']}")
                await asyncio.to_thread(core.write_task_config, self.data_dir, account, force)

                saved = {key: os.environ[key] for key in env if key in os.environ}
                fresh = set(env) - set(saved)
                os.environ.update(env)
                try:
                    _configure_logging(Path(env["ARTIFACTS_DIR"]), label=account_id, reset=True)
                    await core_run(dry_run=dry_run)
                finally:
                    os.environ.update(saved)
                    for key in fresh:
                        os.environ.pop(key, None)
                record.update(self._parse_result(account))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("[%s] 执行异常", account_id)
            # 风控熔断：触发安全验证/风险拦截时，给账号 24-72 小时随机冷却，
            # 避免连续踩雷（风控对重复触发的处罚是逐级加重的）。
            if _is_risk_control_error(exc):
                hours = random.randint(24, 72)
                account["cooldown_until"] = str(time.time() + hours * 3600)
                await asyncio.to_thread(core.save_config, self.cfg)
                LOGGER.warning("[%s] 触发风控拦截，已设置 %d 小时冷却", account_id, hours)
            record["status"] = "failed"
            record["failures"] = [{"friend": "", "error": str(exc)}]
            record["failed"] = 1
        finally:
            record["running"] = False
            record["finished_at"] = datetime.now().astimezone().isoformat()
            account["last_run"] = {
                "status": record["status"],
                "finished_at": record["finished_at"],
                "success": record["success"],
                "failed": record["failed"],
                "sent": record["sent"],
            }
            await asyncio.to_thread(core.save_config, self.cfg)
        LOGGER.info("════ [%s] 执行完成: %s（成功 %d 失败 %d，发送 %d 条）════",
                    account_id, record["status"], record["success"], record["failed"], record["sent"])
        return dict(record)

    def _parse_result(self, account: dict[str, Any]) -> dict[str, Any]:
        """读取 result.json 并把脱敏别名映射回真实好友名。

        状态判定：有失败 → failed；实际发送过或试运行 → success；
        一条都没发且非试运行（当日防重复全部跳过）→ skipped。
        """
        result_file = self.data_dir / "artifacts" / account["id"] / "result.json"
        raw = core.read_json(result_file, None)
        real_names = list(account["task"]["friends"])
        result = {"total": 0, "success": 0, "failed": 0, "sent": 0, "failures": [], "status": "unknown"}
        if not isinstance(raw, dict):
            result["status"] = "failed"
            result["failures"] = [{"friend": "", "error": "未生成运行结果文件"}]
            return result
        for item in raw.get("results", []):
            if not isinstance(item, dict):
                continue
            alias = str(item.get("target", ""))
            friend = _alias_to_name(alias, real_names)
            result["total"] += 1
            result["sent"] += int(item.get("sent", 0) or 0)
            if item.get("status") == "success":
                result["success"] += 1
            elif item.get("status") == "failed":
                result["failed"] += 1
                result["failures"].append({"friend": friend or alias, "error": str(item.get("error") or "未知错误")})
        if result["failed"]:
            result["status"] = "failed"
        elif result["sent"] or raw.get("dry_run"):
            result["status"] = "success"
        else:
            result["status"] = "skipped"
        return result


def _alias_to_name(alias: str, real_names: list[str]) -> str:
    match = re.fullmatch(r"好友(\d+)", alias or "")
    if not match:
        return alias
    index = int(match.group(1)) - 1
    if 0 <= index < len(real_names):
        return real_names[index]
    return alias


def _cooldown_hours(account: dict[str, Any]) -> float:
    """返回账号剩余风控冷却小时数（无冷却时返回 0）。"""
    raw = account.get("cooldown_until")
    if not raw:
        return 0.0
    try:
        until = float(raw)
    except (TypeError, ValueError):
        return 0.0
    remaining = until - time.time()
    return max(0.0, remaining) / 3600.0


def _is_risk_control_error(exc: Exception) -> bool:
    """沿异常链判断是否由抖音风控拦截（安全验证/风险页面）触发。"""
    from app.browser import RiskControlError

    current: Exception | None = exc
    while current is not None:
        if isinstance(current, RiskControlError):
            return True
        current = getattr(current, "__cause__", None)
    return False
