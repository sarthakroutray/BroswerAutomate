"""
transport.py — FastMCP server + WebSocket bridge for browser extension.

Architecture:
  Browser Extension <-- WebSocket --> WS Server (asyncio)
                                        |
  MCP Client (Claude) <-- stdio --> FastMCP Server
                                        |
                                   Tool Handlers
                                        |
                                   Agent Orchestrator

FastMCP handles all MCP protocol compliance including:
  - Tool registration with auto-generated JSON schemas
  - stdio transport for MCP clients
  - Lifecycle management

A parallel WebSocket server maintains the browser extension connection
on port 8000 (configurable via BROWSER_WS_PORT).
"""

import os
import uuid
import asyncio
import logging
import time
import json
from typing import Optional

from .config import (
    SERVER_NAME, SERVER_VERSION,
    DEFAULT_MAX_STEPS,
    WS_HOST, WS_PORT,
    ENABLE_IDEMPOTENCY_GUARDS,
    IDEMPOTENCY_WINDOW_SECONDS,
)
from .browser_state import browser_manager, _mcp_session_tracker
from .tools import register_tools, TOOL_DISPATCH
from .tools.runtime import (
    MUTATING_TOOLS,
    build_idempotency_key,
    idempotency_registry,
)
from .agent.orchestrator import AutonomousAgent
from .observability import event_bus

logger = logging.getLogger("browser-agent")

# ── MCP imports ───────────────────────────────────────────────────────────────
MCP_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except ImportError:
    try:
        from mcp.server import Server
        # Fallback: use raw Server if FastMCP not available
        MCP_AVAILABLE = True
    except ImportError:
        pass

# ── Singleton agent ───────────────────────────────────────────────────────────
agent = AutonomousAgent(browser_manager, _mcp_session_tracker)


# ── Task progress callback ───────────────────────────────────────────────────
async def send_task_progress(task, message):
    if browser_manager._browser_ws:
        try:
            ws = browser_manager._browser_ws
            msg = json.dumps({
                "type": "TASK_PROGRESS", "task_id": task.task_id,
                "status": task.status, "step": task.step_count,
                "max_steps": task.max_steps, "message": message,
                "summary": task.summary if task.status != "running" else None,
            })
            await ws.send(msg)
        except Exception:
            pass

agent.set_progress_callback(send_task_progress)
idempotency_registry.ttl_seconds = IDEMPOTENCY_WINDOW_SECONDS


# ── FastMCP Server Creation ──────────────────────────────────────────────────

def create_mcp_server() -> "FastMCP":
    """Create and configure the FastMCP server with all tools registered."""
    mcp = FastMCP(SERVER_NAME)

    # Register all tools based on active profile
    register_tools(mcp)

    # Register the agent tool separately (needs special handling)
    from .config import get_enabled_tool_names
    enabled = get_enabled_tool_names()

    if "browser_run_task" in enabled:
        @mcp.tool(
            name="browser_run_task",
            description=(
                "Autonomous agent that executes multi-step browser tasks with failure "
                "classification, retries, and safe stop guards. "
                "USE for complex multi-step workflows that require visual understanding. "
                "Provide a clear goal description. The agent will observe, plan, and act "
                "autonomously until the goal is achieved or max_steps is reached."
            ),
        )
        async def browser_run_task(
            goal: str,
            tab_id: Optional[str] = None,
            max_steps: int = 30,
        ) -> str:
            """Run an autonomous browser task.

            Args:
                goal: What to accomplish (3-4000 chars).
                tab_id: Optional tab ID. Uses active tab if not specified.
                max_steps: Maximum steps (1-100, default 30).
            """
            if not goal or len(goal.strip()) < 3:
                return "Error: goal required (min 3 characters)"
            max_steps = max(1, min(max_steps, 100))
            tab = browser_manager.resolve_tab(tab_id)
            task_id = str(uuid.uuid4())[:8]
            result = await agent.run_task(task_id, goal, tab.tab_id, max_steps=max_steps)
            summary = f"Task: {result.status}\nSteps: {result.step_count}/{result.max_steps}\n"
            if result.summary:
                summary += f"Summary: {result.summary}\n"
            if result.error:
                summary += f"Error: {result.error}\n"
            return summary

    return mcp


