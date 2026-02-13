"""
quiz.py — Quiz-related tool handlers.

Tools: quiz_extract, quiz_answer, quiz_navigate
(browser_solve_quiz logic moves to agent orchestrator)
"""

import re
import json
import asyncio
import logging
from typing import Optional

from ..browser_state import browser_manager, send_with_retries, click_first_selector
from ..config import QUIZ_NAV_SELECTORS

logger = logging.getLogger("browser-agent")

try:
    from mcp.types import TextContent
except ImportError:
    from dataclasses import dataclass
    @dataclass
    class TextContent:
        type: str
        text: str


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


async def handle_quiz_extract(arguments: dict) -> list:
    """quiz_extract — extract structured quiz data."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))

    quiz_data = {}
    try:
        quiz_resp = await send_with_retries(
            {"type": "EXTRACT_QUIZ_STRUCTURE", "tab_id": tab.tab_id}, timeout=8.0,
        )
        if quiz_resp and not quiz_resp.get("error"):
            quiz_data = quiz_resp
    except Exception:
        pass

    if not quiz_data or not quiz_data.get("questions"):
        try:
            resp = await browser_manager.send({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
            dom = resp.get("dom_state", {})
            if dom:
                browser_manager.update_dom(tab.tab_id, dom)
        except Exception:
            dom = tab.dom_state or {}

        fallback_data = {"question": "", "options": [], "question_number": "", "total_questions": ""}

        text_resp = await browser_manager.send(
            {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "Question", "selector": ""},
        )
        full_text = text_resp.get("text", "")

        q_match = re.search(r'Question\s+No\s*:\s*(\d+)\s*/\s*(\d+)', full_text)
        if q_match:
            fallback_data["question_number"] = q_match.group(1)
            fallback_data["total_questions"] = q_match.group(2)

        q_lines = []
        in_question = False
        for line in full_text.split('\n'):
            if 'Multi Choice' in line or 'Single File' in line or 'Problem Statement' in line:
                in_question = True
                continue
            if in_question and ('Marks :' in line or 'Input format' in line or 'Output format' in line):
                break
            if in_question and line.strip():
                q_lines.append(line.strip())
        fallback_data["question"] = ' '.join(q_lines)

        radio_groups = dom.get("radios", {}) if isinstance(dom, dict) else {}
        for group_name, buttons in radio_groups.items():
            for btn in buttons:
                fallback_data["options"].append({
                    "text": btn.get("text", ""),
                    "selector": btn.get("selector", ""),
                    "value": btn.get("value", ""),
                    "selected": btn.get("checked", False),
                })

        fallback_data["full_context"] = full_text[:2000]
        quiz_data = fallback_data

    return [TextContent(type="text", text=json.dumps(quiz_data, indent=2))]


async def handle_quiz_answer(arguments: dict) -> list:
    """quiz_answer — click quiz answer option."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    selector = arguments.get("selector")
    if not selector:
        return [TextContent(type="text", text="Error: selector required")]

    await browser_manager.send({
        "type": "EXECUTE_ACTIONS", "tab_id": tab.tab_id,
        "steps": [{"action": "click", "selector": selector}],
    })

    await asyncio.sleep(0.5)

    try:
        dom_resp = await browser_manager.send({"type": "REQUEST_DOM", "tab_id": tab.tab_id})
        dom = dom_resp.get("dom_state", {})
        selected = "Unknown"
        for group_name, buttons in dom.get("radios", {}).items():
            for btn in buttons:
                if btn.get("checked"):
                    selected = btn.get("text", "")
                    break
        return [TextContent(type="text", text=f"Selected: {selected}\nAnswered: 1 question")]
    except Exception:
        return [TextContent(type="text", text="Option selected successfully")]


async def handle_quiz_navigate(arguments: dict) -> list:
    """quiz_navigate — Next / Previous / Submit."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    action = arguments.get("action", "next").lower()
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
        return [TextContent(
            type="text",
            text=f"Could not trigger '{action}' navigation. Try browser_act with a page-specific selector.",
        )]

    await asyncio.sleep(1.5)
    after_q, after_total = await get_quiz_position(tab.tab_id)

    if action in {"next", "previous"} and before_q is not None and after_q is not None and after_q == before_q:
        return [TextContent(
            type="text",
            text=f"Triggered '{action}' but question did not change (still {after_q}/{after_total or before_total or '?'})",
        )]

    if after_q is not None:
        return [TextContent(type="text", text=f"Navigated {action}. Now on Question {after_q}/{after_total or '?'}")]

    return [TextContent(type="text", text=f"Triggered navigation action: {action}")]
