"""
interaction.py — Browser interaction tool handlers.

Tools: browser_act, browser_fill_form, browser_eval_js
"""

import logging
import re
import hashlib
import json
from ..browser_state import browser_manager
from ..config import JS_EXPRESSION_STRICT_MODE
from .schemas import ActionStep, ALLOWED_ACTIONS, JS_DENY_PATTERNS

logger = logging.getLogger("browser-agent")

try:
    from mcp.types import TextContent
except ImportError:
    from dataclasses import dataclass
    @dataclass
    class TextContent:
        type: str
        text: str


ACTION_REQUIRING_SELECTOR = {
    "click", "type", "select", "check", "uncheck", "hover",
    "clear", "focus", "submit", "double_click",
}
JS_SAFE_PATH_RE = re.compile(
    r"^(?:window|document|location)"
    r"(?:\.[A-Za-z_$][\w$]*|\[['\"][A-Za-z0-9_$:\-]+['\"]\]|\[\d+\]){0,12}$"
)


def _dom_hash(dom: dict) -> str:
    if not isinstance(dom, dict):
        return ""
    material = {
        "url": dom.get("url", ""),
        "title": dom.get("title", ""),
        "inputs": len(dom.get("inputs", [])),
        "buttons": len(dom.get("buttons", [])),
        "links": len(dom.get("links", [])),
        "text": (dom.get("textSummary", "") or "")[:400],
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=True)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:16]


def _derive_step_token(step: ActionStep) -> str:
    material = f"{step.action}|{step.selector}|{step.value}|{step.x}|{step.y}|{step.amount}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _validate_action_step(step: ActionStep, idx: int) -> ActionStep:
    action = (step.action or "").strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Action[{idx}] '{action}' is not allowed")
    step.action = action

    if action in ACTION_REQUIRING_SELECTOR and not (step.selector and str(step.selector).strip()):
        raise ValueError(f"Action[{idx}] '{action}' requires selector")

    if action == "click_at":
        has_xy = step.x is not None and step.y is not None
        has_value_coords = isinstance(step.value, str) and "," in step.value
        if not has_xy and not has_value_coords:
            raise ValueError(f"Action[{idx}] click_at requires x/y or value='x,y'")

    if action == "navigate":
        value = str(step.value or "").strip()
        if not value:
            raise ValueError(f"Action[{idx}] navigate requires value URL")
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(f"Action[{idx}] navigate only allows http/https URLs")

    if step.selector_fallbacks and not isinstance(step.selector_fallbacks, list):
        raise ValueError(f"Action[{idx}] selector_fallbacks must be a list")
    if isinstance(step.selector_fallbacks, list):
        cleaned = []
        for item in step.selector_fallbacks[:8]:
            if isinstance(item, str) and item.strip():
                cleaned.append(item.strip())
        step.selector_fallbacks = cleaned or None

    if action in {"submit", "navigate", "click", "click_at"} and not step.idempotency_token:
        step.idempotency_token = _derive_step_token(step)

    return step