# ── WebSocket Server for Browser Extension ────────────────────────────────────

async def handle_browser_websocket(websocket):
    """Handle WebSocket connection from browser extension."""
    logger.info("Browser WebSocket connected")
    browser_manager.clear_all()
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

            if msg_type == "DOM_UPDATE":
                tab_id = data.get("tab_id")
                if req_id:
                    await browser_manager.resolve_pending(req_id, data)
                if tab_id:
                    browser_manager.register_tab(str(tab_id))
                    browser_manager.update_dom(str(tab_id), data.get("dom_state", {}))
                try:
                    await websocket.send(json.dumps({"type": "ACK"}))
                except Exception:
                    pass

            elif msg_type in (
                "SCREENSHOT_RESULT", "HTML_RESULT", "TEXT_EXTRACT_RESULT",
                "ELEMENT_INFO_RESULT", "ELEMENT_WAIT_RESULT", "JS_RESULT",
                "SET_CODE_RESULT", "GET_CODE_RESULT", "CODING_PROBLEM_RESULT",
            ):
                if req_id:
                    await browser_manager.resolve_pending(req_id, data)

            elif msg_type in ("ACTION_COMPLETE", "TAB_OPENED", "TAB_CLOSED", "TAB_SWITCHED"):
                if msg_type == "TAB_OPENED":
                    new_id = str(data.get("tab_id", ""))
                    if new_id:
                        browser_manager.register_tab(new_id)
                elif msg_type == "TAB_CLOSED":
                    tid = str(data.get("tab_id", ""))
                    if tid:
                        browser_manager.remove_tab(tid)
                elif msg_type == "TAB_SWITCHED":
                    tid = str(data.get("tab_id", ""))
                    if tid:
                        browser_manager.set_active_tab(tid)
                if req_id:
                    await browser_manager.resolve_pending(req_id, data)

            elif msg_type == "START_TASK":
                goal = data.get("goal", "").strip()
                if goal:
                    asyncio.create_task(handle_task_from_extension(data))

            elif msg_type == "STOP_TASK":
                task_id = data.get("task_id")
                if task_id:
                    agent.stop_task(task_id)

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


async def handle_task_from_extension(data: dict):
    """Handle task requests from the browser extension."""
    goal = data.get("goal", "")
    tab_id = data.get("tab_id")
    max_steps = data.get("max_steps", DEFAULT_MAX_STEPS)

    has_api_key = bool(
        os.environ.get("ANTHROPIC_API_KEY") or
        os.environ.get("OPENAI_API_KEY") or
        os.environ.get("GEMINI_API_KEY")
    )

    if not has_api_key:
        if browser_manager._browser_ws:
            try:
                await browser_manager._browser_ws.send(json.dumps({
                    "type": "TASK_PROGRESS", "task_id": "error", "status": "error",
                    "step": 0, "max_steps": 0,
                    "message": "No LLM available. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY.",
                    "summary": None,
                }))
            except Exception:
                pass
        return

    if not tab_id:
        tab = browser_manager.get_active_tab()
        tab_id = tab.tab_id if tab else None
    if not tab_id:
        return

    task_id = str(uuid.uuid4())[:8]
    await agent.run_task(task_id, goal, str(tab_id), max_steps)


async def run_websocket_server():
    """Run the WebSocket server for browser extension communication."""
    try:
        import websockets
        logger.info(f"Starting WebSocket server on {WS_HOST}:{WS_PORT}")

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

mcp_server = create_mcp_server() if MCP_AVAILABLE else None


async def run_mcp_server():
    """Run FastMCP server on stdio with WebSocket server in parallel."""
    if mcp_server is None:
        logger.error("MCP SDK not available. Install: pip install mcp")
        return

    logger.info(f"{SERVER_NAME} v{SERVER_VERSION} starting (FastMCP)")

    # Start WebSocket server in background for browser extension
    ws_task = asyncio.create_task(run_websocket_server())

    try:
        # Run FastMCP on stdio
        await mcp_server.run_stdio_async()
    finally:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
