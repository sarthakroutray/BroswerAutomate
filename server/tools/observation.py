"""
observation.py — The `browser_see` tool: everything the model needs to
understand a page.

Modes:
  page     — structured DOM snapshot with exact selectors + page-type analysis
  context  — one-shot page context: kind (quiz/coding/form), blockers
             (login/captcha/2FA), quiz structure, coding problem, DOM
  text     — visible text, optionally searched (query) or scoped (selector)
  html     — raw page HTML
  element  — detailed info about one element (requires selector)
  editor   — current code-editor content (ACE/Monaco/CodeMirror/textarea)
  quiz     — structured quiz extraction (questions, options, navigation)
  coding   — structured coding-problem extraction (statement, I/O, samples)

Any mode can additionally return a screenshot (screenshot=True).
"""

import json
import logging
from typing import Optional, Union, List, Any

from ..browser_state import browser_manager, send_with_retries
from ..config import SCREENSHOT_TIMEOUT, MAX_TEXT_SUMMARY_CHARS
from ..errors import format_tool_result

logger = logging.getLogger("browser-agent")

SEE_MODES = {"page", "context", "text", "html", "element", "editor", "quiz", "coding"}

MODE_TO_WS_TYPE = {
    "page": "REQUEST_DOM",
    "context": "EXTRACT_PAGE_CONTEXT",
    "text": "EXTRACT_TEXT",
    "html": "REQUEST_HTML",
    "element": "GET_ELEMENT_INFO",
    "editor": "GET_CODE",
    "quiz": "EXTRACT_QUIZ_STRUCTURE",
    "coding": "EXTRACT_CODING_PROBLEM",
}


