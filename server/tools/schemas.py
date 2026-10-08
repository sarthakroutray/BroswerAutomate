"""
schemas.py — Strict type definitions for tool inputs.

Pydantic models for FastMCP auto-schema generation.
Enums enforce closed argument sets to prevent hallucinated parameters.
"""

from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


# ── Browser action enum ──────────────────────────────────────────────────────

class BrowserActionType(str, Enum):
    CLICK = "click"
    CLICK_AT = "click_at"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    TYPE = "type"
    CLEAR = "clear"
    SELECT = "select"
    CHECK = "check"
    UNCHECK = "uncheck"
    PRESS_KEY = "press_key"
    SCROLL = "scroll"
    SCROLL_ELEMENT = "scroll_element"
    HOVER = "hover"
    FOCUS = "focus"
    SUBMIT = "submit"
    NAVIGATE = "navigate"
    WAIT = "wait"
    SET_CODE = "set_code"
    GET_CODE = "get_code"
    GO_BACK = "go_back"
    GO_FORWARD = "go_forward"
    RELOAD = "reload"
    UPLOAD = "upload"
    DIALOG_ACCEPT = "dialog_accept"
    DIALOG_DISMISS = "dialog_dismiss"

ALLOWED_ACTIONS = {e.value for e in BrowserActionType}


class FindMode(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    STARTS_WITH = "startsWith"
    ENDS_WITH = "endsWith"
    WORD = "word"
    REGEX = "regex"

ALLOWED_FIND_MODES = {e.value for e in FindMode}


class ActionStep(BaseModel):
    """A single browser action within a batch."""
    action: BrowserActionType = Field(
        ...,
        description="The browser action to perform."
    )
    selector: Optional[str] = Field(
        None,
        description=(
            "Element target: CSS selector, XPath (starts with /), shadow path "
            "('host >>> inner') or text selector ('text=\"Buy now\"', 'text*=\"partial\"'). "
            "Required for click, type, select, check, uncheck, hover, clear, focus, "
            "submit, double_click, right_click, drag, scroll_element, upload. Optional for wait (waits for the element) and "
            "press_key (focus target)."
        ),
    )
    selector_fallbacks: Optional[List[str]] = Field(
        None,
        description="Fallback selectors tried in order if primary selector fails.",
        max_length=8,
    )
    value: Optional[str] = Field(
        None,
        description=(
            "Action payload: text for type/select, key name for press_key "
            "(Enter, Tab, Escape, ...; chords like 'Ctrl+C', 'Ctrl+Shift+T' supported), "
            "URL for navigate, 'up'/'down' for scroll and scroll_element, "
            "milliseconds for wait, 'x,y' for click_at, 'x1,y1,x2,y2' or "
            "'dx,dy' offset for drag, full source code for set_code, local file "
            "path for upload."
        ),
    )
    x: Optional[float] = Field(None, description="Viewport X coordinate for click_at.")
    y: Optional[float] = Field(None, description="Viewport Y coordinate for click_at.")
    amount: Optional[int] = Field(None, description="Scroll amount in pixels.")
    idempotency_token: Optional[str] = Field(
        None,
        description="Optional deduplication token to prevent repeated execution."
    )
    expected_change: Optional[str] = Field(
        None,
        description="Expected DOM change type after action.",
        pattern="^(none|dom|url|dom_or_url)$",
    )
    trusted: Optional[bool] = Field(
        None,
        description=(
            "Use trusted CDP input (chrome.debugger) for click/click_at. "
            "Required for canvas and bot-walled targets that ignore synthetic "
            "events. Shows a 'Chrome is being controlled' banner while attached."
        ),
    )
    timeout_ms: Optional[int] = Field(
        None,
        description=(
            "Per-step timeout in milliseconds (500-30000). Overrides the "
            "default 10s action timeout for slow operations."
        ),
        ge=500,
        le=30000,
    )
    auto_wait: Optional[bool] = Field(
        None,
        description=(
            "Wait for the target to become interactable before acting "
            "(default true for click/type/select/check). Set false to act "
            "immediately without waiting."
        ),
    )
    frame: Optional[str] = Field(
        None,
        description=(
            "Same-origin iframe scope: CSS selector of the iframe to resolve "
            "'selector' inside. Cross-origin frames cannot be automated."
        ),
    )
    multiple: Optional[bool] = Field(
        None,
        description="For select: choose multiple options (multi-select elements).",
    )
