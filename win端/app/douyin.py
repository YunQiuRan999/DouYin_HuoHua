from __future__ import annotations

import asyncio
import random
import re

from playwright.async_api import Locator, Page

from app.selectors import CHAT_PANEL_MARKERS, MESSAGE_INPUTS, SEARCH_INPUTS


class PageOperationError(RuntimeError):
    pass


RETRY_DELAY_MS = 3_000


class DouyinChat:
    def __init__(
        self,
        page: Page,
        timeout_ms: int = 15_000,
        confirm_timeout_ms: int = 15_000,
    ) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.confirm_timeout_ms = confirm_timeout_ms

    async def open_target(self, name: str, retries: int = 1) -> None:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                await self._open_target_once(name)
                return
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    await self.page.wait_for_timeout(RETRY_DELAY_MS)
        if last_error is not None:
            raise last_error
        raise PageOperationError("打开聊天失败")

    async def _open_target_once(self, name: str) -> None:
        search = await first_visible(self.page, SEARCH_INPUTS, self.timeout_ms)
        await search.click()
        # 清空输入框：全选删除比 fill("") 更接近真人操作
        try:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Delete")
        except Exception:
            await search.fill("")
        # 逐字输入好友名（产生完整键盘事件流，搜索是每次任务的第一动作）
        await type_text_like_human(self.page, name)
        await self.page.wait_for_timeout(1_500)

        result = await self._search_result(name)
        if result is None:
            raise PageOperationError("搜索不到目标好友")
        await result.click(force=True)
        await self._confirm_opened(name)

    async def _search_result(self, name: str) -> Locator | None:
        # Search mode renders a separate SearchPanel. Its "发消息" action is the
        # correct control; clicking the hidden conversation cache does not mount
        # the composer.
        #
        # Identity must be resolved from the per-result name node, never from the
        # collection container: `[class*="SearchPanelitem"]` also matches an outer
        # `SearchPanelitems` wrapper, whose descendants would then contain the
        # target name while `.first` returns another row's button. Scope to result
        # rows and require the matched name node and its button to be visible here.
        search_items = self.page.locator('[class*="SearchPanelitembox"], [class*="SearchPanelitem-box"], [class*="SearchPanelitem_box"]')
        name_selectors = (
            '[class*="SearchPanelitemtitle"]',
            '[class*="SearchPanelitemTitle"]',
            '[class*="SearchPanelitem_title"]',
            '[class*="SearchPanelitem-title"]',
            '[class*="SearchPanelitemname"]',
            '[class*="SearchPanelitemName"]',
            '[class*="SearchPanelitem_name"]',
            '[class*="SearchPanelitem-name"]',
        )

        # Two-phase priority: an exact friend name always wins over a group whose
        # display name happens to start with it. Pass 1 scans every search row for
        # an exact name; only if none is found does pass 2 accept a group member
        # count suffix like "4161(7)" for target "4161". This ordering guarantees
        # "test" never returns "test(7)" or "test1".
        for index in range(await search_items.count()):
            item = search_items.nth(index)
            name_locator = await _visible_exact_text_locator(item, name_selectors, name)
            if name_locator is None:
                continue
            button = item.locator('[class*="SearchPanelitemchat_btn"]').first
            try:
                if await button.count() and await button.is_visible():
                    return button
            except Exception:
                continue

        for index in range(await search_items.count()):
            item = search_items.nth(index)
            name_locator = await _visible_group_text_locator(item, name_selectors, name)
            if name_locator is None:
                continue
            button = item.locator('[class*="SearchPanelitemchat_btn"]').first
            try:
                if await button.count() and await button.is_visible():
                    return button
            except Exception:
                continue

        # The nickname node can be hidden while its conversation row is visible.
        # Locate and click the complete row instead of relying on text visibility.
        row_selectors = (
            '[data-e2e="conversation-item"]',
            '[class*="conversationConversationItem"]',
            '[class*="conversation-item"]',
            '[class*="ConversationItem"]',
        )
        title_selectors = (
            '[class*="conversationConversationItemtitle"]',
            '[class*="ConversationItemtitle"]',
            '[class*="ConversationItemTitle"]',
            '[class*="conversation-item-title"]',
            '[class*="conversation-item-Title"]',
        )
        for selector in row_selectors:
            rows = self.page.locator(selector)
            for index in range(await rows.count()):
                row = rows.nth(index)
                title_locator = await _visible_exact_text_locator(row, title_selectors, name)
                if title_locator is None:
                    continue
                try:
                    if await row.is_visible():
                        return row
                except Exception:
                    continue

        # Second-phase group suffix over conversation rows (same priority rule).
        for selector in row_selectors:
            rows = self.page.locator(selector)
            for index in range(await rows.count()):
                row = rows.nth(index)
                title_locator = await _visible_group_text_locator(row, title_selectors, name)
                if title_locator is None:
                    continue
                try:
                    if await row.is_visible():
                        return row
                except Exception:
                    continue

        # Some Douyin builds render the title itself as hidden, but keep a visible
        # ancestor as the actionable result. Find that ancestor from the hidden title.
        # This hidden-title fallback stays STRICT exact only: a hidden stale name
        # node (group or plain) must never be trusted to resolve the recipient.
        hidden_titles = self.page.locator('[class*="conversationConversationItemtitle"]')
        for index in range(await hidden_titles.count()):
            title = hidden_titles.nth(index)
            if not await _text_equals(title, name):
                continue
            row = title.locator(
                "xpath=ancestor::*[contains(@class, 'conversationConversationItem')][1]"
            )
            if await row.count() and await row.is_visible():
                return row

        return None

    async def message_input(self) -> Locator:
        return await first_visible(self.page, MESSAGE_INPUTS, self.timeout_ms)

    async def _confirm_opened(self, name: str, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms if timeout_ms is not None else self.confirm_timeout_ms
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            last_error = await self._chat_open_error(name)
            if last_error is None:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise last_error
            await self.page.wait_for_timeout(500)

    async def _chat_open_error(self, name: str) -> PageOperationError | None:
        # Confirm the right-side current chat by the authoritative chat title, which
        # must itself be visible. A visible header retaining a hidden stale name node
        # (common during SPA transitions) must not confirm the wrong recipient, and
        # a secondary username/title field must never substitute for the chat title.
        title_selectors = (
            '[class*="RightPanelHeadertitle"]',
            '[class*="RightPanelHeaderTitle"]',
            '[class*="RightPanelHeader_title"]',
            '[class*="RightPanelHeader-title"]',
            '[class*="chatHeadertitle"]',
            '[class*="ChatHeaderTitle"]',
            '[class*="chatHeader_title"]',
            '[class*="ChatHeader-title"]',
            '[class*="name"]',
            '[class*="Name"]',
            '[class*="nickname"]',
            '[class*="Nickname"]',
        )
        for selector in CHAT_PANEL_MARKERS[:3]:
            headers = self.page.locator(selector)
            for index in range(await headers.count()):
                header = headers.nth(index)
                try:
                    if not await header.is_visible():
                        continue
                except Exception:
                    continue
                if await _visible_exact_or_group_text_in(header, title_selectors, name):
                    return None

        composer_visible = await self._composer_visible()
        return PageOperationError(
            f"点击搜索结果后无法确认聊天已打开（输入框: {'有' if composer_visible else '无'}）"
        )

    async def _composer_visible(self) -> bool:
        for selector in MESSAGE_INPUTS:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return True
            except Exception:
                continue
        return False


async def _visible_exact_text_in(container: Locator, selectors: tuple[str, ...], expected: str) -> bool:
    return await _visible_exact_text_locator(container, selectors, expected) is not None


async def _visible_exact_text_locator(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> Locator | None:
    for selector in selectors:
        nodes = container.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if await _text_equals(node, expected):
                try:
                    if await node.is_visible():
                        return node
                except Exception:
                    continue
    return None


async def _visible_group_text_locator(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> Locator | None:
    # Second-phase match for group chats: accepts a trailing "(N)"/"（N）"
    # member count. Only reached after the exact pass found nothing, so a bare
    # "test" never reaches here when an exact "test" row exists.
    for selector in selectors:
        nodes = container.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if await _group_name_matches(node, expected):
                try:
                    if await node.is_visible():
                        return node
                except Exception:
                    continue
    return None


async def _visible_exact_or_group_text_in(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> bool:
    # Chat-header confirmation: an exact title wins; otherwise a group member
    # count suffix also confirms. The node must be visible in both cases, so a
    # hidden stale title node can never confirm the wrong recipient.
    if await _visible_exact_text_locator(container, selectors, expected) is not None:
        return True
    return await _visible_group_text_locator(container, selectors, expected) is not None


async def _has_exact_text_in(container: Locator, selectors: tuple[str, ...], expected: str) -> bool:
    for selector in selectors:
        if await _has_exact_text(container.locator(selector), expected):
            return True
    return False


async def _has_exact_text(locators: Locator, expected: str) -> bool:
    for index in range(await locators.count()):
        if await _text_equals(locators.nth(index), expected):
            return True
    return False


async def _text_equals(locator: Locator, expected: str) -> bool:
    try:
        return (await locator.inner_text(timeout=500)).strip() == expected
    except Exception:
        return False


# Matches a group chat display name: the configured target name optionally
# followed by exactly one pair of brackets containing a pure member count,
# e.g. "4161" / "4161(7)" / "4161（123）". This is an INDEPENDENT helper kept
# separate from _text_equals so the friend exact-match semantics stay strict:
# it must never let "test" match "test1" (no trailing brackets to legitimize a
# longer name). re.fullmatch anchors both ends; re.escape makes the name literal.
_GROUP_COUNT_SUFFIX_RE_TEMPLATE = r"{name}\s*[\(（]\s*\d+\s*[\)）]"


def _group_count_suffix_matches(actual: str, expected: str) -> bool:
    actual = actual.strip()
    expected = expected.strip()
    if actual == expected:
        return True
    pattern = _GROUP_COUNT_SUFFIX_RE_TEMPLATE.format(name=re.escape(expected))
    return re.fullmatch(pattern, actual) is not None


async def _group_name_matches(locator: Locator, expected: str) -> bool:
    try:
        return _group_count_suffix_matches(
            await locator.inner_text(timeout=500), expected
        )
    except Exception:
        return False


async def first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 15_000) -> Locator:
    per_selector = max(500, timeout_ms // max(1, len(selectors)))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=per_selector)
            return locator
        except Exception:
            continue
    raise PageOperationError(f"找不到页面元素，已尝试: {', '.join(selectors)}")


async def type_text_like_human(page: Page, content: str) -> None:
    """按 1-3 字分块输入，产生完整键盘事件流（含中文）。

    Playwright 的 keyboard.type 只对 US 键盘字符产生真实 keydown/keyup，
    中文字符仅有 input 事件。这里对非 ASCII 字符改用 CDP Input.dispatchKeyEvent
    （keyDown 携带 text）注入，产生 keydown → input → keyup 完整事件序列，
    消除"一次性插入整段"和"无按键事件"的机器特征。
    节奏：字符间 80-250ms 随机（贴近真人中文输入速度），小概率出现思考停顿。
    """
    chunk_size = 1 if len(content) <= 4 else random.randint(2, 3)
    chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
    cdp = None
    try:
        for index, chunk in enumerate(chunks):
            if all(ord(character) < 128 for character in chunk):
                await page.keyboard.type(chunk, delay=random.randint(80, 250))
            else:
                if cdp is None:
                    cdp = await page.context.new_cdp_session(page)
                for event_type in ("keyDown", "keyUp"):
                    await cdp.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": event_type,
                            "text": chunk if event_type == "keyDown" else "",
                            "key": chunk,
                            "code": "Unidentified",
                            "windowsVirtualKeyCode": 0,
                        },
                    )
            if index < len(chunks) - 1:
                delay = random.randint(80, 250)
                if random.random() < 0.08:
                    delay += random.randint(200, 500)
                await page.wait_for_timeout(delay)
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass


# 最新一条自己发出的消息气泡（与 sender.LATEST_OUTGOING_MESSAGE 保持一致）
_LATEST_OUTGOING_SELECTOR = (
    '.messageMessageListlist [data-index="0"] '
    '.messageMessageBoxmessageBox:has(.messageMessageBoxcontentBox.messageMessageBoxisFromMe)'
)

# 找到该气泡之前最近的日期/时间标记文本。抖音私信列表在跨天时会插入
# 日期分隔条（"今天/昨天/MM-DD"），气泡旁有小字时间（HH:MM）。
_TODAY_OUTGOING_JS = f"""([latestSel]) => {{
  const latest = document.querySelector(latestSel);
  if (!latest) return null;
  const list = latest.closest('[class*="messageMessageList"]') || document.body;
  const nodes = list.querySelectorAll(
    '[class*="Divider"],[class*="divider"],[class*="date"],[class*="Date"],time,'
    + '[data-e2e*="time"],[data-e2e*="Time"]'
  );
  let lastText = null;
  for (const n of nodes) {{
    if (n === latest) continue;
    // n 在 latest 之后（otherNode 在 node 之前 → PRECEDING）则后续标记都无关
    if (n.compareDocumentPosition(latest) & Node.DOCUMENT_POSITION_PRECEDING) break;
    const t = (n.textContent || '').trim();
    if (t) lastText = t;
  }}
  return lastText;
}}"""


async def has_today_outgoing_message(page: Page) -> bool:
    """检测当前打开的对话：最新一条自己发出的消息是否在今天。

    真人当天已发过消息时返回 True（可据此跳过当天自动发送，避免重复打扰与风控风险）。
    检测失败（页面结构未知/选择器不匹配）时保守返回 False（不跳过，宁发勿漏）。
    """
    try:
        marker = await page.evaluate(_TODAY_OUTGOING_JS, [_LATEST_OUTGOING_SELECTOR])
    except Exception:
        return False
    if not marker:
        return False
    text = str(marker).strip()
    if "今天" in text:
        return True
    if "昨天" in text:
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        # 气泡前最近的标记是 HH:MM 且无"昨天/日期"分隔 → 与当天会话相邻，视为今天
        return True
    return False