async def handle_act(arguments: dict) -> list:
    """browser_act — execute a sequence of browser actions."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    raw_actions = arguments.get("actions", [])
    actions = []
    for idx, ad in enumerate(raw_actions):
        try:
            step = ActionStep(**ad)
            actions.append(_validate_action_step(step, idx))
        except Exception as e:
            return [TextContent(type="text", text=f"Invalid action: {e}")]

    if not actions:
        return [TextContent(type="text", text="No actions provided")]

    resp = await browser_manager.send({
        "type": "EXECUTE_ACTIONS",
        "tab_id": tab.tab_id,
        "steps": [a.model_dump() for a in actions],
    })
    results = resp.get("results") or resp.get("result")
    if isinstance(results, list):
        failures = sum(1 for r in results if not r.get("success"))
        lines = [
            f"- {r.get('action','?')}: {'OK' if r.get('success') else 'FAIL: '+r.get('error','?')}"
            for r in results
        ]
        summary = f"Actions: {len(results) - failures}/{len(results)} succeeded"
        return [TextContent(type="text", text=summary + "\n" + "\n".join(lines))]
    if isinstance(results, dict) and results.get("error"):
        return [TextContent(type="text", text=f"Error: {results['error']}")]
    return [TextContent(type="text", text="Actions sent successfully")]


async def handle_fill_form(arguments: dict) -> list:
    """browser_fill_form — intelligent form filling."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    fields = arguments.get("fields", {})
    should_submit = arguments.get("submit", False)

    if not fields:
        return [TextContent(type="text", text="Error: No fields provided")]

    # Get DOM for field matching
    try:
        resp = await browser_manager.send({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
        dom = resp.get("dom_state", {})
        if dom:
            browser_manager.update_dom(tab.tab_id, dom)
    except Exception:
        dom = tab.dom_state or {}

    before_hash = _dom_hash(dom)
    before_url = (dom or {}).get("url", "")

    actions = []
    matched = []
    for fk, fv in fields.items():
        key = str(fk)
        value = "" if fv is None else str(fv)
        found = None
        for inp in dom.get("inputs", []) + dom.get("selects", []):
            if (
                (inp.get("name") and inp["name"].lower() == key.lower()) or
                (inp.get("label") and key.lower() in inp["label"].lower()) or
                (inp.get("placeholder") and key.lower() in inp["placeholder"].lower()) or
                (inp.get("id") and inp["id"].lower() == key.lower())
            ):
                found = inp["selector"]
                break
        if not found:
            found = key

        is_select = any(s["selector"] == found for s in dom.get("selects", []))
        if is_select:
            actions.append({"action": "select", "selector": found, "value": value})
        else:
            actions.append({"action": "clear", "selector": found})
            actions.append({"action": "type", "selector": found, "value": value})
        matched.append(key)

    if should_submit:
        for btn in dom.get("buttons", []):
            if btn.get("type") == "submit" or "submit" in btn.get("text", "").lower():
                actions.append({"action": "click", "selector": btn["selector"]})
                break

    if actions:
        await browser_manager.send({
            "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id, "steps": actions,
        })

    output = f"Filled {len(matched)} fields: {', '.join(matched)}"
    if should_submit:
        await browser_manager.send({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
        latest = browser_manager.tabs.get(tab.tab_id).dom_state or {}
        after_hash = _dom_hash(latest)
        after_url = latest.get("url", "")
        changed = before_hash != after_hash or before_url != after_url
        output += "\nForm submitted." if changed else "\nForm submit triggered; no immediate page transition detected."
    return [TextContent(type="text", text=output)]


async def handle_eval_js(arguments: dict) -> list:
    """browser_eval_js — evaluate JavaScript in page context."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    expression = arguments.get("expression", "")
    if not expression:
        return [TextContent(type="text", text="Error: No expression provided")]
    expression = str(expression).strip()
    if len(expression) > 500:
        return [TextContent(type="text", text="Error: Expression too long (max 500 chars)")]

    dangerous = list(JS_DENY_PATTERNS) + [
        "document.cookie", "localStorage", "sessionStorage", "indexedDB",
        "fetch(", "WebSocket(", "postMessage(", ";", "\n", "=>", "while(",
        "for(", "function ", "try{", "catch(",
    ]
    for d in dangerous:
        if d.lower() in expression.lower():
            return [TextContent(type="text", text=f"Error: Disallowed pattern: {d}")]
    if JS_EXPRESSION_STRICT_MODE and not JS_SAFE_PATH_RE.match(expression):
        return [TextContent(
            type="text",
            text="Error: Only read-only property-path expressions are allowed (e.g. document.title, location.href)",
        )]

    resp = await browser_manager.send(
        {"type": "EXECUTE_JS", "tab_id": tab.tab_id, "expression": expression},
        timeout=15.0,
    )
    if resp.get("error"):
        return [TextContent(type="text", text=f"JS Error: {resp['error']}")]
    return [TextContent(type="text", text=f"Result: {resp.get('result', 'undefined')}")]
