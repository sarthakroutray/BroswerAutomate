"""
navigation.py — Consolidated navigation tool handlers.

Tools:
  browser_navigate  — goto/back/forward/reload/new_tab/close_tab/switch_tab
  browser_list_tabs — list all active tabs
"""

import time
import json
import asyncio
import hashlib
import logging
from typing import Optional

from ..browser_state import browser_manager

logger = logging.getLogger("browser-agent")

try:
    from mcp.types import TextContent
except ImportError:
    from dataclasses import dataclass
    @dataclass
    class TextContent:
        type: str
        text: str


def _dom_hash(dom: dict) -> str:
    if not isinstance(dom, dict):
        return ""
    material = {
        "url": dom.get("url", ""),
        "title": dom.get("title", ""),
        "buttons": len(dom.get("buttons", [])),
        "links": len(dom.get("links", [])),
        "text": (dom.get("textSummary", "") or "")[:300],
    }
    return hashlib.sha1(json.dumps(material, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:16]


async def _capture_nav_snapshot(tab_id: str) -> dict:
    try:
        resp = await browser_manager.send({"type": "REQUEST_DOM", "tab_id": tab_id})
        dom = resp.get("dom_state", {})
        if dom:
            browser_manager.update_dom(tab_id, dom)
    except Exception:
        dom = (browser_manager.tabs.get(tab_id).dom_state if browser_manager.tabs.get(tab_id) else {}) or {}
    tab = browser_manager.tabs.get(tab_id)
    return {
        "url": dom.get("url") or (tab.url if tab else ""),
        "hash": _dom_hash(dom),
    }


async def _verify_transition(tab_id: str, before: dict, wait_seconds: float = 1.8) -> tuple[bool, dict]:
    deadline = time.time() + max(0.4, wait_seconds)
    last = before
    while time.time() < deadline:
        await asyncio.sleep(0.3)
        current = await _capture_nav_snapshot(tab_id)
        last = current
        if current.get("url") != before.get("url") or current.get("hash") != before.get("hash"):
            return True, current
    return False, last


async def handle_navigate(
    action: str,
    url: Optional[str] = None,
    tab_id: Optional[str] = None,
    hard_reload: bool = False,
    active: bool = True,
) -> str:
    """Unified browser navigation: goto URL, back, forward, reload, manage tabs.

    USE THIS TOOL:
    - To navigate to a URL (action='goto', url required)
    - To go back/forward in history (action='back'/'forward')
    - To reload the page (action='reload')
    - To open/close/switch tabs (action='new_tab'/'close_tab'/'switch_tab')

    DO NOT USE THIS TOOL:
    - For clicking links or buttons (use browser_execute_actions)
    - For form submission (use browser_execute_actions)

    Args:
        action: Navigation action - goto|back|forward|reload|new_tab|close_tab|switch_tab
        url: Target URL (required for goto and new_tab)
        tab_id: Tab ID (required for close_tab and switch_tab)
        hard_reload: Bypass cache on reload
        active: Whether new tab should be active

    Returns: Navigation result with URL transition status.
    """
    if action == "goto":
        if not url:
            return "Error: 'url' required for goto"
        tab = browser_manager.resolve_tab(tab_id)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "navigate", "value": url}],
        })
        changed, after = await _verify_transition(tab.tab_id, before, wait_seconds=2.8)
        if changed:
            return f"Navigated to: {after.get('url') or url}"
        return f"Navigation triggered to: {url} (transition not yet confirmed)"

    if action == "back":
        tab = browser_manager.resolve_tab(tab_id)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "go_back"}],
        })
        changed, after = await _verify_transition(tab.tab_id, before)
        return "Navigated back" if changed else "Back action sent; transition not yet confirmed"

    if action == "forward":
        tab = browser_manager.resolve_tab(tab_id)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "go_forward"}],
        })
        changed, after = await _verify_transition(tab.tab_id, before)
        return "Navigated forward" if changed else "Forward action sent; transition not yet confirmed"

    if action == "reload":
        tab = browser_manager.resolve_tab(tab_id)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "reload", "value": "hard" if hard_reload else "soft"}],
        })
        changed, _after = await _verify_transition(tab.tab_id, before)
        base = "Page reloaded" + (" (hard)" if hard_reload else "")
        if changed:
            return base
        return base + " - reload command sent; DOM transition pending"

    if action == "new_tab":
        if not url:
            return "Error: 'url' required for new_tab"
        if not browser_manager.connected:
            return "Error: No browser connection"
        resp = await browser_manager.send({"type": "OPEN_TAB", "url": url, "active": active})
        new_id = str(resp.get("tab_id", ""))
        if new_id:
            browser_manager.register_tab(new_id)
            if active:
                browser_manager.set_active_tab(new_id)
        return f"Opened tab: {url}"

    if action == "close_tab":
        if not tab_id:
            return "Error: tab_id required for close_tab"
        if len(browser_manager.tabs) <= 1:
            return "Cannot close the last tab"
        tab = browser_manager.resolve_tab(tab_id)
        await browser_manager.send({"type": "CLOSE_TAB", "tab_id": tab.tab_id})
        browser_manager.remove_tab(tab.tab_id)
        return f"Closed tab {tab.tab_id}"

    if action == "switch_tab":
        if not tab_id:
            return "Error: tab_id required for switch_tab"
        tab = browser_manager.resolve_tab(tab_id)
        try:
            await browser_manager.send({"type": "SWITCH_TAB", "tab_id": tab.tab_id})
        except Exception:
            pass
        browser_manager.set_active_tab(tab.tab_id)
        return f"Switched to tab {tab.tab_id}: {tab.title or tab.url}"

    return f"Unknown navigation action: {action}"


async def handle_list_tabs() -> str:
    """List all active browser tabs with URLs, titles, and status.

    USE THIS TOOL:
    - To discover available tabs and their IDs
    - To check which tab is currently active
    - To verify browser connection status

    DO NOT USE THIS TOOL:
    - For page content (use browser_get_page_state or browser_extract_text)

    Returns: List of all tabs with IDs, URLs, titles, and active status.
    """
    if not browser_manager.tabs:
        return "No browser tabs connected"
    lines = []
    for t in browser_manager.tabs.values():
        active = " [ACTIVE]" if t.tab_id == browser_manager.active_tab_id else ""
        age = int(time.time() - t.last_updated)
        lines.append(
            f"- {t.tab_id}{active}\n  URL: {t.url or 'N/A'}\n  Title: {t.title or 'N/A'}\n  DOM: {age}s ago"
        )
    return f"Connected: {browser_manager.connected}\n\n" + "\n\n".join(lines)
