"""
navigation.py — browser_navigate + browser_list_tabs tool handlers.

Consolidates: browser_go_back, browser_go_forward, browser_reload,
browser_open_tab, browser_close_tab, browser_switch_tab into one tool.
"""

import time
import json
import asyncio
import hashlib
import logging

from ..browser_state import browser_manager

logger = logging.getLogger("browser-agent")

# ── Type stubs for MCP content ────────────────────────────────────────────────
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


async def handle_navigate(arguments: dict) -> list:
    action = arguments.get("action", "goto")
    tab_id_arg = arguments.get("tab_id")
    url = arguments.get("url")
    hard = arguments.get("hard_reload", False)
    active = arguments.get("active", True)

    if action == "goto":
        if not url:
            return [TextContent(type="text", text="Error: 'url' required for goto")]
        tab = browser_manager.resolve_tab(tab_id_arg)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "navigate", "value": url}],
        })
        changed, after = await _verify_transition(tab.tab_id, before, wait_seconds=2.8)
        if changed:
            return [TextContent(type="text", text=f"Navigated to: {after.get('url') or url}")]
        return [TextContent(type="text", text=f"Navigation triggered to: {url} (transition not yet confirmed)")]

    if action == "back":
        tab = browser_manager.resolve_tab(tab_id_arg)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "go_back"}],
        })
        changed, after = await _verify_transition(tab.tab_id, before)
        return [TextContent(type="text", text="Navigated back" if changed else "Back action sent; transition not yet confirmed")]

    if action == "forward":
        tab = browser_manager.resolve_tab(tab_id_arg)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "go_forward"}],
        })
        changed, after = await _verify_transition(tab.tab_id, before)
        return [TextContent(type="text", text="Navigated forward" if changed else "Forward action sent; transition not yet confirmed")]

    if action == "reload":
        tab = browser_manager.resolve_tab(tab_id_arg)
        before = await _capture_nav_snapshot(tab.tab_id)
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
            "steps": [{"action": "reload", "value": "hard" if hard else "soft"}],
        })
        changed, _after = await _verify_transition(tab.tab_id, before)
        base = "Page reloaded" + (" (hard)" if hard else "")
        if changed:
            return [TextContent(type="text", text=base)]
        return [TextContent(type="text", text=base + " — reload command sent; DOM transition pending")]

    if action == "new_tab":
        if not url:
            return [TextContent(type="text", text="Error: 'url' required for new_tab")]
        if not browser_manager.connected:
            return [TextContent(type="text", text="Error: No browser connection")]
        resp = await browser_manager.send({"type": "OPEN_TAB", "url": url, "active": active})
        new_id = str(resp.get("tab_id", ""))
        if new_id:
            browser_manager.register_tab(new_id)
            if active:
                browser_manager.set_active_tab(new_id)
        return [TextContent(type="text", text=f"Opened tab: {url}")]

    if action == "close_tab":
        tid = tab_id_arg
        if not tid:
            return [TextContent(type="text", text="Error: tab_id required for close_tab")]
        if len(browser_manager.tabs) <= 1:
            return [TextContent(type="text", text="Cannot close the last tab")]
        tab = browser_manager.resolve_tab(tid)
        await browser_manager.send({"type": "CLOSE_TAB", "tab_id": tab.tab_id})
        browser_manager.remove_tab(tab.tab_id)
        return [TextContent(type="text", text=f"Closed tab {tab.tab_id}")]

    if action == "switch_tab":
        tid = tab_id_arg
        if not tid:
            return [TextContent(type="text", text="Error: tab_id required for switch_tab")]
        tab = browser_manager.resolve_tab(tid)
        try:
            await browser_manager.send({"type": "SWITCH_TAB", "tab_id": tab.tab_id})
        except Exception:
            pass
        browser_manager.set_active_tab(tab.tab_id)
        return [TextContent(type="text", text=f"Switched to tab {tab.tab_id}: {tab.title or tab.url}")]

    return [TextContent(type="text", text=f"Unknown navigation action: {action}")]


async def handle_list_tabs(arguments: dict) -> list:
    if not browser_manager.tabs:
        return [TextContent(type="text", text="No browser tabs connected")]
    lines = []
    for t in browser_manager.tabs.values():
        active = " [ACTIVE]" if t.tab_id == browser_manager.active_tab_id else ""
        age = int(time.time() - t.last_updated)
        lines.append(
            f"- {t.tab_id}{active}\n  URL: {t.url or 'N/A'}\n  Title: {t.title or 'N/A'}\n  DOM: {age}s ago"
        )
    return [TextContent(
        type="text",
        text=f"Connected: {browser_manager.connected}\n\n" + "\n\n".join(lines),
    )]
