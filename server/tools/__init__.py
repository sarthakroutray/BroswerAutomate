"""
tools/__init__.py — FastMCP Tool Registry.

Registers all tools with the FastMCP server using native decorators.
Provides tool definitions, dispatch table, and profile-based filtering.
"""

from functools import wraps

from .observation import (
    handle_get_page_state,
    handle_take_screenshot,
    handle_extract_text,
    handle_wait_for_element,
)
from .navigation import handle_navigate, handle_list_tabs
from .interaction import handle_execute_actions, handle_execute_script
from .quiz import handle_navigate_quiz
from .coding import (
    handle_get_coding_problem,
    handle_set_code_editor,
    handle_get_code_editor,
    handle_compile_and_run,
    handle_get_test_results,
    handle_submit_solution,
)
from ..browser_state import _mcp_session_tracker

# ── Dispatch table (used by agent and transport) ──────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    # Observation
    "browser_get_page_state":    handle_get_page_state,
    "browser_take_screenshot":   handle_take_screenshot,
    "browser_extract_text":      handle_extract_text,
    "browser_wait_for_element":  handle_wait_for_element,
    # Navigation
    "browser_navigate":          handle_navigate,
    "browser_list_tabs":         handle_list_tabs,
    # Interaction
    "browser_execute_actions":   handle_execute_actions,
    "browser_execute_script":    handle_execute_script,
    # Quiz
    "browser_navigate_quiz":     handle_navigate_quiz,
    # Coding
    "browser_get_coding_problem":  handle_get_coding_problem,
    "browser_set_code_editor":     handle_set_code_editor,
    "browser_get_code_editor":     handle_get_code_editor,
    "browser_compile_and_run":     handle_compile_and_run,
    "browser_get_test_results":    handle_get_test_results,
    "browser_submit_solution":     handle_submit_solution,
}

# Mutating tools for idempotency guard
MUTATING_TOOL_NAMES = {
    "browser_execute_actions",
    "browser_navigate",
    "browser_run_task",
    "browser_navigate_quiz",
    "browser_set_code_editor",
    "browser_compile_and_run",
    "browser_submit_solution",
}


def _wrap_tool_handler(handler):
    @wraps(handler)
    async def wrapped(*args, **kwargs):
        try:
            from mcp.server.fastmcp import Context
            ctx = Context.current()
            session = getattr(ctx, "session", None) if ctx else None
            if session is not None:
                await _mcp_session_tracker.update_session(session)
        except Exception:
            pass
        return await handler(*args, **kwargs)
    return wrapped


