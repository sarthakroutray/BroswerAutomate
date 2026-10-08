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
    TYPE = "type"
    CLEAR = "clear"
    SELECT = "select"
    CHECK = "check"
    UNCHECK = "uncheck"
    PRESS_KEY = "press_key"
    SCROLL = "scroll"
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

ALLOWED_ACTIONS = {e.value for e in BrowserActionType}


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
            "submit, double_click. Optional for wait (waits for the element) and "
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
            "(Enter, Tab, Escape, ...), URL for navigate, 'up'/'down' for scroll, "
            "milliseconds for wait, 'x,y' for click_at, full source code for set_code."
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
