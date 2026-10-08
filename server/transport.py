"""
transport.py — FastMCP server + WebSocket bridge for the browser extension.

Architecture:
  Browser Extension <-- WebSocket --> WS Server (asyncio)
                                        |
  MCP Client (LLM)    <-- stdio --> FastMCP Server
                                        |
                                   Tool Handlers
                          (browser_see / browser_act / browser_js / browser_tabs)

There is NO LLM inside this server: no MCP sampling, no API keys, no agent.
The MCP client's own model does all the thinking through the four tools.

A parallel WebSocket server maintains the browser extension connection
on port 8000 (configurable via BROWSER_WS_PORT).
"""

import asyncio
import logging
import json
from urllib.parse import urlparse

from .config import (
    SERVER_NAME, SERVER_VERSION,
    WS_HOST, WS_PORT,
    WS_AUTH_TOKEN,
)
from .browser_state import browser_manager
from .tools import register_tools

logger = logging.getLogger("browser-agent")

# ── MCP imports ───────────────────────────────────────────────────────────────
MCP_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except ImportError:
    pass


def create_mcp_server() -> "FastMCP":
    """Create and configure the FastMCP server with all tools registered."""
    mcp = FastMCP(SERVER_NAME)
    register_tools(mcp)
    return mcp


# ── WebSocket Server for Browser Extension ────────────────────────────────────

RESULT_MESSAGE_TYPES = (
    "SCREENSHOT_RESULT", "HTML_RESULT", "TEXT_EXTRACT_RESULT",
    "ELEMENT_INFO_RESULT", "ELEMENT_WAIT_RESULT", "JS_RESULT",
    "SET_CODE_RESULT", "GET_CODE_RESULT", "CODING_PROBLEM_RESULT",
    "QUIZ_STRUCTURE_RESULT", "PAGE_CONTEXT_RESULT",
)

TAB_EVENT_TYPES = ("ACTION_COMPLETE", "TAB_OPENED", "TAB_CLOSED", "TAB_SWITCHED")


async def handle_browser_websocket(websocket):
    """Handle WebSocket connection from browser extension."""
    # Validate Origin header — only allow local origins
    headers = getattr(websocket, "request_headers", {}) or {}
    origin = headers.get("Origin") or getattr(websocket, "origin", None) or ""
    if origin:
        parsed = urlparse(origin)
        allowed_hosts = {'localhost', '127.0.0.1', ''}
        if parsed.hostname not in allowed_hosts and not origin.startswith('chrome-extension://'):
            logger.warning(f"Rejected WebSocket from origin: {origin}")
            await websocket.close(4403, "Forbidden origin")
            return

    # Authentication handshake. The AUTH message is always expected first;
    # the token only has to match when BROWSER_WS_AUTH_TOKEN is configured.
    try:
        raw_auth = await asyncio.wait_for(websocket.recv(), timeout=5.0)
        auth_data = json.loads(raw_auth)
        if auth_data.get("type") != "AUTH":
            logger.warning("WebSocket auth failed: first message was not AUTH")
            await websocket.close(4401, "Authentication required")
            return
        if WS_AUTH_TOKEN and auth_data.get("token") != WS_AUTH_TOKEN:
            logger.warning("WebSocket auth failed: invalid token")
            await websocket.close(4401, "Authentication failed")
            return
        await websocket.send(json.dumps({"type": "AUTH_OK"}))
    except Exception as e:
        logger.warning(f"WebSocket auth handshake failed: {e}")
        await websocket.close(4401, "Authentication required")
        return

    logger.info("Browser WebSocket connected")
    browser_manager.set_browser_ws(websocket)

    try:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            req_id = data.get("request_id")

            if msg_type == "PONG":
                continue

            if msg_type == "TAB_SNAPSHOT":
                tabs_data = data.get("tabs", [])
                for tab_info in tabs_data:
                    tid = str(tab_info.get("tab_id", ""))
                    if tid:
                        browser_manager.register_tab(tid)
                        tab = browser_manager.tabs.get(tid)
                        if tab:
                            tab.url = tab_info.get("url", "")
                            tab.title = tab_info.get("title", "")
                active_id = data.get("active_tab_id")
                if active_id:
                    browser_manager.set_active_tab(str(active_id))
                continue

            if msg_type == "TAB_CREATED":
                tid = str(data.get("tab_id", ""))
                if tid:
                    browser_manager.register_tab(tid)
                    tab = browser_manager.tabs.get(tid)
                    if tab:
                        tab.url = data.get("url", tab.url)
                        tab.title = data.get("title", tab.title)
                continue

            if msg_type == "DOM_UPDATE":
                tab_id = data.get("tab_id")
                if req_id:
                    await browser_manager.resolve_pending(req_id, data)
                if tab_id:
                    browser_manager.register_tab(str(tab_id))
                    dom_state = data.get("dom_state")
                    # Never let an error/empty reply wipe a good cached snapshot.
                    if dom_state:
                        browser_manager.update_dom(str(tab_id), dom_state)
                try:
                    await websocket.send(json.dumps({"type": "ACK"}))
                except Exception:
                    pass

            elif msg_type in RESULT_MESSAGE_TYPES:
                if req_id:
                    await browser_manager.resolve_pending(req_id, data)

            elif msg_type in TAB_EVENT_TYPES:
                if msg_type == "TAB_OPENED":
                    new_id = str(data.get("tab_id", ""))
                    if new_id:
                        browser_manager.register_tab(new_id)
                        tab = browser_manager.tabs.get(new_id)
                        if tab:
                            tab.url = data.get("url", tab.url)
                            tab.title = data.get("title", tab.title)
                elif msg_type == "TAB_CLOSED":
                    tid = str(data.get("tab_id", ""))
                    if tid:
                        browser_manager.remove_tab(tid)
                elif msg_type == "TAB_SWITCHED":
                    tid = str(data.get("tab_id", ""))
                    if tid:
                        browser_manager.register_tab(tid)
                        tab = browser_manager.tabs.get(tid)
                        if tab:
                            tab.url = data.get("url", tab.url)
                            tab.title = data.get("title", tab.title)
                        browser_manager.set_active_tab(tid)
                if req_id:
                    await browser_manager.resolve_pending(req_id, data)

            else:
                if req_id:
                    await browser_manager.resolve_pending(req_id, data)
                else:
                    logger.debug(f"Unhandled WS message: {msg_type}")

    except Exception as e:
        if "closed" not in str(e).lower() and "disconnect" not in str(e).lower():
            logger.error(f"WebSocket error: {e}")
        else:
            logger.info("Browser WebSocket disconnected")
    finally:
        browser_manager.set_browser_ws(None)


