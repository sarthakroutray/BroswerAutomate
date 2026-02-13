"""
tools/__init__.py — Tool registry.

Central dispatch table mapping tool names → handlers.
Also provides MCP Tool definitions for list_tools().
"""

from .navigation import handle_navigate, handle_list_tabs
from .observation import (
    handle_observe, handle_screenshot, handle_extract_content,
    handle_inspect_element, handle_wait_element,
)
from .interaction import handle_act, handle_fill_form, handle_eval_js
from .quiz import handle_quiz_extract, handle_quiz_answer, handle_quiz_navigate
from .coding import (
    handle_extract_problem, handle_code_inject, handle_code_read_editor,
    handle_compile_run, handle_get_results, handle_submit,
)
from .schemas import ALLOWED_ACTIONS

# Tools that can mutate browser/application state.
MUTATING_TOOL_NAMES = {
    "browser_act", "browser_fill_form", "browser_navigate", "browser_run_task",
    "quiz_answer", "quiz_navigate", "code_inject", "code_compile_run", "code_submit",
}


def _strictify_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            out[k] = _strictify_schema(v)
        elif isinstance(v, list):
            out[k] = [_strictify_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    if out.get("type") == "object" and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def _augment_schema(tool_name: str, schema: dict) -> dict:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return schema
    props = schema.setdefault("properties", {})
    props.setdefault(
        "response_mode",
        {
            "type": "string",
            "enum": ["legacy", "structured", "dual"],
            "default": "dual",
            "description": "legacy=existing text output, structured=JSON envelope only, dual=both",
        },
    )
    if tool_name in MUTATING_TOOL_NAMES:
        props.setdefault(
            "idempotency_key",
            {
                "type": "string",
                "minLength": 8,
                "maxLength": 120,
                "description": "Optional duplicate-execution guard token for mutating actions",
            },
        )
    return schema


# ── Dispatch table ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    # observation
    "browser_observe":          handle_observe,
    "browser_screenshot":       handle_screenshot,
    "browser_extract_content":  handle_extract_content,
    "browser_inspect_element":  handle_inspect_element,
    "browser_wait_element":     handle_wait_element,
    # navigation
    "browser_navigate":         handle_navigate,
    "browser_list_tabs":        handle_list_tabs,
    # interaction
    "browser_act":              handle_act,
    "browser_fill_form":        handle_fill_form,
    "browser_eval_js":          handle_eval_js,
    # quiz
    "quiz_extract":             handle_quiz_extract,
    "quiz_answer":              handle_quiz_answer,
    "quiz_navigate":            handle_quiz_navigate,
    # coding
    "code_extract_problem":     handle_extract_problem,
    "code_inject":              handle_code_inject,
    "code_read_editor":         handle_code_read_editor,
    "code_compile_run":         handle_compile_run,
    "code_get_results":         handle_get_results,
    "code_submit":              handle_submit,
}


# ── MCP Tool schema definitions ──────────────────────────────────────────────

