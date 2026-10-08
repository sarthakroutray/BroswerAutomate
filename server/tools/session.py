"""
session.py — The `browser_session` tool: bridge health and batch control.

Orthogonal to browser_tabs (which manages pages): this manages the
MCP <-> extension bridge itself — connection status, cancelling in-flight
action batches, and re-sync hints when the bridge drops.
"""

import time

from ..browser_state import browser_manager, send_with_retries
from ..errors import format_tool_result

SESSION_ACTIONS = {"status", "stop", "reconnect"}


async def handle_session(action: str, tab_id: str | None = None) -> str:
    """Inspect or control the browser-bridge session.

    USE THIS TOOL:
    - action='status': is the extension connected? which tabs are tracked?
      (also asks the extension for its own view when connected)
    - action='stop': cancel the in-flight action batch on a tab (or active
      tab) — sets an abort flag the content script checks between steps,
      hides the overlay, and fails pending bridge requests for that tab
    - action='reconnect': get re-sync instructions when the bridge is down
      (server URL, auth-token hint, tabs known before the drop)

    Args:
        action: status|stop|reconnect
        tab_id: Tab ID for stop (defaults to the active tab).

    Returns: JSON envelope with session state or stop confirmation.
    """
    action = (action or "").strip().lower()
    if action not in SESSION_ACTIONS:
        return format_tool_result(
            status="error", code="INVALID_ENUM",
            message=f"Unknown action '{action}'. Valid actions: {sorted(SESSION_ACTIONS)}",
            data={"action": action},
        )

    if action == "status":
        tabs = [
            {
                "tab_id": t.tab_id,
                "url": t.url,
                "title": t.title,
                "active": t.tab_id == browser_manager.active_tab_id,
                "last_updated_seconds_ago": int(time.time() - t.last_updated),
                "has_cached_dom": t.dom_state is not None,
            }
            for t in browser_manager.tabs.values()
        ]
        data: dict = {
            "connected": browser_manager.connected,
            "active_tab_id": browser_manager.active_tab_id,
            "tab_count": len(tabs),
            "tabs": tabs,
        }
        if browser_manager.connected:
            try:
                resp = await send_with_retries({"type": "GET_STATUS"}, timeout=8.0)
                data["extension"] = {
                    k: resp.get(k)
                    for k in ("connected", "authenticated", "trackedTabs",
                              "tabCount", "serverUrl")
                    if k in resp
                }
            except (ConnectionError, TimeoutError) as e:
                data["extension"] = {"reachable": False, "error": str(e)}
        return format_tool_result(
            status="success", code="SESSION_STATUS",
            message=(
                f"Bridge connected ({len(tabs)} tab(s))"
                if browser_manager.connected
                else "No browser connection — extension is not connected"
            ),
            data=data,
        )

    if action == "stop":
        target = None
        if browser_manager.connected:
            try:
                target = browser_manager.resolve_tab(tab_id)
            except (ValueError, ConnectionError):
                target = None
        stopped = extension_cancel = False
        if target is not None:
            try:
                resp = await send_with_retries(
                    {"type": "CANCEL_ACTIONS", "tab_id": target.tab_id},
                    timeout=8.0,
                )
                extension_cancel = not resp.get("error")
                stopped = bool(resp.get("cancelled", extension_cancel))
            except (ConnectionError, TimeoutError):
                stopped = False
        if target is not None:
            browser_manager.fail_tab_pending(
                target.tab_id, "Cancelled by browser_session(stop)"
            )
        return format_tool_result(
            status="success" if (stopped or extension_cancel) else "error",
            code="ACTIONS_CANCELLED" if (stopped or extension_cancel) else "STOP_FAILED",
            message=(
                f"Cancelled in-flight actions on tab {target.tab_id}"
                if target and (stopped or extension_cancel)
                else "No in-flight batch to cancel or bridge unreachable"
            ),
            data={
                "tab_id": target.tab_id if target else tab_id,
                "cancelled": stopped or extension_cancel,
                "extension_ack": extension_cancel,
            },
        )

    known_tabs = [
        {"tab_id": t.tab_id, "url": t.url, "title": t.title}
        for t in browser_manager.tabs.values()
    ]
    return format_tool_result(
        status="success" if browser_manager.connected else "error",
        code="SESSION_RECONNECT_INFO",
        message=(
            "Bridge is connected — no reconnect needed"
            if browser_manager.connected
            else (
                "Bridge is down. In the extension popup: check the server URL "
                "(default ws://localhost:8000), the auth token if "
                "BROWSER_WS_AUTH_TOKEN is set, then press Connect. "
                f"Server last knew {len(known_tabs)} tab(s); they re-sync via "
                "TAB_SNAPSHOT on reconnect."
            )
        ),
        data={
            "connected": browser_manager.connected,
            "default_server_url": "ws://localhost:8000",
            "known_tabs": known_tabs,
        },
    )