async def run_websocket_server():
    """Run the WebSocket server for browser extension communication."""
    try:
        import websockets
        logger.info(f"Starting WebSocket server on {WS_HOST}:{WS_PORT}")
        if WS_AUTH_TOKEN:
            logger.info(
                "Browser WS auth token enforced. Configure the extension with "
                f"ws://{WS_HOST}:{WS_PORT} and token {WS_AUTH_TOKEN}"
            )
        else:
            logger.info(
                "Browser WS auth disabled (localhost-only bridge). "
                "Set BROWSER_WS_AUTH_TOKEN to enforce a token."
            )

        async def router(websocket, path=None):
            # websockets 10.x passes path as second arg, 11+ uses websocket.path
            ws_path = path if path is not None else getattr(websocket, 'path', '/ws/browser')
            if ws_path in ("/ws/browser", "/"):
                await handle_browser_websocket(websocket)
            else:
                # Legacy endpoint /ws/{tab_id}
                parts = ws_path.strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "ws":
                    tab_id = parts[1]
                    browser_manager.register_tab(tab_id)
                    await handle_browser_websocket(websocket)
                else:
                    await handle_browser_websocket(websocket)

        server = await websockets.serve(
            router,
            WS_HOST,
            WS_PORT,
            ping_interval=25,
            ping_timeout=10,
        )
        logger.info(f"WebSocket server listening on ws://{WS_HOST}:{WS_PORT}")
        await server.wait_closed()
    except ImportError:
        logger.warning(
            "websockets package not installed. Browser extension connection unavailable. "
            "Install with: pip install websockets"
        )
    except Exception as e:
        logger.error(f"WebSocket server error: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

# Lazy-initialized FastMCP server. Constructing it at module import time
# spins up the full tool registry, which breaks any test that just wants
# to import a single symbol (e.g. to read a constant) without paying for
# all of that. Tests that need a real FastMCP server should call
# `get_mcp_server()`.
_mcp_server_instance = None


def get_mcp_server():
    """Return the FastMCP server, constructing it on first access."""
    global _mcp_server_instance
    if _mcp_server_instance is None and MCP_AVAILABLE:
        _mcp_server_instance = create_mcp_server()
    return _mcp_server_instance


# Backward-compat: code that did `from server.transport import mcp_server`
# still gets an attribute, but it's populated on first access (effectively
# the same thing for the only call site — `run_mcp_server` — which is the
# entry point and runs everything anyway).
def __getattr__(name):
    if name == "mcp_server":
        return get_mcp_server()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def run_mcp_server():
    """Run FastMCP server on stdio with WebSocket server in parallel."""
    mcp = get_mcp_server()
    if mcp is None:
        logger.error("MCP SDK not available. Install: pip install mcp")
        return

    logger.info(f"{SERVER_NAME} v{SERVER_VERSION} starting (FastMCP)")

    # Start WebSocket server in background for browser extension
    ws_task = asyncio.create_task(run_websocket_server())

    try:
        # Run FastMCP on stdio
        await mcp.run_stdio_async()
    finally:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
