"""
interaction.py — Browser interaction tool handlers.

Consolidated tools:
  browser_execute_actions — click/type/select/scroll/hover/form fill/all actions
  browser_execute_script  — sandboxed JS evaluation

Absorbs former browser_act, browser_fill_form, browser_eval_js, and quiz_answer
into two clear tools with distinct purposes.
"""

import logging
import re
import hashlib
import json
from typing import Optional, List

from ..browser_state import browser_manager
from ..config import JS_EXPRESSION_STRICT_MODE
from ..errors import format_tool_result
from ..policy import evaluate_action_batch
from .schemas import ActionStep, BrowserActionType, ALLOWED_ACTIONS, JS_DENY_PATTERNS

logger = logging.getLogger("browser-agent")


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
    from ..browser_state import dom_fingerprint
    return dom_fingerprint(dom)


def _derive_step_token(action: str, selector: Optional[str], value: Optional[str],
                        x: Optional[float], y: Optional[float], amount: Optional[int]) -> str:
    material = f"{action}|{selector}|{value}|{x}|{y}|{amount}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _validate_action_step(step: ActionStep, idx: int) -> dict:
    action = step.action.value if isinstance(step.action, BrowserActionType) else str(step.action).strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Action[{idx}] '{action}' is not allowed")

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

    # Build cleaned dict
    d = {"action": action}
    if step.selector is not None:
        d["selector"] = step.selector
    if step.value is not None:
        d["value"] = step.value
    if step.x is not None:
        d["x"] = step.x
    if step.y is not None:
        d["y"] = step.y
    if step.amount is not None:
        d["amount"] = step.amount
    if step.selector_fallbacks:
        cleaned = [s.strip() for s in step.selector_fallbacks[:8] if isinstance(s, str) and s.strip()]
        if cleaned:
            d["selector_fallbacks"] = cleaned
    if step.expected_change:
        d["expected_change"] = step.expected_change

    # Auto-generate idempotency token for risky actions
    if step.idempotency_token:
        d["idempotency_token"] = step.idempotency_token
    elif action in {"submit", "navigate", "click", "click_at"}:
        d["idempotency_token"] = _derive_step_token(
            action, step.selector, step.value, step.x, step.y, step.amount
        )

    return d


def _coerce_action_step(raw_step, idx: int) -> ActionStep:
    if isinstance(raw_step, ActionStep):
        return raw_step
    if isinstance(raw_step, dict):
        return ActionStep.model_validate(raw_step)
    raise ValueError(f"Action[{idx}] must be an object")


async def handle_execute_actions(
    actions: List[ActionStep],
    tab_id: Optional[str] = None,
    allow_unsafe: bool = False,
) -> str:
    """Execute browser actions in sequence: click, type, select, scroll, navigate, etc.
    Use EXACT selectors from browser_get_page_state. Supports batching multiple actions.

    USE THIS TOOL:
    - To click buttons, links, radio buttons, checkboxes
    - To type text into input fields
    - To select dropdown options
    - To scroll the page
    - To fill forms (use clear+type action pairs for each field)
    - To submit forms (click the submit button)
    - To press keyboard keys

    DO NOT USE THIS TOOL:
    - For URL navigation (use browser_navigate instead)
    - For reading page content (use browser_extract_text)
    - Do NOT invent selectors — get them from browser_get_page_state first

    Args:
        actions: List of action objects. Each has 'action' (required) and optional
                 selector, value, x, y, amount fields depending on the action type.
        tab_id: Optional tab ID. Uses active tab if not specified.

    Returns: Summary of action results with success/failure for each action.
    """
    tab = browser_manager.resolve_tab(tab_id)

    if not actions:
        return format_tool_result(
            status="error",
            code="MISSING_REQUIRED_FIELD",
            message="No actions provided",
            data={"tab_id": tab.tab_id},
        )

    validated_steps = []
    for idx, raw_step in enumerate(actions):
        try:
            step = _coerce_action_step(raw_step, idx)
            validated_steps.append(_validate_action_step(step, idx))
        except Exception as e:
            return format_tool_result(
                status="error",
                code="INVALID_ARGUMENT",
                message=f"Invalid action at index {idx}: {e}",
                data={"tab_id": tab.tab_id, "index": idx},
            )

    policy_decision = evaluate_action_batch(
        actions=validated_steps,
        current_url=tab.url or ((tab.dom_state or {}).get("url", "")),
        allow_unsafe=allow_unsafe,
    )
    if not policy_decision.allowed:
        return format_tool_result(
            status="error",
            code=policy_decision.code,
            message=policy_decision.message,
            data={"tab_id": tab.tab_id, **policy_decision.data},
        )

    resp = await browser_manager.send({
        "type": "EXECUTE_ACTIONS",
        "tab_id": tab.tab_id,
        "steps": validated_steps,
    })

    # Unwrap nested ACTION_COMPLETE payload: extension sends {result: {results: [...]}}
    results = resp.get("result", resp)
    if isinstance(results, dict):
        if results.get("error"):
            return format_tool_result(
                status="error",
                code="ACTION_EXECUTION_FAILED",
                message=results["error"],
                data={"tab_id": tab.tab_id, "results": []},
            )
        results = results.get("results", results)
    if not isinstance(results, list):
        results = resp.get("results", [])

    if not isinstance(results, list):
        results = []

    failures = sum(1 for r in results if isinstance(r, dict) and not r.get("success"))
    succeeded = len(results) - failures
    message = f"Executed {len(results)} action(s): {succeeded} succeeded, {failures} failed"
    status = "success" if failures == 0 else "error"
    code = "ACTIONS_EXECUTED" if failures == 0 else "ACTION_EXECUTION_FAILED"
    return format_tool_result(
        status=status,
        code=code,
        message=message,
        data={
            "tab_id": tab.tab_id,
            "results": results,
            "summary": {
                "total": len(results),
                "succeeded": succeeded,
                "failed": failures,
            },
        },
    )


