"""
quiz.py — Quiz navigation tool handler.

Tools:
  browser_navigate_quiz — Next / Previous / Submit quiz navigation

NOTE: quiz_answer is removed — use browser_execute_actions with click action.
      quiz_extract is removed — use browser_get_page_state (auto-detects quizzes).
"""

import re
import asyncio
import logging
from typing import Optional

from ..browser_state import browser_manager, send_with_retries, click_first_selector
from ..config import QUIZ_NAV_SELECTORS

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
        return (
            f"Could not trigger '{action}' navigation. "
            "Try browser_execute_actions with a page-specific selector."
        )

    await asyncio.sleep(1.5)
    after_q, after_total = await get_quiz_position(tab.tab_id)

    if action in {"next", "previous"} and before_q is not None and after_q is not None and after_q == before_q:
        return (
            f"Triggered '{action}' but question did not change "
            f"(still {after_q}/{after_total or before_total or '?'})"
        )

    if after_q is not None:
        return f"Navigated {action}. Now on Question {after_q}/{after_total or '?'}"

    return f"Triggered navigation action: {action}"
