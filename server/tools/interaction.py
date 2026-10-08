"""
interaction.py — The `browser_act` and `browser_js` tools.

browser_act: batched, human-like page actions (click/type/select/press/scroll/
check/set_code/...) executed in order with retries and change verification.
browser_js: full-power escape hatch — arbitrary JavaScript in the page's own
MAIN world (page variables, frameworks, fetch, DOM — everything).
"""

import logging
from typing import Optional, List

from ..browser_state import browser_manager
from ..config import MAX_ACTION_STEPS, MAX_JS_RESULT_CHARS, JS_TIMEOUT
from ..errors import format_tool_result
from .schemas import ActionStep, BrowserActionType, ALLOWED_ACTIONS

logger = logging.getLogger("browser-agent")


ACTION_REQUIRING_SELECTOR = {
    "click", "type", "select", "check", "uncheck", "hover",
    "clear", "focus", "submit", "double_click", "right_click",
    "drag", "scroll_element", "upload",
}


def _parse_key_chord(key: str) -> dict:
    """Parse 'Ctrl+Shift+T' style chords into modifiers + key."""
    parts = [p.strip() for p in str(key or "").split("+") if p.strip()]
    modifiers = {"ctrl": False, "shift": False, "alt": False, "meta": False}
    main = ""
    for part in parts:
        low = part.lower()
        if low in ("ctrl", "control"):
            modifiers["ctrl"] = True
        elif low == "shift":
            modifiers["shift"] = True
        elif low in ("alt", "option"):
            modifiers["alt"] = True
        elif low in ("meta", "cmd", "command", "win", "super"):
            modifiers["meta"] = True
        else:
            main = part
    if not main and parts:
        main = parts[-1]
    return {"key": main or key, "modifiers": modifiers, "is_chord": any(modifiers.values())}


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

    if action == "drag":
        has_selector = bool(step.selector and str(step.selector).strip())
        has_value = isinstance(step.value, str) and "," in str(step.value)
        if not has_selector and not has_value:
            raise ValueError(f"Action[{idx}] drag requires selector (drag that element) or value='x1,y1,x2,y2'")

    if action == "navigate":
        value = str(step.value or "").strip()
        if not value:
            raise ValueError(f"Action[{idx}] navigate requires value URL")
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(f"Action[{idx}] navigate only allows http/https URLs")

    if action == "upload":
        if not str(step.value or "").strip():
            raise ValueError(f"Action[{idx}] upload requires a file path in 'value'")

    if action == "set_code" and not str(step.value or ""):
        raise ValueError(f"Action[{idx}] set_code requires the full source code in 'value'")

    if action == "scroll_element":
        direction = str(step.value or "down").strip().lower()
        if direction not in ("up", "down", "left", "right", "top", "bottom"):
            raise ValueError(f"Action[{idx}] scroll_element value must be up/down/left/right/top/bottom")

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
        raw = step.selector_fallbacks
        truncated = len(raw) > 8
        cleaned = [s.strip() for s in raw[:8] if isinstance(s, str) and s.strip()]
        if cleaned:
            d["selector_fallbacks"] = cleaned
        if truncated:
            d["_fallbacks_truncated"] = True
    if step.expected_change:
        d["expected_change"] = step.expected_change
    if step.trusted:
        d["trusted"] = True
    if step.timeout_ms is not None:
        d["timeout_ms"] = step.timeout_ms
    if step.auto_wait is not None:
        d["auto_wait"] = step.auto_wait
    if step.frame:
        d["frame"] = step.frame
    if step.multiple:
        d["multiple"] = True
    if action == "press_key" and step.value:
        chord = _parse_key_chord(step.value)
        if chord["is_chord"]:
            d["key"] = chord["key"]
            d["modifiers"] = chord["modifiers"]

    # Only explicitly provided tokens dedupe — never auto-generate them, so
    # intentionally repeated actions (double-clicking "+" etc.) always run.
    if step.idempotency_token:
        d["idempotency_token"] = step.idempotency_token

    return d


def _coerce_action_step(raw_step, idx: int) -> ActionStep:
    if isinstance(raw_step, ActionStep):
        return raw_step
    if isinstance(raw_step, dict):
        return ActionStep.model_validate(raw_step)
    raise ValueError(f"Action[{idx}] must be an object")


