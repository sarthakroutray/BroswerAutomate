"""
runtime.py — Tool runtime safety helpers (validation, idempotency, envelopes).

Preserved from v5 with minor adaptations for FastMCP architecture.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Optional


RESPONSE_MODES = {"legacy", "structured", "dual"}
MUTATING_TOOLS = {
    "browser_execute_actions",
    "browser_navigate",
    "browser_run_task",
    "browser_navigate_quiz",
    "browser_set_code_editor",
    "browser_compile_and_run",
    "browser_submit_solution",
}


class ToolInputError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_ARGUMENT",
        retriable: bool = False,
        context: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.context = context or {}


class IdempotencyRegistry:
    def __init__(self, ttl_seconds: float = 8.0, max_entries: int = 512):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, float] = {}

    def _prune(self, now: float):
        stale = [k for k, ts in self._entries.items() if now - ts > self.ttl_seconds]
        for k in stale:
            self._entries.pop(k, None)
        if len(self._entries) <= self.max_entries:
            return
        for key, _ in sorted(self._entries.items(), key=lambda kv: kv[1])[: len(self._entries) - self.max_entries]:
            self._entries.pop(key, None)

    def check_and_record(self, key: str) -> bool:
        if not key:
            return True
        now = time.time()
        self._prune(now)
        ts = self._entries.get(key)
        if ts is not None and now - ts <= self.ttl_seconds:
            return False
        self._entries[key] = now
        return True


idempotency_registry = IdempotencyRegistry()


def build_trace_id(tool_name: str) -> str:
    return f"{tool_name}-{uuid.uuid4().hex[:10]}"


def build_idempotency_key(tool_name: str, arguments: dict[str, Any]) -> str:
    user_key = arguments.get("idempotency_key")
    if isinstance(user_key, str) and user_key.strip():
        return f"user:{tool_name}:{user_key.strip()}"

    stable = {
        "tool": tool_name,
        "tab_id": arguments.get("tab_id"),
        "goal": arguments.get("goal"),
        "action": arguments.get("action"),
        "actions": str(arguments.get("actions")),
        "selector": arguments.get("selector"),
        "url": arguments.get("url"),
    }
    payload = json.dumps(stable, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"auto:{tool_name}:{digest}"


def derive_response_mode(arguments: dict[str, Any]) -> str:
    mode = str(arguments.get("response_mode", "dual")).strip().lower()
    return mode if mode in RESPONSE_MODES else "dual"


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


def render_envelope_text(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
