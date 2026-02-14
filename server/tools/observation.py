"""
observation.py — Page observation tool handlers.

Consolidated tools:
  browser_get_page_state  — structured DOM + page-type analysis
  browser_take_screenshot — capture screenshot as base64 PNG
  browser_extract_text    — text/html/element extraction (merged with inspect_element)
  browser_wait_for_element — poll for element appearance

ROUTING GUIDANCE (embedded in tool descriptions):
  - Use browser_get_page_state BEFORE browser_execute_actions to get selectors.
  - Use browser_extract_text to read page content or inspect specific elements.
  - Use browser_wait_for_element only when you need to wait for dynamic content.
  - Do NOT use browser_take_screenshot for data extraction — use browser_extract_text.
"""

import json
import logging
from typing import Optional

from ..browser_state import browser_manager, send_with_retries
from ..config import SCREENSHOT_TIMEOUT, MAX_TEXT_SUMMARY_CHARS
from ..errors import ToolResponse, success_response, error_response, ErrorCode

logger = logging.getLogger("browser-agent")

try:
    from mcp.types import TextContent, ImageContent
except ImportError:
    from dataclasses import dataclass
    @dataclass
    class TextContent:
        type: str
        text: str
    @dataclass
    class ImageContent:
        type: str
        data: str
        mimeType: str