def get_mcp_tool_definitions() -> list[dict]:
    """Returns raw dicts suitable for constructing mcp.types.Tool objects."""
    tool_defs = [
        # ── observation ──
        {
            "name": "browser_observe",
            "description": "📄 Get structured DOM data + page-type analysis with selectors. Use before browser_act; do not invent selectors.",
            "inputSchema": {"type": "object", "properties": {
                "tab_id": {"type": "string", "description": "Optional: specific tab ID."},
            }},
        },
        {
            "name": "browser_screenshot",
            "description": "📸 Take a screenshot of the active tab. Returns base64 PNG for visual verification.",
            "inputSchema": {"type": "object", "properties": {
                "tab_id": {"type": "string"},
            }},
        },
        {
            "name": "browser_extract_content",
            "description": "🔍 Extract content from page. scope: visible_text | specific_element | structured_dom | raw_html. Optionally filter by query or selector.",
            "inputSchema": {"type": "object", "properties": {
                "scope": {"type": "string", "enum": ["visible_text", "specific_element", "structured_dom", "raw_html"]},
                "selector": {"type": "string", "description": "CSS selector (for specific_element)"},
                "query": {"type": "string", "description": "Text to search for"},
                "tab_id": {"type": "string"},
            }},
        },
        {
            "name": "browser_inspect_element",
            "description": "🔎 Get detailed info about a DOM element: tag, classes, attributes, bounding box, styles, visibility.",
            "inputSchema": {"type": "object", "properties": {
                "selector": {"type": "string", "description": "CSS selector"},
                "tab_id": {"type": "string"},
            }, "required": ["selector"]},
        },
        {
            "name": "browser_wait_element",
            "description": "⏳ Wait for an element to appear. Polls until found or timeout.",
            "inputSchema": {"type": "object", "properties": {
                "selector": {"type": "string"},
                "timeout": {"type": "integer", "description": "Max wait in ms (default: 10000, max: 30000)", "default": 10000},
                "require_visible": {"type": "boolean", "default": True, "description": "When true, waits for visibility/interactability too"},
                "tab_id": {"type": "string"},
            }, "required": ["selector"]},
        },
        # ── navigation ──
        {
            "name": "browser_navigate",
            "description": "🧭 Unified navigation: goto URL, back, forward, reload, new_tab, close_tab, switch_tab.",
            "inputSchema": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["goto", "back", "forward", "reload", "new_tab", "close_tab", "switch_tab"]},
                "url": {"type": "string", "description": "Required for goto and new_tab"},
                "tab_id": {"type": "string", "description": "Tab ID (required for close_tab and switch_tab)"},
                "hard_reload": {"type": "boolean", "default": False},
                "active": {"type": "boolean", "default": True},
            }, "required": ["action"]},
        },
        {
            "name": "browser_list_tabs",
            "description": "📋 List all active browser tabs with URLs, titles, and status.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # ── interaction ──
        {
            "name": "browser_act",
            "description": "⚡ Execute browser actions safely with retries and guards. Use selectors from browser_observe; avoid unsupported/custom action names.",
            "inputSchema": {"type": "object", "properties": {
                "actions": {"type": "array", "items": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
                    "selector": {"type": "string"},
                    "selector_fallbacks": {"type": "array", "items": {"type": "string"}},
                    "value": {"type": "string"},
                    "x": {"type": "number"}, "y": {"type": "number"},
                    "amount": {"type": "integer"},
                    "idempotency_token": {"type": "string"},
                    "expected_change": {"type": "string", "enum": ["none", "dom", "url", "dom_or_url"]},
                }, "required": ["action"]}},
                "tab_id": {"type": "string"},
            }, "required": ["actions"]},
        },
        {
            "name": "browser_fill_form",
            "description": "📝 Intelligently fill a form. Match fields by name/label/placeholder. Optionally submit.",
            "inputSchema": {"type": "object", "properties": {
                "fields": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Field → value pairs"},
                "submit": {"type": "boolean", "default": False},
                "tab_id": {"type": "string"},
            }, "required": ["fields"]},
        },
        {
            "name": "browser_eval_js",
            "description": "⚡ Evaluate a restricted read-only JS property path in page context. No eval/import/network/storage access.",
            "inputSchema": {"type": "object", "properties": {
                "expression": {"type": "string", "description": "Read-only path (e.g. document.title, location.href)", "minLength": 1, "maxLength": 500},
                "tab_id": {"type": "string"},
            }, "required": ["expression"]},
        },
        # ── agent ──
        {
            "name": "browser_run_task",
            "description": "🤖 Autonomous agent loop with failure classification, retries, and safe stop guards for multi-step workflows.",
            "inputSchema": {"type": "object", "properties": {
                "goal": {"type": "string", "description": "What to accomplish", "minLength": 3, "maxLength": 4000},
                "tab_id": {"type": "string"},
                "max_steps": {"type": "integer", "default": 30, "minimum": 1, "maximum": 100},
            }, "required": ["goal"]},
        },
        # ── quiz ──
        {
            "name": "quiz_extract",
            "description": "📝 Extract structured quiz data: questions, options (with selectors), current state.",
            "inputSchema": {"type": "object", "properties": {
                "tab_id": {"type": "string"},
            }},
        },
        {
            "name": "quiz_answer",
            "description": "✅ Answer a quiz question by clicking the specified option selector.",
            "inputSchema": {"type": "object", "properties": {
                "selector": {"type": "string", "description": "CSS selector of the answer option"},
                "tab_id": {"type": "string"},
            }, "required": ["selector"]},
        },
        {
            "name": "quiz_navigate",
            "description": "➡️ Navigate quiz: next, previous, or submit.",
            "inputSchema": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["next", "previous", "submit"]},
                "tab_id": {"type": "string"},
            }, "required": ["action"]},
        },
        # ── coding ──
        {
            "name": "code_extract_problem",
            "description": "💻 Extract coding problem: statement, I/O format, constraints, samples, language.",
            "inputSchema": {"type": "object", "properties": {"tab_id": {"type": "string"}}},
        },
        {
            "name": "code_inject",
            "description": "📄 Insert code into ACE/Monaco/CodeMirror editor via JS injection.",
            "inputSchema": {"type": "object", "properties": {
                "code": {"type": "string", "description": "Complete source code", "minLength": 1},
                "tab_id": {"type": "string"},
            }, "required": ["code"]},
        },
        {
            "name": "code_read_editor",
            "description": "📋 Read current code from the active code editor.",
            "inputSchema": {"type": "object", "properties": {"tab_id": {"type": "string"}}},
        },
        {
            "name": "code_compile_run",
            "description": "🔨 Click Compile & Run, wait for results. Returns test case results and errors.",
            "inputSchema": {"type": "object", "properties": {
                "wait_time": {"type": "integer", "description": "Seconds to wait (default: 10)", "default": 10, "minimum": 2, "maximum": 45},
                "tab_id": {"type": "string"},
            }},
        },
        {
            "name": "code_get_results",
            "description": "📊 Parse test results from page: passed/failed, error messages, expected vs actual.",
            "inputSchema": {"type": "object", "properties": {"tab_id": {"type": "string"}}},
        },
        {
            "name": "code_submit",
            "description": "📤 Submit the coding solution and capture submission result.",
            "inputSchema": {"type": "object", "properties": {"tab_id": {"type": "string"}}},
        },
    ]

    for td in tool_defs:
        td["inputSchema"] = _augment_schema(td["name"], _strictify_schema(td.get("inputSchema", {})))
    return tool_defs
