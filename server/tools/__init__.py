"""
tools/__init__.py — FastMCP Tool Registry.

Four universal tools cover everything the model needs to drive a browser:

  browser_see   — observe: DOM with exact selectors, text, HTML, elements,
                  code editors, quiz/coding-problem extraction, screenshots
  browser_act   — act: batched human-like actions (click/type/select/keys/
                  scroll/set_code/...) with retries and change verification
  browser_js    — escape hatch: arbitrary JavaScript in the page's MAIN world
  browser_tabs  — navigate and manage tabs

Everything else (solving quizzes, filling forms, buying things, completing
courses) is composition of these four by the MCP client's own model — no
in-server LLM, no sampling, no API keys.
"""

from .observation import handle_see
from .interaction import handle_act, handle_js
from .navigation import handle_tabs

# ── Dispatch table ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    "browser_see":  handle_see,
    "browser_act":  handle_act,
    "browser_js":   handle_js,
    "browser_tabs": handle_tabs,
}


def register_tools(mcp_server):
    """Register all tools with a FastMCP server instance.

    Called from transport.py during server initialization. Each tool is
    registered with its handler function, gaining automatic JSON schema
    generation from type hints and docstrings.
    """
    mcp_server.tool(
        name="browser_see",
        description=(
            "See and understand the current browser page. Returns structured DOM "
            "with EXACT selectors for every input/button/link/checkbox/radio/select, "
            "plus page analysis. Modes: page (default), context (one-shot page kind + "
            "blockers + quiz/coding data), text (searchable visible text), html, "
            "element (detailed info, needs selector), editor (read code from "
            "ACE/Monaco/CodeMirror), quiz (questions + option selectors), coding "
            "(problem statement + samples). Set screenshot=true to also get a PNG. "
            "USE BEFORE browser_act to discover valid selectors."
        ),
        structured_output=False,
    )(handle_see)

    mcp_server.tool(
        name="browser_act",
        description=(
            "Perform a batch of human-like browser actions in order: click, click_at, "
            "double_click, type, clear, select, check, uncheck, press_key, scroll, "
            "hover, focus, submit, navigate, wait, set_code, get_code, go_back, "
            "go_forward, reload. Supports CSS/XPath/shadow-path/text selectors "
            "(text=\"Buy now\"), fallback selectors, and idempotency tokens. "
            "set_code writes complete source into ACE/Monaco/CodeMirror editors. "
            "USE EXACT selectors from browser_see. DO NOT invent selectors."
        ),
    )(handle_act)

    mcp_server.tool(
        name="browser_js",
        description=(
            "Run arbitrary JavaScript in the page's MAIN world (full page privileges: "
            "framework internals, fetch, storage, DOM — anything) and return its "
            "(awaited) result. Expression or async function body with return. "
            "USE for anything browser_act can't express: reading app state, driving "
            "frameworks, network calls, multi-step DOM surgery. "
            "DO NOT use for simple clicks/typing (browser_act fires proper input "
            "events that frameworks require)."
        ),
    )(handle_js)

    mcp_server.tool(
        name="browser_tabs",
        description=(
            "Navigate and manage browser tabs. Actions: goto (needs url), back, "
            "forward, reload (hard_reload to bypass cache), new_tab (needs url), "
            "close_tab (needs tab_id), switch_tab (needs tab_id), list (all tabs "
            "with IDs/URLs/titles). USE for direct URL navigation and tab management. "
            "DO NOT use for clicking links (use browser_act)."
        ),
    )(handle_tabs)

    return mcp_server
