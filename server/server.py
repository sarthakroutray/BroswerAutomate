"""
server.py — Browser Automation Server with Autonomous AI Agent v4

Architecture:
  Extension ← WebSocket → FastAPI Server → AI API (Anthropic/OpenAI)
                                ↕
                         MCP Protocol → LLM Client (Claude Desktop)

Features:
  - MCP tools for LLM-driven browser automation
  - Autonomous AI agent that can see and drive the browser independently
  - Screenshot capture + structured DOM for visual understanding
  - Quiz completion, form filling, multi-step task execution
  - Concurrent WebSocket communication with request correlation
  - Expanded toolset: text extraction, element waiting, navigation history,
    form autofill, safe JS execution, element inspection
"""

import os
import sys
import json
import time
import asyncio
import logging
import uuid
import re
import traceback
import base64
from typing import Any, Optional
from datetime import datetime
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dc_field

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# MCP SDK
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logging.warning("MCP SDK not installed. Run: pip install mcp")

# MCP Sampling types
try:
    from mcp.types import SamplingMessage, CreateMessageResult, ModelPreferences, ModelHint
    MCP_SAMPLING_AVAILABLE = True
except ImportError:
    try:
        from mcp.types import SamplingMessage, CreateMessageResult
        ModelPreferences = None
        ModelHint = None
        MCP_SAMPLING_AVAILABLE = True
    except ImportError:
        MCP_SAMPLING_AVAILABLE = False

# AI SDKs (optional)
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import openai as openai_mod
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# ─── Configuration ────────────────────────────────────────────────────────────

SERVER_NAME = "browser-automation-mcp"
SERVER_VERSION = "4.0.0"
WS_RESPONSE_TIMEOUT = 30.0
SCREENSHOT_TIMEOUT = 10.0
DEFAULT_MAX_STEPS = 30
AGENT_STEP_DELAY = 1.5
MAX_TEXT_SUMMARY_CHARS = 12000
MAX_TASK_HISTORY = 50
WS_PING_INTERVAL = 25.0
MCP_SAMPLING_TIMEOUT = 45.0
DEFAULT_MCP_MODEL_HINTS = [
    "gpt-4o",
    "gpt-4.1",
    "claude-3-5-sonnet-20241022",
]

SUPPORTED_TOOL_PROFILES = {"full", "coding", "minimal", "manual"}
FULL_TOOL_NAMES = {
    "browser_get_page_state",
    "browser_take_screenshot",
    "browser_extract_text",
    "browser_get_element_info",
    "browser_wait_for_element",
    "browser_execute_actions",
    "browser_list_tabs",
    "browser_switch_tab",
    "browser_open_tab",
    "browser_close_tab",
    "browser_go_back",
    "browser_go_forward",
    "browser_reload",
    "browser_run_task",
    "browser_navigate_quiz",
    "browser_solve_quiz",
    "browser_get_coding_problem",
    "browser_set_code_editor",
    "browser_get_code_editor",
    "browser_compile_and_run",
    "browser_get_test_results",
    "browser_submit_solution",
    "browser_solve_coding_problem",
}
CODING_TOOL_NAMES = {
    "browser_list_tabs",
    "browser_switch_tab",
    "browser_take_screenshot",
    "browser_extract_text",
    "browser_navigate_quiz",
    "browser_get_coding_problem",
    "browser_set_code_editor",
    "browser_get_code_editor",
    "browser_compile_and_run",
    "browser_get_test_results",
    "browser_submit_solution",
    "browser_solve_coding_problem",
}
MINIMAL_TOOL_NAMES = {
    "browser_get_page_state",
    "browser_take_screenshot",
    "browser_execute_actions",
    "browser_run_task",
    "browser_list_tabs",
    "browser_switch_tab",
    "browser_open_tab",
    "browser_close_tab",
}

MANUAL_TOOL_NAMES = {
    "browser_get_page_state",
    "browser_take_screenshot",
    "browser_extract_text",
    "browser_get_element_info",
    "browser_wait_for_element",
    "browser_execute_actions",
    "browser_list_tabs",
    "browser_switch_tab",
    "browser_open_tab",
    "browser_close_tab",
    "browser_go_back",
    "browser_go_forward",
    "browser_reload",
    "browser_navigate_quiz",
    "browser_solve_quiz",
    "browser_get_coding_problem",
    "browser_set_code_editor",
    "browser_get_code_editor",
    "browser_compile_and_run",
    "browser_get_test_results",
    "browser_submit_solution",
}

DEFAULT_TOOL_PROFILE = "coding"


def resolve_tool_profile() -> str:
    profile = os.getenv("BROWSER_TOOL_PROFILE", "").strip().lower()
    if "--tool-profile" in sys.argv:
        idx = sys.argv.index("--tool-profile") + 1
        if idx < len(sys.argv):
            profile = sys.argv[idx].strip().lower()
    if not profile:
        profile = DEFAULT_TOOL_PROFILE
    if profile not in SUPPORTED_TOOL_PROFILES:
        print(
            f"[browser-agent] Invalid tool profile '{profile}', falling back to '{DEFAULT_TOOL_PROFILE}'",
            file=sys.stderr,
        )
        profile = DEFAULT_TOOL_PROFILE
    return profile


ACTIVE_TOOL_PROFILE = resolve_tool_profile()


def get_enabled_tool_names() -> Optional[set[str]]:
    if ACTIVE_TOOL_PROFILE == "full":
        return FULL_TOOL_NAMES
    if ACTIVE_TOOL_PROFILE == "coding":
        return CODING_TOOL_NAMES
    if ACTIVE_TOOL_PROFILE == "minimal":
        return MINIMAL_TOOL_NAMES
    return MANUAL_TOOL_NAMES


def get_mcp_model_hints() -> list[str]:
    env_value = os.getenv("BROWSER_MCP_MODEL_HINTS", "").strip()
    if not env_value:
        return list(DEFAULT_MCP_MODEL_HINTS)
    hints = [item.strip() for item in env_value.split(",") if item.strip()]
    return hints or list(DEFAULT_MCP_MODEL_HINTS)


async def create_mcp_message_with_fallback(
    session,
    *,
    messages,
    max_tokens: int,
    system_prompt: str,
):
    base_kwargs = {
        "messages": messages,
        "max_tokens": max_tokens,
        "system_prompt": system_prompt,
    }

    errors = []

    if ModelPreferences is not None and ModelHint is not None:
        for hint in get_mcp_model_hints():
            try:
                hinted_kwargs = dict(base_kwargs)
                hinted_kwargs["model_preferences"] = ModelPreferences(
                    hints=[ModelHint(name=hint)]
                )
                return await session.create_message(**hinted_kwargs)
            except Exception as e:
                errors.append(f"hint '{hint}' failed: {e}")

    try:
        return await session.create_message(**base_kwargs)
    except Exception as e:
        errors.append(f"default sampling failed: {e}")

    raise ValueError("; ".join(errors[-4:]))

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("browser-agent")

# ─── Agent System Prompt ──────────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are an autonomous browser automation agent. You can see the current webpage through a screenshot and structured DOM data. Execute actions to accomplish the user's goal.

## Available Actions

| Action       | selector | value | Description                          |
|--------------|----------|-------|--------------------------------------|
| click        | required | no    | Click an element                     |
| click_at     | no       | no    | Click at viewport coordinates (x, y) |
| type         | required | yes   | Type text into input (clears first)  |
| clear        | required | no    | Clear an input field                 |
| select       | required | yes   | Select dropdown option by text/value |
| check        | required | no    | Check a checkbox                     |
| uncheck      | required | no    | Uncheck a checkbox                   |
| press_key    | optional | yes   | Press key: Enter, Tab, Escape, etc.  |
| scroll       | no       | yes   | Scroll "up" or "down"               |
| hover        | required | no    | Hover over an element                |
| navigate     | no       | yes   | Navigate to a URL                    |
| wait         | no       | yes   | Wait N milliseconds (max 5000)       |
| double_click | required | no    | Double-click an element              |
| focus        | required | no    | Focus an element                     |
| submit       | required | no    | Submit a form                        |
| go_back      | no       | no    | Browser back button                  |
| go_forward   | no       | no    | Browser forward button               |
| reload       | no       | no    | Reload the current page              |

## Rules

1. Analyze BOTH the screenshot AND DOM data carefully before acting
2. Use EXACT CSS selectors from the DOM state — they point to real elements
3. Take 1-5 targeted actions per step. You'll see the updated state after each step.
4. For quizzes: read questions carefully, reason through answers logically, then select the correct ones
5. For multi-step flows: complete one page/step at a time, then proceed
6. If an element isn't visible, try scrolling first
7. For radio buttons: click the one with the correct answer
8. For checkboxes: check all that apply
9. After selecting answers, look for a Submit/Next/Continue button
10. Set "done": true ONLY when the goal is fully accomplished or definitely impossible

## Response Format (STRICT JSON — no markdown, no code fences, no extra text)

{
  "thinking": "Brief analysis of what you see and your plan for this step",
  "actions": [
    {"action": "click", "selector": "#option-b"},
    {"action": "type", "selector": "#answer", "value": "42"}
  ],
  "done": false,
  "summary": "Only fill when done=true. Describe what was accomplished."
}"""

QUIZ_SYSTEM_PROMPT = """You are an expert quiz solver with deep knowledge across multiple subjects. You are analyzing quiz questions and selecting the correct answers.

## Your Process
1. **Read the question carefully** — identify exactly what is being asked
2. **Analyze ALL options** — read each option completely before choosing
3. **Use elimination** — rule out obviously wrong answers first
4. **Apply domain knowledge** — use your expertise to identify the correct answer
5. **Verify your choice** — double-check your reasoning before selecting

## Rules
- For single-choice (radio buttons): select exactly ONE correct answer
- For multi-choice (checkboxes): select ALL correct answers
- Always reason through WHY each option is correct or wrong
- If unsure between two options, explain your reasoning and pick the best one
- Never guess randomly — always apply logical reasoning

