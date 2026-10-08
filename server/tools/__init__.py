"""
tools/__init__.py — FastMCP Tool Registry.

Six universal tools cover everything the model needs to drive a browser:

  browser_see   — observe: DOM with exact selectors, text, HTML, elements,
                  code editors, quiz/coding-problem extraction, text search,
                  screenshots (viewport/element/full-page)
  browser_act   — act: batched human-like actions (click/right-click/drag/
                  type/select/keys/chords/scroll/upload/dialogs/set_code/...)
                  with retries and change verification
  browser_js    — escape hatch: arbitrary JavaScript in the page's MAIN world
  browser_tabs  — navigate and manage tabs (with wait_until confirmation)
  browser_wait  — explicit waits: selector/text visibility, interactability,
                  DOM stability — with observable results
  browser_session — bridge health: connection status, cancel in-flight
                  batches, reconnect guidance

Everything else (solving quizzes, filling forms, buying things, completing
courses) is composition of these six by the MCP client's own model — no
in-server LLM, no sampling, no API keys.
"""

from .observation import handle_see
from .interaction import handle_act, handle_js
from .navigation import handle_tabs
from .wait import handle_wait
from .session import handle_session

# ── Dispatch table ────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, callable] = {
    "browser_see":  handle_see,
    "browser_act":  handle_act,
    "browser_js":   handle_js,
    "browser_tabs": handle_tabs,
    "browser_wait": handle_wait,
    "browser_session": handle_session,
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
            "plus page analysis. Modes: page (default, paginate with limit/offset), "
            "context (one-shot page kind + blockers + quiz/coding data), text "
            "(searchable visible text), html, element (detailed info, needs selector), "
            "editor (read code from ACE/Monaco/CodeMirror), quiz (questions + option "
            "selectors), coding (problem statement + samples), find (search by visible "
            "text: needs query, find_mode exact|contains|startsWith|endsWith|word|regex). "
            "Set screenshot=true to also get a PNG (clip_selector for element region, "
            "full_page for whole page). USE BEFORE browser_act to discover valid selectors."
        ),
        structured_output=False,
    )(handle_see)

    mcp_server.tool(
        name="browser_act",
        description=(
            "Perform a batch of human-like browser actions in order: click, click_at, "
            "double_click, right_click, drag, type, clear, select, check, uncheck, "
            "press_key (incl. chords like Ctrl+C), scroll, scroll_element, hover, "
            "focus, submit, navigate, wait, upload, dialog_accept, dialog_dismiss, "
            "set_code, get_code, go_back, go_forward, reload. Supports CSS/XPath/"
            "shadow-path/text selectors (text=\"Buy now\"), fallback selectors, "
            "same-origin frame scope, per-step timeouts, and idempotency tokens. "
            "trusted=true on click/click_at uses CDP input for canvas/bot-walled "
            "targets. set_code writes complete source into ACE/Monaco/CodeMirror editors. "
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
            "with IDs/URLs/titles). wait_until load|dom|networkidle controls "
            "confirmation strictness; unconfirmed transitions return partial:true. "
            "USE for direct URL navigation and tab management. "
            "DO NOT use for clicking links (use browser_act)."
        ),
    )(handle_tabs)

    mcp_server.tool(
        name="browser_wait",
        description=(
            "Wait for page state with observable results: selector or visible text "
            "to appear (require_visible, require_interactable options), or pure DOM "
            "stability (require_stable). Returns found/visible/interactable/elapsed "
            "detail. Prefer over browser_act wait steps for slow SPAs and AJAX content."
        ),
    )(handle_wait)

    mcp_server.tool(
        name="browser_session",
        description=(
            "Bridge health and control. Actions: status (extension connection, "
            "tracked tabs, cached DOM), stop (cancel the in-flight action batch on "
            "a tab), reconnect (re-sync guidance when the bridge is down). "
            "Orthogonal to browser_tabs, which manages pages."
        ),
    )(handle_session)

    return mcp_server
