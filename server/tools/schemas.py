"""
schemas.py — Strict type definitions for tool inputs and shared types.

Uses stdlib dataclasses + enums (no pydantic dependency).
Tool handlers receive raw dicts; these classes are for documentation
and optional validation.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


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


# ── Action step (used by handle_act) ─────────────────────────────────────────

@dataclass
class ActionStep:
    action: str
    selector: Optional[str] = None
    value: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    amount: Optional[int] = None
    selector_fallbacks: Optional[List[str]] = None
    idempotency_token: Optional[str] = None
    expected_change: Optional[str] = None

    def model_dump(self) -> dict:
        """Compat shim — produces a dict like pydantic's model_dump."""
        d = {"action": self.action}
        if self.selector is not None:
            d["selector"] = self.selector
        if self.value is not None:
            d["value"] = self.value
        if self.x is not None:
            d["x"] = self.x
        if self.y is not None:
            d["y"] = self.y
        if self.amount is not None:
            d["amount"] = self.amount
        if self.selector_fallbacks:
            d["selector_fallbacks"] = self.selector_fallbacks
        if self.idempotency_token:
            d["idempotency_token"] = self.idempotency_token
        if self.expected_change:
            d["expected_change"] = self.expected_change
        return d


# ── HTTP / Transport Models ──────────────────────────────────────────────────

@dataclass
class DOMState:
    url: str = ""
    title: str = ""
    timestamp: Optional[int] = None
    viewport: Optional[Dict[str, Any]] = None
    inputs: List[Dict[str, Any]] = field(default_factory=list)
    buttons: List[Dict[str, Any]] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)
    selects: List[Dict[str, Any]] = field(default_factory=list)
    headings: List[Dict[str, Any]] = field(default_factory=list)
    checkboxes: List[Dict[str, Any]] = field(default_factory=list)
    radioButtons: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    images: List[Dict[str, Any]] = field(default_factory=list)
    textSummary: str = ""

    def model_dump(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "timestamp": self.timestamp,
            "viewport": self.viewport,
            "inputs": self.inputs,
            "buttons": self.buttons,
            "links": self.links,
            "selects": self.selects,
            "headings": self.headings,
            "checkboxes": self.checkboxes,
            "radioButtons": self.radioButtons,
            "tables": self.tables,
            "images": self.images,
            "textSummary": self.textSummary,
        }

@dataclass
class TabRegistration:
    tab_id: str
    url: str = ""
    title: str = ""

@dataclass
class TaskRequest:
    goal: str
    tab_id: Optional[str] = None
    max_steps: int = 30


# ── JS deny patterns ─────────────────────────────────────────────────────────

JS_DENY_PATTERNS = ["eval(", "Function(", "import(", "XMLHttpRequest"]