## Response Format (STRICT JSON)
{
  "thinking": "Step-by-step analysis: 1) The question asks about X. 2) Option A says Y — this is wrong because... 3) Option B says Z — this is correct because...",
  "answers": [
    {"selector": "CSS_SELECTOR_HERE", "confidence": 0.95, "reason": "Brief reason"}
  ],
  "done": false,
  "summary": "Only fill when done=true"
}"""

# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info(f"Browser Automation Server v{SERVER_VERSION} starting")
    logger.info(f"MCP: {MCP_AVAILABLE} | MCP Sampling: {MCP_SAMPLING_AVAILABLE}")
    logger.info(f"MCP tool profile: {ACTIVE_TOOL_PROFILE}")
    logger.info(f"Fallback AI — Anthropic: {ANTHROPIC_AVAILABLE} | OpenAI: {OPENAI_AVAILABLE} | Gemini: {GEMINI_AVAILABLE}")
    logger.info("=" * 60)
    yield
    logger.info("Server shutting down")

# ─── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(title="Browser Automation Server", version=SERVER_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ─── Browser State Management ─────────────────────────────────────────────────

class BrowserTab:
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
        self._browser_ws: Optional[WebSocket] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._req_counter: int = 0
        self._send_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._ping_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._browser_ws is not None

    def set_browser_ws(self, ws: Optional[WebSocket]):
        old = self._browser_ws
        self._browser_ws = ws
        if ws is None and old is not None:
            self._fail_all_pending("Browser WebSocket disconnected")
            if self._ping_task:
                self._ping_task.cancel()
                self._ping_task = None
        elif ws is not None:
            self._ping_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        """Periodic pings to detect stale connections."""
        try:
            while self._browser_ws:
                await asyncio.sleep(WS_PING_INTERVAL)
                if self._browser_ws:
                    try:
                        await self._browser_ws.send_json({"type": "PING"})
                    except Exception:
                        logger.warning("Heartbeat failed — connection stale")
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
            raise ConnectionError("No browser connection. Make sure the Chrome extension is connected.")
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
                await self._browser_ws.send_json(message)
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


browser_manager = BrowserManager()

# ─── Global MCP Session Tracker ───────────────────────────────────────────────
# Keeps track of the most recent MCP session so that direct WebSocket requests
# from the browser extension can also use MCP sampling when connected to VS Code Copilot

class MCPSessionTracker:
    def __init__(self):
        self._current_session = None
        self._lock = None
    
    def _ensure_lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
    
    async def update_session(self, session):
        self._ensure_lock()
        async with self._lock:
            self._current_session = session
            logger.debug("Updated global MCP session")
    
    async def get_session(self):
        self._ensure_lock()
        async with self._lock:
            return self._current_session

_mcp_session_tracker = MCPSessionTracker()

# ─── Autonomous Agent ─────────────────────────────────────────────────────────

@dataclass
class TaskState:
    task_id: str
    goal: str
    tab_id: str
    status: str = "running"
    error: Optional[str] = None
    summary: Optional[str] = None
    step_count: int = 0
    max_steps: int = DEFAULT_MAX_STEPS
    history: list = dc_field(default_factory=list)
    cancelled: bool = False
    created_at: float = dc_field(default_factory=time.time)


class AutonomousAgent:
    def __init__(self, browser_mgr: BrowserManager):
        self.browser = browser_mgr
        self.tasks: dict[str, TaskState] = {}
        self._progress_callback = None
        self._mcp_session = None

    def set_mcp_session(self, session):
        self._mcp_session = session

    def set_progress_callback(self, cb):
        self._progress_callback = cb

    def _prune_old_tasks(self):
        completed = [(tid, t) for tid, t in self.tasks.items() if t.status != "running"]
        completed.sort(key=lambda x: x[1].created_at)
        while len(completed) > MAX_TASK_HISTORY:
            tid, _ = completed.pop(0)
            del self.tasks[tid]

    async def _get_screenshot(self, tab_id: str) -> Optional[str]:
        try:
            resp = await self.browser.send(
                {"type": "TAKE_SCREENSHOT", "tab_id": tab_id},
                timeout=SCREENSHOT_TIMEOUT,
            )
            return resp.get("screenshot")
        except Exception as e:
            logger.warning(f"Screenshot failed: {e}")
            return None

    async def _get_dom(self, tab_id: str) -> Optional[dict]:
        try:
            resp = await self.browser.send({"type": "REQUEST_DOM", "tab_id": tab_id})
            dom = resp.get("dom_state", {})
            if dom:
                self.browser.update_dom(tab_id, dom)
            return dom
        except Exception as e:
            logger.warning(f"DOM request failed: {e}")
            tab = self.browser.tabs.get(tab_id)
            return tab.dom_state if tab else None

    async def _ask_ai(self, goal, dom, screenshot, history) -> dict:
        dom_text = format_dom_for_display(dom) if dom else "No DOM data available."
        user_content = f"## Goal\n{goal}\n\n## Current Page State\n{dom_text}"
        if history:
            recent = history[-5:]
            lines = []
            for h in recent:
                lines.append(
                    f"Step {h['step']}: {h.get('thinking','')}\n"
                    f"Actions: {json.dumps(h.get('actions',[]))}\n"
                    f"Result: {json.dumps(h.get('result',{}))}"
                )
            user_content += "\n\n## Recent Action History\n" + "\n\n".join(lines)
        user_content += "\n\nRespond with your next actions as strict JSON."

        mcp_errors = []

        # Try both known sessions (request-context session first, then global tracked session)
        sessions_to_try = []
        if self._mcp_session is not None:
            sessions_to_try.append(self._mcp_session)
        global_session = await _mcp_session_tracker.get_session()
        if global_session is not None and global_session is not self._mcp_session:
            sessions_to_try.append(global_session)

        if sessions_to_try and MCP_SAMPLING_AVAILABLE:
            for session in sessions_to_try:
                try:
                    return await asyncio.wait_for(
                        self._ask_via_mcp(user_content, screenshot, session),
                        timeout=MCP_SAMPLING_TIMEOUT,
                    )
                except Exception as e:
                    mcp_errors.append(f"vision attempt failed: {e}")
                    logger.warning(f"MCP sampling vision attempt failed: {e}")

                # Retry without image context for providers/clients that fail on image payloads
                try:
                    return await asyncio.wait_for(
                        self._ask_via_mcp(user_content, None, session),
                        timeout=MCP_SAMPLING_TIMEOUT,
                    )
                except Exception as e:
                    mcp_errors.append(f"text-only attempt failed: {e}")
                    logger.warning(f"MCP sampling text-only attempt failed: {e}")

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if anthropic_key and ANTHROPIC_AVAILABLE:
            return await self._ask_anthropic(user_content, screenshot, anthropic_key)
        elif openai_key and OPENAI_AVAILABLE:
            return await self._ask_openai(user_content, screenshot, openai_key)
        elif gemini_key and GEMINI_AVAILABLE:
            return await self._ask_gemini(user_content, screenshot, gemini_key)
        if mcp_errors:
            raise ValueError(
                "MCP sampling was available but failed and no fallback API key is set. "
                "Last MCP errors: " + " | ".join(mcp_errors[-2:]) +
                "\nSet ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY, or invoke browser_run_task from an active MCP client session."
            )

        raise ValueError(
            "No LLM available. Either:\n"
            "  1. Invoke browser_run_task from your MCP client so sampling session is active\n"
            "  2. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY env var"
        )

    async def _ask_via_mcp(self, text, screenshot, session) -> dict:
        messages = []
        if screenshot:
            messages.append(SamplingMessage(
                role="user",
                content=ImageContent(type="image", data=screenshot, mimeType="image/png"),
            ))
        messages.append(SamplingMessage(
            role="user", content=TextContent(type="text", text=text),
        ))
        result = await create_mcp_message_with_fallback(
            session,
            messages=messages,
            max_tokens=2048,
            system_prompt=AGENT_SYSTEM_PROMPT,
        )
        response_text = ""
        if hasattr(result, "content"):
            if hasattr(result.content, "text"):
                response_text = result.content.text
            elif isinstance(result.content, str):
                response_text = result.content
            else:
                response_text = str(result.content)
        else:
            response_text = str(result)
        return self._parse_ai_response(response_text)

    async def _ask_anthropic(self, text, screenshot, api_key) -> dict:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        content = []
        if screenshot:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": screenshot}})
        content.append({"type": "text", "text": text})
        resp = await client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=2048,
            system=AGENT_SYSTEM_PROMPT, messages=[{"role": "user", "content": content}],
        )
        return self._parse_ai_response(resp.content[0].text)

    async def _ask_openai(self, text, screenshot, api_key) -> dict:
        client = openai_mod.AsyncOpenAI(api_key=api_key)
        content = []
        if screenshot:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot}", "detail": "high"}})
        content.append({"type": "text", "text": text})
        resp = await client.chat.completions.create(
            model="gpt-4o", max_tokens=2048,
            messages=[{"role": "system", "content": AGENT_SYSTEM_PROMPT}, {"role": "user", "content": content}],
        )
        return self._parse_ai_response(resp.choices[0].message.content)

    async def _ask_gemini(self, text, screenshot, api_key) -> dict:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash-exp",
            system_instruction=AGENT_SYSTEM_PROMPT
        )
        contents = []
        if screenshot:
            import PIL.Image
            import io
            image_data = base64.b64decode(screenshot)
            image = PIL.Image.open(io.BytesIO(image_data))
            contents.append(image)
        contents.append(text)
        resp = await asyncio.to_thread(
            model.generate_content,
            contents,
            generation_config={"max_output_tokens": 2048}
        )
        return self._parse_ai_response(resp.text)

    def _parse_ai_response(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            return {"thinking": text, "actions": [], "done": False}

    async def _execute_actions(self, tab_id, actions) -> dict:
        if not actions:
            return {"results": [], "error": None}
        steps = []
        for a in actions:
            step = {"action": a.get("action", "")}
            if a.get("selector"):
                step["selector"] = a["selector"]
            if a.get("value") is not None:
                step["value"] = str(a["value"])
            steps.append(step)
        try:
            resp = await self.browser.send({
                "type": "EXECUTE_ACTIONS", "tab_id": tab_id, "steps": steps,
            })
            return resp.get("result", resp.get("results", resp))
        except Exception as e:
            return {"error": str(e)}

    async def _notify(self, task, message):
        if self._progress_callback:
            await self._progress_callback(task, message)

    async def run_task(self, task_id, goal, tab_id, max_steps=DEFAULT_MAX_STEPS) -> TaskState:
        task = TaskState(task_id=task_id, goal=goal, tab_id=tab_id, max_steps=max_steps)
        self.tasks[task_id] = task
        self._prune_old_tasks()
        await self._notify(task, f"Starting: {goal}")

        try:
            for step_num in range(1, max_steps + 1):
                if task.cancelled:
                    task.status = "stopped"
                    task.summary = "Task stopped by user"
                    break
                task.step_count = step_num
                await self._notify(task, f"Step {step_num}/{max_steps}: Capturing page...")

                screenshot = await self._get_screenshot(tab_id)
                dom = await self._get_dom(tab_id)
                if not dom and not screenshot:
                    await self._notify(task, f"Step {step_num}: Waiting for page to load...")
                    await asyncio.sleep(2)
                    continue

                await self._notify(task, f"Step {step_num}/{max_steps}: AI analyzing page...")
                ai_response = await self._ask_ai(goal, dom, screenshot, task.history)
                thinking = ai_response.get("thinking", "")
                actions = ai_response.get("actions", [])
                done = ai_response.get("done", False)
                summary = ai_response.get("summary", "")

                await self._notify(task, f"Step {step_num}: {thinking[:120]}")

                if done:
                    task.status = "completed"
                    task.summary = summary or "Task completed"
                    await self._notify(task, f"✓ {task.summary}")
                    break

                if actions:
                    action_names = ", ".join([a.get("action", "?") for a in actions])
                    await self._notify(task, f"Step {step_num}: Executing: {action_names}")
                    result = await self._execute_actions(tab_id, actions)
                else:
                    result = {"note": "No actions taken"}

                task.history.append({"step": step_num, "thinking": thinking, "actions": actions, "result": result})
                await asyncio.sleep(AGENT_STEP_DELAY)

            if task.status == "running":
                task.status = "max_steps"
                task.summary = f"Reached maximum {max_steps} steps"
                await self._notify(task, task.summary)

        except Exception as e:
            task.status = "error"
            task.error = str(e)
            logger.error(f"Task {task_id} error: {e}", exc_info=True)
            await self._notify(task, f"Error: {e}")

        return task

    def stop_task(self, task_id):
        task = self.tasks.get(task_id)
        if task and task.status == "running":
            task.cancelled = True
            return True
        return False


agent = AutonomousAgent(browser_manager)


async def call_llm_for_quiz(prompt: str) -> Optional[dict]:
    """Call LLM with quiz-specific system prompt for MCQ reasoning."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    parse = agent._parse_ai_response

    # Try MCP sampling first
    global_session = await _mcp_session_tracker.get_session()
    if global_session and MCP_SAMPLING_AVAILABLE:
        try:
            from mcp.types import SamplingMessage, TextContent as MCPTextContent
            result = await asyncio.wait_for(
                global_session.create_message(
                    messages=[SamplingMessage(role="user", content=MCPTextContent(type="text", text=prompt))],
                    system_prompt=QUIZ_SYSTEM_PROMPT,
                    max_tokens=2048,
                ),
                timeout=MCP_SAMPLING_TIMEOUT,
            )
            if hasattr(result, 'content'):
                text = result.content.text if hasattr(result.content, 'text') else str(result.content)
                return parse(text)
        except Exception as e:
            logger.warning(f"MCP sampling for quiz failed: {e}")

    if anthropic_key and ANTHROPIC_AVAILABLE:
        client = anthropic.AsyncAnthropic(api_key=anthropic_key)
        resp = await client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=2048,
            system=QUIZ_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return parse(resp.content[0].text)

    if openai_key and OPENAI_AVAILABLE:
        client = openai_mod.AsyncOpenAI(api_key=openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o", max_tokens=2048,
            messages=[
                {"role": "system", "content": QUIZ_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return parse(resp.choices[0].message.content)

    if gemini_key and GEMINI_AVAILABLE:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash-exp",
            system_instruction=QUIZ_SYSTEM_PROMPT,
        )
        resp = await asyncio.to_thread(
            model.generate_content, prompt,
            generation_config={"max_output_tokens": 2048},
        )
        return parse(resp.text)

    raise ValueError("No LLM available for quiz solving. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY.")


async def send_task_progress(task: TaskState, message: str):
    if browser_manager._browser_ws:
        try:
            await browser_manager._browser_ws.send_json({
                "type": "TASK_PROGRESS", "task_id": task.task_id,
                "status": task.status, "step": task.step_count,
                "max_steps": task.max_steps, "message": message,
                "summary": task.summary, "goal": task.goal,
            })
        except Exception:
            pass

agent.set_progress_callback(send_task_progress)

# ─── Pydantic Models ──────────────────────────────────────────────────────────

class DOMState(BaseModel):
    url: str = ""
    title: str = ""
    timestamp: Optional[int] = None
    viewport: Optional[dict] = None
    inputs: list[dict] = Field(default_factory=list)
    buttons: list[dict] = Field(default_factory=list)
    links: list[dict] = Field(default_factory=list)
    selects: list[dict] = Field(default_factory=list)
    headings: list[dict] = Field(default_factory=list)
    checkboxes: list[dict] = Field(default_factory=list)
    radioButtons: list[dict] = Field(default_factory=list)
    tables: list[dict] = Field(default_factory=list)
    images: list[dict] = Field(default_factory=list)
    textSummary: str = ""

ALLOWED_ACTIONS = {
    "type", "click", "click_at", "select", "scroll", "navigate", "wait",
    "check", "uncheck", "press_key", "hover", "clear", "submit",
    "double_click", "focus", "go_back", "go_forward", "reload",
}

class ActionStep(BaseModel):
    action: str
    selector: Optional[str] = None
    value: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    amount: Optional[int] = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, v):
        if v not in ALLOWED_ACTIONS:
            raise ValueError(f"Action '{v}' not allowed. Must be one of: {ALLOWED_ACTIONS}")
        return v

    @field_validator("x", "y")
    @classmethod
    def validate_coordinate(cls, v):
        if v is None:
            return v
        if not isinstance(v, (int, float)):
            raise ValueError("Coordinate values must be numeric")
        return float(v)

class ExecuteActionsRequest(BaseModel):
    tab_id: Optional[str] = None
    actions: list[ActionStep]

class TabRegistration(BaseModel):
    tab_id: str
    url: str = ""
    title: str = ""

class TaskRequest(BaseModel):
    goal: str
    tab_id: Optional[str] = None
    max_steps: int = DEFAULT_MAX_STEPS

# ─── Helpers ──────────────────────────────────────────────────────────────────

def analyze_page_type(dom_state: dict) -> dict:
    radios = dom_state.get("radioButtons", [])
    checkboxes = dom_state.get("checkboxes", [])
    inputs = dom_state.get("inputs", [])
    buttons = dom_state.get("buttons", [])
    selects = dom_state.get("selects", [])
    links = dom_state.get("links", [])
    tables = dom_state.get("tables", [])
    title = dom_state.get("title", "").lower()
    url = dom_state.get("url", "").lower()
    suggestions = []
    page_type = "generic"
    confidence = 0.5

    if radios or ("quiz" in title or "test" in title or "exam" in title or "assessment" in title):
        page_type = "quiz"
        confidence = 0.9 if radios else 0.7
        radio_groups = len(set(r.get("name","") for r in radios if r.get("name")))
        suggestions.append({
            "action": "complete_quiz",
            "reasoning": f"Detected {len(radios)} radio buttons in {radio_groups} groups",
            "command": "Use browser_run_task with goal: 'Complete the quiz by analyzing each question and selecting the correct answers'"
        })
    elif inputs and (checkboxes or selects or any("submit" in b.get("text","").lower() for b in buttons)):
        page_type = "form"
        confidence = 0.8
        suggestions.append({"action": "fill_form", "reasoning": f"Detected {len(inputs)} input fields with submit",
            "command": "Use browser_fill_form or browser_run_task with goal: 'Fill out the form'"})
    elif any("login" in inp.get("name","").lower() or "password" in inp.get("type","") for inp in inputs):
        page_type = "login"
        confidence = 0.9
        suggestions.append({"action": "login", "reasoning": "Detected login form",
            "command": "Use browser_fill_form to fill credentials and submit"})
    elif checkboxes and ("survey" in title or "feedback" in title):
        page_type = "survey"
        confidence = 0.8
        suggestions.append({"action": "complete_survey", "reasoning": "Detected survey checkboxes",
            "command": "Use browser_run_task with goal: 'Complete the survey'"})
    elif tables:
        page_type = "data_table"
        confidence = 0.7
        suggestions.append({"action": "extract_data", "reasoning": f"Detected {len(tables)} table(s)",
            "command": "Use browser_extract_text to extract table data"})
    elif len(links) > 20:
        page_type = "navigation"
        confidence = 0.6
        suggestions.append({"action": "navigate", "reasoning": f"{len(links)} links found",
            "command": "Use browser_get_page_state to see links, then click the desired one"})

    return {
        "type": page_type, "confidence": confidence, "suggestions": suggestions,
        "context": {
            "has_quiz": len(radios) > 0, "has_form": len(inputs) > 0,
            "has_checkboxes": len(checkboxes) > 0,
            "radio_groups": len(set(r.get("name","") for r in radios if r.get("name"))),
            "input_fields": len(inputs), "buttons": len(buttons),
            "links": len(links), "tables": len(tables),
        }
    }

def format_dom_for_display(dom_state: dict) -> str:
    parts = []
    parts.append(f"## Page: {dom_state.get('title', 'N/A')}")
    parts.append(f"URL: {dom_state.get('url', 'N/A')}")

    radios = dom_state.get("radioButtons", [])
    if radios:
        parts.append(f"\n## Radio Buttons ({len(radios)} found)")
        groups: dict[str, list] = {}
        for rb in radios:
            groups.setdefault(rb.get("name","unknown"), []).append(rb)
        for group_name, items in groups.items():
            parts.append(f"  Group: {group_name}")
            for rb in items:
                label = rb.get("label") or rb.get("value") or rb.get("context") or "unlabeled"
                state = "SELECTED" if rb.get("checked") else "  "
                parts.append(f'    ({state}) {label}\n      selector: {rb.get("selector","N/A")}')

    checkboxes = dom_state.get("checkboxes", [])
    if checkboxes:
        parts.append(f"\n## Checkboxes ({len(checkboxes)} found)")
        for cb in checkboxes:
            label = cb.get("label") or cb.get("context") or "unlabeled"
            state = "CHECKED" if cb.get("checked") else "unchecked"
            parts.append(f'  - [{state}] {label}\n    selector: {cb.get("selector","N/A")}')

    vp = dom_state.get("viewport", {})
    if vp:
        parts.append(f"Viewport: {vp.get('width','?')}x{vp.get('height','?')} | scrollY: {vp.get('scrollY',0)} / {vp.get('scrollHeight',0)}")

    headings = dom_state.get("headings", [])
    if headings:
        parts.append("\n## Headings")
        for h in headings[:15]:
            parts.append(f"  [{h.get('tag','h')}] {h.get('text','')}")

    inputs = dom_state.get("inputs", [])
    if inputs:
        parts.append(f"\n## Input Fields ({len(inputs)} found)")
        for inp in inputs[:50]:
            label = inp.get("label") or inp.get("placeholder") or inp.get("name") or "unlabeled"
            flags = []
            if inp.get("disabled"): flags.append("DISABLED")
            if inp.get("readOnly"): flags.append("READONLY")
            if inp.get("required"): flags.append("REQUIRED")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            parts.append(f'  - {label} (type={inp.get("type","text")}){flag_str}\n    selector: {inp.get("selector","N/A")}\n    value: "{inp.get("value","")}"')

    selects = dom_state.get("selects", [])
    if selects:
        parts.append(f"\n## Dropdowns ({len(selects)} found)")
        for sel in selects[:30]:
            label = sel.get("label") or sel.get("name") or "unlabeled"
            opts_text = ", ".join([f'"{o.get("text","")}"' for o in sel.get("options",[])[:8]])
            parts.append(f'  - {label}\n    selector: {sel.get("selector","N/A")}\n    current: {sel.get("currentValue","")}\n    options: [{opts_text}]')

    buttons = dom_state.get("buttons", [])
    if buttons:
        parts.append(f"\n## Buttons ({len(buttons)} found)")
        for btn in buttons[:50]:
            disabled = " [DISABLED]" if btn.get("disabled") else ""
            parts.append(f'  - "{btn.get("text","")}"{disabled}\n    selector: {btn.get("selector","N/A")}')

    links = dom_state.get("links", [])
    if links:
        parts.append(f"\n## Links ({len(links)} visible, showing first 30)")
        for link in links[:30]:
            parts.append(f'  - "{link.get("text","")}"\n    href: {link.get("href","#")}\n    selector: {link.get("selector","N/A")}')

    tables = dom_state.get("tables", [])
    if tables:
        parts.append(f"\n## Tables ({len(tables)})")
        for i, table in enumerate(tables[:3]):
            parts.append(f"  Table {i+1}: {table.get('caption','No caption')}")
            headers = table.get("headers", [])
            if headers:
                parts.append(f"    Headers: {' | '.join(headers)}")
            for row in table.get("rows",[])[:10]:
                parts.append(f"    Row: {' | '.join(str(c) for c in row)}")

    images = dom_state.get("images", [])
    if images:
        parts.append(f"\n## Images ({len(images)})")
        for img in images[:10]:
            alt = img.get("alt") or "no alt text"
            parts.append(f'  - [{alt}] src: {img.get("src","")[:80]}\n    selector: {img.get("selector","N/A")}')

    text_summary = dom_state.get("textSummary", "")
    if text_summary:
        truncated = text_summary[:MAX_TEXT_SUMMARY_CHARS]
        suffix = f"... [{len(text_summary)} total chars]" if len(text_summary) > MAX_TEXT_SUMMARY_CHARS else ""
        parts.append(f"\n## Visible Page Text ({len(text_summary)} chars)")
        parts.append(f"  {truncated}{suffix}")

    return "\n".join(parts)


COMPILE_BUTTON_SELECTORS = [
    "#programme-compile",
    "button[id*='compile']",
    "//button[contains(text(),'Compile')]",
    "//button[contains(text(),'Run')]",
    "//button[contains(text(),'Compile & Run')]",
    ".compile-btn",
    "[data-action='compile']",
]

SUBMIT_BUTTON_SELECTORS = [
    "#tt-footer-submit-answer",
    "button[id*='submit']",
    "//button[contains(text(),'Submit Code')]",
    "//button[contains(text(),'Submit')]",
    ".submit-btn",
]

QUIZ_NAV_SELECTORS = {
    "next": [
        "#next-btn",
        "button[id*='next']",
        "[data-action='next']",
        "button.next",
        ".next-btn",
        "a.next",
        "//button[contains(normalize-space(),'Next')]",
        "//a[contains(normalize-space(),'Next')]",
        "//*[@role='button' and contains(normalize-space(),'Next')]",
        "//*[contains(translate(@class,'NEXT','next'),'next')]",
    ],
    "previous": [
        "#prev-btn",
        "button[id*='prev']",
        "[data-action='previous']",
        "[data-action='prev']",
        "button.prev",
        ".prev-btn",
        "a.prev",
        "//button[contains(normalize-space(),'Previous')]",
        "//button[contains(normalize-space(),'Prev')]",
        "//a[contains(normalize-space(),'Previous')]",
        "//*[@role='button' and contains(normalize-space(),'Prev')]",
        "//*[contains(translate(@class,'PREV','prev'),'prev')]",
    ],
    "submit": [
        "#tt-header-submit",
        "#tt-footer-submit-answer",
        "button[id*='submit']",
        "[data-action='submit']",
        ".submit-btn",
        "//button[contains(normalize-space(),'Submit Test')]",
        "//button[contains(normalize-space(),'Submit Code')]",
        "//button[contains(normalize-space(),'Submit')]",
        "//a[contains(normalize-space(),'Submit')]",
        "//*[@role='button' and contains(normalize-space(),'Submit')]",
    ],
}

QUIZ_POSITION_PATTERN = re.compile(r"Question\s*No\s*:?\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def parse_compilation_summary(full_text: str) -> dict:
    output = {"status": "unknown", "passed": 0, "total": 0, "compiler_errors": [], "test_cases": []}

    result_match = re.search(r'(\d+)/(\d+)\s+(?:Sample\s+)?[Tt]estcase(?:s)?\s+[Pp]assed', full_text)
    if result_match:
        output["passed"] = int(result_match.group(1))
        output["total"] = int(result_match.group(2))
        output["status"] = "all_passed" if output["passed"] == output["total"] else "some_failed"

    passed_match = re.search(r'(\d+)/(\d+)\s+Testcases\s+Passed', full_text)
    if passed_match:
        output["passed"] = int(passed_match.group(1))
        output["total"] = int(passed_match.group(2))
        output["status"] = "all_passed" if output["passed"] == output["total"] else "some_failed"

    error_match = re.search(r'Compiler Message\s+(.+?)(?=Sample Testcase|Testcase|\Z)', full_text, re.DOTALL)
    if error_match:
        error_text = error_match.group(1).strip()
        if error_text and 'error' in error_text.lower():
            output["status"] = "compilation_error"
            output["compiler_errors"] = [line.strip() for line in error_text.split('\n') if line.strip()][:15]

    test_case_pattern = re.findall(r'Testcase\s+(\d+)\s*[-–]\s*(Passed|Failed)', full_text, re.IGNORECASE)
    for test_case_number, test_case_status in test_case_pattern:
        output["test_cases"].append({"number": test_case_number, "status": test_case_status})

    expected_actual = re.findall(r'Expected Output\s+(.+?)(?:Your Output|Output)\s+(.+?)(?=Testcase|\Z)', full_text, re.DOTALL)
    for index, (expected, actual) in enumerate(expected_actual):
        if index < len(output["test_cases"]):
            output["test_cases"][index]["expected"] = expected.strip()[:300]
            output["test_cases"][index]["actual"] = actual.strip()[:300]

    return output


def extract_code_from_response(response_text: str) -> str:
    cleaned = (response_text or "").strip()
    if not cleaned:
        return ""

    fenced_block = re.search(r"```(?:[a-zA-Z0-9_+.-]+)?\s*([\s\S]*?)```", cleaned)
    if fenced_block:
        cleaned = fenced_block.group(1).strip()

    cleaned = re.sub(r'^```(?:\w+)?\s*\n?', '', cleaned)
    cleaned = re.sub(r'\n?\s*```\s*$', '', cleaned)
    cleaned = cleaned.strip()

    if not cleaned:
        return ""

    code_markers = [
        "#include", "int main", "using namespace", "public class", "class ",
        "def ", "import ", "from ", "fn main", "package ",
    ]
    for marker in code_markers:
        marker_index = cleaned.find(marker)
        if marker_index > 0:
            cleaned = cleaned[marker_index:]
            break

    return cleaned.strip()


def looks_like_source_code(candidate: str) -> bool:
    if not candidate:
        return False
    lines = [line for line in candidate.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    signal_tokens = [";", "{", "}", "#include", "def ", "class ", "import ", "return ", "main("]
    lowered = candidate.lower()
    return any(token.lower() in lowered for token in signal_tokens)


SAFE_RETRY_MESSAGE_TYPES = {
    "REQUEST_DOM",
    "TAKE_SCREENSHOT",
    "REQUEST_HTML",
    "EXTRACT_TEXT",
    "GET_ELEMENT_INFO",
    "WAIT_FOR_ELEMENT",
    "GET_CODE",
    "EXTRACT_CODING_PROBLEM",
}


async def send_with_retries(message: dict, timeout: float = WS_RESPONSE_TIMEOUT, retries: int = 2) -> dict:
    message_type = message.get("type", "")
    attempts = max(1, retries + 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await browser_manager.send(message, timeout=timeout)
        except (ConnectionError, TimeoutError) as error:
            last_error = error
            should_retry = message_type in SAFE_RETRY_MESSAGE_TYPES and attempt < attempts
            if not should_retry:
                break
            await asyncio.sleep(0.25 * attempt)
    raise last_error if last_error else TimeoutError("Request failed with unknown error")


async def click_first_selector(tab_id: str, selectors: list[str], timeout: float = 5.0) -> bool:
    for selector in selectors:
        try:
            response = await browser_manager.send(
                {"type": "EXECUTE_ACTIONS", "tab_id": tab_id, "steps": [{"action": "click", "selector": selector}]},
                timeout=timeout,
            )
            results = response.get("results") or response.get("result")
            if isinstance(results, list):
                if any(step_result.get("success") for step_result in results):
                    return True
            elif isinstance(results, dict):
                if not results.get("error"):
                    return True
        except Exception:
            continue
    return False


async def get_quiz_position(tab_id: str) -> tuple[Optional[int], Optional[int]]:
    try:
        response = await send_with_retries(
            {"type": "EXTRACT_TEXT", "tab_id": tab_id, "query": "Question No", "selector": ""},
            timeout=8.0,
            retries=1,
        )
        source = "\n".join(response.get("matches", [])) if response.get("matches") else response.get("text", "")
        match = QUIZ_POSITION_PATTERN.search(source or "")
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return None, None


# ─── WebSocket Endpoint ──────────────────────────────────────────────────────

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

            elif msg_type in ("SCREENSHOT_RESULT", "HTML_RESULT", "TEXT_EXTRACT_RESULT",
                              "ELEMENT_INFO_RESULT", "ELEMENT_WAIT_RESULT", "JS_RESULT",
                              "SET_CODE_RESULT", "GET_CODE_RESULT", "CODING_PROBLEM_RESULT"):
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


# ─── Task Handler ─────────────────────────────────────────────────────────────

async def handle_task_from_extension(data: dict):
    goal = data.get("goal", "")
    tab_id = data.get("tab_id")
    max_steps = data.get("max_steps", DEFAULT_MAX_STEPS)

    # Check for MCP session (either agent's session or global session)
    global_session = await _mcp_session_tracker.get_session()
    has_mcp = (agent._mcp_session is not None or global_session is not None) and MCP_SAMPLING_AVAILABLE
    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("GEMINI_API_KEY"))

    if not has_mcp and not has_api_key:
        if browser_manager._browser_ws:
            try:
                await browser_manager._browser_ws.send_json({
                    "type": "TASK_PROGRESS", "task_id": "error", "status": "error",
                    "step": 0, "max_steps": 0,
                    "message": "No LLM available. MCP sampling requires an active MCP session (invoke from MCP client), or set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY.",
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


# ─── HTTP Endpoints ───────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {
        "status": "healthy", "timestamp": datetime.utcnow().isoformat(),
        "server": SERVER_NAME, "version": SERVER_VERSION,
        "mcp_available": MCP_AVAILABLE,
        "ai_providers": {"anthropic": ANTHROPIC_AVAILABLE, "openai": OPENAI_AVAILABLE},
        "active_tabs": len(browser_manager.tabs), "ws_connected": browser_manager.connected,
    }

@app.post("/task")
async def start_task_http(req: TaskRequest):
    tab_id = req.tab_id
    if not tab_id:
        tab = browser_manager.get_active_tab()
        if not tab: raise HTTPException(400, "No active tab")
        tab_id = tab.tab_id
    task_id = str(uuid.uuid4())[:8]
    asyncio.create_task(agent.run_task(task_id, req.goal, tab_id, req.max_steps))
    return {"task_id": task_id, "status": "started"}

@app.get("/task/{task_id}")
async def get_task_http(task_id: str):
    task = agent.tasks.get(task_id)
    if not task: raise HTTPException(404, "Task not found")
    return {"task_id": task.task_id, "goal": task.goal, "status": task.status, "step": task.step_count,
            "max_steps": task.max_steps, "summary": task.summary, "error": task.error}

@app.delete("/task/{task_id}")
async def stop_task_http(task_id: str):
    if agent.stop_task(task_id): return {"status": "stopping"}
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
    if tab_id not in browser_manager.tabs: raise HTTPException(404, f"Tab {tab_id} not found")
    tab = browser_manager.tabs[tab_id]
    if tab.dom_state is None: raise HTTPException(404, f"No DOM state for tab {tab_id}")
    return tab.dom_state

@app.get("/tabs")
async def list_tabs_http():
    return {
        "tabs": [{"tab_id": t.tab_id, "url": t.url, "title": t.title, "last_updated": t.last_updated, "has_dom": t.dom_state is not None}
                 for t in browser_manager.tabs.values()],
        "active_tab_id": browser_manager.active_tab_id, "ws_connected": browser_manager.connected,
    }

@app.post("/tabs/{tab_id}/actions")
async def execute_actions_http(tab_id: str, request: ExecuteActionsRequest):
    if tab_id not in browser_manager.tabs: raise HTTPException(404, f"Tab {tab_id} not found")
    if not browser_manager.connected: raise HTTPException(503, "Browser not connected")
    try:
        return await browser_manager.send({"type": "EXECUTE_ACTIONS", "tab_id": tab_id, "steps": [a.model_dump() for a in request.actions]})
    except Exception as e:
        raise HTTPException(503, str(e))


# ─── MCP Server ──────────────────────────────────────────────────────────────

if MCP_AVAILABLE:
    mcp_server = Server(SERVER_NAME)

    @mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        all_tools = [
            Tool(name="browser_get_page_state", description="📄 Get structured DOM data of the active tab: radio buttons, checkboxes, inputs, buttons, links, dropdowns, tables, images, and EXACT CSS selectors.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string","description":"Optional: specific tab ID."}}}),
            Tool(name="browser_suggest_action", description="🤖 Analyze the page and suggest the best action. Detects quizzes, forms, login pages, surveys, data tables, and navigation pages.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string","description":"Optional: specific tab ID."}}}),
            Tool(name="browser_take_screenshot", description="📸 Take a screenshot of the active tab. Returns base64-encoded PNG.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string","description":"Optional: specific tab ID."}}}),
            Tool(name="browser_get_raw_html", description="Get the complete raw HTML source. Use when structured DOM is insufficient.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string","description":"Optional: specific tab ID."}}}),
            Tool(name="browser_extract_text", description="🔍 Extract/search text on the page. Provide query to search, selector to target an element, or neither for all visible text.", inputSchema={"type":"object","properties":{"query":{"type":"string","description":"Text to search for."},"selector":{"type":"string","description":"CSS selector to extract from."},"tab_id":{"type":"string"}}}),
            Tool(name="browser_get_element_info", description="🔎 Get detailed info about a DOM element: tag, classes, attributes, bounding box, computed styles, visibility.", inputSchema={"type":"object","properties":{"selector":{"type":"string","description":"CSS selector."},"tab_id":{"type":"string"}},"required":["selector"]}),
            Tool(name="browser_wait_for_element", description="⏳ Wait for an element to appear. Polls until found or timeout.", inputSchema={"type":"object","properties":{"selector":{"type":"string","description":"CSS selector to wait for."},"timeout":{"type":"integer","description":"Max wait in ms (default: 10000, max: 30000).","default":10000},"tab_id":{"type":"string"}},"required":["selector"]}),
            Tool(name="browser_execute_actions", description="Execute browser actions in sequence: click, click_at, type, select, scroll, navigate, wait, check, uncheck, press_key, hover, clear, focus, submit, double_click, go_back, go_forward, reload. Use EXACT selectors from browser_get_page_state when selector-based actions are used.", inputSchema={"type":"object","properties":{"actions":{"type":"array","description":"Actions to execute","items":{"type":"object","properties":{"action":{"type":"string","enum":list(ALLOWED_ACTIONS)},"selector":{"type":"string"},"value":{"type":"string"},"x":{"type":"number","description":"Viewport X coordinate for click_at."},"y":{"type":"number","description":"Viewport Y coordinate for click_at."},"amount":{"type":"integer","description":"Optional amount for scroll actions."}},"required":["action"]}},"tab_id":{"type":"string"}},"required":["actions"]}),
            Tool(name="browser_fill_form", description="📝 Intelligently fill a form. Match fields by name, label, placeholder, or selector. Optionally submit.", inputSchema={"type":"object","properties":{"fields":{"type":"object","description":"Field identifier → value pairs.","additionalProperties":{"type":"string"}},"submit":{"type":"boolean","default":False},"tab_id":{"type":"string"}},"required":["fields"]}),
            Tool(name="browser_execute_javascript", description="⚡ Evaluate a JavaScript expression in page context. Returns the result. Sandboxed — no eval/import/fetch.", inputSchema={"type":"object","properties":{"expression":{"type":"string","description":"JS expression to evaluate."},"tab_id":{"type":"string"}},"required":["expression"]}),
            Tool(name="browser_list_tabs", description="List all active browser tabs with URLs, titles, and status.", inputSchema={"type":"object","properties":{}}),
            Tool(name="browser_switch_tab", description="Switch to a different tab.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}},"required":["tab_id"]}),
            Tool(name="browser_open_tab", description="Open a new tab with a URL.", inputSchema={"type":"object","properties":{"url":{"type":"string"},"active":{"type":"boolean","default":True}},"required":["url"]}),
            Tool(name="browser_close_tab", description="Close a tab. Cannot close the last one.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}},"required":["tab_id"]}),
            Tool(name="browser_go_back", description="Navigate back in history.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}}}),
            Tool(name="browser_go_forward", description="Navigate forward in history.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}}}),
            Tool(name="browser_reload", description="Reload the page. Set hard=true to bypass cache.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"},"hard":{"type":"boolean","default":False}}}),
            Tool(name="browser_run_task", description="🤖 AUTONOMOUS AI AGENT — Runs complex tasks hands-free. Takes screenshots, analyzes the page, decides and executes actions, repeats until done. Perfect for quizzes, forms, multi-step workflows.", inputSchema={"type":"object","properties":{"goal":{"type":"string","description":"What to accomplish."},"tab_id":{"type":"string"},"max_steps":{"type":"integer","default":30}},"required":["goal"]}),
            
            # Quiz/Coding specialized tools
            Tool(name="browser_get_quiz_structure", description="📝 Extract structured quiz data: questions, options (with selectors), current state. Returns JSON with question text, all options, radio/checkbox selectors for programmatic answering.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string","description":"Optional: specific tab ID."}}}),
            Tool(name="browser_answer_question", description="✅ Answer a quiz question by clicking the specified option selector. Handles radio buttons, checkboxes. Verifies selection and captures result.", inputSchema={"type":"object","properties":{"selector":{"type":"string","description":"CSS selector or radio button value selector from browser_get_quiz_structure."},"tab_id":{"type":"string"}},"required":["selector"]}),
            Tool(name="browser_navigate_quiz", description="➡️ Navigate quiz (Next/Previous/Submit). Detects and clicks appropriate navigation buttons.", inputSchema={"type":"object","properties":{"action":{"type":"string","enum":["next","previous","submit"],"description":"Navigation action"},"tab_id":{"type":"string"}},"required":["action"]}),
            Tool(name="browser_solve_quiz", description="🧠 AUTONOMOUS QUIZ SOLVER — Extracts questions, reasons through answers using AI, selects correct options, and navigates through the entire quiz automatically. Handles radio buttons, checkboxes, and clickable options.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string","description":"Optional: specific tab ID."},"max_questions":{"type":"integer","description":"Max questions to attempt (default: 50).","default":50},"auto_navigate":{"type":"boolean","description":"Auto-click Next after answering (default: true).","default":True}}}),
            Tool(name="browser_get_coding_problem", description="💻 Extract coding problem: statement, input/output format, constraints, sample test cases, language. Returns structured problem data for code generation.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}}}),
            Tool(name="browser_set_code_editor", description="📄 Insert code into ACE/Monaco/CodeMirror editor. Uses page-level JS injection to directly call editor.setValue(), bypassing auto-indent issues. Instant and reliable.", inputSchema={"type":"object","properties":{"code":{"type":"string","description":"Complete code solution to insert."},"tab_id":{"type":"string"}},"required":["code"]}),
            Tool(name="browser_get_code_editor", description="📋 Read current code from the active code editor (ACE/Monaco/CodeMirror). Returns the code currently in the editor.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}}}),
            Tool(name="browser_compile_and_run", description="🔨 Click 'Compile & Run' or similar button, wait for results. Returns compilation status, test case results, errors.", inputSchema={"type":"object","properties":{"wait_time":{"type":"integer","description":"Seconds to wait for compilation (default: 10)","default":10},"tab_id":{"type":"string"}}}),
            Tool(name="browser_submit_solution", description="📤 Submit the coding solution. Clicks 'Submit Code' or similar button and captures submission result.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}}}),
            Tool(name="browser_get_test_results", description="📊 Parse test results from page: passed/failed count, error messages, expected vs actual output. Returns structured test data.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string"}}}),
            Tool(name="browser_solve_coding_problem", description="🧠 ONE-SHOT coding problem solver using the connected MCP LLM. Extracts the problem from the page, generates a correct solution using MCP sampling (VS Code Copilot/Claude Desktop), injects it into the code editor, compiles, checks results, and optionally retries if tests fail. Handles ACE/Monaco/CodeMirror editors on platforms like Neocolab, HackerRank, LeetCode, etc. Requires an active MCP client connection.", inputSchema={"type":"object","properties":{"tab_id":{"type":"string","description":"Optional: specific tab ID."},"auto_submit":{"type":"boolean","description":"Auto-submit after all sample tests pass (default: false).","default":False},"max_retries":{"type":"integer","description":"Max retries if compilation/tests fail (default: 2).","default":2},"language_override":{"type":"string","description":"Override detected language (e.g. 'C', 'C++', 'Python', 'Java')."}},"required":[]}),
        ]
        enabled = get_enabled_tool_names()
        if enabled is None:
            return all_tools
        return [tool for tool in all_tools if tool.name in enabled]

    @mcp_server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent]:
        # Update both the agent's session and the global tracker
        try:
            session = mcp_server.request_context.session
            agent.set_mcp_session(session)
            await _mcp_session_tracker.update_session(session)
        except Exception:
            pass

        enabled = get_enabled_tool_names()
        if enabled is not None and name not in enabled:
            return [
                TextContent(
                    type="text",
                    text=f"Tool '{name}' is disabled in '{ACTIVE_TOOL_PROFILE}' profile. Allowed tools: {', '.join(sorted(enabled))}",
                )
            ]

        try:
            if name == "browser_get_page_state":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                try:
                    resp = await send_with_retries({"type":"REQUEST_DOM","tab_id":tab.tab_id})
                    dom = resp.get("dom_state",{})
                    if dom: browser_manager.update_dom(tab.tab_id, dom)
                except (ConnectionError, TimeoutError):
                    if tab.dom_state is None:
                        return [TextContent(type="text", text=f"Error: Cannot get DOM for tab {tab.tab_id}")]
                if tab.dom_state is None:
                    return [TextContent(type="text", text=f"No DOM state for tab {tab.tab_id}.")]
                return [TextContent(type="text", text=format_dom_for_display(tab.dom_state))]

            elif name == "browser_suggest_action":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                try:
                    resp = await send_with_retries({"type":"REQUEST_DOM","tab_id":tab.tab_id})
                    dom = resp.get("dom_state",{})
                    if dom: browser_manager.update_dom(tab.tab_id, dom)
                except (ConnectionError, TimeoutError):
                    if tab.dom_state is None:
                        return [TextContent(type="text", text="Error: Cannot get page state")]
                if tab.dom_state is None:
                    return [TextContent(type="text", text="No DOM state available")]
                analysis = analyze_page_type(tab.dom_state)
                output = f"## Page Analysis\n\n**Type:** {analysis['type'].upper()}\n**Confidence:** {analysis['confidence']:.0%}\n\n"
                if analysis['suggestions']:
                    output += "## Recommended Actions\n\n"
                    for i, s in enumerate(analysis['suggestions'], 1):
                        output += f"{i}. **{s['action'].replace('_',' ').title()}**\n   {s['reasoning']}\n   ```\n   {s['command']}\n   ```\n\n"
                else:
                    output += "Generic page. Use browser_get_page_state, browser_take_screenshot, or browser_run_task.\n\n"
                ctx = analysis['context']
                output += f"## Elements\n- Radios: {ctx['radio_groups']} groups | Inputs: {ctx['input_fields']} | Checkboxes: {ctx['has_checkboxes']}\n- Buttons: {ctx['buttons']} | Links: {ctx['links']} | Tables: {ctx['tables']}\n"
                return [TextContent(type="text", text=output)]

            elif name == "browser_take_screenshot":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                resp = await send_with_retries({"type":"TAKE_SCREENSHOT","tab_id":tab.tab_id}, timeout=SCREENSHOT_TIMEOUT)
                screenshot = resp.get("screenshot")
                if screenshot:
                    return [ImageContent(type="image", data=screenshot, mimeType="image/png")]
                return [TextContent(type="text", text="Failed to capture screenshot")]

            elif name == "browser_get_raw_html":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                resp = await send_with_retries({"type":"REQUEST_HTML","tab_id":tab.tab_id}, timeout=10.0)
                html = resp.get("html", "")
                if html:
                    if len(html) > 500000:
                        html = html[:500000] + f"\n\n[TRUNCATED — {len(html)} chars total]"
                    return [TextContent(type="text", text=html)]
                return [TextContent(type="text", text=f"Error: {resp.get('error','No HTML returned')}")]

            elif name == "browser_extract_text":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                resp = await send_with_retries({"type":"EXTRACT_TEXT","tab_id":tab.tab_id,"query":arguments.get("query",""),"selector":arguments.get("selector","")}, timeout=15.0)
                matches = resp.get("matches", [])
                if matches:
                    output = f"Found {len(matches)} matches:\n\n"
                    for i, m in enumerate(matches, 1):
                        output += f"--- Match {i} ---\n{m}\n\n"
                    return [TextContent(type="text", text=output)]
                text = resp.get("text", "")
                if text:
                    return [TextContent(type="text", text=text[:100000])]
                return [TextContent(type="text", text=resp.get("error","No text found"))]

            elif name == "browser_get_element_info":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                resp = await send_with_retries({"type":"GET_ELEMENT_INFO","tab_id":tab.tab_id,"selector":arguments["selector"]}, timeout=10.0)
                if resp.get("error"):
                    return [TextContent(type="text", text=f"Error: {resp['error']}")]
                info = resp.get("info", {})
                parts = [f"## Element: <{info.get('tag','unknown')}>"]
                if info.get("id"): parts.append(f"ID: {info['id']}")
                if info.get("classes"): parts.append(f"Classes: {', '.join(info['classes'])}")
                if info.get("text"): parts.append(f"Text: {info['text'][:500]}")
                if info.get("attributes"):
                    parts.append("Attributes:")
                    for k, v in info["attributes"].items(): parts.append(f"  {k}: {v}")
                if info.get("rect"):
                    r = info["rect"]
                    parts.append(f"Position: ({r.get('x',0):.0f}, {r.get('y',0):.0f}) Size: {r.get('width',0):.0f}x{r.get('height',0):.0f}")
                if info.get("styles"):
                    parts.append("Styles:")
                    for k, v in info["styles"].items(): parts.append(f"  {k}: {v}")
                parts.append(f"Visible: {info.get('visible','?')} | Enabled: {info.get('enabled','?')}")
                return [TextContent(type="text", text="\n".join(parts))]

            elif name == "browser_wait_for_element":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                timeout_ms = min(arguments.get("timeout", 10000), 30000)
                resp = await send_with_retries({"type":"WAIT_FOR_ELEMENT","tab_id":tab.tab_id,"selector":arguments["selector"],"timeout":timeout_ms}, timeout=timeout_ms/1000+5)
                if resp.get("found"):
                    return [TextContent(type="text", text=f"Element found: {arguments['selector']}\nTag: {resp.get('tag','?')}\nText: {resp.get('text','')[:200]}")]
                return [TextContent(type="text", text=f"Element not found within {timeout_ms}ms: {arguments['selector']}")]

            elif name == "browser_execute_actions":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                actions = []
                for ad in arguments.get("actions", []):
                    try: actions.append(ActionStep(**ad))
                    except Exception as e: return [TextContent(type="text", text=f"Invalid action: {e}")]
                if not actions:
                    return [TextContent(type="text", text="No actions provided")]
                resp = await browser_manager.send({"type":"EXECUTE_ACTIONS","tab_id":tab.tab_id,"steps":[a.model_dump() for a in actions]})
                results = resp.get("results") or resp.get("result")
                if isinstance(results, list):
                    lines = [f"- {r.get('action','?')}: {'OK' if r.get('success') else 'FAIL: '+r.get('error','?')}" for r in results]
                    return [TextContent(type="text", text="Actions:\n"+"\n".join(lines))]
                elif isinstance(results, dict) and results.get("error"):
                    return [TextContent(type="text", text=f"Error: {results['error']}")]
                return [TextContent(type="text", text="Actions sent successfully")]

            elif name == "browser_fill_form":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                fields = arguments.get("fields", {})
                should_submit = arguments.get("submit", False)
                if not fields:
                    return [TextContent(type="text", text="Error: No fields provided")]
                try:
                    resp = await browser_manager.send({"type":"REQUEST_DOM","tab_id":tab.tab_id})
                    dom = resp.get("dom_state",{})
                    if dom: browser_manager.update_dom(tab.tab_id, dom)
                except Exception:
                    dom = tab.dom_state or {}
                actions = []
                matched, unmatched = [], []
                for fk, fv in fields.items():
                    found = None
                    for inp in dom.get("inputs",[]) + dom.get("selects",[]):
                        if (inp.get("name") and inp["name"].lower()==fk.lower()) or \
                           (inp.get("label") and fk.lower() in inp["label"].lower()) or \
                           (inp.get("placeholder") and fk.lower() in inp["placeholder"].lower()) or \
                           (inp.get("id") and inp["id"].lower()==fk.lower()):
                            found = inp["selector"]; break
                    if not found: found = fk
                    is_select = any(s["selector"]==found for s in dom.get("selects",[]))
                    if is_select:
                        actions.append({"action":"select","selector":found,"value":fv})
                    else:
                        actions.append({"action":"clear","selector":found})
                        actions.append({"action":"type","selector":found,"value":fv})
                    matched.append(fk)
                if should_submit:
                    for btn in dom.get("buttons",[]):
                        if btn.get("type")=="submit" or "submit" in btn.get("text","").lower():
                            actions.append({"action":"click","selector":btn["selector"]}); break
                if actions:
                    await browser_manager.send({"type":"EXECUTE_ACTIONS","tab_id":tab.tab_id,"steps":actions})
                output = f"Filled {len(matched)} fields: {', '.join(matched)}"
                if should_submit: output += "\nForm submitted."
                return [TextContent(type="text", text=output)]

            elif name == "browser_execute_javascript":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                expression = arguments.get("expression", "")
                if not expression:
                    return [TextContent(type="text", text="Error: No expression provided")]
                dangerous = ["eval(", "Function(", "import(", "XMLHttpRequest"]
                for d in dangerous:
                    if d.lower() in expression.lower():
                        return [TextContent(type="text", text=f"Error: Disallowed pattern: {d}")]
                resp = await browser_manager.send({"type":"EXECUTE_JS","tab_id":tab.tab_id,"expression":expression}, timeout=15.0)
                if resp.get("error"):
                    return [TextContent(type="text", text=f"JS Error: {resp['error']}")]
                return [TextContent(type="text", text=f"Result: {resp.get('result','undefined')}")]

            elif name == "browser_list_tabs":
                if not browser_manager.tabs:
                    return [TextContent(type="text", text="No browser tabs connected")]
                lines = []
                for t in browser_manager.tabs.values():
                    active = " [ACTIVE]" if t.tab_id==browser_manager.active_tab_id else ""
                    age = int(time.time()-t.last_updated)
                    lines.append(f"- {t.tab_id}{active}\n  URL: {t.url or 'N/A'}\n  Title: {t.title or 'N/A'}\n  DOM: {age}s ago")
                return [TextContent(type="text", text=f"Connected: {browser_manager.connected}\n\n"+"\n\n".join(lines))]

            elif name == "browser_switch_tab":
                tab_id = arguments.get("tab_id")
                if not tab_id: return [TextContent(type="text", text="Error: tab_id required")]
                tab = browser_manager.resolve_tab(tab_id)
                try: await browser_manager.send({"type":"SWITCH_TAB","tab_id":tab.tab_id})
                except Exception: pass
                browser_manager.set_active_tab(tab.tab_id)
                return [TextContent(type="text", text=f"Switched to tab {tab.tab_id}: {tab.title or tab.url}")]

            elif name == "browser_open_tab":
                url = arguments.get("url")
                if not url: return [TextContent(type="text", text="Error: url required")]
                if not browser_manager.connected: return [TextContent(type="text", text="Error: No browser connection")]
                active = arguments.get("active", True)
                resp = await browser_manager.send({"type":"OPEN_TAB","url":url,"active":active})
                new_id = str(resp.get("tab_id",""))
                if new_id:
                    browser_manager.register_tab(new_id)
                    if active: browser_manager.set_active_tab(new_id)
                return [TextContent(type="text", text=f"Opened tab: {url}")]

            elif name == "browser_close_tab":
                tab_id = arguments.get("tab_id")
                if not tab_id: return [TextContent(type="text", text="Error: tab_id required")]
                if len(browser_manager.tabs)<=1: return [TextContent(type="text", text="Cannot close the last tab")]
                tab = browser_manager.resolve_tab(tab_id)
                await browser_manager.send({"type":"CLOSE_TAB","tab_id":tab.tab_id})
                browser_manager.remove_tab(tab.tab_id)
                return [TextContent(type="text", text=f"Closed tab {tab.tab_id}")]

            elif name == "browser_go_back":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                await browser_manager.send({"type":"EXECUTE_ACTIONS","tab_id":tab.tab_id,"steps":[{"action":"go_back"}]})
                return [TextContent(type="text", text="Navigated back")]

            elif name == "browser_go_forward":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                await browser_manager.send({"type":"EXECUTE_ACTIONS","tab_id":tab.tab_id,"steps":[{"action":"go_forward"}]})
                return [TextContent(type="text", text="Navigated forward")]

            elif name == "browser_reload":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                hard = arguments.get("hard", False)
                await browser_manager.send({"type":"EXECUTE_ACTIONS","tab_id":tab.tab_id,"steps":[{"action":"reload","value":"hard" if hard else "soft"}]})
                return [TextContent(type="text", text="Page reloaded"+(" (hard)" if hard else ""))]

            elif name == "browser_run_task":
                goal = arguments.get("goal")
                if not goal: return [TextContent(type="text", text="Error: goal required")]
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                max_steps = arguments.get("max_steps", DEFAULT_MAX_STEPS)
                task_id = str(uuid.uuid4())[:8]
                result = await agent.run_task(task_id, goal, tab.tab_id, max_steps=max_steps)
                summary = f"Task: {result.status}\nSteps: {result.step_count}/{result.max_steps}\n"
                if result.summary: summary += f"Summary: {result.summary}\n"
                if result.error: summary += f"Error: {result.error}\n"
                return [TextContent(type="text", text=summary)]

            elif name == "browser_get_quiz_structure":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                
                # Use enhanced content script extraction
                quiz_data = {}
                try:
                    quiz_resp = await send_with_retries(
                        {"type": "EXTRACT_QUIZ_STRUCTURE", "tab_id": tab.tab_id},
                        timeout=8.0
                    )
                    if quiz_resp and not quiz_resp.get("error"):
                        quiz_data = quiz_resp
                except Exception:
                    pass
                
                # Fallback: DOM-based extraction if content script extraction failed
                if not quiz_data or not quiz_data.get("questions"):
                    try:
                        resp = await browser_manager.send({"type":"REQUEST_DOM","tab_id":tab.tab_id})
                        dom = resp.get("dom_state",{})
                        if dom: browser_manager.update_dom(tab.tab_id, dom)
                    except Exception:
                        dom = tab.dom_state or {}
                    
                    fallback_data = {"question": "", "options": [], "question_number": "", "total_questions": ""}
                    
                    text_resp = await browser_manager.send({"type":"EXTRACT_TEXT","tab_id":tab.tab_id,"query":"Question","selector":""})
                    full_text = text_resp.get("text", "")
                    
                    q_match = re.search(r'Question\s+No\s*:\s*(\d+)\s*/\s*(\d+)', full_text)
                    if q_match:
                        fallback_data["question_number"] = q_match.group(1)
                        fallback_data["total_questions"] = q_match.group(2)
                    
                    q_lines = []
                    in_question = False
                    for line in full_text.split('\n'):
                        if 'Multi Choice' in line or 'Single File' in line or 'Problem Statement' in line:
                            in_question = True
                            continue
                        if in_question and ('Marks :' in line or 'Input format' in line or 'Output format' in line):
                            break
                        if in_question and line.strip():
                            q_lines.append(line.strip())
                    fallback_data["question"] = ' '.join(q_lines)
                    
                    radio_groups = dom.get("radios", {})
                    for group_name, buttons in radio_groups.items():
                        for btn in buttons:
                            fallback_data["options"].append({
                                "text": btn.get("text", ""),
                                "selector": btn.get("selector", ""),
                                "value": btn.get("value", ""),
                                "selected": btn.get("checked", False)
                            })
                    
                    fallback_data["full_context"] = full_text[:2000]
                    quiz_data = fallback_data
                
                return [TextContent(type="text", text=json.dumps(quiz_data, indent=2))]

            elif name == "browser_solve_quiz":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                max_questions = arguments.get("max_questions", 50)
                auto_navigate = arguments.get("auto_navigate", True)
                log_lines = []
                questions_answered = 0

                for q_idx in range(max_questions):
                    # 1. Extract current quiz structure
                    quiz_data = {}
                    try:
                        quiz_resp = await send_with_retries(
                            {"type": "EXTRACT_QUIZ_STRUCTURE", "tab_id": tab.tab_id},
                            timeout=8.0
                        )
                        if quiz_resp and not quiz_resp.get("error"):
                            quiz_data = quiz_resp
                    except Exception as e:
                        log_lines.append(f"⚠️ Quiz extraction failed: {e}")
                        break

                    questions = quiz_data.get("questions", [])
                    unanswered = [q for q in questions if not q.get("answered")]
                    
                    if not unanswered:
                        if questions:
                            log_lines.append(f"✅ All visible questions already answered")
                        else:
                            log_lines.append(f"⚠️ No quiz questions found on page")
                        break

                    # 2. For each unanswered question, use AI to reason through it
                    for question in unanswered:
                        q_text = question.get("question_text", "")
                        options = question.get("options", [])
                        q_type = question.get("type", "single_choice")
                        
                        if not options:
                            continue

                        quiz_prompt = f"""Answer this quiz question.

Question: {q_text}

Options:
{chr(10).join(f'  {chr(65+i)}. {opt["text"]} [selector: {opt["selector"]}]' for i, opt in enumerate(options))}

Type: {q_type} ({'select one' if q_type == 'single_choice' else 'select all that apply'})

Page context: {quiz_data.get('page_context', '')[:1000]}

Respond with STRICT JSON:
{{"thinking": "your step-by-step reasoning", "answers": [{{"selector": "exact CSS selector from options above", "confidence": 0.0-1.0}}]}}"""

                        try:
                            ai_response = await call_llm_for_quiz(quiz_prompt)
                            if not ai_response:
                                log_lines.append(f"⚠️ AI returned no response for: {q_text[:60]}")
                                continue

                            answers = ai_response.get("answers", [])
                            thinking = ai_response.get("thinking", "")
                            
                            for ans in answers:
                                selector = ans.get("selector", "")
                                confidence = ans.get("confidence", 0)
                                if not selector:
                                    continue

                                # Click the answer
                                click_resp = await browser_manager.send({
                                    "type": "EXECUTE_ACTIONS",
                                    "tab_id": tab.tab_id,
                                    "steps": [{"action": "click", "selector": selector}]
                                })
                                click_results = click_resp.get("results", [])
                                success = any(r.get("success") for r in click_results) if isinstance(click_results, list) else False

                                if success:
                                    questions_answered += 1
                                    log_lines.append(f"✅ Q{q_idx+1}: Answered (confidence: {confidence:.0%})")
                                    log_lines.append(f"   Reasoning: {thinking[:120]}")
                                else:
                                    log_lines.append(f"❌ Q{q_idx+1}: Click failed for selector: {selector}")

                            await asyncio.sleep(0.5)

                        except Exception as e:
                            log_lines.append(f"⚠️ Error solving question: {e}")

                    # 3. Auto-navigate to next question if applicable
                    if auto_navigate and quiz_data.get("has_navigation"):
                        nav = quiz_data.get("navigation", {})
                        next_sel = nav.get("next")
                        if next_sel:
                            try:
                                await browser_manager.send({
                                    "type": "EXECUTE_ACTIONS",
                                    "tab_id": tab.tab_id,
                                    "steps": [{"action": "click", "selector": next_sel}]
                                })
                                await asyncio.sleep(1.5)
                            except Exception:
                                log_lines.append("⚠️ Could not navigate to next question")
                                break
                        else:
                            break
                    else:
                        break

                log_lines.insert(0, f"📝 Quiz Solver: Answered {questions_answered} question(s)")
                return [TextContent(type="text", text="\n".join(log_lines))]

            elif name == "browser_answer_question":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                selector = arguments.get("selector")
                if not selector:
                    return [TextContent(type="text", text="Error: selector required")]
                
                # Click the option
                resp = await browser_manager.send({
                    "type":"EXECUTE_ACTIONS",
                    "tab_id":tab.tab_id,
                    "steps":[{"action":"click","selector":selector}]
                })
                
                # Wait briefly for state update
                await asyncio.sleep(0.5)
                
                # Verify selection
                try:
                    dom_resp = await browser_manager.send({"type":"REQUEST_DOM","tab_id":tab.tab_id})
                    dom = dom_resp.get("dom_state",{})
                    selected = "Unknown"
                    for group_name, buttons in dom.get("radios", {}).items():
                        for btn in buttons:
                            if btn.get("checked"):
                                selected = btn.get("text", "")
                                break
                    return [TextContent(type="text", text=f"Selected: {selected}\nAnswered: 1 question")]
                except:
                    return [TextContent(type="text", text="Option selected successfully")]

            elif name == "browser_navigate_quiz":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                action = arguments.get("action", "next").lower()

                if action not in {"next", "previous", "submit"}:
                    action = "next"

                before_q, before_total = await get_quiz_position(tab.tab_id)
                selectors = QUIZ_NAV_SELECTORS.get(action, QUIZ_NAV_SELECTORS["next"])

                clicked = await click_first_selector(tab.tab_id, selectors, timeout=6.0)

                if not clicked and action in {"next", "previous"}:
                    key_value = "ArrowRight" if action == "next" else "ArrowLeft"
                    try:
                        fallback_resp = await browser_manager.send(
                            {
                                "type": "EXECUTE_ACTIONS",
                                "tab_id": tab.tab_id,
                                "steps": [{"action": "press_key", "value": key_value}],
                            },
                            timeout=5.0,
                        )
                        fallback_results = fallback_resp.get("results") or fallback_resp.get("result")
                        if isinstance(fallback_results, list):
                            clicked = any(step_result.get("success") for step_result in fallback_results)
                        elif isinstance(fallback_results, dict):
                            clicked = not fallback_results.get("error")
                    except Exception:
                        clicked = False

                if not clicked:
                    return [
                        TextContent(
                            type="text",
                            text=f"Could not trigger '{action}' navigation. Try browser_execute_actions with a page-specific selector.",
                        )
                    ]

                await asyncio.sleep(1.5)

                after_q, after_total = await get_quiz_position(tab.tab_id)

                if action in {"next", "previous"} and before_q is not None and after_q is not None and after_q == before_q:
                    return [
                        TextContent(
                            type="text",
                            text=f"Triggered '{action}' but question did not change (still {after_q}/{after_total or before_total or '?'})",
                        )
                    ]

                if after_q is not None:
                    return [TextContent(type="text", text=f"Navigated {action}. Now on Question {after_q}/{after_total or '?'}")]

                return [TextContent(type="text", text=f"Triggered navigation action: {action}")]

            elif name == "browser_get_coding_problem":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                
                # Use structured extraction from content script
                structured = {}
                try:
                    structured = await send_with_retries(
                        {"type": "EXTRACT_CODING_PROBLEM", "tab_id": tab.tab_id}, timeout=15.0,
                    )
                except Exception:
                    pass
                
                # Also get full text as supplement
                resp = await send_with_retries({"type":"EXTRACT_TEXT","tab_id":tab.tab_id,"query":"","selector":""}, timeout=15.0)
                full_text = structured.get("full_text", "") or resp.get("text", "")
                
                # Get raw HTML for <pre> blocks
                raw_resp = await send_with_retries(
                    {"type": "REQUEST_HTML", "tab_id": tab.tab_id}, timeout=15.0,
                )
                raw_html = raw_resp.get("html", "")
                import html as html_module
                pre_contents = re.findall(r'<pre[^>]*>(.*?)</pre>', raw_html, re.DOTALL)
                pre_texts = [html_module.unescape(re.sub(r'<[^>]+>', '', p)).strip() for p in pre_contents]
                
                problem = {
                    "url": structured.get("url", ""),
                    "title": structured.get("title", ""),
                    "question_number": "",
                    "total_questions": "",
                    "problem_statement": structured.get("problem_statement", ""),
                    "input_format": structured.get("input_format", ""),
                    "output_format": structured.get("output_format", ""),
                    "constraints": structured.get("constraints", ""),
                    "sample_inputs": structured.get("sample_inputs", []),
                    "sample_outputs": structured.get("sample_outputs", []),
                    "pre_blocks": pre_texts,
                    "language": structured.get("detected_language", "") or "C",
                    "editor_type": structured.get("editor_type", ""),
                    "has_editor": structured.get("has_editor", False),
                    "compile_button": structured.get("compile_button", ""),
                    "submit_button": structured.get("submit_button", ""),
                }
                
                # Question number (supplement from full text)
                q_match = re.search(r'Question\s*No\s*:?\s*(\d+)\s*/\s*(\d+)', full_text)
                if q_match:
                    problem["question_number"] = q_match.group(1)
                    problem["total_questions"] = q_match.group(2)
                
                # Fill in sections from regex on full text if structured didn't get them
                if not problem["problem_statement"]:
                    statement_match = re.search(r'Problem Statement\s*[:\-]?\s*(.+?)(?=Input format|Marks|$)', full_text, re.DOTALL)
                    if statement_match:
                        problem["problem_statement"] = statement_match.group(1).strip()
                
                if not problem["input_format"]:
                    input_match = re.search(r'Input format\s*[:\-]?\s*(.+?)(?=Output format|Code constraints|$)', full_text, re.DOTALL)
                    if input_match:
                        problem["input_format"] = input_match.group(1).strip()
                
                if not problem["output_format"]:
                    output_match = re.search(r'Output format\s*[:\-]?\s*(.+?)(?=Code constraints|Sample test|$)', full_text, re.DOTALL)
                    if output_match:
                        problem["output_format"] = output_match.group(1).strip()
                
                if not problem["constraints"]:
                    constraints_match = re.search(r'Code constraints\s*[:\-]?\s*(.+?)(?=Sample test|$)', full_text, re.DOTALL)
                    if constraints_match:
                        problem["constraints"] = constraints_match.group(1).strip()
                
                # Extract sample I/O from text if structured didn't get them
                if not problem["sample_inputs"]:
                    sample_section = re.search(r'Sample test cases\s*:?\s*(.+?)(?=Note\s*:|Fill your code|Marks\s*:|$)', full_text, re.DOTALL)
                    if sample_section:
                        sample_text = sample_section.group(1)
                        io_pairs = re.findall(r'Input\s*\d+\s*:?\s*(.+?)Output\s*\d+\s*:?\s*(.+?)(?=Input\s*\d+\s*:|$)', sample_text, re.DOTALL)
                        for inp, out in io_pairs:
                            problem["sample_inputs"].append(inp.strip())
                            problem["sample_outputs"].append(out.strip())
                
                # Detect language from page if not already detected
                if problem["language"] == "C" and not structured.get("detected_language"):
                    lang_match = re.search(r'(?:C\+\+|Python|Java|JavaScript|C)\s*(?:\(\d+\)|\(GCC\))', full_text)
                    if lang_match:
                        problem["language"] = lang_match.group(0).split('(')[0].strip()
                
                # Include truncated raw text for context
                problem["full_text"] = full_text[:6000]
                
                return [TextContent(type="text", text=json.dumps(problem, indent=2))]

            elif name == "browser_set_code_editor":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                code = arguments.get("code", "")
                if not code:
                    return [TextContent(type="text", text="Error: code required")]
                
                # Send SET_CODE to background.js which uses chrome.scripting.executeScript MAIN world
                resp = await browser_manager.send({
                    "type":"SET_CODE",
                    "code":code,
                    "tab_id":tab.tab_id
                }, timeout=15.0)
                
                success = resp.get("success", False)
                msg = resp.get("message", "Unknown")
                lines = resp.get("lines", 0)
                
                if not success:
                    return [TextContent(type="text", text=f"Failed to set code: {msg}")]
                
                # Wait for editor to render
                await asyncio.sleep(0.5)
                
                # Verify by reading back
                try:
                    verify_resp = await send_with_retries({
                        "type":"GET_CODE",
                        "tab_id":tab.tab_id
                    }, timeout=10.0)
                    verify_code = verify_resp.get("code", "")
                    verify_lines = len(verify_code.strip().split('\n')) if verify_code.strip() else 0
                    expected_lines = len(code.strip().split('\n'))
                    
                    if verify_lines >= expected_lines - 2:
                        return [TextContent(type="text", text=f"Code inserted successfully ({lines} lines, {len(code)} chars) - {msg}. Verified: {verify_lines} lines in editor.")]
                    else:
                        return [TextContent(type="text", text=f"WARNING: Code may be incomplete. Expected {expected_lines} lines but editor has {verify_lines}. Try again or check manually.")]
                except:
                    return [TextContent(type="text", text=f"Code inserted ({lines} lines, {len(code)} chars) - {msg}")]

            elif name == "browser_get_code_editor":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                resp = await send_with_retries({
                    "type":"GET_CODE",
                    "tab_id":tab.tab_id
                }, timeout=10.0)
                
                success = resp.get("success", False)
                code = resp.get("code", "")
                editor = resp.get("editor", "unknown")
                
                if not success:
                    return [TextContent(type="text", text=f"Could not read editor: {resp.get('message', 'unknown error')}")]
                
                line_count = len(code.split('\n')) if code else 0
                return [TextContent(type="text", text=f"Editor: {editor} ({line_count} lines)\n\n```\n{code}\n```")]

            elif name == "browser_compile_and_run":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                wait_time = max(2, min(int(arguments.get("wait_time", 10)), 45))
                
                # Wait to ensure editor state is committed
                await asyncio.sleep(1.0)
                
                # Try multiple selectors for compile button
                compile_selectors = COMPILE_BUTTON_SELECTORS
                
                clicked = await click_first_selector(tab.tab_id, compile_selectors, timeout=5.0)
                
                if not clicked:
                    return [TextContent(type="text", text="Error: Could not find compile/run button")]
                
                # Poll for results, returning early when we have a stable signal
                full_text = ""
                for _ in range(max(1, wait_time // 2)):
                    await asyncio.sleep(2)
                    results_resp = await send_with_retries({"type":"EXTRACT_TEXT","tab_id":tab.tab_id,"query":"","selector":""}, timeout=15.0)
                    full_text = results_resp.get("text", "")
                    if re.search(r'(\d+)/(\d+)\s+(?:Sample\s+)?[Tt]estcase(?:s)?\s+[Pp]assed', full_text):
                        break
                    if re.search(r'Compiler Message|Compilation Error|\berror\b', full_text, re.IGNORECASE):
                        break

                output = parse_compilation_summary(full_text)
                
                summary = f"Status: {output['status']}\n"
                if output["total"]:
                    summary += f"Passed: {output['passed']}/{output['total']}\n"
                if output["compiler_errors"]:
                    summary += f"\nCompiler Errors:\n" + '\n'.join(output["compiler_errors"][:5]) + "\n"
                for tc in output["test_cases"]:
                    summary += f"\nTestcase {tc['number']}: {tc['status']}"
                    if tc.get('expected') and tc['status'] == 'Failed':
                        summary += f"\n  Expected: {tc['expected'][:100]}"
                        summary += f"\n  Got: {tc.get('actual', 'N/A')[:100]}"
                
                return [TextContent(type="text", text=summary)]

            elif name == "browser_submit_solution":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                
                # Try multiple selectors for submit button
                submit_selectors = SUBMIT_BUTTON_SELECTORS
                
                clicked = await click_first_selector(tab.tab_id, submit_selectors, timeout=5.0)
                
                if not clicked:
                    return [TextContent(type="text", text="Error: Could not find submit button")]
                
                # Wait for submission results
                await asyncio.sleep(5)
                
                # Extract final results
                results_resp = await send_with_retries({"type":"EXTRACT_TEXT","tab_id":tab.tab_id,"query":"","selector":""}, timeout=15.0)
                full_text = results_resp.get("text", "")
                
                # Parse submission results
                result = {"status": "submitted", "passed": 0, "total": 0, "test_details": []}
                
                passed_match = re.search(r'(\d+)/(\d+)\s+[Tt]estcase(?:s)?\s+[Pp]assed', full_text)
                if passed_match:
                    result["passed"] = int(passed_match.group(1))
                    result["total"] = int(passed_match.group(2))
                
                # Check for compilation errors
                if 'Compilation failed' in full_text or ('Compiler Message' in full_text and 'error' in full_text.lower()):
                    result["status"] = "compilation_error"
                
                # Test case details
                tc_matches = re.findall(r'(?:Test Case|Testcase)\s*(\d+).*?(Passed|Failed|Compilation failed)', full_text, re.IGNORECASE)
                for tc_num, tc_status in tc_matches:
                    result["test_details"].append({"case": tc_num, "status": tc_status})
                
                summary = f"Submission: {result['status']}\n"
                if result["total"]:
                    summary += f"Result: {result['passed']}/{result['total']} testcases passed\n"
                for td in result["test_details"]:
                    summary += f"  Test {td['case']}: {td['status']}\n"
                
                return [TextContent(type="text", text=summary)]

            elif name == "browser_get_test_results":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                
                # Extract test results section
                resp = await browser_manager.send({"type":"EXTRACT_TEXT","tab_id":tab.tab_id,"query":"","selector":""})
                full_text = resp.get("text", "")
                
                results = {
                    "passed": 0,
                    "failed": 0,
                    "total": 0,
                    "compiler_errors": [],
                    "test_cases": []
                }
                
                # Parse result summary
                result_match = re.search(r'Result\s+(\d+)/(\d+)\s+(?:Sample\s+)?testcase(?:s)?\s+passed', full_text, re.IGNORECASE)
                if result_match:
                    results["passed"] = int(result_match.group(1))
                    results["total"] = int(result_match.group(2))
                    results["failed"] = results["total"] - results["passed"]
                
                # Extract compiler errors
                error_match = re.search(r'Compiler Message\s+(.+?)(?=Sample Testcase|Result|$)', full_text, re.DOTALL)
                if error_match:
                    errors = error_match.group(1).strip()
                    results["compiler_errors"] = errors.split('\n')[:10]
                
                # Extract test case details
                test_matches = re.findall(r'Testcase\s+(\d+)\s+-\s+(Passed|Failed)\s+Expected Output\s+(.+?)Output\s+(.+?)(?=Testcase|$)', full_text, re.DOTALL)
                for test_num, status, expected, actual in test_matches:
                    results["test_cases"].append({
                        "number": test_num,
                        "status": status,
                        "expected": expected.strip()[:200],
                        "actual": actual.strip()[:200]
                    })
                
                return [TextContent(type="text", text=json.dumps(results, indent=2))]

            elif name == "browser_solve_coding_problem":
                tab = browser_manager.resolve_tab(arguments.get("tab_id"))
                auto_submit = arguments.get("auto_submit", False)
                max_retries = min(arguments.get("max_retries", 2), 5)
                language_override = arguments.get("language_override", "")

                log_lines = []

                # ── Step 1: Deep-extract the coding problem ──
                log_lines.append("## Step 1: Extracting problem from page...")

                # Try structured extraction first
                structured = {}
                try:
                    structured = await browser_manager.send(
                        {"type": "EXTRACT_CODING_PROBLEM", "tab_id": tab.tab_id}, timeout=15.0,
                    )
                except Exception as e:
                    log_lines.append(f"  Structured extraction failed: {e}")

                # Also get full text as fallback/supplement
                text_resp = await browser_manager.send(
                    {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "", "selector": ""},
                    timeout=15.0,
                )
                full_text = structured.get("full_text", "") or text_resp.get("text", "")

                # Also get raw HTML for <pre> blocks
                raw_resp = await browser_manager.send(
                    {"type": "REQUEST_HTML", "tab_id": tab.tab_id}, timeout=15.0,
                )
                raw_html = raw_resp.get("html", "")

                # Extract <pre> tags from HTML
                import html as html_module
                pre_contents = re.findall(r'<pre[^>]*>(.*?)</pre>', raw_html, re.DOTALL)
                pre_texts = [html_module.unescape(re.sub(r'<[^>]+>', '', p)).strip() for p in pre_contents]

                # Merge pre_blocks from structured extraction
                struct_pre = structured.get("pre_blocks", [])
                if struct_pre and not pre_texts:
                    pre_texts = [b.get("text", "") for b in struct_pre if b.get("text")]

                # Detect language
                detected_lang = language_override or structured.get("detected_language", "") or "C"
                if not language_override and not structured.get("detected_language"):
                    lang_match = re.search(r'(?:C\+\+|Python|Java|JavaScript|C)\s*(?:\(\d+\)|\(GCC\))', full_text)
                    if lang_match:
                        detected_lang = lang_match.group(0).split('(')[0].strip()
                    elif 'python' in full_text.lower():
                        detected_lang = "Python"
                    elif 'c++' in full_text.lower() or 'cpp' in full_text.lower():
                        detected_lang = "C++"

                # Use compile/submit button selectors from structured data
                compile_sel = structured.get("compile_button", "")
                submit_sel = structured.get("submit_button", "")

                # Build problem description for LLM
                problem_text = f"""CODING PROBLEM (solve in {detected_lang}):

=== FULL PAGE TEXT ===
{full_text[:8000]}

=== PRE-FORMATTED BLOCKS (sample I/O) ===
"""
                for i, pt in enumerate(pre_texts):
                    problem_text += f"\n--- Block {i+1} ---\n{pt}\n"

                # Add structured sections if available
                if structured.get("problem_statement"):
                    problem_text += f"\n=== PROBLEM STATEMENT ===\n{structured['problem_statement']}\n"
                if structured.get("input_format"):
                    problem_text += f"\n=== INPUT FORMAT ===\n{structured['input_format']}\n"
                if structured.get("output_format"):
                    problem_text += f"\n=== OUTPUT FORMAT ===\n{structured['output_format']}\n"
                if structured.get("constraints"):
                    problem_text += f"\n=== CONSTRAINTS ===\n{structured['constraints']}\n"
                if structured.get("sample_inputs"):
                    for i, si in enumerate(structured["sample_inputs"]):
                        problem_text += f"\n=== SAMPLE INPUT {i+1} ===\n{si}\n"
                if structured.get("sample_outputs"):
                    for i, so in enumerate(structured["sample_outputs"]):
                        problem_text += f"\n=== SAMPLE OUTPUT {i+1} ===\n{so}\n"

                log_lines.append(f"  Language: {detected_lang}")
                log_lines.append(f"  Editor: {structured.get('editor_type', 'unknown')}")
                log_lines.append(f"  Extracted {len(full_text)} chars, {len(pre_texts)} pre blocks")
                if compile_sel:
                    log_lines.append(f"  Compile btn: {compile_sel}")
                if submit_sel:
                    log_lines.append(f"  Submit btn: {submit_sel}")

                # ── Step 2: Ask the LLM to generate code ──
                log_lines.append("\n## Step 2: Generating solution via LLM...")

                code_gen_prompt = f"""{problem_text}

=== INSTRUCTIONS ===
1. Read the problem statement, input format, output format, sample test cases, and constraints carefully.
2. Write a COMPLETE, COMPILABLE solution in {detected_lang}.
3. Match the EXACT output format shown in the sample outputs (every space, every newline matters).
4. The output must match character-for-character including trailing spaces if the sample shows them.
5. Read input from stdin, write output to stdout.
6. Do NOT include any explanatory text — output ONLY the raw code.
7. Do NOT wrap the code in markdown code fences.
8. Handle edge cases properly.
"""

                # Get the MCP session to ask the LLM
                mcp_session = None
                try:
                    mcp_session = mcp_server.request_context.session
                except Exception:
                    pass
                if not mcp_session:
                    mcp_session = await _mcp_session_tracker.get_session()

                # Verify MCP sampling is available
                if not mcp_session or not MCP_SAMPLING_AVAILABLE:
                    return [TextContent(type="text", text="Error: No MCP LLM connection available. This tool requires an active MCP client (like VS Code Copilot or Claude Desktop) with sampling support.")]

                generated_code = ""
                last_generation_error = ""
                generation_attempts = max(2, min(max_retries + 1, 4))
                for generation_attempt in range(1, generation_attempts + 1):
                    try:
                        attempt_prompt = code_gen_prompt
                        if generation_attempt > 1:
                            attempt_prompt += (
                                "\n\nIMPORTANT: Your previous response was empty or not valid code. "
                                "Return ONLY complete source code without explanations."
                            )
                        messages = [SamplingMessage(
                            role="user",
                            content=TextContent(type="text", text=attempt_prompt),
                        )]
                        kwargs = {
                            "messages": messages,
                            "max_tokens": 4096,
                            "system_prompt": "You are an expert competitive programmer. Output ONLY raw source code, no markdown fences, no explanations. The code must compile and produce exact output matching the problem's expected output format.",
                        }
                        result = await create_mcp_message_with_fallback(
                            mcp_session,
                            messages=kwargs["messages"],
                            max_tokens=kwargs["max_tokens"],
                            system_prompt=kwargs["system_prompt"],
                        )

                        raw_output = ""
                        if hasattr(result, "content"):
                            if hasattr(result.content, "text"):
                                raw_output = result.content.text
                            elif isinstance(result.content, str):
                                raw_output = result.content
                            else:
                                raw_output = str(result.content)
                        else:
                            raw_output = str(result)

                        candidate_code = extract_code_from_response(raw_output)
                        if candidate_code and looks_like_source_code(candidate_code):
                            generated_code = candidate_code
                            break

                        last_generation_error = "LLM returned empty/non-code output"
                        log_lines.append(f"  Attempt {generation_attempt}: empty/non-code output, retrying...")
                    except Exception as e:
                        last_generation_error = str(e)
                        log_lines.append(f"  Attempt {generation_attempt}: MCP sampling error: {e}")

                if not generated_code:
                    return [TextContent(type="text", text="\n".join(log_lines) + f"\n\nError: Failed to generate valid code. Last issue: {last_generation_error or 'unknown'}")]

                code_lines = len(generated_code.split('\n'))
                log_lines.append(f"  Generated {code_lines} lines of {detected_lang} code")

                # ── Step 3: Inject code into the editor ──
                log_lines.append("\n## Step 3: Injecting code into editor...")

                for attempt in range(max_retries + 1):
                    # Inject code
                    set_resp = await browser_manager.send(
                        {"type": "SET_CODE", "code": generated_code, "tab_id": tab.tab_id},
                        timeout=15.0,
                    )
                    set_success = set_resp.get("success", False)
                    set_msg = set_resp.get("message", "Unknown")

                    if not set_success:
                        log_lines.append(f"  Injection failed: {set_msg}")
                        if attempt == max_retries:
                            log_lines.append("\n## Result: FAILED to inject code into editor")
                            log_lines.append(f"\n## Generated Code ({detected_lang}):\n```{detected_lang.lower()}\n{generated_code}\n```")
                            return [TextContent(type="text", text="\n".join(log_lines))]
                        await asyncio.sleep(1)
                        continue

                    log_lines.append(f"  Code injected via {set_msg}")

                    # Verify injection
                    await asyncio.sleep(0.5)
                    verify_resp = await browser_manager.send(
                        {"type": "GET_CODE", "tab_id": tab.tab_id}, timeout=10.0,
                    )
                    verify_code = verify_resp.get("code", "")
                    verify_lines = len(verify_code.strip().split('\n')) if verify_code.strip() else 0

                    if verify_lines < code_lines - 3:
                        log_lines.append(f"  WARNING: Verification shows {verify_lines}/{code_lines} lines — retrying...")
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue
                    else:
                        log_lines.append(f"  Verified: {verify_lines} lines in editor")
                        break

                # ── Step 4: Compile & Run ──
                log_lines.append("\n## Step 4: Compiling & running...")
                await asyncio.sleep(0.5)

                # Click compile button
                compile_clicked = False
                compile_selectors = []
                # Use detected selector first if available
                if compile_sel:
                    compile_selectors.append(compile_sel)
                compile_selectors.extend(COMPILE_BUTTON_SELECTORS)
                for sel in compile_selectors:
                    try:
                        await browser_manager.send(
                            {"type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
                             "steps": [{"action": "click", "selector": sel}]},
                            timeout=5.0,
                        )
                        compile_clicked = True
                        break
                    except Exception:
                        continue

                if not compile_clicked:
                    log_lines.append("  Could not find Compile & Run button")
                    log_lines.append(f"\n## Generated Code ({detected_lang}):\n```{detected_lang.lower()}\n{generated_code}\n```")
                    return [TextContent(type="text", text="\n".join(log_lines))]

                # Wait for compilation results
                await asyncio.sleep(8)

                # Extract results
                result_text_resp = await browser_manager.send(
                    {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "", "selector": ""},
                    timeout=15.0,
                )
                result_text = result_text_resp.get("text", "")

                # Parse results
                passed = 0
                total = 0
                has_error = False
                error_info = ""

                result_match = re.search(r'(\d+)/(\d+)\s+(?:Sample\s+)?[Tt]estcase(?:s)?\s+[Pp]assed', result_text)
                if result_match:
                    passed = int(result_match.group(1))
                    total = int(result_match.group(2))

                error_match = re.search(r'(?:Compiler Message|Compilation Error|Error)\s*:?\s*(.+?)(?=Sample Testcase|Testcase|Result|\Z)', result_text, re.DOTALL)
                if error_match:
                    err_text = error_match.group(1).strip()
                    if 'error' in err_text.lower():
                        has_error = True
                        error_info = err_text[:500]

                if has_error:
                    log_lines.append(f"  Compilation error: {error_info[:200]}")
                elif total > 0:
                    log_lines.append(f"  Results: {passed}/{total} testcases passed")
                else:
                    log_lines.append("  Could not parse test results — check manually")

                # ── Step 5: Retry if needed ──
                if (has_error or (total > 0 and passed < total)) and max_retries > 0:
                    for retry in range(1, max_retries + 1):
                        log_lines.append(f"\n## Retry {retry}/{max_retries}: Fixing code...")

                        # Build fix prompt
                        fix_prompt = f"""The previous {detected_lang} solution had issues.

ORIGINAL PROBLEM:
{problem_text[:4000]}

PREVIOUS CODE:
```
{generated_code}
```

"""
                        if has_error:
                            fix_prompt += f"""COMPILER ERROR:
{error_info}

Fix the compilation error and output the complete corrected code.
"""
                        else:
                            # Extract expected vs actual from results
                            expected_actual = re.findall(
                                r'Expected Output[:\s]*(.+?)(?:Your Output|Actual Output|Output)[:\s]*(.+?)(?=Testcase|Expected|$)',
                                result_text, re.DOTALL
                            )
                            fix_prompt += f"TEST RESULTS: {passed}/{total} passed\n"
                            for i, (exp, act) in enumerate(expected_actual[:3]):
                                fix_prompt += f"\nFailed Test {i+1}:\n  Expected: {exp.strip()[:300]}\n  Got: {act.strip()[:300]}\n"
                            fix_prompt += "\nFix the code to produce the exact expected output. Output ONLY the complete corrected code, no explanations."

                        # Ask LLM for fix
                        fixed_code = ""
                        if not mcp_session or not MCP_SAMPLING_AVAILABLE:
                            log_lines.append("  Cannot retry: MCP LLM not available")
                            break
                        
                        try:
                            msgs = [SamplingMessage(role="user", content=TextContent(type="text", text=fix_prompt))]
                            fix_kwargs = {"messages": msgs, "max_tokens": 4096,
                                "system_prompt": "You are an expert competitive programmer. Output ONLY the complete fixed source code, no markdown, no explanations."}
                            fix_result = await create_mcp_message_with_fallback(
                                mcp_session,
                                messages=fix_kwargs["messages"],
                                max_tokens=fix_kwargs["max_tokens"],
                                system_prompt=fix_kwargs["system_prompt"],
                            )
                            if hasattr(fix_result, "content"):
                                if hasattr(fix_result.content, "text"):
                                    fixed_code = fix_result.content.text
                                elif isinstance(fix_result.content, str):
                                    fixed_code = fix_result.content
                                else:
                                    fixed_code = str(fix_result.content)
                            fixed_code = extract_code_from_response(fixed_code)
                        except Exception as fix_e:
                            log_lines.append(f"  Fix attempt failed: {fix_e}")
                            break

                        if not fixed_code:
                            log_lines.append("  LLM returned empty fix")
                            break

                        generated_code = fixed_code

                        # Re-inject
                        set_resp2 = await browser_manager.send(
                            {"type": "SET_CODE", "code": generated_code, "tab_id": tab.tab_id},
                            timeout=15.0,
                        )
                        if not set_resp2.get("success"):
                            log_lines.append(f"  Re-injection failed: {set_resp2.get('message','')}")
                            break

                        log_lines.append("  Fixed code injected, recompiling...")
                        await asyncio.sleep(1)

                        # Re-compile
                        for sel in compile_selectors:
                            try:
                                await browser_manager.send(
                                    {"type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
                                     "steps": [{"action": "click", "selector": sel}]},
                                    timeout=5.0)
                                break
                            except Exception:
                                continue

                        await asyncio.sleep(8)

                        # Re-check results
                        retry_text_resp = await browser_manager.send(
                            {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "", "selector": ""},
                            timeout=15.0,
                        )
                        result_text = retry_text_resp.get("text", "")

                        has_error = False
                        error_match2 = re.search(r'(?:Compiler Message|Compilation Error|Error)\s*:?\s*(.+?)(?=Sample Testcase|Testcase|Result|\Z)', result_text, re.DOTALL)
                        if error_match2:
                            err2 = error_match2.group(1).strip()
                            if 'error' in err2.lower():
                                has_error = True
                                error_info = err2[:500]

                        result_match2 = re.search(r'(\d+)/(\d+)\s+(?:Sample\s+)?[Tt]estcase(?:s)?\s+[Pp]assed', result_text)
                        if result_match2:
                            passed = int(result_match2.group(1))
                            total = int(result_match2.group(2))
                            log_lines.append(f"  Results: {passed}/{total} testcases passed")
                        elif has_error:
                            log_lines.append(f"  Still has error: {error_info[:100]}")

                        if not has_error and total > 0 and passed == total:
                            log_lines.append("  All tests passed!")
                            break

                # ── Step 6: Auto-submit if requested ──
                if auto_submit and not has_error and total > 0 and passed == total:
                    log_lines.append("\n## Step 6: Submitting solution...")
                    submit_selectors = []
                    if submit_sel:
                        submit_selectors.append(submit_sel)
                    submit_selectors.extend(SUBMIT_BUTTON_SELECTORS)
                    for sel in submit_selectors:
                        try:
                            await browser_manager.send(
                                {"type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
                                 "steps": [{"action": "click", "selector": sel}]},
                                timeout=5.0)
                            log_lines.append("  Solution submitted!")
                            break
                        except Exception:
                            continue
                    await asyncio.sleep(3)

                # ── Final summary ──
                status = "SUCCESS" if (not has_error and total > 0 and passed == total) else "NEEDS_REVIEW"
                log_lines.append(f"\n## Final Status: {status}")
                if total > 0:
                    log_lines.append(f"Test Results: {passed}/{total} passed")

                return [TextContent(type="text", text="\n".join(log_lines))]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except ConnectionError as e:
            return [TextContent(type="text", text=f"Connection error: {e}")]
        except TimeoutError as e:
            return [TextContent(type="text", text=f"Timeout: {e}")]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            logger.error(f"Tool {name} error: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Error: {e}")]


# ─── Main Entry Point ─────────────────────────────────────────────────────────

async def run_http_bg(port: int = 8000):
    import uvicorn
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning",
        log_config={"version":1,"disable_existing_loggers":False,
            "handlers":{"default":{"class":"logging.StreamHandler","stream":"ext://sys.stderr"}},
            "loggers":{"uvicorn":{"handlers":["default"],"level":"WARNING"},"uvicorn.error":{"handlers":["default"],"level":"WARNING"},"uvicorn.access":{"handlers":["default"],"level":"WARNING"}}})
    await uvicorn.Server(config).serve()

async def run_mcp_server():
    if not MCP_AVAILABLE:
        logger.error("MCP SDK not available. Install: pip install mcp")
        return
    logger.info("Starting MCP stdio + HTTP/WS on port 8000...")
    http_task = asyncio.create_task(run_http_bg(8000))
    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options())
    finally:
        http_task.cancel()
        try: await http_task
        except asyncio.CancelledError: pass

if __name__ == "__main__":
    import uvicorn
    if "--http" in sys.argv:
        port = 8000
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")+1
            if idx < len(sys.argv): port = int(sys.argv[idx])
        logger.info(f"Starting HTTP server on port {port}")
        uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True, log_level="info")
    else:
        asyncio.run(run_mcp_server())
