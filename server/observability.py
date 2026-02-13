"""
observability.py — Structured event logging and telemetry.
"""

import time
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Literal, Union
from collections import deque

logger = logging.getLogger("browser-agent")


@dataclass
class ToolEvent:
    tool_name: str
    status: Literal["started", "success", "error", "timeout"]
    duration_ms: float = 0.0
    input_summary: str = ""
    output_summary: str = ""
    error_code: Optional[str] = None
    trace_id: str = ""
    retriable: Optional[bool] = None
    category: Optional[str] = None
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "error_code": self.error_code,
            "trace_id": self.trace_id,
            "retriable": self.retriable,
            "category": self.category,
            "context": self.context,
            "timestamp": self.timestamp,
        }


class EventBus:
    def __init__(self, max_events: int = 200):
        self._events: deque[ToolEvent] = deque(maxlen=max_events)

    def emit(self, event: Union[ToolEvent, str], payload: Optional[dict] = None):
        if isinstance(event, ToolEvent):
            ev = event
        else:
            data = payload or {}
            status = data.get("status")
            if status not in {"started", "success", "error", "timeout"}:
                status = "error" if data.get("error") else "success"
            summary = ""
            if "summary" in data:
                summary = str(data.get("summary", ""))
            elif "error" in data:
                summary = str(data.get("error", ""))
            else:
                summary = json.dumps(data, default=str)[:240]
            ev = ToolEvent(
                tool_name=str(event),
                status=status,
                output_summary=summary,
                error_code=str(data.get("code", "")) or None,
                context=data,
            )

        event = ev
        self._events.append(event)
        level = logging.ERROR if event.status in {"error", "timeout"} else logging.DEBUG
        logger.log(
            level,
            "[%s] %s trace=%s (%s) %.0fms — %s",
            event.status.upper(),
            event.tool_name,
            event.trace_id or "-",
            event.input_summary[:80],
            event.duration_ms,
            event.output_summary[:120] or event.error_code or "",
        )

    def get_recent(self, count: int = 20) -> list[ToolEvent]:
        items = list(self._events)
        return items[-count:]

    def get_recent_dicts(self, count: int = 20) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.get_recent(count)]

    def tool_stats(self) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        for ev in self._events:
            if ev.tool_name not in stats:
                stats[ev.tool_name] = {"calls": 0, "errors": 0, "avg_ms": 0.0, "total_ms": 0.0}
            s = stats[ev.tool_name]
            s["calls"] += 1
            s["total_ms"] += ev.duration_ms
            if ev.status in ("error", "timeout"):
                s["errors"] += 1
            s["avg_ms"] = s["total_ms"] / s["calls"]
        return stats

    def emit_tool_start(self, tool_name: str, *, trace_id: str, input_summary: str = ""):
        self.emit(
            ToolEvent(
                tool_name=tool_name,
                status="started",
                trace_id=trace_id,
                input_summary=input_summary,
            )
        )

    def emit_tool_end(
        self,
        tool_name: str,
        *,
        trace_id: str,
        status: Literal["success", "error", "timeout"],
        duration_ms: float,
        output_summary: str = "",
        error_code: Optional[str] = None,
        retriable: Optional[bool] = None,
        category: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ):
        self.emit(
            ToolEvent(
                tool_name=tool_name,
                status=status,
                trace_id=trace_id,
                duration_ms=duration_ms,
                output_summary=output_summary,
                error_code=error_code,
                retriable=retriable,
                category=category,
                context=context or {},
            )
        )


event_bus = EventBus()
