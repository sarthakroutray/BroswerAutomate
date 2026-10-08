"""
navigation.py — The `browser_tabs` tool: navigation and tab management.

Actions: goto, back, forward, reload, new_tab, close_tab, switch_tab, list.
"""

import time
import asyncio
import logging
from typing import Optional

from ..browser_state import browser_manager
from ..errors import format_tool_result

logger = logging.getLogger("browser-agent")

TAB_ACTIONS = {"goto", "back", "forward", "reload", "new_tab", "close_tab", "switch_tab", "list"}


def _dom_hash(dom: dict) -> str:
    if not isinstance(dom, dict):
        return ""
    from ..browser_state import dom_fingerprint
    return dom_fingerprint(dom)


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


async def _verify_transition(tab_id: str, before: dict, wait_seconds: float = 5.0) -> tuple[bool, dict]:
    """Poll for a URL or DOM change after triggering navigation.

    Slow sites can take 4-6s to fully transition. We poll at a moderate cadence
    (0.5s) up to the total ceiling. URL changes count as a confirmed transition
    even if the DOM hasn't fully repainted yet, since the next tool call will
    pick up the new page state anyway.
    """
    deadline = time.time() + max(0.4, wait_seconds)
    last = before
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        current = await _capture_nav_snapshot(tab_id)
        last = current
        if current.get("url") != before.get("url") or current.get("hash") != before.get("hash"):
            return True, current
    return False, last


def _validate_url(url: Optional[str], action: str) -> Optional[str]:
    if not url:
        return f"'url' required for {action}"
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"{action} only allows http:// and https:// URLs"
    return None