async def handle_act(
    actions: List[ActionStep],
    tab_id: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Perform a batch of human-like browser actions, executed in order.

    USE THIS TOOL:
    - To click buttons, links, radio buttons, checkboxes (incl. text selectors
      like {"action":"click","selector":"text=\"Buy now\""})
    - To right-click for context menus: {"action":"right_click","selector":"..."}
    - To drag: {"action":"drag","selector":"...","value":"x2,y2"} (target coords)
      or {"action":"drag","value":"x1,y1,x2,y2"} (absolute coords)
    - To type into inputs (pair 'clear' + 'type' to replace a value)
    - To select dropdown options, check/uncheck boxes
    - To press keys (Enter, Tab, Escape, ArrowDown, ...) and chords
      (Ctrl+C, Ctrl+Shift+T, Alt+F4 — parsed into modifiers automatically)
    - To scroll the page or one element: {"action":"scroll_element",
      "selector":".feed","value":"down","amount":500}
    - To upload files: {"action":"upload","selector":"input[type=file]",
      "value":"/path/to/file"}
    - To answer dialogs: dialog_accept / dialog_dismiss
    - To submit forms, go back/forward, reload, navigate to a URL
    - To wait — {"action":"wait","value":"2000"} pauses, or with a selector it
      waits until that element appears (value = timeout ms). For robust waits
      with visibility/interactability checks, prefer browser_wait.
    - To write code into ACE/Monaco/CodeMirror editors: {"action":"set_code",
      "value":"<full source>"}; {"action":"get_code"} reads it back
    - Trusted clicks for canvas/bot-walled targets:
      {"action":"click","selector":"...","trusted":true} (uses CDP input,
      shows a control banner)

    Selectors come from browser_see output. Each also accepts CSS, XPath,
    shadow paths ('host >>> inner') and text selectors ('text="exact"',
    'text*="partial"', 'text^="starts"', 'text$="ends"'). Use selector_fallbacks
    for alternates, 'frame' to scope into a same-origin iframe. Batches are
    atomic-ish: execution stops at a failed navigate. Max 20 steps per batch;
    longer batches are truncated with a warning in the response.

    DO NOT USE THIS TOOL:
    - To read the page (use browser_see)
    - For arbitrary JavaScript (use browser_js)

    Args:
        actions: List of action objects ({action, selector, value, x, y, amount, ...}).
        tab_id: Optional tab ID. Uses the active tab if not specified.
        timeout: Optional overall batch timeout in seconds (overrides auto).

    Returns: JSON envelope with per-action results (success/failure + error).
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
    warnings = []
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

    if len(actions) > MAX_ACTION_STEPS:
        warnings.append(
            f"Batch truncated: {len(actions)} steps provided, "
            f"only first {MAX_ACTION_STEPS} executed. Split into smaller batches."
        )
    truncated_fallbacks = sum(1 for s in validated_steps if s.pop("_fallbacks_truncated", False))
    if truncated_fallbacks:
        warnings.append(
            f"{truncated_fallbacks} step(s) had >8 selector_fallbacks; extras ignored."
        )

    # Batches with waits/typing can legitimately take a while.
    effective_timeout = timeout if timeout else max(30.0, 8.0 + 2.5 * len(validated_steps))
    resp = await browser_manager.send({
        "type": "EXECUTE_ACTIONS",
        "tab_id": tab.tab_id,
        "steps": validated_steps[:MAX_ACTION_STEPS],
    }, timeout=effective_timeout)

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
    if warnings:
        message += " | WARNINGS: " + " ".join(warnings)
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
            "warnings": warnings,
        },
    )


async def handle_js(
    script: str,
    tab_id: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Run arbitrary JavaScript in the page and return its result.

    The script runs in the page's own MAIN world — it can touch page variables
    and frameworks (React/Angular/Vue state, monaco/ace editors, jQuery, ...),
    the DOM, storage, and the network (fetch). This is the universal escape
    hatch: anything browser_act can't express, express here.

    USE THIS TOOL:
    - To read or manipulate data the DOM snapshot doesn't expose
    - To drive app internals (set framework state, call page functions)
    - To fetch/XHR, read localStorage/cookies, trigger downloads
    - To do multi-step DOM surgery in one call

    Write a JS expression (e.g. "document.title", "await fetch('/api').then(r=>r.json())")
    or a statement body ending with `return <value>` — the result (awaited if a
    Promise) is JSON-serialized and returned.

    DO NOT USE THIS TOOL:
    - For simple clicks/typing/form fills (browser_act is more reliable — it
      fires real input events frameworks listen for)
    - For reading pages (use browser_see)

    Args:
        script: JavaScript expression or async function body (max 100 KB).
        tab_id: Optional tab ID. Uses the active tab if not specified.
        timeout: Optional execution timeout in seconds (default 15).

    Returns: JSON envelope with the serialized result or the thrown error.
    """
    tab = browser_manager.resolve_tab(tab_id)
    if not script or not str(script).strip():
        return format_tool_result(
            status="error",
            code="MISSING_REQUIRED_FIELD",
            message="No script provided",
            data={"tab_id": tab.tab_id},
        )
    script = str(script)
    if len(script) > MAX_JS_RESULT_CHARS:
        return format_tool_result(
            status="error",
            code="INVALID_ARGUMENT",
            message=f"Script too long (max {MAX_JS_RESULT_CHARS} chars)",
            data={"tab_id": tab.tab_id},
        )

    resp = await browser_manager.send(
        {"type": "EXECUTE_JS", "tab_id": tab.tab_id, "script": script},
        timeout=timeout if timeout else JS_TIMEOUT,
    )
    if resp.get("error"):
        return format_tool_result(
            status="error",
            code="JS_EXECUTION_ERROR",
            message=resp["error"],
            data={"tab_id": tab.tab_id},
        )
    return format_tool_result(
        status="success",
        code="JS_RESULT_READY",
        message="Script executed",
        data={
            "tab_id": tab.tab_id,
            "result": str(resp.get("result", "undefined"))[:MAX_JS_RESULT_CHARS],
        },
    )
