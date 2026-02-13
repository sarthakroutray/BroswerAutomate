"""
orchestrator.py — Autonomous browser agent with deterministic reliability guards.
"""

from __future__ import annotations

import json
import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field as dc_field
from typing import Optional, Any, Union

from ..browser_state import MCPSessionTracker
from ..config import (
    SCREENSHOT_TIMEOUT,
    AGENT_STEP_DELAY,
    DEFAULT_MAX_STEPS,
    MAX_TASK_HISTORY,
    AGENT_MAX_RETRY_PER_STEP,
    AGENT_MAX_RETRY_PER_CATEGORY,
    AGENT_STAGNATION_LIMIT,
    AGENT_DOM_STABILIZE_WAIT_MS,
    ENABLE_AGENT_ACTION_VERIFICATION,
    ENABLE_AGENT_FAILURE_CLASSIFICATION,
    ENABLE_AGENT_SAFE_STOP,
)
from ..llm.prompts import AGENT_SYSTEM_PROMPT
from ..llm.provider import LLMProvider
from ..observability import event_bus
from ..tools.observation import format_dom_for_display
from ..tools.schemas import ALLOWED_ACTIONS

logger = logging.getLogger("browser-agent")

PASSIVE_ACTIONS = {"wait", "hover", "focus", "press_key"}
RETRIABLE_CATEGORIES = {
    "timeout",
    "connection",
    "selector_missing",
    "not_interactable",
    "occluded",
    "no_state_change",
    "unknown_retriable",
}


@dataclass
class TaskState:
    task_id: str
    goal: str
    tab_id: str
    status: str = "running"
    step_count: int = 0
    max_steps: int = DEFAULT_MAX_STEPS
    history: list = dc_field(default_factory=list)
    summary: str = ""
    error: str = ""
    cancelled: bool = False
    created_at: float = dc_field(default_factory=time.time)
    stagnation_count: int = 0
    retry_by_category: dict[str, int] = dc_field(default_factory=dict)
    last_action_signature: str = ""
    last_failure: str = ""