async def handle_execute_script(
    expression: str,
    tab_id: Optional[str] = None,
) -> str:
    """Evaluate a restricted read-only JavaScript property path in page context.

    USE THIS TOOL:
    - To read page properties like document.title, location.href
    - To check element properties via safe property paths
    - For read-only DOM inspection not available through other tools

    DO NOT USE THIS TOOL:
    - For modifying the page (use browser_execute_actions instead)
    - For network requests, storage access, or code execution
    - For anything that browser_extract_text can already do

    SECURITY: Only read-only property-path expressions are allowed.
    No eval, Function, import, fetch, localStorage, cookies, loops, or semicolons.

    Args:
        expression: Read-only JS path (e.g. document.title, location.href). Max 500 chars.

    Returns: The evaluated result value.
    """
    tab = browser_manager.resolve_tab(tab_id)
    if not expression:
        return format_tool_result(
            status="error",
            code="MISSING_REQUIRED_FIELD",
            message="No expression provided",
            data={"tab_id": tab.tab_id},
        )
    expression = str(expression).strip()
    if len(expression) > 500:
        return format_tool_result(
            status="error",
            code="INVALID_ARGUMENT",
            message="Expression too long (max 500 chars)",
            data={"tab_id": tab.tab_id},
        )

    dangerous = list(JS_DENY_PATTERNS) + [
        "document.cookie", "localStorage", "sessionStorage", "indexedDB",
        "fetch(", "WebSocket(", "postMessage(", ";", "\n", "=>", "while(",
        "for(", "function ", "try{", "catch(",
    ]
    for d in dangerous:
        if d.lower() in expression.lower():
            return format_tool_result(
                status="error",
                code="UNSAFE_OPERATION",
                message=f"Disallowed pattern: {d}",
                data={"tab_id": tab.tab_id, "expression": expression},
            )
    if JS_EXPRESSION_STRICT_MODE and not JS_SAFE_PATH_RE.match(expression):
        return format_tool_result(
            status="error",
            code="INVALID_ARGUMENT",
            message="Only read-only property-path expressions are allowed (e.g. document.title, location.href)",
            data={"tab_id": tab.tab_id, "expression": expression},
        )

    resp = await browser_manager.send(
        {"type": "EXECUTE_JS", "tab_id": tab.tab_id, "expression": expression},
        timeout=15.0,
    )
    if resp.get("error"):
        return format_tool_result(
            status="error",
            code="JS_EXECUTION_ERROR",
            message=resp["error"],
            data={"tab_id": tab.tab_id, "expression": expression},
        )
    return format_tool_result(
        status="success",
        code="JS_RESULT_READY",
        message=f"Read expression '{expression}'",
        data={
            "tab_id": tab.tab_id,
            "expression": expression,
            "result": resp.get("result", "undefined"),
        },
    )
