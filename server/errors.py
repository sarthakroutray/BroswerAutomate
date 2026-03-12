"""
errors.py — Structured MCP-compliant error handling layer.

All tool responses are wrapped in a standardised envelope:

    {
        "status": "success" | "error",
        "code": "ERROR_CODE",
        "message": "Human-readable explanation",
        "retriable": true | false,
        "details": { ... }
    }

Error codes are capability-scoped and deterministic so that LLM clients
can learn routing adjustments from failure signals.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("browser-agent")


# ── Error codes ───────────────────────────────────────────────────────────────

class ErrorCode(str, Enum):
    # Validation
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_ENUM = "INVALID_ENUM"
    INVALID_TYPE = "INVALID_TYPE"
    INVALID_LENGTH = "INVALID_LENGTH"

    # Connection
    NO_BROWSER_CONNECTION = "NO_BROWSER_CONNECTION"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"

    # Execution
    TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
    TOOL_EXECUTION_SUCCESS = "TOOL_EXECUTION_SUCCESS"
    UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"

    # Policy
    TOOL_DISABLED = "TOOL_DISABLED"
    DUPLICATE_ACTION_BLOCKED = "DUPLICATE_ACTION_BLOCKED"
    UNSAFE_OPERATION = "UNSAFE_OPERATION"

    # Agent
    AGENT_EXECUTION_ERROR = "AGENT_EXECUTION_ERROR"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"

    # DOM / Browser
    SELECTOR_NOT_FOUND = "SELECTOR_NOT_FOUND"
    TAB_NOT_FOUND = "TAB_NOT_FOUND"
    NO_DOM_STATE = "NO_DOM_STATE"
    JS_EXECUTION_ERROR = "JS_EXECUTION_ERROR"

    # Coding
    COMPILATION_ERROR = "COMPILATION_ERROR"
    SUBMIT_ERROR = "SUBMIT_ERROR"


# ── Retriability mapping ─────────────────────────────────────────────────────

_RETRIABLE_CODES = {
    ErrorCode.CONNECTION_ERROR,
    ErrorCode.TIMEOUT,
    ErrorCode.AGENT_EXECUTION_ERROR,
    ErrorCode.SELECTOR_NOT_FOUND,
    ErrorCode.NO_DOM_STATE,
}


def is_retriable(code: ErrorCode) -> bool:
    """Determine if an error code is inherently retriable."""
    return code in _RETRIABLE_CODES


# ── Structured response builder ──────────────────────────────────────────────

@dataclass
class ToolResponse:
    """Immutable structured response from any tool invocation."""
    status: str  # "success" | "error"
    code: str
    message: str
    retriable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
            "details": self.details,
        }

    def to_text(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"))


def success_response(
    message: str,
    *,
    code: str = ErrorCode.TOOL_EXECUTION_SUCCESS,
    details: Optional[dict[str, Any]] = None,
) -> ToolResponse:
    return ToolResponse(
        status="success",
        code=code if isinstance(code, str) else code.value,
        message=message,
        retriable=False,
        details=details or {},
    )


def error_response(
    message: str,
    *,
    code: ErrorCode = ErrorCode.TOOL_EXECUTION_ERROR,
    retriable: Optional[bool] = None,
    details: Optional[dict[str, Any]] = None,
) -> ToolResponse:
    if retriable is None:
        retriable = is_retriable(code)
    return ToolResponse(
        status="error",
        code=code.value if isinstance(code, ErrorCode) else code,
        message=message,
        retriable=retriable,
        details=details or {},
    )


# ── Exception → ToolResponse mapping ─────────────────────────────────────────

def exception_to_response(
    exc: Exception,
    *,
    tool_name: str = "",
    context: Optional[dict[str, Any]] = None,
) -> ToolResponse:
    """Map any Python exception into a structured ToolResponse."""
    details = dict(context or {})
    details["exception_type"] = type(exc).__name__

    if isinstance(exc, ConnectionError):
        return error_response(
            f"Connection error: {exc}",
            code=ErrorCode.CONNECTION_ERROR,
            retriable=True,
            details=details,
        )
    if isinstance(exc, TimeoutError):
        return error_response(
            f"Timeout: {exc}",
            code=ErrorCode.TIMEOUT,
            retriable=True,
            details=details,
        )
    if isinstance(exc, ValueError):
        return error_response(
            str(exc),
            code=ErrorCode.INVALID_ARGUMENT,
            retriable=False,
            details=details,
        )

    logger.error(f"Unhandled exception in tool {tool_name}: {exc}", exc_info=True)
    return error_response(
        f"Internal error: {exc}",
        code=ErrorCode.UNHANDLED_EXCEPTION,
        retriable=False,
        details=details,
    )


# ── Envelope builder (backward-compatible with runtime.py) ────────────────────

def build_tool_envelope(
    *,
    tool_name: str,
    status: str,
    code: str,
    message: str,
    trace_id: str,
    retriable: bool,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "message": message,
        "retriable": bool(retriable),
        "context": {
            "tool": tool_name,
            "trace_id": trace_id,
            **(context or {}),
        },
    }


# ── Structured tool response formatter ────────────────────────────────────────

def format_tool_result(
    *,
    status: str = "success",
    code: str = "OK",
    message: str,
    data: Optional[dict[str, Any]] = None,
) -> str:
    """Format a structured tool response as JSON string for MCP tool returns."""
    envelope = {
        "status": status,
        "code": code,
        "message": message,
        "data": data or {},
    }
    return json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
