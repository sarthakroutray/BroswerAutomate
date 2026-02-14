"""
schemas.py — Strict type definitions for tool inputs and shared types.

Pydantic models for FastMCP auto-schema generation.
Enums enforce closed argument sets to prevent hallucinated parameters.
"""

from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ── Browser action enum ──────────────────────────────────────────────────────

class BrowserActionType(str, Enum):
    CLICK = "click"
    CLICK_AT = "click_at"
    TYPE = "type"
    CLEAR = "clear"
    SELECT = "select"
    CHECK = "check"
    UNCHECK = "uncheck"
    PRESS_KEY = "press_key"
    SCROLL = "scroll"
    HOVER = "hover"
    NAVIGATE = "navigate"
    WAIT = "wait"
    DOUBLE_CLICK = "double_click"
    FOCUS = "focus"
    SUBMIT = "submit"
    GO_BACK = "go_back"
    GO_FORWARD = "go_forward"
    RELOAD = "reload"

ALLOWED_ACTIONS = {e.value for e in BrowserActionType}


# ── Navigation action enum ───────────────────────────────────────────────────

class NavigationAction(str, Enum):
    GOTO = "goto"
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"
    NEW_TAB = "new_tab"
    CLOSE_TAB = "close_tab"
    SWITCH_TAB = "switch_tab"


# ── Extraction scope enum ────────────────────────────────────────────────────

class ExtractionScope(str, Enum):
    VISIBLE_TEXT = "visible_text"
    SPECIFIC_ELEMENT = "specific_element"
    STRUCTURED_DOM = "structured_dom"
    RAW_HTML = "raw_html"
    ELEMENT_INFO = "element_info"


# ── Quiz navigation action ───────────────────────────────────────────────────

class QuizNavAction(str, Enum):
    NEXT = "next"
    PREVIOUS = "previous"
    SUBMIT = "submit"


# ── Pydantic models for tool inputs ──────────────────────────────────────────

class ActionStep(BaseModel):
    """A single browser action within a batch."""
    action: BrowserActionType = Field(
        ...,
        description="The browser action to perform."
    )
    selector: Optional[str] = Field(
        None,
        description="CSS selector targeting the element. Required for click, type, select, check, uncheck, hover, clear, focus, submit, double_click."
    )
    selector_fallbacks: Optional[List[str]] = Field(
        None,
        description="Fallback selectors tried in order if primary selector fails.",
        max_length=8,
    )
    value: Optional[str] = Field(
        None,
        description="Value for type/select/press_key/scroll/navigate/wait actions."
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


# ── Transport models (dataclass-style, no pydantic needed) ────────────────────

class DOMState(BaseModel):
    url: str = ""
    title: str = ""
    timestamp: Optional[int] = None
    viewport: Optional[Dict[str, Any]] = None
    inputs: List[Dict[str, Any]] = Field(default_factory=list)
    buttons: List[Dict[str, Any]] = Field(default_factory=list)
    links: List[Dict[str, Any]] = Field(default_factory=list)
    selects: List[Dict[str, Any]] = Field(default_factory=list)
    headings: List[Dict[str, Any]] = Field(default_factory=list)
    checkboxes: List[Dict[str, Any]] = Field(default_factory=list)
    radioButtons: List[Dict[str, Any]] = Field(default_factory=list)
    tables: List[Dict[str, Any]] = Field(default_factory=list)
    images: List[Dict[str, Any]] = Field(default_factory=list)
    textSummary: str = ""


class TabRegistration(BaseModel):
    tab_id: str
    url: str = ""
    title: str = ""


class TaskRequest(BaseModel):
    goal: str
    tab_id: Optional[str] = None
    max_steps: int = 30


# ── JS deny patterns ─────────────────────────────────────────────────────────

JS_DENY_PATTERNS = ["eval(", "Function(", "import(", "XMLHttpRequest"]
