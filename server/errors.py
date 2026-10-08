"""
errors.py — Structured tool response envelope.

All tool responses are wrapped in a standardised envelope:

    {
        "status": "success" | "error",
        "code": "ERROR_CODE",
        "message": "Human-readable explanation",
        "data": { ... }
    }
"""

from __future__ import annotations

import json
from typing import Any, Optional


def format_tool_result(
    *,
    status: str = "success",
    code: str = "OK",
    message: str,
    data: Optional[dict[str, Any]] = None,
) -> str:
    """Format a structured tool response as a JSON string for MCP tool returns."""
    envelope = {
        "status": status,
        "code": code,
        "message": message,
        "data": data or {},
    }
    return json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
