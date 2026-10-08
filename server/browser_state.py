"""
browser_state.py — Browser tab management and WebSocket communication.
"""

import time
import asyncio
import logging
import json
import hashlib
from typing import Optional

from .config import WS_RESPONSE_TIMEOUT, WS_PING_INTERVAL

logger = logging.getLogger("browser-agent")


def dom_fingerprint(dom: Optional[dict]) -> str:
    """Canonical DOM fingerprint used for state-change detection.

    Captures url, title, counts of all interactive element types, and a 500-char
    prefix of the visible text. Same input → same hash; if the page changed in
    any of these dimensions, the hash changes. This is the single source of
    truth for "did the page change?" — used by the agent loop, navigation
    verification, and action verification.
    """
    if not isinstance(dom, dict):
        return ""
    material = {
        "url": dom.get("url", ""),
        "title": dom.get("title", ""),
        "inputs": len(dom.get("inputs", [])),
        "buttons": len(dom.get("buttons", [])),
        "links": len(dom.get("links", [])),
        "selects": len(dom.get("selects", [])),
        "checkboxes": len(dom.get("checkboxes", [])),
        "radios": len(dom.get("radioButtons", [])),
        "text": (dom.get("textSummary", "") or "")[:500],
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=True)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:20]


class BrowserTab:
    __slots__ = ("tab_id", "url", "title", "dom_state", "last_updated")

    def __init__(self, tab_id: str):
        self.tab_id = tab_id
        self.url = ""
        self.title = ""
        self.dom_state: Optional[dict] = None
        self.last_updated = time.time()


class BrowserManager:
    def __init__(self):
        self.tabs: dict[str, BrowserTab] = {}
        self.active_tab_id: Optional[str] = None
        self._browser_ws = None  # WebSocket instance
        self._pending: dict[str, asyncio.Future] = {}
        self._req_counter: int = 0
        self._send_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._ping_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._browser_ws is not None

    def set_browser_ws(self, ws):
        old = self._browser_ws
        if self._ping_task:
            self._ping_task.cancel()
            self._ping_task = None
        self._browser_ws = ws
        if old is not None and old is not ws:
            # Old socket is gone — fail any in-flight requests so callers don't hang.
            self._fail_all_pending("Browser WebSocket reconnected; previous request abandoned")
        if ws is None and old is not None:
            self._fail_all_pending("Browser WebSocket disconnected")
        elif ws is not None:
            self._ping_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        try:
            while self._browser_ws:
                await asyncio.sleep(WS_PING_INTERVAL)
                if self._browser_ws:
                    try:
                        async with self._send_lock:
                            await self._browser_ws.send(json.dumps({"type": "PING"}))
                    except Exception as error:
                        logger.warning(f"Heartbeat failed — connection stale: {error}")
                        self.set_browser_ws(None)
                        break
        except asyncio.CancelledError:
            pass

    def _next_req_id(self) -> str:
        self._req_counter += 1
        return f"req_{self._req_counter}"

    async def resolve_pending(self, request_id: str, data: dict) -> bool:
        async with self._pending_lock:
            fut = self._pending.get(request_id)
            if fut and not fut.done():
                fut.set_result(data)
                return True
        return False

    def _fail_all_pending(self, msg: str):
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError(msg))
        self._pending.clear()

    def register_tab(self, tab_id: str) -> BrowserTab:
        tab_id = str(tab_id)
        if tab_id not in self.tabs:
            self.tabs[tab_id] = BrowserTab(tab_id)
            logger.info(f"Registered tab: {tab_id}")
        if self.active_tab_id is None:
            self.active_tab_id = tab_id
        return self.tabs[tab_id]

    def update_dom(self, tab_id: str, dom: dict):
        tab_id = str(tab_id)
        if tab_id not in self.tabs:
            self.register_tab(tab_id)
        t = self.tabs[tab_id]
        t.dom_state = dom
        t.url = dom.get("url", t.url)
        t.title = dom.get("title", t.title)
        t.last_updated = time.time()

    def get_active_tab(self) -> Optional[BrowserTab]:
        if self.active_tab_id and self.active_tab_id in self.tabs:
            return self.tabs[self.active_tab_id]
        if self.tabs:
            first = next(iter(self.tabs.values()))
            self.active_tab_id = first.tab_id
            return first
        return None

    def resolve_tab(self, tab_id: Optional[str] = None) -> BrowserTab:
        if not self.connected:
            raise ConnectionError(
                "No browser connection. Make sure the Chrome extension is connected."
            )
        if tab_id:
            tab_id = str(tab_id)
            if tab_id not in self.tabs:
                raise ValueError(f"Tab {tab_id} not found. Use browser_list_tabs.")
            return self.tabs[tab_id]
        tab = self.get_active_tab()
        if not tab:
            raise ValueError("No active browser tabs.")
        return tab

    def set_active_tab(self, tab_id: str):
        tab_id = str(tab_id)
        if tab_id in self.tabs:
            self.active_tab_id = tab_id

    def remove_tab(self, tab_id: str):
        tab_id = str(tab_id)
        if tab_id in self.tabs:
            del self.tabs[tab_id]
            if self.active_tab_id == tab_id:
                self.active_tab_id = next(iter(self.tabs), None)

    def clear_all(self):
        self.tabs.clear()
        self.active_tab_id = None

    async def send(self, message: dict, timeout: float = WS_RESPONSE_TIMEOUT) -> dict:
        if not self._browser_ws:
            raise ConnectionError("No browser connection available.")
        req_id = self._next_req_id()
        message["request_id"] = req_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        async with self._pending_lock:
            self._pending[req_id] = fut
        try:
            async with self._send_lock:
                await self._browser_ws.send(json.dumps(message))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Extension did not respond within {timeout}s")
        except Exception as e:
            if isinstance(e, (ConnectionError, TimeoutError)):
                raise
            raise ConnectionError(f"WebSocket communication failed: {e}")
        finally:
            async with self._pending_lock:
                self._pending.pop(req_id, None)


