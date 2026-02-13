"""
observation.py — Page observation tool handlers.

Tools: browser_observe, browser_screenshot, browser_extract_content,
browser_inspect_element, browser_wait_element
"""

import logging
from typing import Optional

from ..browser_state import browser_manager, send_with_retries
from ..config import SCREENSHOT_TIMEOUT, MAX_TEXT_SUMMARY_CHARS

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
            "command": "Use browser_fill_form",
        })
    elif any("login" in inp.get("name", "").lower() or "password" in inp.get("type", "") for inp in inputs):
        page_type = "login"
        confidence = 0.9
        suggestions.append({"action": "login", "reasoning": "Detected login form", "command": "Use browser_fill_form"})
    elif tables:
        page_type = "data_table"
        confidence = 0.7
        suggestions.append({"action": "extract_data", "reasoning": f"{len(tables)} table(s)", "command": "Use browser_extract_content"})
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

async def handle_observe(arguments: dict) -> list:
    """browser_observe — structured DOM + page analysis."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    try:
        resp = await send_with_retries({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
        dom = resp.get("dom_state", {})
        if dom:
            browser_manager.update_dom(tab.tab_id, dom)
    except (ConnectionError, TimeoutError):
        if tab.dom_state is None:
            return [TextContent(type="text", text=f"Error: Cannot get DOM for tab {tab.tab_id}")]

    if tab.dom_state is None:
        return [TextContent(type="text", text=f"No DOM state for tab {tab.tab_id}.")]

    dom_display = format_dom_for_display(tab.dom_state)
    analysis = analyze_page_type(tab.dom_state)

    header = f"**Page Type:** {analysis['type'].upper()} (confidence: {analysis['confidence']:.0%})\n\n"
    if analysis["suggestions"]:
        header += "**Suggested actions:**\n"
        for s in analysis["suggestions"]:
            header += f"- {s['action']}: {s['reasoning']}\n"
        header += "\n"

    return [TextContent(type="text", text=header + dom_display)]


async def handle_screenshot(arguments: dict) -> list:
    """browser_screenshot — capture active tab."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    resp = await send_with_retries(
        {"type": "TAKE_SCREENSHOT", "tab_id": tab.tab_id},
        timeout=SCREENSHOT_TIMEOUT,
    )
    screenshot = resp.get("screenshot")
    if screenshot:
        return [ImageContent(type="image", data=screenshot, mimeType="image/png")]
    return [TextContent(type="text", text="Failed to capture screenshot")]


async def handle_extract_content(arguments: dict) -> list:
    """browser_extract_content — flexible content extraction by scope."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    scope = arguments.get("scope", "visible_text")
    selector = arguments.get("selector", "")
    query = arguments.get("query", "")

    if scope == "structured_dom":
        try:
            resp = await send_with_retries({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
            dom = resp.get("dom_state", {})
            if dom:
                browser_manager.update_dom(tab.tab_id, dom)
        except (ConnectionError, TimeoutError):
            pass
        if tab.dom_state:
            return [TextContent(type="text", text=format_dom_for_display(tab.dom_state))]
        return [TextContent(type="text", text="No DOM state available")]

    if scope == "raw_html":
        resp = await send_with_retries({"type": "REQUEST_HTML", "tab_id": tab.tab_id}, timeout=10.0)
        html = resp.get("html", "")
        if html:
            if len(html) > 500000:
                html = html[:500000] + f"\n\n[TRUNCATED — {len(html)} chars total]"
            return [TextContent(type="text", text=html)]
        return [TextContent(type="text", text=f"Error: {resp.get('error', 'No HTML returned')}")]

    if scope == "specific_element":
        resp = await send_with_retries(
            {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": query or "", "selector": selector or ""},
            timeout=15.0,
        )
    else:  # visible_text
        resp = await send_with_retries(
            {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": query or "", "selector": selector or ""},
            timeout=15.0,
        )

    matches = resp.get("matches", [])
    if matches:
        output = f"Found {len(matches)} matches:\n\n"
        for i, m in enumerate(matches, 1):
            output += f"--- Match {i} ---\n{m}\n\n"
        return [TextContent(type="text", text=output)]

    text = resp.get("text", "")
    if text:
        return [TextContent(type="text", text=text[:100000])]
    return [TextContent(type="text", text=resp.get("error", "No text found"))]


async def handle_inspect_element(arguments: dict) -> list:
    """browser_inspect_element — detailed element info."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    resp = await send_with_retries(
        {"type": "GET_ELEMENT_INFO", "tab_id": tab.tab_id, "selector": arguments["selector"]},
        timeout=10.0,
    )
    if resp.get("error"):
        return [TextContent(type="text", text=f"Error: {resp['error']}")]

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
    return [TextContent(type="text", text="\n".join(parts))]


async def handle_wait_element(arguments: dict) -> list:
    """browser_wait_element — poll for element appearance."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    timeout_ms = min(arguments.get("timeout", 10000), 30000)
    require_visible = arguments.get("require_visible", True)
    resp = await send_with_retries(
        {
            "type": "WAIT_FOR_ELEMENT",
            "tab_id": tab.tab_id,
            "selector": arguments["selector"],
            "timeout": timeout_ms,
            "require_visible": require_visible,
        },
        timeout=timeout_ms / 1000 + 5,
    )
    if resp.get("found"):
        return [TextContent(
            type="text",
            text=(
                f"Element found: {arguments['selector']}\n"
                f"Tag: {resp.get('tag','?')}\n"
                f"Visible: {resp.get('visible', '?')} | Interactable: {resp.get('interactable', '?')}\n"
                f"Text: {resp.get('text','')[:200]}"
            ),
        )]
    return [TextContent(type="text", text=f"Element not found within {timeout_ms}ms: {arguments['selector']}")]
