"""
transport.py — FastAPI + WebSocket + MCP Server wiring.

Thin HTTP/WS layer that delegates all logic to tool handlers and the agent.
"""

import os
import uuid
import asyncio
import logging
import time
import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .config import (
    SERVER_NAME, SERVER_VERSION,
    DEFAULT_MAX_STEPS, get_enabled_tool_names,
    ACTIVE_TOOL_PROFILE,
    ENABLE_STRUCTURED_TOOL_RESPONSES,
    STRICT_TOOL_VALIDATION,
    ENABLE_IDEMPOTENCY_GUARDS,
    IDEMPOTENCY_WINDOW_SECONDS,
)
from .browser_state import browser_manager, _mcp_session_tracker
from .tools import TOOL_DISPATCH, get_mcp_tool_definitions
from .tools.schemas import DOMState, TabRegistration, TaskRequest
from .tools.runtime import (
    ToolInputError,
    MUTATING_TOOLS,
    build_idempotency_key,
    build_tool_envelope,
    build_trace_id,
    derive_response_mode,
    idempotency_registry,
    infer_envelope_from_result,
    render_envelope_text,
    validate_and_sanitize_arguments,
)
from .agent.orchestrator import AutonomousAgent
from .observability import event_bus

logger = logging.getLogger("browser-agent")

# ── MCP imports ───────────────────────────────────────────────────────────────
MCP_AVAILABLE = False
MCP_SAMPLING_AVAILABLE = False
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, ImageContent
    MCP_AVAILABLE = True
    try:
        from mcp.types import SamplingMessage
        MCP_SAMPLING_AVAILABLE = True
    except ImportError:
        pass
except ImportError:
    pass


# ── Singleton agent ───────────────────────────────────────────────────────────
agent = AutonomousAgent(browser_manager, _mcp_session_tracker)


# ── Task progress callback ───────────────────────────────────────────────────
async def send_task_progress(task, message):
    if browser_manager._browser_ws:
        try:
            await browser_manager._browser_ws.send_json({
                "type": "TASK_PROGRESS", "task_id": task.task_id,
                "status": task.status, "step": task.step_count,
                "max_steps": task.max_steps, "message": message,
                "summary": task.summary if task.status != "running" else None,
            })
        except Exception:
            pass

agent.set_progress_callback(send_task_progress)

idempotency_registry.ttl_seconds = IDEMPOTENCY_WINDOW_SECONDS


def _tool_schema_map() -> dict[str, dict]:
    return {td["name"]: td.get("inputSchema", {}) for td in get_mcp_tool_definitions()}


def _content_list(value) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [TextContent(type="text", text=str(value))]


def _format_tool_response(base_result: list, envelope: dict, response_mode: str) -> list:
    contents = _content_list(base_result)
    if not ENABLE_STRUCTURED_TOOL_RESPONSES or response_mode == "legacy":
        return contents

    envelope_text = render_envelope_text(envelope)
    envelope_content = TextContent(type="text", text=envelope_text)

    if response_mode == "structured":
        non_text = [c for c in contents if not hasattr(c, "text")]
        return non_text + [envelope_content]
    return contents + [envelope_content]


def _error_envelope(
    *,
    tool_name: str,
    trace_id: str,
    code: str,
    message: str,
    retriable: bool,
    context=None,
) -> dict:
    return build_tool_envelope(
        tool_name=tool_name,
        status="error",
        code=code,
        message=message,
        retriable=retriable,
        trace_id=trace_id,
        context=context or {},
    )


# ── FastAPI lifespan  ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app_: FastAPI):
    logger.info(f"{SERVER_NAME} v{SERVER_VERSION} starting")
    yield
    logger.info("Shutting down")

app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION, lifespan=lifespan)


# ── WebSocket endpoints ──────────────────────────────────────────────────────