# Module-level singleton
browser_manager = BrowserManager()


# ── Helper: retry-safe sends ─────────────────────────────────────────────────

SAFE_RETRY_MESSAGE_TYPES = {
    "REQUEST_DOM", "TAKE_SCREENSHOT", "REQUEST_HTML", "EXTRACT_TEXT",
    "GET_ELEMENT_INFO", "WAIT_FOR_ELEMENT", "GET_CODE",
    "EXTRACT_CODING_PROBLEM", "EXTRACT_QUIZ_STRUCTURE", "EXTRACT_PAGE_CONTEXT",
}


def _steps_retry_safe(message: dict) -> bool:
    steps = message.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    # Only retry action batches that are explicitly idempotent at step level.
    for step in steps:
        if not isinstance(step, dict):
            return False
        token = step.get("idempotency_token")
        action = str(step.get("action", "")).lower()
        if token:
            continue
        if action in {"wait", "hover", "focus", "scroll"}:
            continue
        return False
    return True


def _is_retriable_message(message: dict) -> bool:
    message_type = message.get("type", "")
    if message_type in SAFE_RETRY_MESSAGE_TYPES:
        return True
    if message_type == "EXECUTE_ACTIONS":
        return _steps_retry_safe(message)
    return False


async def send_with_retries(
    message: dict,
    timeout: float = WS_RESPONSE_TIMEOUT,
    retries: int = 2,
) -> dict:
    attempts = max(1, retries + 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await browser_manager.send(message, timeout=timeout)
        except (ConnectionError, TimeoutError) as error:
            last_error = error
            should_retry = _is_retriable_message(message) and attempt < attempts
            if not should_retry:
                break
            await asyncio.sleep(0.25 * attempt)
    raise last_error if last_error else TimeoutError("Request failed with unknown error")