# ── Helpers ───────────────────────────────────────────────────────────────────

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
            "command": "Use browser_run_task with goal: 'Complete the quiz'",
        })
    elif inputs and (checkboxes or selects or any("submit" in b.get("text", "").lower() for b in buttons)):
        page_type = "form"
        confidence = 0.8
        suggestions.append({
            "action": "fill_form",
            "reasoning": f"Detected {len(inputs)} input fields with submit",
            "command": "Use browser_execute_actions with fill_form actions",
        })
    elif any("login" in inp.get("name", "").lower() or "password" in inp.get("type", "") for inp in inputs):
        page_type = "login"
        confidence = 0.9
        suggestions.append({"action": "login", "reasoning": "Detected login form", "command": "Use browser_execute_actions"})
    elif tables:
        page_type = "data_table"
        confidence = 0.7
        suggestions.append({"action": "extract_data", "reasoning": f"{len(tables)} table(s)", "command": "Use browser_extract_text"})
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


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def handle_get_page_state(tab_id: Optional[str] = None) -> str:
    """Get structured DOM data of the active tab: radio buttons, checkboxes,
    inputs, buttons, links, dropdowns, tables, images, and EXACT CSS selectors.

    USE THIS TOOL:
    - Before browser_execute_actions to discover valid selectors
    - To understand page structure, form fields, and interactive elements
    - To detect page type (quiz, form, login, data table)

    DO NOT USE THIS TOOL:
    - For text content extraction (use browser_extract_text instead)
    - For screenshots (use browser_take_screenshot instead)
    - When you already have fresh selectors from a recent call

    Returns: Structured DOM with all interactive elements and their CSS selectors.
    """
    tab = browser_manager.resolve_tab(tab_id)
    try:
        resp = await send_with_retries({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
        dom = resp.get("dom_state", {})
        if dom:
            browser_manager.update_dom(tab.tab_id, dom)
    except (ConnectionError, TimeoutError):
        if tab.dom_state is None:
            return f"Error: Cannot get DOM for tab {tab.tab_id}"

    if tab.dom_state is None:
        return f"No DOM state for tab {tab.tab_id}."

    dom_display = format_dom_for_display(tab.dom_state)
    analysis = analyze_page_type(tab.dom_state)

    header = f"**Page Type:** {analysis['type'].upper()} (confidence: {analysis['confidence']:.0%})\n\n"
    if analysis["suggestions"]:
        header += "**Suggested actions:**\n"
        for s in analysis["suggestions"]:
            header += f"- {s['action']}: {s['reasoning']}\n"
        header += "\n"

    return header + dom_display


async def handle_take_screenshot(tab_id: Optional[str] = None) -> list:
    """Take a screenshot of the active tab. Returns base64-encoded PNG image.

    USE THIS TOOL:
    - For visual verification of page state after actions
    - When DOM data alone is insufficient to understand the layout
    - To see rendered content like charts, images, or complex layouts

    DO NOT USE THIS TOOL:
    - For data extraction (use browser_extract_text instead)
    - As a substitute for browser_get_page_state (DOM is more structured)

    Returns: Base64-encoded PNG screenshot image.
    """
    tab = browser_manager.resolve_tab(tab_id)
    resp = await send_with_retries(
        {"type": "TAKE_SCREENSHOT", "tab_id": tab.tab_id},
        timeout=SCREENSHOT_TIMEOUT,
    )
    screenshot = resp.get("screenshot")
    if screenshot:
        return [ImageContent(type="image", data=screenshot, mimeType="image/png")]
    return [TextContent(type="text", text="Failed to capture screenshot")]


async def handle_extract_text(
    scope: str = "visible_text",
    selector: Optional[str] = None,
    query: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> str:
    """Extract or search text content from the page.

    USE THIS TOOL:
    - To read visible text content from the page
    - To search for specific text using the 'query' parameter
    - To get raw HTML source (scope='raw_html')
    - To extract text from a specific element (scope='specific_element' + selector)
    - To get detailed element info: tag, classes, attributes, bounding box (scope='element_info' + selector)

    DO NOT USE THIS TOOL:
    - For getting interactive element selectors (use browser_get_page_state)
    - For visual content (use browser_take_screenshot)

    Scope options:
    - visible_text: All visible text on the page (default)
    - specific_element: Text from a specific CSS selector
    - structured_dom: Full structured DOM display
    - raw_html: Raw HTML source
    - element_info: Detailed info about a specific element (requires selector)

    Returns: Extracted text content or element details.
    """
    tab = browser_manager.resolve_tab(tab_id)
    sel = selector or ""
    qry = query or ""

    if scope == "element_info":
        if not sel:
            return "Error: 'selector' is required for element_info scope"
        resp = await send_with_retries(
            {"type": "GET_ELEMENT_INFO", "tab_id": tab.tab_id, "selector": sel},
            timeout=10.0,
        )
        if resp.get("error"):
            return f"Error: {resp['error']}"

        info = resp.get("info", {})
        parts = [f"## Element: <{info.get('tag','unknown')}>"]
        if info.get("id"):
            parts.append(f"ID: {info['id']}")
        if info.get("classes"):
            parts.append(f"Classes: {', '.join(info['classes'])}")
        if info.get("text"):
            parts.append(f"Text: {info['text'][:500]}")
        if info.get("attributes"):
            parts.append("Attributes:")
            for k, v in info["attributes"].items():
                parts.append(f"  {k}: {v}")
        if info.get("rect"):
            r = info["rect"]
            parts.append(f"Position: ({r.get('x',0):.0f}, {r.get('y',0):.0f}) Size: {r.get('width',0):.0f}x{r.get('height',0):.0f}")
        if info.get("styles"):
            parts.append("Styles:")
            for k, v in info["styles"].items():
                parts.append(f"  {k}: {v}")
        parts.append(f"Visible: {info.get('visible','?')} | Enabled: {info.get('enabled','?')}")
        return "\n".join(parts)

    if scope == "structured_dom":
        try:
            resp = await send_with_retries({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
            dom = resp.get("dom_state", {})
            if dom:
                browser_manager.update_dom(tab.tab_id, dom)
        except (ConnectionError, TimeoutError):
            pass
        if tab.dom_state:
            return format_dom_for_display(tab.dom_state)
        return "No DOM state available"

    if scope == "raw_html":
        resp = await send_with_retries({"type": "REQUEST_HTML", "tab_id": tab.tab_id}, timeout=10.0)
        html = resp.get("html", "")
        if html:
            if len(html) > 500000:
                html = html[:500000] + f"\n\n[TRUNCATED - {len(html)} chars total]"
            return html
        return f"Error: {resp.get('error', 'No HTML returned')}"

    # visible_text or specific_element
    resp = await send_with_retries(
        {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": qry, "selector": sel},
        timeout=15.0,
    )

    matches = resp.get("matches", [])
    if matches:
        output = f"Found {len(matches)} matches:\n\n"
        for i, m in enumerate(matches, 1):
            output += f"--- Match {i} ---\n{m}\n\n"
        return output

    text = resp.get("text", "")
    if text:
        return text[:100000]
    return resp.get("error", "No text found")


async def handle_wait_for_element(
    selector: str,
    timeout: int = 10000,
    require_visible: bool = True,
    tab_id: Optional[str] = None,
) -> str:
    """Wait for an element to appear on the page. Polls until found or timeout.

    USE THIS TOOL:
    - When waiting for dynamic content to load after navigation or actions
    - When an element might not be immediately available
    - Before interacting with elements that load asynchronously

    DO NOT USE THIS TOOL:
    - For elements already visible (use browser_execute_actions directly)
    - For general page observation (use browser_get_page_state)

    Args:
        selector: CSS selector of the element to wait for.
        timeout: Maximum wait time in milliseconds (default 10000, max 30000).
        require_visible: When true, waits for visibility/interactability too.

    Returns: Element found status with tag, visibility, and text preview.
    """
    tab = browser_manager.resolve_tab(tab_id)
    timeout_ms = min(timeout, 30000)
    resp = await send_with_retries(
        {
            "type": "WAIT_FOR_ELEMENT",
            "tab_id": tab.tab_id,
            "selector": selector,
            "timeout": timeout_ms,
            "require_visible": require_visible,
        },
        timeout=timeout_ms / 1000 + 5,
    )
    if resp.get("found"):
        return (
            f"Element found: {selector}\n"
            f"Tag: {resp.get('tag','?')}\n"
            f"Visible: {resp.get('visible', '?')} | Interactable: {resp.get('interactable', '?')}\n"
            f"Text: {resp.get('text','')[:200]}"
        )
    return f"Element not found within {timeout_ms}ms: {selector}"