@app.websocket("/ws/browser")
async def websocket_browser_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Browser WebSocket connected")
    browser_manager.clear_all()
    browser_manager.set_browser_ws(websocket)
    try:
        while True:
            data = await websocket.receive_json()
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
                    await websocket.send_json({"type": "ACK"})
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

    except WebSocketDisconnect:
        logger.info("Browser WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        browser_manager.set_browser_ws(None)


@app.websocket("/ws/{tab_id}")
async def websocket_legacy_endpoint(websocket: WebSocket, tab_id: str):
    await websocket.accept()
    browser_manager.register_tab(tab_id)
    browser_manager.set_browser_ws(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            req_id = data.get("request_id")
            if msg_type == "DOM_UPDATE":
                browser_manager.update_dom(tab_id, data.get("dom_state", {}))
                await websocket.send_json({"status": "acknowledged"})
            elif req_id:
                await browser_manager.resolve_pending(req_id, data)
    except WebSocketDisconnect:
        pass


# ── Task handler (from extension) ────────────────────────────────────────────

async def handle_task_from_extension(data: dict):
    goal = data.get("goal", "")
    tab_id = data.get("tab_id")
    max_steps = data.get("max_steps", DEFAULT_MAX_STEPS)

    global_session = await _mcp_session_tracker.get_session()
    has_mcp = (agent._llm._mcp_session is not None or global_session is not None) and MCP_SAMPLING_AVAILABLE
    has_api_key = bool(
        os.environ.get("ANTHROPIC_API_KEY") or
        os.environ.get("OPENAI_API_KEY") or
        os.environ.get("GEMINI_API_KEY")
    )

    if not has_mcp and not has_api_key:
        if browser_manager._browser_ws:
            try:
                await browser_manager._browser_ws.send_json({
                    "type": "TASK_PROGRESS", "task_id": "error", "status": "error",
                    "step": 0, "max_steps": 0,
                    "message": "No LLM available. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY.",
                    "summary": None,
                })
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


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {
        "status": "healthy", "timestamp": datetime.utcnow().isoformat(),
        "server": SERVER_NAME, "version": SERVER_VERSION,
        "mcp_available": MCP_AVAILABLE,
        "active_tabs": len(browser_manager.tabs),
        "ws_connected": browser_manager.connected,
    }

@app.post("/task")
async def start_task_http(req: TaskRequest):
    tab_id = req.tab_id
    if not tab_id:
        tab = browser_manager.get_active_tab()
        if not tab:
            raise HTTPException(400, "No active tab")
        tab_id = tab.tab_id
    task_id = str(uuid.uuid4())[:8]
    asyncio.create_task(agent.run_task(task_id, req.goal, tab_id, req.max_steps))
    return {"task_id": task_id, "status": "started"}

@app.get("/task/{task_id}")
async def get_task_http(task_id: str):
    task = agent.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {
        "task_id": task.task_id, "goal": task.goal, "status": task.status,
        "step": task.step_count, "max_steps": task.max_steps,
        "summary": task.summary, "error": task.error,
    }

@app.delete("/task/{task_id}")
async def stop_task_http(task_id: str):
    if agent.stop_task(task_id):
        return {"status": "stopping"}
    raise HTTPException(404, "Task not found or not running")

@app.post("/tabs/register")
async def register_tab_http(registration: TabRegistration):
    tab = browser_manager.register_tab(registration.tab_id)
    tab.url = registration.url
    tab.title = registration.title
    return {"status": "registered", "tab_id": registration.tab_id}

@app.post("/tabs/{tab_id}/dom")
async def update_dom_http(tab_id: str, dom_state: DOMState):
    browser_manager.update_dom(tab_id, dom_state.model_dump())
    return {"status": "updated"}

@app.get("/tabs/{tab_id}/dom")
async def get_dom_http(tab_id: str):
    if tab_id not in browser_manager.tabs:
        raise HTTPException(404, f"Tab {tab_id} not found")
    tab = browser_manager.tabs[tab_id]
    if tab.dom_state is None:
        raise HTTPException(404, f"No DOM state for tab {tab_id}")
    return tab.dom_state

@app.get("/tabs")
async def list_tabs_http():
    return {
        "tabs": [
            {"tab_id": t.tab_id, "url": t.url, "title": t.title,
             "last_updated": t.last_updated, "has_dom": t.dom_state is not None}
            for t in browser_manager.tabs.values()
        ],
        "active_tab_id": browser_manager.active_tab_id,
        "ws_connected": browser_manager.connected,
    }

@app.post("/tabs/{tab_id}/actions")
async def execute_actions_http(tab_id: str):
    if tab_id not in browser_manager.tabs:
        raise HTTPException(404, f"Tab {tab_id} not found")
    if not browser_manager.connected:
        raise HTTPException(503, "Browser not connected")
    raise HTTPException(501, "Use browser_act MCP tool instead")


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp_server = None

if MCP_AVAILABLE:
    mcp_server = Server(SERVER_NAME)

    @mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        tool_defs = get_mcp_tool_definitions()
        all_tools = [Tool(**td) for td in tool_defs]
        enabled = get_enabled_tool_names()
        if enabled is None:
            return all_tools
        return [t for t in all_tools if t.name in enabled]

    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list:
        raw_args = arguments or {}
        trace_id = build_trace_id(name)
        started_at = time.perf_counter()
        response_mode = derive_response_mode(raw_args if isinstance(raw_args, dict) else {})
        input_summary = json.dumps(raw_args, default=str)[:240] if isinstance(raw_args, dict) else str(raw_args)[:240]
        event_bus.emit_tool_start(name, trace_id=trace_id, input_summary=input_summary)

        # Update MCP session
        try:
            session = mcp_server.request_context.session
            agent.set_mcp_session(session)
            await _mcp_session_tracker.update_session(session)
        except Exception:
            pass

        # Profile gating
        enabled = get_enabled_tool_names()
        if enabled is not None and name not in enabled:
            legacy = [TextContent(
                type="text",
                text=f"Tool '{name}' is disabled in '{ACTIVE_TOOL_PROFILE}' profile.",
            )]
            envelope = _error_envelope(
                tool_name=name,
                trace_id=trace_id,
                code="TOOL_DISABLED",
                message=f"Tool '{name}' is disabled in '{ACTIVE_TOOL_PROFILE}' profile.",
                retriable=False,
                context={"profile": ACTIVE_TOOL_PROFILE},
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="error",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=envelope["code"],
                retriable=False,
                category="policy",
            )
            return _format_tool_response(legacy, envelope, response_mode)

        schema_map = _tool_schema_map()
        schema = schema_map.get(name, {"type": "object", "properties": {}})
        validation_meta = {"unknown_fields": [], "coerced_fields": []}
        try:
            if STRICT_TOOL_VALIDATION:
                arguments, validation_meta = validate_and_sanitize_arguments(raw_args, schema)
            else:
                arguments = raw_args if isinstance(raw_args, dict) else {}
        except ToolInputError as e:
            legacy = [TextContent(type="text", text=f"Error: {e}")]
            envelope = _error_envelope(
                tool_name=name,
                trace_id=trace_id,
                code=e.code,
                message=str(e),
                retriable=e.retriable,
                context={"validation": e.context},
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="error",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=e.code,
                retriable=e.retriable,
                category="validation",
                context=e.context,
            )
            return _format_tool_response(legacy, envelope, response_mode)

        idempotency_key = None
        if ENABLE_IDEMPOTENCY_GUARDS and name in MUTATING_TOOLS:
            idempotency_key = build_idempotency_key(name, arguments)
            if not idempotency_registry.check_and_record(idempotency_key):
                legacy = [TextContent(type="text", text="Error: Duplicate action blocked by idempotency guard")]
                envelope = _error_envelope(
                    tool_name=name,
                    trace_id=trace_id,
                    code="DUPLICATE_ACTION_BLOCKED",
                    message="Duplicate action blocked by idempotency guard",
                    retriable=False,
                    context={"idempotency_key": idempotency_key},
                )
                duration_ms = (time.perf_counter() - started_at) * 1000
                event_bus.emit_tool_end(
                    name,
                    trace_id=trace_id,
                    status="error",
                    duration_ms=duration_ms,
                    output_summary=envelope["message"],
                    error_code=envelope["code"],
                    retriable=False,
                    category="idempotency",
                )
                return _format_tool_response(legacy, envelope, response_mode)

        # Special case: browser_run_task uses the agent
        if name == "browser_run_task":
            try:
                goal = arguments.get("goal")
                if not goal:
                    legacy = [TextContent(type="text", text="Error: goal required")]
                    envelope = _error_envelope(
                        tool_name=name,
                        trace_id=trace_id,
                        code="MISSING_REQUIRED_FIELD",
                        message="goal required",
                        retriable=False,
                    )
                    duration_ms = (time.perf_counter() - started_at) * 1000
                    event_bus.emit_tool_end(
                        name,
                        trace_id=trace_id,
                        status="error",
                        duration_ms=duration_ms,
                        output_summary=envelope["message"],
                        error_code=envelope["code"],
                        retriable=False,
                        category="validation",
                    )
                    return _format_tool_response(legacy, envelope, response_mode)
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                max_steps = arguments.get("max_steps", DEFAULT_MAX_STEPS)
                task_id = str(uuid.uuid4())[:8]
                result = await agent.run_task(task_id, goal, tab.tab_id, max_steps=max_steps)
                summary = f"Task: {result.status}\nSteps: {result.step_count}/{result.max_steps}\n"
                if result.summary:
                    summary += f"Summary: {result.summary}\n"
                if result.error:
                    summary += f"Error: {result.error}\n"
                legacy = [TextContent(type="text", text=summary)]
                envelope = infer_envelope_from_result(
                    name,
                    trace_id,
                    legacy,
                    context={
                        "task_id": task_id,
                        "validation": validation_meta,
                        "idempotency_key": idempotency_key,
                    },
                )
                duration_ms = (time.perf_counter() - started_at) * 1000
                event_bus.emit_tool_end(
                    name,
                    trace_id=trace_id,
                    status="error" if envelope["status"] == "error" else "success",
                    duration_ms=duration_ms,
                    output_summary=envelope["message"],
                    error_code=envelope["code"] if envelope["status"] == "error" else None,
                    retriable=envelope.get("retriable"),
                    category="agent",
                )
                return _format_tool_response(legacy, envelope, response_mode)
            except Exception as e:
                legacy = [TextContent(type="text", text=f"Error: {e}")]
                envelope = _error_envelope(
                    tool_name=name,
                    trace_id=trace_id,
                    code="AGENT_EXECUTION_ERROR",
                    message=str(e),
                    retriable=True,
                    context={"validation": validation_meta},
                )
                duration_ms = (time.perf_counter() - started_at) * 1000
                event_bus.emit_tool_end(
                    name,
                    trace_id=trace_id,
                    status="error",
                    duration_ms=duration_ms,
                    output_summary=envelope["message"],
                    error_code=envelope["code"],
                    retriable=True,
                    category="agent",
                )
                return _format_tool_response(legacy, envelope, response_mode)

        # Dispatch to tool handlers
        handler = TOOL_DISPATCH.get(name)
        if not handler:
            legacy = [TextContent(type="text", text=f"Unknown tool: {name}")]
            envelope = _error_envelope(
                tool_name=name,
                trace_id=trace_id,
                code="UNKNOWN_TOOL",
                message=f"Unknown tool: {name}",
                retriable=False,
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="error",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=envelope["code"],
                retriable=False,
                category="validation",
            )
            return _format_tool_response(legacy, envelope, response_mode)

        try:
            raw_result = await handler(arguments)
            envelope = infer_envelope_from_result(
                name,
                trace_id,
                raw_result,
                context={
                    "validation": validation_meta,
                    "idempotency_key": idempotency_key,
                },
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="error" if envelope["status"] == "error" else "success",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=envelope["code"] if envelope["status"] == "error" else None,
                retriable=envelope.get("retriable"),
                category="tool_execution",
                context={"validation": validation_meta},
            )
            return _format_tool_response(raw_result, envelope, response_mode)
        except ConnectionError as e:
            legacy = [TextContent(type="text", text=f"Connection error: {e}")]
            envelope = _error_envelope(
                tool_name=name,
                trace_id=trace_id,
                code="CONNECTION_ERROR",
                message=f"Connection error: {e}",
                retriable=True,
                context={"validation": validation_meta},
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="error",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=envelope["code"],
                retriable=True,
                category="connection",
            )
            return _format_tool_response(legacy, envelope, response_mode)
        except TimeoutError as e:
            legacy = [TextContent(type="text", text=f"Timeout: {e}")]
            envelope = _error_envelope(
                tool_name=name,
                trace_id=trace_id,
                code="TIMEOUT",
                message=f"Timeout: {e}",
                retriable=True,
                context={"validation": validation_meta},
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="timeout",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=envelope["code"],
                retriable=True,
                category="timeout",
            )
            return _format_tool_response(legacy, envelope, response_mode)
        except ValueError as e:
            legacy = [TextContent(type="text", text=f"Error: {e}")]
            envelope = _error_envelope(
                tool_name=name,
                trace_id=trace_id,
                code="VALUE_ERROR",
                message=str(e),
                retriable=False,
                context={"validation": validation_meta},
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="error",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=envelope["code"],
                retriable=False,
                category="validation",
            )
            return _format_tool_response(legacy, envelope, response_mode)
        except Exception as e:
            logger.error(f"Tool {name} error: {e}", exc_info=True)
            legacy = [TextContent(type="text", text=f"Error: {e}")]
            envelope = _error_envelope(
                tool_name=name,
                trace_id=trace_id,
                code="UNHANDLED_EXCEPTION",
                message=str(e),
                retriable=False,
                context={"validation": validation_meta},
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            event_bus.emit_tool_end(
                name,
                trace_id=trace_id,
                status="error",
                duration_ms=duration_ms,
                output_summary=envelope["message"],
                error_code=envelope["code"],
                retriable=False,
                category="exception",
            )
            return _format_tool_response(legacy, envelope, response_mode)


# ── Entry points ──────────────────────────────────────────────────────────────

async def run_http_bg(port: int = 8000):
    import uvicorn
    config = uvicorn.Config(
        app=app, host="127.0.0.1", port=port, log_level="warning",
        log_config={
            "version": 1, "disable_existing_loggers": False,
            "handlers": {"default": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr"}},
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": "WARNING"},
                "uvicorn.error": {"handlers": ["default"], "level": "WARNING"},
                "uvicorn.access": {"handlers": ["default"], "level": "WARNING"},
            },
        },
    )
    await uvicorn.Server(config).serve()


async def run_mcp_server():
    if not MCP_AVAILABLE or mcp_server is None:
        logger.error("MCP SDK not available. Install: pip install mcp")
        return
    logger.info("Starting MCP stdio + HTTP/WS on port 8000...")
    http_task = asyncio.create_task(run_http_bg(8000))
    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options())
    finally:
        http_task.cancel()
        try:
            await http_task
        except asyncio.CancelledError:
            pass
