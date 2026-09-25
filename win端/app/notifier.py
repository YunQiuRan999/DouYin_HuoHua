from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from app.models import TargetResult


MAX_RESULTS_PER_SECTION = 15
MAX_MARKDOWN_BYTES = 18_000
NOTIFY_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


async def send_dingtalk_notification(
    webhook: str,
    secret: str,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
) -> None:
    title, markdown = build_dingtalk_markdown(task_id, dry_run, results, screenshots)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
        "at": {"isAtAll": False},
    }
    await asyncio.to_thread(_post_json, _signed_webhook_url(webhook, secret), payload)


def build_dingtalk_markdown(
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
    finished_at: datetime | None = None,
) -> tuple[str, str]:
    successes = [result for result in results if result.status == "success"]
    failures = [result for result in results if result.status == "failed"]
    status = "全部成功" if not failures else "存在失败"
    mode = "检查模式（未发送消息）" if dry_run else "正式发送"
    finished = (finished_at or datetime.now(timezone.utc)).astimezone(NOTIFY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %z")
    title = f"抖音自动发送：{status}"
    lines = [
        f"### {title}",
        "",
        f"> **任务**：{_markdown_text(task_id, limit=100)}  ",
        f"> **模式**：{mode}  ",
        f"> **完成时间**：{finished}  ",
        f"> **结果**：成功 {len(successes)} 人，失败 {len(failures)} 人",
        "",
        f"#### 成功名单（{len(successes)}）",
    ]
    if successes:
        for index, result in enumerate(successes[:MAX_RESULTS_PER_SECTION], 1):
            detail = "验证通过" if dry_run else f"已发送 {result.sent} 条"
            lines.append(f"{index}. **{_markdown_text(result.target, limit=100)}** - {detail}")
        if len(successes) > MAX_RESULTS_PER_SECTION:
            lines.append(f"- 其余 {len(successes) - MAX_RESULTS_PER_SECTION} 人已省略")
    else:
        lines.append("无")

    lines.extend(["", f"#### 失败名单（{len(failures)}）"])
    if failures:
        for index, result in enumerate(failures[:MAX_RESULTS_PER_SECTION], 1):
            error = _markdown_text(result.error or "未知错误", limit=300)
            sent = f"，已发送 {result.sent} 条" if result.sent else ""
            lines.append(f"{index}. **{_markdown_text(result.target, limit=100)}**{sent}")
            lines.append(f"   - 原因：{error}")
        if len(failures) > MAX_RESULTS_PER_SECTION:
            lines.append(f"- 其余 {len(failures) - MAX_RESULTS_PER_SECTION} 人已省略")
    else:
        lines.append("无")

    if screenshots:
        lines.extend(["", "#### 失败截图"])
        lines.extend(f"- `{_markdown_text(path.name, limit=100)}`" for path in screenshots[:MAX_RESULTS_PER_SECTION])
        run_url = _github_run_url()
        if run_url:
            lines.extend(
                [
                    "",
                    f"[打开本次 GitHub Actions 运行并下载截图]({run_url})",
                    "",
                    "> 截图将在任务结束后出现在该次运行底部的 Artifacts 中。",
                ]
            )

    return title, _truncate_utf8("\n".join(lines), MAX_MARKDOWN_BYTES)


def _signed_webhook_url(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    signature = base64.b64encode(hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()).decode()
    parsed = urlsplit(webhook)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((('timestamp', str(timestamp)), ('sign', signature)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _post_json(url: str, payload: dict) -> None:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8")
    result = json.loads(body)
    if result.get("errcode") != 0:
        raise RuntimeError(f"钉钉机器人返回错误: {result.get('errmsg', body)}")


def _github_run_url() -> str | None:
    server = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if not server or not repository or not run_id:
        return None
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"


def _markdown_text(value: str, limit: int | None = None) -> str:
    text = " ".join(value.splitlines()).strip()
    if limit is not None and len(text) > limit:
        text = f"{text[:limit - 3]}..."
    for character in ("\\", "`", "*", "_", "[", "]", "#", ">", "|"):
        text = text.replace(character, f"\\{character}")
    return text


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix = "\n\n> 通知内容过长，部分内容已省略。"
    available = max_bytes - len(suffix.encode("utf-8"))
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode("utf-8")) <= available:
            low = middle
        else:
            high = middle - 1
    return f"{text[:low]}{suffix}"