async def handle_tabs(
    action: str,
    url: Optional[str] = None,
    tab_id: Optional[str] = None,
    hard_reload: bool = False,
    active: bool = True,
) -> str:
    """Navigate the browser and manage tabs.

    USE THIS TOOL:
    - action='goto': navigate the current tab to a URL
    - action='back' / 'forward': history navigation
    - action='reload': reload (hard_reload=True bypasses cache)
    - action='new_tab': open a URL in a new tab (active=False keeps it in background)
    - action='close_tab' / 'switch_tab': manage tabs (needs tab_id)
    - action='list': list all tabs with IDs, URLs, titles, active flag

    DO NOT USE THIS TOOL:
    - For clicking links (use browser_act with click) — only use goto when you
      know the exact URL

    Args:
        action: goto|back|forward|reload|new_tab|close_tab|switch_tab|list
        url: Target URL (required for goto and new_tab; http/https only).
        tab_id: Tab ID (required for close_tab and switch_tab; optional elsewhere).
        hard_reload: Bypass cache on reload.
        active: Whether a new tab should be focused.

    Returns: JSON envelope with the navigation/tab result and transition status.
    """
    action = (action or "").strip().lower()
    if action not in TAB_ACTIONS:
        return format_tool_result(
            status="error", code="INVALID_ENUM",
            message=f"Unknown action '{action}'. Valid actions: {sorted(TAB_ACTIONS)}",
            data={"action": action},
        )

    if action == "list":
        tabs = []
        for t in browser_manager.tabs.values():
            tabs.append({
                "tab_id": t.tab_id,
                "url": t.url,
                "title": t.title,
                "active": t.tab_id == browser_manager.active_tab_id,
                "last_updated_seconds_ago": int(time.time() - t.last_updated),
            })
        return format_tool_result(
            status="success",
            code="TAB_LIST_READY",
            message="Browser tabs listed" if tabs else "No browser tabs connected",
            data={
                "connected": browser_manager.connected,
                "active_tab_id": browser_manager.active_tab_id,
                "tabs": tabs,
            },
        )

    if action in ("goto", "new_tab"):
        err = _validate_url(url, action)
        if err:
            code = "MISSING_REQUIRED_FIELD" if not url else "UNSAFE_OPERATION"
            return format_tool_result(status="error", code=code, message=err, data={"url": url})

    if action == "goto":
        tab = browser_manager.resolve_tab(tab_id)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "navigate", "value": url}],
        })
        changed, after = await _verify_transition(tab.tab_id, before, wait_seconds=6.0)
        message = f"Navigated to {after.get('url') or url}" if changed else f"Navigation triggered to {url} but transition is not yet confirmed"
        return format_tool_result(
            status="success",
            code="NAVIGATION_TRIGGERED",
            message=message,
            data={"tab_id": tab.tab_id, "url": url, "confirmed": changed, "after": after},
        )

    if action in ("back", "forward"):
        nav_action = "go_back" if action == "back" else "go_forward"
        tab = browser_manager.resolve_tab(tab_id)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": nav_action}],
        })
        changed, after = await _verify_transition(tab.tab_id, before, wait_seconds=5.0)
        return format_tool_result(
            status="success",
            code="NAVIGATION_TRIGGERED",
            message=f"Navigated {action}" if changed else f"{action} action sent; transition not yet confirmed",
            data={"tab_id": tab.tab_id, "confirmed": changed, "after": after},
        )

    if action == "reload":
        tab = browser_manager.resolve_tab(tab_id)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "reload", "value": "hard" if hard_reload else "soft"}],
        })
        changed, _after = await _verify_transition(tab.tab_id, before, wait_seconds=5.0)
        base = "Page reloaded" + (" (hard)" if hard_reload else "")
        return format_tool_result(
            status="success",
            code="NAVIGATION_TRIGGERED",
            message=base if changed else base + " - reload command sent; DOM transition pending",
            data={"tab_id": tab.tab_id, "confirmed": changed, "hard_reload": hard_reload},
        )

    if action == "new_tab":
        if not browser_manager.connected:
            return format_tool_result(status="error", code="NO_BROWSER_CONNECTION", message="No browser connection", data={})
        resp = await browser_manager.send({"type": "OPEN_TAB", "url": url, "active": active})
        if resp.get("error"):
            return format_tool_result(status="error", code="TOOL_EXECUTION_ERROR", message=f"Error opening tab: {resp['error']}", data={"url": url})
        new_id = str(resp.get("tab_id", ""))
        if new_id:
            browser_manager.register_tab(new_id)
            if active:
                browser_manager.set_active_tab(new_id)
        return format_tool_result(
            status="success",
            code="TAB_OPENED",
            message=f"Opened tab: {url}",
            data={"tab_id": new_id, "url": url, "active": active},
        )

    if action == "close_tab":
        if not tab_id:
            return format_tool_result(status="error", code="MISSING_REQUIRED_FIELD", message="tab_id required for close_tab", data={"tab_id": tab_id})
        if len(browser_manager.tabs) <= 1:
            return format_tool_result(status="error", code="INVALID_ARGUMENT", message="Cannot close the last tab", data={"tab_id": tab_id})
        tab = browser_manager.resolve_tab(tab_id)
        resp = await browser_manager.send({"type": "CLOSE_TAB", "tab_id": tab.tab_id})
        if resp.get("error"):
            return format_tool_result(status="error", code="TOOL_EXECUTION_ERROR", message=f"Error closing tab: {resp['error']}", data={"tab_id": tab.tab_id})
        browser_manager.remove_tab(tab.tab_id)
        return format_tool_result(
            status="success",
            code="TAB_CLOSED",
            message=f"Closed tab {tab.tab_id}",
            data={"tab_id": tab.tab_id},
        )

    # switch_tab
    if not tab_id:
        return format_tool_result(status="error", code="MISSING_REQUIRED_FIELD", message="tab_id required for switch_tab", data={"tab_id": tab_id})
    tab = browser_manager.resolve_tab(tab_id)
    try:
        resp = await browser_manager.send({"type": "SWITCH_TAB", "tab_id": tab.tab_id})
        if resp.get("error"):
            return format_tool_result(status="error", code="TOOL_EXECUTION_ERROR", message=f"Error switching tab: {resp['error']}", data={"tab_id": tab.tab_id})
    except Exception as e:
        return format_tool_result(status="error", code="TOOL_EXECUTION_ERROR", message=f"Error switching tab: {e}", data={"tab_id": tab.tab_id})
    browser_manager.set_active_tab(tab.tab_id)
    return format_tool_result(
        status="success",
        code="TAB_SWITCHED",
        message=f"Switched to tab {tab.tab_id}: {tab.title or tab.url}",
        data={"tab_id": tab.tab_id, "title": tab.title, "url": tab.url},
    )
