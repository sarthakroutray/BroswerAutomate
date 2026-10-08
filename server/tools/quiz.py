"""
quiz.py — Quiz navigation tool handler.

Tools:
  browser_navigate_quiz — Next / Previous / Submit quiz navigation
  browser_solve_quiz    — End-to-end solver (extract → answer → next → repeat → submit)

NOTE: quiz_answer is removed — use browser_execute_actions with click action.
      quiz_extract is removed — use browser_get_page_state (auto-detects quizzes).
"""

import re
import asyncio
import json
import logging
from typing import Optional

from ..browser_state import browser_manager, send_with_retries, click_first_selector
from ..config import QUIZ_NAV_SELECTORS
from ..errors import format_tool_result
from ..policy import evaluate_action

logger = logging.getLogger("browser-agent")


QUIZ_POSITION_PATTERN = re.compile(
    r"Question\s*No\s*:?\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE
)


async def get_quiz_position(tab_id: str) -> tuple[Optional[int], Optional[int]]:
    try:
        response = await send_with_retries(
            {"type": "EXTRACT_TEXT", "tab_id": tab_id, "query": "Question No", "selector": ""},
            timeout=8.0, retries=1,
        )
        source = "\n".join(response.get("matches", [])) if response.get("matches") else response.get("text", "")
        match = QUIZ_POSITION_PATTERN.search(source or "")
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return None, None


async def handle_navigate_quiz(
    action: str,
    tab_id: Optional[str] = None,
    allow_unsafe: bool = False,
) -> str:
    """Navigate quiz pages: next, previous, or submit the quiz.

    USE THIS TOOL:
    - To move to the next quiz question (action='next')
    - To go back to a previous question (action='previous')
    - To submit the entire quiz (action='submit')

    DO NOT USE THIS TOOL:
    - To select quiz answers (use browser_execute_actions with click)
    - To read quiz questions (use browser_get_page_state)
    - For non-quiz page navigation (use browser_navigate)

    Args:
        action: Quiz navigation action — next|previous|submit

    Returns: Navigation result with question position tracking.
    """
    tab = browser_manager.resolve_tab(tab_id)
    if action not in {"next", "previous", "submit"}:
        action = "next"

    if action == "submit":
        decision = evaluate_action(
            action="submit",
            current_url=tab.url or ((tab.dom_state or {}).get("url", "")),
            allow_unsafe=allow_unsafe,
        )
        if not decision.allowed:
            return format_tool_result(
                status="error",
                code=decision.code,
                message=decision.message,
                data={"tab_id": tab.tab_id, **decision.data},
            )

    before_q, before_total = await get_quiz_position(tab.tab_id)
    selectors = QUIZ_NAV_SELECTORS.get(action, QUIZ_NAV_SELECTORS["next"])
    clicked = await click_first_selector(tab.tab_id, selectors, timeout=6.0)

    if not clicked and action in {"next", "previous"}:
        key_value = "ArrowRight" if action == "next" else "ArrowLeft"
        try:
            fallback_resp = await browser_manager.send(
                {"type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
                 "steps": [{"action": "press_key", "value": key_value}]},
                timeout=5.0,
            )
            fallback_results = fallback_resp.get("results") or fallback_resp.get("result")
            if isinstance(fallback_results, list):
                clicked = any(r.get("success") for r in fallback_results)
            elif isinstance(fallback_results, dict):
                clicked = not fallback_results.get("error")
        except Exception:
            clicked = False

    if not clicked:
        return format_tool_result(
            status="error",
            code="QUIZ_NAVIGATION_FAILED",
            message=(
                f"Could not trigger '{action}' navigation. "
                "Try browser_execute_actions with a page-specific selector."
            ),
            data={"tab_id": tab.tab_id, "action": action},
        )

    await asyncio.sleep(1.5)
    after_q, after_total = await get_quiz_position(tab.tab_id)

    if action in {"next", "previous"} and before_q is not None and after_q is not None and after_q == before_q:
        return format_tool_result(
            status="error",
            code="QUIZ_NAVIGATION_UNCONFIRMED",
            message=(
                f"Triggered '{action}' but question did not change "
                f"(still {after_q}/{after_total or before_total or '?'})"
            ),
            data={"tab_id": tab.tab_id, "action": action, "question": after_q, "total": after_total or before_total},
        )

    if after_q is not None:
        return format_tool_result(
            status="success",
            code="QUIZ_NAVIGATION_DONE",
            message=f"Navigated {action}. Now on Question {after_q}/{after_total or '?'}",
            data={"tab_id": tab.tab_id, "action": action, "question": after_q, "total": after_total},
        )

    return format_tool_result(
        status="success",
        code="QUIZ_NAVIGATION_DONE",
        message=f"Triggered navigation action: {action}",
        data={"tab_id": tab.tab_id, "action": action},
    )


async def handle_solve_quiz(
    max_questions: int = 50,
    confidence_threshold: float = 0.5,
    tab_id: Optional[str] = None,
    allow_unsafe: bool = False,
) -> str:
    """End-to-end quiz solver. Detects the quiz, extracts questions, asks the LLM
    for an answer per question, clicks it, navigates Next, repeats, and submits.

    USE THIS TOOL:
    - When on a quiz / assessment page and you want the LLM to attempt all
      questions in one shot (e.g. "solve this 10-question quiz").
    - When the page structure is: question → options → next → repeat → submit.

    DO NOT USE THIS TOOL:
    - For non-quiz pages (use browser_run_task instead).
    - When the page has a login wall, captcha, or 2FA — those are surfaced as
      errors so you can solve them manually first.

    Args:
        max_questions: Hard cap on questions to attempt (default 50).
        confidence_threshold: Skip questions where the LLM's confidence is
            below this (default 0.5). Such questions are recorded as `skipped`.
        tab_id: Optional tab ID. Uses active tab if not specified.
        allow_unsafe: Passed to the final Submit action.

    Returns: Per-question log + final summary (answered, skipped, submit status).
    """
    from ..solvers.quiz import solve_quiz
    from ..llm.provider import LLMProvider
    from ..browser_state import _mcp_session_tracker

    tab = browser_manager.resolve_tab(tab_id)
    llm = LLMProvider(_mcp_session_tracker)
    result = await solve_quiz(
        tab_id=tab.tab_id,
        max_questions=max_questions,
        confidence_threshold=confidence_threshold,
        llm=llm,
        allow_unsafe=allow_unsafe,
    )
    # Re-shape: top-level fields on data, with summary at top
    data = result.pop("data", {})
    return format_tool_result(
        status=result.get("status", "error"),
        code=result.get("code", "QUIZ_SOLVER_ERROR"),
        message=result.get("message", "Quiz solver finished"),
        data=data,
    )