class AutonomousAgent:
    def __init__(self, browser_mgr, session_tracker: MCPSessionTracker):
        self.browser = browser_mgr
        self.tasks: dict[str, TaskState] = {}
        self._progress_callback = None
        self._llm = LLMProvider(session_tracker)
        self._session_tracker = session_tracker

    @property
    def llm(self) -> LLMProvider:
        return self._llm

    def set_mcp_session(self, session):
        self._llm.set_mcp_session(session)

    def set_progress_callback(self, cb):
        self._progress_callback = cb

    def _prune_old_tasks(self):
        completed = [(tid, t) for tid, t in self.tasks.items() if t.status != "running"]
        completed.sort(key=lambda x: x[1].created_at)
        while len(completed) > MAX_TASK_HISTORY:
            tid, _ = completed.pop(0)
            del self.tasks[tid]

    async def _get_screenshot(self, tab_id: str) -> Optional[str]:
        try:
            resp = await self.browser.send(
                {"type": "TAKE_SCREENSHOT", "tab_id": tab_id},
                timeout=SCREENSHOT_TIMEOUT,
            )
            return resp.get("screenshot")
        except Exception as e:
            logger.warning(f"Screenshot failed: {e}")
            return None

    async def _get_dom(self, tab_id: str) -> Optional[dict]:
        try:
            resp = await self.browser.send({"type": "REQUEST_DOM", "tab_id": tab_id})
            dom = resp.get("dom_state", {})
            if dom:
                self.browser.update_dom(tab_id, dom)
            return dom
        except Exception as e:
            logger.warning(f"DOM request failed: {e}")
            tab = self.browser.tabs.get(tab_id)
            return tab.dom_state if tab else None

    def _dom_fingerprint(self, dom: Optional[dict]) -> str:
        if not dom:
            return ""
        material = {
            "url": dom.get("url", ""),
            "title": dom.get("title", ""),
            "inputs": len(dom.get("inputs", [])),
            "buttons": len(dom.get("buttons", [])),
            "links": len(dom.get("links", [])),
            "selects": len(dom.get("selects", [])),
            "checkboxes": len(dom.get("checkboxes", [])),
            "radios": len(dom.get("radioButtons", [])),
            "text": (dom.get("textSummary", "") or "")[:500],
        }
        encoded = json.dumps(material, sort_keys=True, ensure_ascii=True)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:20]

    def _snapshot_from_dom(self, dom: Optional[dict]) -> dict[str, Any]:
        url = (dom or {}).get("url", "")
        return {
            "url": url,
            "hash": self._dom_fingerprint(dom),
            "dom": dom or {},
            "ts": time.time(),
        }

    def _sanitize_actions(self, actions: Any) -> list[dict]:
        if not isinstance(actions, list):
            return []
        sanitized = []
        for raw in actions[:5]:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action", "")).strip().lower()
            if action not in ALLOWED_ACTIONS:
                continue
            step = {"action": action}
            for key in ("selector", "value", "expected_change"):
                if key in raw and raw.get(key) is not None:
                    step[key] = str(raw.get(key))
            for key in ("x", "y", "amount"):
                if key in raw and raw.get(key) is not None:
                    step[key] = raw.get(key)
            if isinstance(raw.get("selector_fallbacks"), list):
                step["selector_fallbacks"] = [
                    str(v) for v in raw.get("selector_fallbacks", [])[:8]
                    if isinstance(v, str) and v.strip()
                ]
            if raw.get("idempotency_token"):
                step["idempotency_token"] = str(raw.get("idempotency_token"))
            sanitized.append(step)
        return sanitized

    def _normalize_ai_response(self, ai_response: dict) -> dict:
        if not isinstance(ai_response, dict):
            return {"thinking": "", "actions": [], "done": False, "summary": ""}
        return {
            "thinking": str(ai_response.get("thinking", "")),
            "actions": self._sanitize_actions(ai_response.get("actions", [])),
            "done": bool(ai_response.get("done", False)),
            "summary": str(ai_response.get("summary", "")),
        }

    async def _ask_ai(self, goal, dom, screenshot, history, last_failure: str) -> dict:
        dom_text = format_dom_for_display(dom) if dom else "No DOM data available."
        user_content = f"## Goal\n{goal}\n\n## Current Page State\n{dom_text}"
        if history:
            recent = history[-5:]
            lines = []
            for h in recent:
                lines.append(
                    f"Step {h['step']}: {h.get('thinking', '')}\n"
                    f"Actions: {json.dumps(h.get('actions', []))}\n"
                    f"Result: {json.dumps(h.get('result', {}))}"
                )
            user_content += "\n\n## Recent Action History\n" + "\n\n".join(lines)
        if last_failure:
            user_content += f"\n\n## Last Failure\n{last_failure}\nUse a correction strategy and avoid repeating the same failed action."
        user_content += "\n\nRespond with your next actions as strict JSON."

        ai_response = await self._llm.ask_json(
            text=user_content,
            system_prompt=AGENT_SYSTEM_PROMPT,
            screenshot=screenshot,
        )
        return self._normalize_ai_response(ai_response)

    def _action_signature(self, actions: list[dict]) -> str:
        encoded = json.dumps(actions, sort_keys=True, ensure_ascii=True)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:20]

    async def _execute_actions(self, tab_id: str, actions: list[dict]) -> dict:
        if not actions:
            return {"results": [], "error": None}
        steps = []
        for a in actions:
            step = {"action": a.get("action", "")}
            for key in ("selector", "value", "x", "y", "amount", "selector_fallbacks", "idempotency_token", "expected_change"):
                if a.get(key) is not None:
                    step[key] = a.get(key)
            steps.append(step)
        try:
            resp = await self.browser.send({
                "type": "EXECUTE_ACTIONS",
                "tab_id": tab_id,
                "steps": steps,
            })
            return resp.get("result", resp.get("results", resp))
        except Exception as e:
            return {"error": str(e)}

    def _classify_result(self, result: Union[dict, list]) -> tuple[bool, str, str]:
        if isinstance(result, dict) and result.get("error"):
            message = str(result.get("error"))
        elif isinstance(result, list):
            failed = [r for r in result if isinstance(r, dict) and not r.get("success")]
            if not failed:
                return True, "ok", ""
            message = str(failed[0].get("error", "unknown action failure"))
        elif isinstance(result, dict) and isinstance(result.get("results"), list):
            return self._classify_result(result.get("results"))
        else:
            return True, "ok", ""

        lowered = message.lower()
        if "timeout" in lowered:
            return False, "timeout", message
        if "connection" in lowered or "socket" in lowered:
            return False, "connection", message
        if "not found" in lowered:
            return False, "selector_missing", message
        if "cross-origin" in lowered or "unsafe url" in lowered:
            return False, "policy", message
        if "interactable" in lowered or "disabled" in lowered or "read-only" in lowered:
            return False, "not_interactable", message
        if "occluded" in lowered or "blocked" in lowered or "overlay" in lowered:
            return False, "occluded", message
        if "disallowed" in lowered or "requires" in lowered:
            return False, "validation", message
        if "failed" in lowered:
            return False, "unknown_retriable", message
        return False, "unknown", message

    async def _verify_action_effect(
        self,
        actions: list[dict],
        before_snapshot: dict[str, Any],
        tab_id: str,
    ) -> tuple[bool, dict[str, Any], str]:
        actionable = [a for a in actions if a.get("action") not in PASSIVE_ACTIONS]
        if not actionable:
            return True, before_snapshot, ""

        await asyncio.sleep(max(0.15, AGENT_DOM_STABILIZE_WAIT_MS / 1000.0))
        after_dom = await self._get_dom(tab_id)
        after_snapshot = self._snapshot_from_dom(after_dom)

        changed = (
            after_snapshot.get("url") != before_snapshot.get("url")
            or after_snapshot.get("hash") != before_snapshot.get("hash")
        )
        if changed:
            return True, after_snapshot, ""

        no_change_actions = {"hover", "focus", "press_key", "wait"}
        if all(a.get("action") in no_change_actions for a in actions):
            return True, after_snapshot, ""
        return False, after_snapshot, "Action completed but no state change was detected"

    async def _apply_retry_correction(self, tab_id: str, category: str, attempt: int):
        try:
            if category == "selector_missing":
                await self.browser.send({
                    "type": "EXECUTE_ACTIONS",
                    "tab_id": tab_id,
                    "steps": [{"action": "scroll", "value": "down", "amount": 450}],
                })
                await asyncio.sleep(0.3)
                return
            if category in {"not_interactable", "occluded"}:
                await self.browser.send({
                    "type": "EXECUTE_ACTIONS",
                    "tab_id": tab_id,
                    "steps": [{"action": "press_key", "value": "Escape"}],
                })
                await asyncio.sleep(0.25)
                return
            if category in {"timeout", "connection", "no_state_change"}:
                await asyncio.sleep(0.4 + 0.25 * attempt)
                return
            await asyncio.sleep(0.25)
        except Exception:
            await asyncio.sleep(0.25)

    def _can_retry_category(self, task: TaskState, category: str) -> bool:
        if category not in RETRIABLE_CATEGORIES:
            return False
        used = task.retry_by_category.get(category, 0)
        return used < AGENT_MAX_RETRY_PER_CATEGORY

    def _mark_category_retry(self, task: TaskState, category: str):
        task.retry_by_category[category] = task.retry_by_category.get(category, 0) + 1

    async def _execute_with_retries(
        self,
        task: TaskState,
        actions: list[dict],
        before_snapshot: dict[str, Any],
    ) -> tuple[dict, dict[str, Any], bool, str]:
        max_attempts = max(1, AGENT_MAX_RETRY_PER_STEP)
        last_result: dict = {"error": "Unknown action failure"}
        last_message = "Unknown action failure"
        last_category = "unknown"
        after_snapshot = before_snapshot

        for attempt in range(1, max_attempts + 1):
            action_result = await self._execute_actions(task.tab_id, actions)
            if isinstance(action_result, dict) and "results" in action_result:
                result_payload = action_result.get("results")
            else:
                result_payload = action_result

            ok, category, message = self._classify_result(result_payload)
            last_result = action_result if isinstance(action_result, dict) else {"results": action_result}
            last_message = message
            last_category = category

            if ENABLE_AGENT_ACTION_VERIFICATION and ok:
                verify_ok, after_snapshot, verify_message = await self._verify_action_effect(
                    actions, before_snapshot, task.tab_id
                )
                if not verify_ok:
                    ok = False
                    category = "no_state_change"
                    message = verify_message
                    last_category = category
                    last_message = message
                else:
                    return last_result, after_snapshot, True, ""
            elif ok:
                return last_result, after_snapshot, True, ""

            if not ENABLE_AGENT_FAILURE_CLASSIFICATION:
                break
            if attempt >= max_attempts:
                break
            if not self._can_retry_category(task, category):
                break

            self._mark_category_retry(task, category)
            await self._apply_retry_correction(task.tab_id, category, attempt)

        return last_result, after_snapshot, False, f"{last_category}: {last_message}".strip(": ")

    async def _notify(self, task: TaskState, message: str):
        if self._progress_callback:
            await self._progress_callback(task, message)

    async def run_task(self, task_id, goal, tab_id, max_steps=DEFAULT_MAX_STEPS) -> TaskState:
        task = TaskState(task_id=task_id, goal=goal, tab_id=tab_id, max_steps=max_steps)
        self.tasks[task_id] = task
        self._prune_old_tasks()
        await self._notify(task, f"Starting: {goal}")
        event_bus.emit("task_start", {"task_id": task_id, "goal": goal, "status": "started"})

        try:
            for step_num in range(1, max_steps + 1):
                if task.cancelled:
                    task.status = "stopped"
                    task.summary = "Task stopped by user"
                    break

                task.step_count = step_num
                await self._notify(task, f"Step {step_num}/{max_steps}: Capturing page...")

                screenshot = await self._get_screenshot(tab_id)
                dom = await self._get_dom(tab_id)
                before_snapshot = self._snapshot_from_dom(dom)

                if not dom and not screenshot:
                    task.stagnation_count += 1
                    await self._notify(task, f"Step {step_num}: Waiting for page to load...")
                    await asyncio.sleep(min(2.5, 0.8 + step_num * 0.15))
                    if ENABLE_AGENT_SAFE_STOP and task.stagnation_count >= AGENT_STAGNATION_LIMIT:
                        task.status = "error"
                        task.error = "No observable page state available after repeated attempts"
                        task.summary = task.error
                        break
                    continue

                await self._notify(task, f"Step {step_num}/{max_steps}: AI analyzing page...")
                ai_response = await self._ask_ai(goal, dom, screenshot, task.history, task.last_failure)
                thinking = ai_response.get("thinking", "")
                actions = ai_response.get("actions", [])
                done = ai_response.get("done", False)
                summary = ai_response.get("summary", "")

                await self._notify(task, f"Step {step_num}: {thinking[:140]}")

                if done:
                    task.status = "completed"
                    task.summary = summary or "Task completed"
                    await self._notify(task, f"✓ {task.summary}")
                    break

                if not actions:
                    task.stagnation_count += 1
                    task.last_failure = "AI returned no executable actions"
                    task.history.append(
                        {"step": step_num, "thinking": thinking, "actions": [], "result": {"note": "No actions"}}
                    )
                    await asyncio.sleep(AGENT_STEP_DELAY)
                    if ENABLE_AGENT_SAFE_STOP and task.stagnation_count >= AGENT_STAGNATION_LIMIT:
                        task.status = "error"
                        task.error = "Agent stalled with repeated empty action plans"
                        task.summary = task.error
                        break
                    continue

                action_sig = self._action_signature(actions)
                if (
                    ENABLE_AGENT_SAFE_STOP
                    and task.last_action_signature
                    and task.last_action_signature == action_sig
                    and task.stagnation_count >= AGENT_STAGNATION_LIMIT - 1
                ):
                    task.status = "error"
                    task.error = "Repeated identical failing action plan; stopping safely"
                    task.summary = task.error
                    break
                task.last_action_signature = action_sig

                action_names = ", ".join([a.get("action", "?") for a in actions])
                await self._notify(task, f"Step {step_num}: Executing: {action_names}")

                # Set deterministic step-level idempotency tokens for risky actions.
                for idx, action in enumerate(actions):
                    if action.get("idempotency_token"):
                        continue
                    if action.get("action") in {"submit", "navigate", "click", "click_at"}:
                        seed = f"{task.task_id}:{step_num}:{idx}:{action_sig}"
                        action["idempotency_token"] = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]

                result, after_snapshot, success, failure_message = await self._execute_with_retries(
                    task, actions, before_snapshot
                )

                changed = (
                    after_snapshot.get("url") != before_snapshot.get("url")
                    or after_snapshot.get("hash") != before_snapshot.get("hash")
                )
                if success and changed:
                    task.stagnation_count = 0
                    task.last_failure = ""
                elif success:
                    task.stagnation_count += 1
                    task.last_failure = "No observable state transition"
                else:
                    task.stagnation_count += 1
                    task.last_failure = failure_message or "Action execution failed"

                task.history.append(
                    {
                        "step": step_num,
                        "thinking": thinking,
                        "actions": actions,
                        "result": result,
                        "verification": {
                            "before_url": before_snapshot.get("url"),
                            "after_url": after_snapshot.get("url"),
                            "before_hash": before_snapshot.get("hash"),
                            "after_hash": after_snapshot.get("hash"),
                            "changed": changed,
                            "success": success,
                            "failure": task.last_failure,
                        },
                    }
                )

                if ENABLE_AGENT_SAFE_STOP and task.stagnation_count >= AGENT_STAGNATION_LIMIT:
                    task.status = "error"
                    task.error = (
                        "Stopped after repeated non-progressing steps. "
                        f"Last failure: {task.last_failure or 'unknown'}"
                    )
                    task.summary = task.error
                    await self._notify(task, task.error)
                    break

                await asyncio.sleep(AGENT_STEP_DELAY)

            if task.status == "running":
                task.status = "max_steps"
                task.summary = f"Reached maximum {max_steps} steps"
                await self._notify(task, task.summary)

        except Exception as e:
            task.status = "error"
            task.error = str(e)
            logger.error(f"Task {task_id} error: {e}", exc_info=True)
            await self._notify(task, f"Error: {e}")

        event_bus.emit("task_end", {"task_id": task_id, "status": task.status, "summary": task.summary or task.error})
        return task

    def stop_task(self, task_id) -> bool:
        task = self.tasks.get(task_id)
        if task and task.status == "running":
            task.cancelled = True
            return True
        return False