def analyze_page_type(dom_state: dict) -> dict:
    radios = dom_state.get("radioButtons", [])
    checkboxes = dom_state.get("checkboxes", [])
    inputs = dom_state.get("inputs", [])
    buttons = dom_state.get("buttons", [])
    selects = dom_state.get("selects", [])
    links = dom_state.get("links", [])
    tables = dom_state.get("tables", [])
    title = dom_state.get("title", "").lower()

    suggestions = []
    page_type = "generic"
    confidence = 0.5

    if radios or any(kw in title for kw in ("quiz", "test", "exam", "assessment")):
        page_type = "quiz"
        confidence = 0.9 if radios else 0.7
        radio_groups = len(set(r.get("name", "") for r in radios if r.get("name")))
        suggestions.append({
            "action": "complete_quiz",
            "reasoning": f"Detected {len(radios)} radio buttons in {radio_groups} groups",
            "command": "Answer via browser_act clicks on the listed option selectors",
        })
    elif inputs and (checkboxes or selects or any("submit" in b.get("text", "").lower() for b in buttons)):
        page_type = "form"
        confidence = 0.8
        suggestions.append({
            "action": "fill_form",
            "reasoning": f"Detected {len(inputs)} input fields with submit",
            "command": "Use browser_act with clear+type action pairs for each field",
        })
    elif any("login" in inp.get("name", "").lower() or "password" in inp.get("type", "") for inp in inputs):
        page_type = "login"
        confidence = 0.9
        suggestions.append({"action": "login", "reasoning": "Detected login form", "command": "Use browser_act"})
    elif tables:
        page_type = "data_table"
        confidence = 0.7
        suggestions.append({"action": "extract_data", "reasoning": f"{len(tables)} table(s)", "command": "Use browser_see with mode='text' or mode='html'"})
    elif len(links) > 20:
        page_type = "navigation"
        confidence = 0.6

    return {
        "type": page_type, "confidence": confidence, "suggestions": suggestions,
        "context": {
            "has_quiz": len(radios) > 0, "has_form": len(inputs) > 0,
            "has_checkboxes": len(checkboxes) > 0,
            "radio_groups": len(set(r.get("name", "") for r in radios if r.get("name"))),
            "input_fields": len(inputs), "buttons": len(buttons),
            "links": len(links), "tables": len(tables),
        },
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
            groups.setdefault(rb.get("name", "unknown"), []).append(rb)
        for group_name, items in groups.items():
            parts.append(f"  Group: {group_name}")
            for rb in items:
                label = rb.get("label") or rb.get("value") or rb.get("context") or "unlabeled"
                state = "SELECTED" if rb.get("checked") else "  "
                parts.append(f'    ({state}) {label}\n      selector: {rb.get("selector", "N/A")}')

    checkboxes = dom_state.get("checkboxes", [])
    if checkboxes:
        parts.append(f"\n## Checkboxes ({len(checkboxes)} found)")
        for cb in checkboxes:
            label = cb.get("label") or cb.get("context") or "unlabeled"
            state = "CHECKED" if cb.get("checked") else "unchecked"
            parts.append(f'  - [{state}] {label}\n    selector: {cb.get("selector", "N/A")}')

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
            if inp.get("disabled"):
                flags.append("DISABLED")
            if inp.get("readOnly"):
                flags.append("READONLY")
            if inp.get("required"):
                flags.append("REQUIRED")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            parts.append(
                f'  - {label} (type={inp.get("type","text")}){flag_str}\n'
                f'    selector: {inp.get("selector","N/A")}\n'
                f'    value: "{inp.get("value","")}"'
            )

    selects = dom_state.get("selects", [])
    if selects:
        parts.append(f"\n## Dropdowns ({len(selects)} found)")
        for sel in selects[:30]:
            label = sel.get("label") or sel.get("name") or "unlabeled"
            opts_text = ", ".join([f'"{o.get("text","")}"' for o in sel.get("options", [])[:8]])
            parts.append(f'  - {label}\n    selector: {sel.get("selector","N/A")}\n    current: {sel.get("currentValue","")}\n    options: [{opts_text}]')

    buttons = dom_state.get("buttons", [])
    if buttons:
        parts.append(f"\n## Buttons ({len(buttons)} found)")
        for btn in buttons[:50]:
            disabled = " [DISABLED]" if btn.get("disabled") else ""
            parts.append(f'  - "{btn.get("text","")}{disabled}"\n    selector: {btn.get("selector","N/A")}')

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
            for row in table.get("rows", [])[:10]:
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


# ── Per-mode handlers ─────────────────────────────────────────────────────────

async def _see_page(tab) -> dict:
    stale = False
    bridge_error = None
    fresh = False
    try:
        resp = await send_with_retries({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
        dom = resp.get("dom_state") or {}
        bridge_error = resp.get("error")
        if dom:
            browser_manager.update_dom(tab.tab_id, dom)
            fresh = True
    except (ConnectionError, TimeoutError) as error:
        bridge_error = str(error)

    if fresh:
        return {
            "status": "success", "code": "PAGE_STATE_READY",
            "message": f"Retrieved page state for tab {tab.tab_id}",
            "data": {
                "tab_id": tab.tab_id,
                "mode": "page",
                "stale": False,
                "analysis": analyze_page_type(tab.dom_state),
                "dom_state": tab.dom_state,
                "display": format_dom_for_display(tab.dom_state),
            },
        }

    if tab.dom_state:
        message = f"Retrieved page state for tab {tab.tab_id} using cached DOM"
        if bridge_error:
            message += f" after bridge failure: {bridge_error}"
        return {
            "status": "success", "code": "PAGE_STATE_READY", "message": message,
            "data": {
                "tab_id": tab.tab_id,
                "mode": "page",
                "stale": True,
                "analysis": analyze_page_type(tab.dom_state),
                "dom_state": tab.dom_state,
                "display": format_dom_for_display(tab.dom_state),
            },
        }

    return {
        "status": "error", "code": "NO_DOM_STATE",
        "message": bridge_error or f"Cannot get DOM for tab {tab.tab_id}",
        "data": {"tab_id": tab.tab_id, "mode": "page"},
    }


async def _see_text(tab, query: str, selector: str) -> dict:
    resp = await send_with_retries(
        {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": query, "selector": selector},
        timeout=15.0,
    )
    matches = resp.get("matches", [])
    if matches:
        return {
            "status": "success", "code": "TEXT_MATCHES_FOUND",
            "message": f"Found {len(matches)} matches",
            "data": {"mode": "text", "tab_id": tab.tab_id, "selector": selector, "query": query, "matches": matches},
        }
    text = resp.get("text", "")
    if text:
        return {
            "status": "success", "code": "TEXT_READY",
            "message": f"Extracted {len(text)} chars of text",
            "data": {"mode": "text", "tab_id": tab.tab_id, "selector": selector, "query": query, "text": text[:100000]},
        }
    return {
        "status": "error", "code": "TEXT_NOT_FOUND",
        "message": resp.get("error", "No text found"),
        "data": {"mode": "text", "tab_id": tab.tab_id, "selector": selector, "query": query},
    }


async def _see_html(tab) -> dict:
    resp = await send_with_retries({"type": "REQUEST_HTML", "tab_id": tab.tab_id}, timeout=10.0)
    html = resp.get("html", "")
    if html:
        if len(html) > 500000:
            html = html[:500000] + f"\n\n[TRUNCATED - {len(html)} chars total]"
        return {
            "status": "success", "code": "HTML_READY",
            "message": f"Retrieved HTML for tab {tab.tab_id}",
            "data": {"mode": "html", "tab_id": tab.tab_id, "html": html},
        }
    return {
        "status": "error", "code": "HTML_EXTRACTION_FAILED",
        "message": resp.get("error", "No HTML returned"),
        "data": {"mode": "html", "tab_id": tab.tab_id},
    }


async def _see_element(tab, selector: str) -> dict:
    resp = await send_with_retries(
        {"type": "GET_ELEMENT_INFO", "tab_id": tab.tab_id, "selector": selector},
        timeout=10.0,
    )
    if resp.get("error"):
        return {
            "status": "error", "code": "ELEMENT_INFO_FAILED",
            "message": resp["error"],
            "data": {"mode": "element", "tab_id": tab.tab_id, "selector": selector},
        }
    return {
        "status": "success", "code": "ELEMENT_INFO_READY",
        "message": f"Retrieved element info for selector '{selector}'",
        "data": {"mode": "element", "tab_id": tab.tab_id, "selector": selector, "info": resp.get("info", {})},
    }


async def _see_ws_payload(tab, mode: str) -> dict:
    """Modes served by a single request/response WS round trip."""
    ws_type = MODE_TO_WS_TYPE[mode]
    resp = await send_with_retries({"type": ws_type, "tab_id": tab.tab_id}, timeout=15.0)
    failure = resp.get("error")
    if not failure and resp.get("success") is False:
        # e.g. GET_CODE reports failures as {success: false, message: ...}
        failure = resp.get("message") or f"{mode} extraction failed"
    if failure:
        return {
            "status": "error", "code": f"{mode.upper()}_EXTRACTION_FAILED",
            "message": failure,
            "data": {"mode": mode, "tab_id": tab.tab_id},
        }
    data = {"mode": mode, "tab_id": tab.tab_id}
    for key, value in resp.items():
        if key not in ("type", "request_id", "tab_id"):
            data[key] = value
    return {
        "status": "success", "code": f"{mode.upper()}_READY",
        "message": f"Retrieved {mode} view for tab {tab.tab_id}",
        "data": data,
    }


async def _capture_screenshot(tab) -> Union[str, None, Any]:
    """Return a FastMCP Image for the tab screenshot, or None on failure."""
    try:
        import base64

        resp = await send_with_retries(
            {"type": "TAKE_SCREENSHOT", "tab_id": tab.tab_id},
            timeout=SCREENSHOT_TIMEOUT,
        )
        screenshot = resp.get("screenshot")
        if not screenshot:
            return None
        from mcp.server.fastmcp import Image
        return Image(data=base64.b64decode(screenshot), format="png")
    except Exception as e:
        logger.warning(f"Screenshot capture failed: {e}")
        return None


# ── Tool handler ──────────────────────────────────────────────────────────────

async def handle_see(
    mode: str = "page",
    selector: Optional[str] = None,
    query: Optional[str] = None,
    screenshot: bool = False,
    tab_id: Optional[str] = None,
) -> Union[str, List[Any]]:
    """See and understand the current page.

    USE THIS TOOL:
    - Before browser_act, to discover exact selectors for the elements to interact with
    - To read page content (text, HTML, tables, quiz questions, problem statements)
    - To inspect a single element (mode='element' + selector)
    - To read the code editor content on coding platforms (mode='editor')
    - To understand what is on screen visually (screenshot=True)

    Modes:
    - page (default): structured DOM snapshot — inputs, buttons, links, selects,
      checkboxes, radios, tables, images, each with an exact selector + page analysis
    - context: one-shot context — page kind (quiz/coding/form), blockers
      (login/captcha/2FA), quiz structure, coding problem, and DOM
    - text: visible page text; use 'query' to search lines, 'selector' to scope
    - html: raw page HTML
    - element: detailed info about one element (requires selector)
    - editor: read current code from ACE/Monaco/CodeMirror/textarea editors
    - quiz: structured quiz extraction — questions, options with selectors,
      current answers, next/prev/submit buttons
    - coding: structured coding problem — statement, I/O format, constraints,
      samples, detected language, editor type, compile/submit buttons

    Set screenshot=True to also receive a PNG screenshot of the tab.

    DO NOT USE THIS TOOL:
    - To interact with the page (use browser_act)
    - To run arbitrary JavaScript (use browser_js)

    Returns: JSON envelope with the requested view. The selectors in the output
    are exactly what browser_act accepts.
    """
    mode = (mode or "page").strip().lower()
    if mode not in SEE_MODES:
        return format_tool_result(
            status="error", code="INVALID_ENUM",
            message=f"Unknown mode '{mode}'. Valid modes: {sorted(SEE_MODES)}",
            data={"mode": mode},
        )

    tab = browser_manager.resolve_tab(tab_id)
    sel = (selector or "").strip()
    qry = (query or "").strip()

    if mode == "element" and not sel:
        return format_tool_result(
            status="error", code="MISSING_REQUIRED_FIELD",
            message="'selector' is required for mode='element'",
            data={"mode": mode, "tab_id": tab.tab_id},
        )

    if mode == "page":
        outcome = await _see_page(tab)
    elif mode == "text":
        outcome = await _see_text(tab, qry, sel)
    elif mode == "html":
        outcome = await _see_html(tab)
    elif mode == "element":
        outcome = await _see_element(tab, sel)
    else:
        outcome = await _see_ws_payload(tab, mode)

    payload = format_tool_result(**outcome)
    if not screenshot:
        return payload

    image = await _capture_screenshot(tab)
    if image is None:
        return payload
    return [payload, image]
