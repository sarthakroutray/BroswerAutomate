"""
wait.py — The `browser_wait` tool: explicit, observable waits.

Unlike browser_act's `wait` step (a blind pause or bare existence check),
browser_wait returns visibility / interactability / stability detail so the
model can decide what to do next instead of guessing.
"""

from typing import Optional

from ..browser_state import browser_manager, send_with_retries
from ..errors import format_tool_result


async def handle_wait(
    selector: Optional[str] = None,
    text: Optional[str] = None,
    timeout_ms: int = 10000,
    require_visible: bool = True,
    require_interactable: bool = False,
    require_stable: bool = False,
    tab_id: Optional[str] = None,
) -> str:
    """Wait for an element or page state, with observable results.

    USE THIS TOOL:
    - To wait for a selector to appear: {"selector": "#results"}
    - To wait for visible text: {"text": "Order confirmed"}
    - To wait until clickable: {"selector": "#pay", "require_interactable": true}
    - To wait for the DOM to settle after an action:
      {"require_stable": true, "timeout_ms": 5000}
    - Before interacting with content loaded by slow SPAs / AJAX

    Prefer this over browser_act's wait step when you need to know WHY
    a wait failed (hidden vs. missing vs. non-interactive).

    Args:
        selector: CSS/XPath/shadow/text selector to wait for.
        text: Visible text to wait for (alternative to selector).
        timeout_ms: Max wait in ms (1000-60000, default 10000).
        require_visible: Element must be visible, not just in the DOM.
        require_interactable: Element must be clickable/typeable.
        require_stable: Also wait for DOM mutations to settle.
        tab_id: Optional tab ID. Uses the active tab if not specified.

    Returns: JSON envelope with found/visible/interactable/elapsed detail.
    """
    tab = browser_manager.resolve_tab(tab_id)

    timeout_ms = max(1000, min(int(timeout_ms or 10000), 60000))

    if not selector and not text and not require_stable:
        return format_tool_result(
            status="error", code="MISSING_REQUIRED_FIELD",
            message="Provide 'selector', 'text', or 'require_stable: true'",
            data={"tab_id": tab.tab_id},
        )

    if require_stable and not selector and not text:
        resp = await send_with_retries(
            {"type": "WAIT_FOR_STABLE", "tab_id": tab.tab_id, "timeout": timeout_ms},
            timeout=timeout_ms / 1000.0 + 10.0,
        )
        if resp.get("error") and not resp.get("stable"):
            return format_tool_result(
                status="error", code="WAIT_STABILITY_TIMEOUT",
                message=resp.get("error", "DOM did not settle in time"),
                data={"tab_id": tab.tab_id, "stable": False,
                      "elapsed": resp.get("elapsed", timeout_ms)},
            )
        return format_tool_result(
            status="success", code="WAIT_STABLE",
            message=f"DOM settled after {resp.get('elapsed', 0)}ms",
            data={"tab_id": tab.tab_id, "stable": True,
                  "elapsed": resp.get("elapsed", 0)},
        )

    payload: dict = {
        "type": "WAIT_FOR_ELEMENT",
        "tab_id": tab.tab_id,
        "timeout": timeout_ms,
        "require_visible": require_visible,
        "require_interactable": require_interactable,
    }
    if selector:
        payload["selector"] = selector
    if text:
        payload["text"] = text

    resp = await send_with_retries(payload, timeout=timeout_ms / 1000.0 + 10.0)

    if resp.get("error") and not resp.get("found"):
        return format_tool_result(
            status="error", code="WAIT_TIMEOUT",
            message=resp.get("error", f"Timed out after {timeout_ms}ms"),
            data={"tab_id": tab.tab_id, "selector": selector, "text": text,
                  "found": False, "elapsed": resp.get("elapsed", timeout_ms)},
        )
    found = bool(resp.get("found"))
    status = "success" if found else "error"
    return format_tool_result(
        status=status,
        code="WAIT_SATISFIED" if found else "WAIT_TIMEOUT",
        message=(
            f"Wait satisfied after {resp.get('elapsed', 0)}ms"
            if found else f"Element not found after {resp.get('elapsed', timeout_ms)}ms"
        ),
        data={
            "tab_id": tab.tab_id,
            "selector": selector,
            "text": text,
            "found": found,
            "tag": resp.get("tag"),
            "text_sample": (resp.get("text") or "")[:200],
            "visible": resp.get("visible"),
            "interactable": resp.get("interactable"),
            "elapsed": resp.get("elapsed", 0),
        },
    )
