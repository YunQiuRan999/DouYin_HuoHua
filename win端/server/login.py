"""扫码登录：在无显示器服务器上用 headless 浏览器打开抖音登录页，
把二维码截图供客户端展示；用户扫码后自动检测登录成功并保存登录态。

注意：服务器出口 IP 可能触发抖音安全验证，若二维码流程失败，
可通过账号 API 直接导入 Cookie（Cookie-Editor 导出的 JSON）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from server import core

LOGGER = logging.getLogger("douyin_server")

DOUYIN_HOME = "https://www.douyin.com/"
DOUYIN_CHAT = "https://www.douyin.com/chat"
LOGIN_TIMEOUT_SECONDS = 180
SESSION_EXPIRE_SECONDS = 600  # 登录会话 10 分钟后自动清理
QR_CLEANUP_DELAY_SECONDS = 300  # 登录成功/失败后 5 分钟删除二维码图片

# 登录弹窗候选选择器（用于精确截取二维码区域）
_QR_PANEL_SELECTORS = (
    '[class*="LoginPanel"]',
    '[class*="loginPanel"]',
    '[class*="login-panel"]',
    '[class*="login_box"]',
    '[class*="loginBox"]',
)


class LoginManager:
    """管理扫码登录会话（sid → 会话状态）。

    headless 说明：
    - headless=False（有头）：弹出真实浏览器窗口，用户可扫码并用鼠标操作人机验证；
    - headless=True（无头）：浏览器不可见，仅截图二维码供客户端展示（服务器场景）。
    """

    def __init__(self, data_dir: Path, browser_path: str = "", headless: bool = True) -> None:
        self.data_dir = data_dir
        self.browser_path = browser_path
        self.headless = headless
        self.sessions: dict[str, dict[str, Any]] = {}
        self._semaphore: asyncio.Semaphore | None = None  # 延迟到事件循环中创建
        self._cleanup_task: asyncio.Task | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        """延迟创建信号量，确保绑定到当前事件循环。"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(2)  # 最多同时两个登录会话
        return self._semaphore

    def start(self, account_id: str, headless: bool | None = None) -> str:
        sid = secrets.token_hex(8)
        use_headless = self.headless if headless is None else headless
        self.sessions[sid] = {
            "status": "starting",
            "account_id": account_id,
            "message": "正在启动浏览器…",
            "created_at": asyncio.get_running_loop().time(),
        }
        asyncio.create_task(self._run(sid, account_id, use_headless))
        # 启动定期清理任务（只启动一次）
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        return sid

    def status(self, sid: str) -> dict[str, Any] | None:
        session = self.sessions.get(sid)
        if session is None:
            return None
        result = {"status": session["status"], "message": session.get("message", "")}
        if session["status"] in ("starting", "pending", "scanning"):
            result["image"] = f"/api/login/qr/{sid}/image"
        if session.get("storage_state"):
            result["storage_state"] = session["storage_state"]
        return result

    async def _cleanup_loop(self) -> None:
        """定期清理过期会话和二维码图片。"""
        while True:
            try:
                await asyncio.sleep(60)
                self._cleanup_expired()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("登录会话清理异常")

    def _cleanup_expired(self) -> None:
        """清理已完成/过期的会话及其二维码图片。"""
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return
        expired = []
        for sid, session in self.sessions.items():
            created = session.get("created_at", 0)
            status = session.get("status")
            # 成功/失败的会话保留 5 分钟后清理；进行中的会话 10 分钟超时
            if status in ("success", "failed"):
                if now - created > QR_CLEANUP_DELAY_SECONDS:
                    expired.append(sid)
            elif now - created > SESSION_EXPIRE_SECONDS:
                expired.append(sid)
        for sid in expired:
            self._remove_session(sid)

    def _remove_session(self, sid: str) -> None:
        """删除会话及其二维码图片。"""
        session = self.sessions.pop(sid, None)
        if session is not None:
            qr_path = self.data_dir / "qr" / f"{sid}.png"
            try:
                if qr_path.is_file():
                    qr_path.unlink()
            except OSError:
                pass

    async def _run(self, sid: str, account_id: str, headless: bool) -> None:
        async with self._get_semaphore():
            await self._execute(sid, account_id, headless)

    async def _execute(self, sid: str, account_id: str, headless: bool) -> None:
        from playwright.async_api import async_playwright

        from app.browser import launch_browser
        from app.selectors import LOGIN_MARKERS, SEARCH_INPUTS
        from app.stealth import STEALTH_SCRIPT

        session = self.sessions[sid]
        qr_dir = self.data_dir / "qr"
        qr_dir.mkdir(parents=True, exist_ok=True)
        image_path = qr_dir / f"{sid}.png"
        output = self.data_dir / "storage_state" / f"{account_id}.json"
        output.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with async_playwright() as playwright:
                browser = await launch_browser(playwright, headless=headless, browser_path=self.browser_path or None)
                try:
                    context = await browser.new_context(
                        locale="zh-CN",
                        viewport={"width": 900, "height": 850},
                    )
                    await context.add_init_script(STEALTH_SCRIPT)
                    page = await context.new_page()
                    session["status"] = "pending"
                    session["message"] = "正在打开抖音登录页…"
                    # asyncio.wait_for 兜底：即使 driver 挂死也能在 60 秒内返回
                    await asyncio.wait_for(
                        page.goto(DOUYIN_HOME, wait_until="domcontentloaded", timeout=45_000),
                        timeout=60,
                    )
                    await page.wait_for_timeout(2_000)
                    await _open_login_panel(page)
                    await page.wait_for_timeout(1_000)

                    await _snapshot_qr(page, image_path)
                    session["status"] = "scanning"
                    session["message"] = "请使用抖音 App 扫码确认登录"

                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + LOGIN_TIMEOUT_SECONDS
                    while True:
                        if await _home_logged_in(page):
                            session["message"] = "检测到登录成功，正在保存登录态…"
                            try:
                                await page.goto(DOUYIN_CHAT, wait_until="domcontentloaded", timeout=30_000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(2_500)
                            if await _chat_ready(page, SEARCH_INPUTS + LOGIN_MARKERS):
                                await context.storage_state(path=str(output))
                                # 登录态=账号接管凭证，收紧文件权限（仅所有者可读，POSIX）
                                try:
                                    os.chmod(output, 0o600)
                                except OSError:
                                    pass
                                session.update(status="success", storage_state=str(output), message="登录成功，登录态已保存")
                                LOGGER.info("扫码登录成功: %s -> %s", account_id, output)
                                return
                        if loop.time() > deadline:
                            session.update(status="failed", message=f"等待扫码超时（{LOGIN_TIMEOUT_SECONDS} 秒）")
                            return
                        # 3 秒轮询：平衡登录成功检测速度与浏览器 CPU 占用（2 秒过密会导致浏览器卡顿）
                        await page.wait_for_timeout(3_000)
                finally:
                    try:
                        await browser.close()
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("扫码登录会话异常")
            session.update(status="failed", message=str(exc))

    def qr_image_path(self, sid: str) -> Path | None:
        path = self.data_dir / "qr" / f"{sid}.png"
        return path if path.is_file() else None


async def _open_login_panel(page) -> None:
    """点击首页「登录」并切换到「扫码登录」面板。"""
    login = page.get_by_text("登录", exact=True)
    if await login.count():
        try:
            await login.first.click(timeout=8_000)
            await page.wait_for_timeout(1_000)
        except Exception:
            pass
    qr_login = page.get_by_text("扫码登录", exact=True)
    if await qr_login.count():
        try:
            await qr_login.first.click(timeout=5_000)
        except Exception:
            pass


async def _snapshot_qr(page, image_path: Path) -> None:
    """优先截取登录弹窗区域，否则整页截图（每步限时，避免长时间卡住）。"""
    for selector in _QR_PANEL_SELECTORS:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                await locator.screenshot(path=str(image_path), timeout=10_000)
                return
        except Exception:
            continue
    await page.screenshot(path=str(image_path), timeout=10_000)


async def _home_logged_in(page) -> bool:
    """粗略判定：首页右上角「登录」按钮已消失（已登录或已是登录态）。"""
    try:
        login_btn = page.get_by_text("登录", exact=True).first
        if await login_btn.count() and await login_btn.is_visible():
            return False
    except Exception:
        pass
    return True


async def _chat_ready(page, selectors) -> bool:
    """私信页已就绪：出现好友搜索框等登录标记。"""
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=2_500)
            return True
        except Exception:
            continue
    return False
