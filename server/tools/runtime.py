"""
runtime.py — Tool runtime safety helpers (validation, idempotency, envelopes).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Optional


RESPONSE_MODES = {"legacy", "structured", "dual"}
MUTATING_TOOLS = {
    "browser_act",
    "browser_fill_form",
    "browser_navigate",
    "quiz_answer",
    "quiz_navigate",
    "code_inject",
    "code_submit",
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


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ToolInputError(
        "Boolean value expected",
        code="INVALID_TYPE",
        retriable=False,
    )


def _coerce_value(value: Any, schema: dict[str, Any], *, key: str) -> Any:
    expected = schema.get("type")
    if expected is None:
        return value

    if expected == "string":
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    if expected == "integer":
        if isinstance(value, bool):
            raise ToolInputError(f"'{key}' must be an integer", code="INVALID_TYPE")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ToolInputError(f"'{key}' must be an integer", code="INVALID_TYPE") from exc
        raise ToolInputError(f"'{key}' must be an integer", code="INVALID_TYPE")

    if expected == "number":
        if isinstance(value, bool):
            raise ToolInputError(f"'{key}' must be a number", code="INVALID_TYPE")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ToolInputError(f"'{key}' must be a number", code="INVALID_TYPE") from exc
        raise ToolInputError(f"'{key}' must be a number", code="INVALID_TYPE")

    if expected == "boolean":
        return _coerce_bool(value)

    if expected == "array":
        if not isinstance(value, list):
            raise ToolInputError(f"'{key}' must be an array", code="INVALID_TYPE")
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return value
        out = []
        for idx, item in enumerate(value):
            out.append(_coerce_value(item, item_schema, key=f"{key}[{idx}]"))
        return out

    if expected == "object":
        if not isinstance(value, dict):
            raise ToolInputError(f"'{key}' must be an object", code="INVALID_TYPE")
        return value

    return value


def validate_and_sanitize_arguments(
    arguments: Optional[dict[str, Any]],
    schema: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    args = arguments or {}
    if not isinstance(args, dict):
        raise ToolInputError("Tool arguments must be an object", code="INVALID_TYPE")

    if not schema or schema.get("type") != "object":
        return args, {"unknown_fields": [], "coerced_fields": []}

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    sanitized: dict[str, Any] = {}
    unknown_fields: list[str] = []
    coerced_fields: list[str] = []

    for key, value in args.items():
        prop_schema = properties.get(key)
        if not isinstance(prop_schema, dict):
            unknown_fields.append(key)
            sanitized[key] = value
            continue

        coerced = _coerce_value(value, prop_schema, key=key)
        if coerced is not value:
            coerced_fields.append(key)

        enum = prop_schema.get("enum")
        if enum:
            if isinstance(coerced, str):
                enum_map = {str(v).lower(): v for v in enum}
                lowered = coerced.lower()
                if lowered not in enum_map:
                    raise ToolInputError(
                        f"Invalid value for '{key}': {coerced}. Allowed: {enum}",
                        code="INVALID_ENUM",
                    )
                coerced = enum_map[lowered]
            elif coerced not in enum:
                raise ToolInputError(
                    f"Invalid value for '{key}': {coerced}. Allowed: {enum}",
                    code="INVALID_ENUM",
                )

        min_len = prop_schema.get("minLength")
        if isinstance(min_len, int) and isinstance(coerced, str) and len(coerced) < min_len:
            raise ToolInputError(
                f"'{key}' must be at least {min_len} characters",
                code="INVALID_LENGTH",
            )

        max_len = prop_schema.get("maxLength")
        if isinstance(max_len, int) and isinstance(coerced, str) and len(coerced) > max_len:
            coerced = coerced[:max_len]
            if key not in coerced_fields:
                coerced_fields.append(key)

        sanitized[key] = coerced

    missing = [k for k in required if k not in sanitized or sanitized[k] in (None, "")]
    if missing:
        raise ToolInputError(
            f"Missing required field(s): {', '.join(missing)}",
            code="MISSING_REQUIRED_FIELD",
            retriable=False,
            context={"missing": missing},
        )

    return sanitized, {"unknown_fields": unknown_fields, "coerced_fields": coerced_fields}


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
        "actions": arguments.get("actions"),
        "selector": arguments.get("selector"),
        "url": arguments.get("url"),
        "fields": arguments.get("fields"),
    }
    payload = json.dumps(stable, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"auto:{tool_name}:{digest}"


def derive_response_mode(arguments: dict[str, Any]) -> str:
    mode = str(arguments.get("response_mode", "dual")).strip().lower()
    return mode if mode in RESPONSE_MODES else "dual"


def _extract_text_chunks(contents: list[Any]) -> list[str]:
    chunks = []
    for item in contents:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return chunks


def _infer_error_from_text(chunks: list[str]) -> tuple[bool, str]:
    if not chunks:
        return False, ""
    first = chunks[0]
    lowered = first.lower()
    is_error = lowered.startswith("error:") or lowered.startswith("timeout:") or lowered.startswith("connection error:")
    return is_error, first


def _infer_retriable(message: str) -> bool:
    lowered = (message or "").lower()
    retriable_markers = (
        "timeout",
        "temporar",
        "connection",
        "retry",
        "stale",
        "detached",
        "no state change",
        "interactable",
    )
    non_retriable_markers = (
        "missing required",
        "invalid",
        "unsafe",
        "cross-origin",
        "disallowed",
    )
    if any(m in lowered for m in non_retriable_markers):
        return False
    return any(m in lowered for m in retriable_markers)


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


def infer_envelope_from_result(
    tool_name: str,
    trace_id: str,
    result: Optional[list[Any]],
    *,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    chunks = _extract_text_chunks(result or [])
    is_error, message = _infer_error_from_text(chunks)
    if not message:
        message = f"{tool_name} completed"

    status = "error" if is_error else "success"
    code = "TOOL_EXECUTION_ERROR" if is_error else "TOOL_EXECUTION_SUCCESS"
    retriable = _infer_retriable(message) if is_error else False
    return build_tool_envelope(
        tool_name=tool_name,
        status=status,
        code=code,
        message=message,
        trace_id=trace_id,
        retriable=retriable,
        context=context,
    )


def render_envelope_text(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