def register_tools(mcp_server):
    """Register all tools with a FastMCP server instance using native decorators.

    This is called from transport.py during server initialization.
    Each tool is registered with its handler function, gaining automatic
    JSON schema generation from type hints and docstrings.
    """
    from ..config import get_enabled_tool_names, ACTIVE_TOOL_PROFILE

    enabled = get_enabled_tool_names()

    # ── Observation tools ─────────────────────────────────────────────────

    if "browser_get_page_state" in enabled:
        mcp_server.tool(
            name="browser_get_page_state",
            description=(
                "Get structured DOM data of the active tab: radio buttons, checkboxes, "
                "inputs, buttons, links, dropdowns, tables, images, and EXACT CSS selectors. "
                "USE BEFORE browser_execute_actions to discover valid selectors. "
                "DO NOT use for text extraction (use browser_extract_text) or screenshots."
            ),
        )(_wrap_tool_handler(handle_get_page_state))

    if "browser_take_screenshot" in enabled:
        mcp_server.tool(
            name="browser_take_screenshot",
            description=(
                "Take a screenshot of the active tab. Returns base64-encoded PNG. "
                "USE for visual verification after actions. "
                "DO NOT use for data extraction (use browser_extract_text instead)."
            ),
        )(_wrap_tool_handler(handle_take_screenshot))

    if "browser_extract_text" in enabled:
        mcp_server.tool(
            name="browser_extract_text",
            description=(
                "Extract or search text content from the page. Supports multiple scopes: "
                "visible_text (default), specific_element (needs selector), structured_dom, "
                "raw_html, element_info (detailed element inspection, needs selector). "
                "USE for reading page content, searching text, or inspecting elements. "
                "DO NOT use for getting interactive selectors (use browser_get_page_state)."
            ),
        )(_wrap_tool_handler(handle_extract_text))

    if "browser_wait_for_element" in enabled:
        mcp_server.tool(
            name="browser_wait_for_element",
            description=(
                "Wait for an element to appear on the page. Polls until found or timeout. "
                "USE when waiting for dynamic content after navigation or actions. "
                "DO NOT use for elements already visible."
            ),
        )(_wrap_tool_handler(handle_wait_for_element))

    # ── Navigation tools ──────────────────────────────────────────────────

    if "browser_navigate" in enabled:
        mcp_server.tool(
            name="browser_navigate",
            description=(
                "Unified browser navigation: goto URL, back, forward, reload, manage tabs. "
                "Actions: goto (needs url), back, forward, reload, new_tab (needs url), "
                "close_tab (needs tab_id), switch_tab (needs tab_id). "
                "DO NOT use for clicking links/buttons (use browser_execute_actions)."
            ),
        )(_wrap_tool_handler(handle_navigate))

    if "browser_list_tabs" in enabled:
        mcp_server.tool(
            name="browser_list_tabs",
            description=(
                "List all active browser tabs with IDs, URLs, titles, and active status. "
                "USE to discover tab IDs for tab management operations."
            ),
        )(_wrap_tool_handler(handle_list_tabs))

    # ── Interaction tools ─────────────────────────────────────────────────

    if "browser_execute_actions" in enabled:
        mcp_server.tool(
            name="browser_execute_actions",
            description=(
                "Execute browser actions in sequence: click, click_at, type, select, scroll, "
                "navigate, wait, check, uncheck, press_key, hover, clear, focus, submit, "
                "double_click, go_back, go_forward, reload. "
                "USE EXACT selectors from browser_get_page_state. Supports batching. "
                "DO NOT invent selectors. DO NOT use for URL navigation (use browser_navigate)."
            ),
        )(_wrap_tool_handler(handle_execute_actions))

    if "browser_execute_script" in enabled:
        mcp_server.tool(
            name="browser_execute_script",
            description=(
                "Evaluate a restricted read-only JS property path in page context. "
                "Examples: document.title, location.href. "
                "SECURITY: No eval, loops, fetch, storage, or side effects allowed. "
                "DO NOT use for page modifications (use browser_execute_actions)."
            ),
        )(_wrap_tool_handler(handle_execute_script))

    # ── Quiz tools ────────────────────────────────────────────────────────

    if "browser_navigate_quiz" in enabled:
        mcp_server.tool(
            name="browser_navigate_quiz",
            description=(
                "Navigate quiz: next, previous, or submit. "
                "USE for quiz page navigation only. "
                "DO NOT use for selecting answers (use browser_execute_actions with click). "
                "DO NOT use for non-quiz navigation (use browser_navigate)."
            ),
        )(_wrap_tool_handler(handle_navigate_quiz))

    # ── Coding tools ──────────────────────────────────────────────────────

    if "browser_get_coding_problem" in enabled:
        mcp_server.tool(
            name="browser_get_coding_problem",
            description=(
                "Extract coding problem: statement, I/O format, constraints, sample test cases, "
                "detected language. Returns structured JSON. "
                "USE before writing code to understand the problem."
            ),
        )(_wrap_tool_handler(handle_get_coding_problem))

    if "browser_set_code_editor" in enabled:
        mcp_server.tool(
            name="browser_set_code_editor",
            description=(
                "Insert complete source code into ACE/Monaco/CodeMirror editor via JS injection. "
                "USE after reading the problem. Replaces all editor content. "
                "The code arg must contain the complete solution."
            ),
        )(_wrap_tool_handler(handle_set_code_editor))

    if "browser_get_code_editor" in enabled:
        mcp_server.tool(
            name="browser_get_code_editor",
            description=(
                "Read current code from the active code editor. "
                "USE to verify code injection or read existing code."
            ),
        )(_wrap_tool_handler(handle_get_code_editor))

    if "browser_compile_and_run" in enabled:
        mcp_server.tool(
            name="browser_compile_and_run",
            description=(
                "Click 'Compile & Run' button, wait for results. "
                "Returns compilation status, test case pass/fail, errors. "
                "USE after injecting code. DO NOT use for final submission."
            ),
        )(_wrap_tool_handler(handle_compile_and_run))

    if "browser_get_test_results" in enabled:
        mcp_server.tool(
            name="browser_get_test_results",
            description=(
                "Parse test results from page: passed/failed count, error messages, "
                "expected vs actual output. Returns structured JSON. "
                "USE to re-check results after compilation."
            ),
        )(_wrap_tool_handler(handle_get_test_results))

    if "browser_submit_solution" in enabled:
        mcp_server.tool(
            name="browser_submit_solution",
            description=(
                "Submit the coding solution. Clicks 'Submit Code' and captures result. "
                "USE only after verifying code passes tests. This is irreversible."
            ),
        )(_wrap_tool_handler(handle_submit_solution))

    return mcp_server
