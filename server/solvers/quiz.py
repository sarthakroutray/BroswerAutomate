"""
server.solvers.quiz — Solve quiz pages end-to-end.

Strategy
--------
1. Call EXTRACT_PAGE_CONTEXT to get the quiz structure (questions, options, selectors).
2. For each question, ask the LLM ONE focused call with just the question and
   options (no screenshot, no DOM dump). The LLM returns a JSON pointer to the
   option it picks + a confidence score + a one-line reason.
3. Click that option via browser_execute_actions.
4. Click Next (or wait for the page to advance on its own).
5. Re-extract and repeat. Stop when we've gone through `total_questions` or
   when the page no longer contains a Next button.
6. Click Submit.

This bypasses the generic browser_run_task loop because quizzes have a fixed
structure and the LLM performs much better when each call is a tight
"here's a question, pick one option" rather than "here's 100k of DOM, do
whatever you think is right".
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from ..browser_state import browser_manager, send_with_retries
from ..tools.observation import handle_get_page_state
from ..tools.quiz import handle_navigate_quiz

logger = logging.getLogger("browser-agent")


QUIZ_ANSWER_SYSTEM_PROMPT = """You are an expert quiz solver. For each question, you will be given:
- The question text (and any context like a code block or passage)
- The available answer options, each with a short label, value, and CSS selector

Your job: pick the SINGLE best answer. Output strict JSON in this exact shape:

{
  "answer_index": <0-based index of the chosen option>,
  "confidence": <float 0.0–1.0, your certainty>,
  "reason": "<one short sentence explaining your reasoning>"
}

Rules:
- For single-choice questions, pick exactly one option.
- For multi-choice questions, return answer_index pointing to ONE option; the
  caller will detect that more options need selecting and ask again with the
  remaining choices.
- Read the question carefully. If options contain code, trace through it.
- If you're uncertain, set confidence below 0.5 and the caller will skip.
- NEVER invent options. Use only what you're given.
- Output ONLY the JSON object — no markdown fences, no preamble."""


async def _extract_quiz_context(tab_id: str) -> dict:
    """Call EXTRACT_PAGE_CONTEXT and pull out the quiz-relevant fields."""
    resp = await send_with_retries({"type": "EXTRACT_PAGE_CONTEXT", "tab_id": tab_id}, timeout=15.0)
    if resp.get("error"):
        return {"kind": "unknown", "error": resp["error"]}
    return resp


def _question_to_prompt(question: dict, quiz_meta: dict, index: int, total: int) -> str:
    """Format a single question + options as a focused LLM prompt."""
    lines = []
    lines.append(f"Quiz: {quiz_meta.get('title', 'Unknown')}")
    lines.append(f"Question {index + 1} of {total}")
    lines.append("")
    if question.get("question_text"):
        lines.append("Question:")
        lines.append(question["question_text"].strip())
        lines.append("")
    lines.append(f"Type: {question.get('type', 'single_choice')}")
    lines.append("Options:")
    for i, opt in enumerate(question.get("options", [])):
        text = (opt.get("text") or "").strip()[:300]
        sel = opt.get("selector") or "(no selector)"
        sel_type = opt.get("type", "?")
        selected_marker = " [currently selected]" if opt.get("selected") else ""
        lines.append(f"  [{i}] {text}{selected_marker}  (selector: {sel}, type: {sel_type})")
    lines.append("")
    lines.append("Respond with strict JSON only.")
    return "\n".join(lines)


async def _ask_llm_for_answer(llm, question: dict, quiz_meta: dict, index: int, total: int) -> dict:
    """One focused LLM call for one question."""
    prompt = _question_to_prompt(question, quiz_meta, index, total)
    raw = await llm.ask(
        text=prompt,
        system_prompt=QUIZ_ANSWER_SYSTEM_PROMPT,
        max_tokens=512,
    )
    # Parse JSON response. The provider's parse_ai_response is more lenient, but
    # we want stricter shape validation here.
    from ..llm.provider import parse_ai_response
    parsed = parse_ai_response(raw)
    return parsed


def _validate_answer(parsed: dict, num_options: int) -> tuple[Optional[int], float, str]:
    idx = parsed.get("answer_index")
    if not isinstance(idx, int) or idx < 0 or idx >= num_options:
        return None, 0.0, str(parsed.get("reason", "no answer_index"))
    try:
        conf = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    reason = str(parsed.get("reason", ""))[:300]
    return idx, conf, reason


async def _click_option(tab_id: str, selector: str) -> bool:
    """Click a quiz option by its CSS selector. Returns True if successful."""
    try:
        resp = await browser_manager.send(
            {"type": "EXECUTE_ACTIONS", "tab_id": tab_id,
             "steps": [{"action": "click", "selector": selector}]},
            timeout=8.0,
        )
        results = resp.get("result") or resp
        if isinstance(results, dict):
            results = results.get("results", [])
        if isinstance(results, list):
            return any(r.get("success") for r in results if isinstance(r, dict))
        return True
    except Exception as e:
        logger.warning(f"click_option failed for {selector}: {e}")
        return False


async def solve_quiz(
    *,
    tab_id: Optional[str] = None,
    max_questions: int = 50,
    confidence_threshold: float = 0.5,
    llm,
    allow_unsafe: bool = False,
) -> dict:
    """End-to-end quiz solver.

    Args:
        tab_id: tab to operate on; defaults to active tab.
        max_questions: hard cap on questions to attempt (default 50).
        confidence_threshold: skip questions where the LLM's confidence is below
            this (default 0.5). The question will be marked `skipped`.
        llm: LLMProvider instance.
        allow_unsafe: passed to navigate_quiz for the final Submit.

    Returns:
        Dict with: status, questions_attempted, answered, skipped,
                   per_question log, final_action, summary.
    """
    tab = browser_manager.resolve_tab(tab_id)
    log: list[dict] = []
    answered = 0
    skipped = 0

    ctx = await _extract_quiz_context(tab.tab_id)
    if ctx.get("kind") not in {"quiz", "mixed"}:
        return {
            "status": "error",
            "code": "NOT_A_QUIZ",
            "message": f"Page kind is '{ctx.get('kind')}', not a quiz. Use browser_run_task for generic flows.",
            "data": {"page_kind": ctx.get("kind"), "blockers": ctx.get("blockers")},
        }
    if ctx.get("blockers", {}).get("has_login"):
        return {"status": "error", "code": "LOGIN_REQUIRED", "message": "Page has a login form. Log in manually first.", "data": {}}
    if ctx.get("blockers", {}).get("has_captcha"):
        return {"status": "error", "code": "CAPTCHA_PRESENT", "message": "Captcha detected. Solve it manually first.", "data": {}}

    quiz = ctx.get("quiz") or {}
    total = quiz.get("total_questions") or 0
    title = quiz.get("title", "")
    questions = quiz.get("questions") or []
    if not questions:
        return {
            "status": "error",
            "code": "NO_QUESTIONS_FOUND",
            "message": "EXTRACT_PAGE_CONTEXT returned no questions. The page may not be a quiz, or extraction failed.",
            "data": {"quiz_raw_keys": list(quiz.keys())},
        }

    # If single-question pages, the questions list will have 1 entry and we'll
    # loop by clicking Next. If all-questions-on-one-page, the list has N
    # entries and we skip the Next loop.
    is_single_question_per_page = total > 1 and len(questions) == 1
    questions_total = total if total else len(questions)

    logger.info(f"Quiz solver: title='{title}', detected {len(questions)} question(s), total={questions_total}, single_question_per_page={is_single_question_per_page}")

    for q_index in range(min(questions_total, max_questions)):
        # Re-extract on every iteration so we always have fresh selectors and
        # current page state (the LLM may have been confused by stale selectors).
        ctx = await _extract_quiz_context(tab.tab_id)
        quiz = ctx.get("quiz") or {}
        questions = quiz.get("questions") or []
        if not questions:
            log.append({"q_index": q_index, "status": "no_questions", "detail": "page no longer has detectable questions"})
            break

        # Pick the first un-answered question
        question = None
        for q in questions:
            if not q.get("answered"):
                question = q
                break
        if question is None:
            # All questions on this page already answered (multi-question layout)
            break

        options = question.get("options") or []
        if not options:
            log.append({"q_index": q_index, "status": "no_options", "question": question.get("question_text", "")[:120]})
            break

        parsed = await _ask_llm_for_answer(llm, question, quiz, q_index, questions_total)
        idx, conf, reason = _validate_answer(parsed, len(options))
        if idx is None or conf < confidence_threshold:
            skipped += 1
            log.append({
                "q_index": q_index,
                "status": "skipped",
                "reason": reason,
                "confidence": conf,
                "question": question.get("question_text", "")[:120],
            })
        else:
            chosen = options[idx]
            clicked = await _click_option(tab.tab_id, chosen["selector"])
            if clicked:
                answered += 1
                log.append({
                    "q_index": q_index,
                    "status": "answered",
                    "answer_index": idx,
                    "confidence": conf,
                    "reason": reason,
                    "question": question.get("question_text", "")[:120],
                    "chosen_text": chosen.get("text", "")[:120],
                })
            else:
                # Click failed; try the next-best option if multi-choice
                log.append({
                    "q_index": q_index,
                    "status": "click_failed",
                    "answer_index": idx,
                    "confidence": conf,
                    "reason": reason,
                })

        # For multi-choice questions, the loop above only picks one option. If
        # the question is multi_choice and the LLM is willing, ask again for the
        # remaining options until it says "no more".
        if question.get("type") == "multi_choice" and idx is not None and conf >= confidence_threshold:
            remaining = [o for j, o in enumerate(options) if j != idx]
            while remaining:
                # Synthesize a synthetic single-choice question for the remaining options
                synth = {
                    "question_text": question.get("question_text", "") + "\n(Select all that apply — pick the next option.)",
                    "options": remaining,
                    "type": "single_choice",
                }
                parsed2 = await _ask_llm_for_answer(llm, synth, quiz, q_index, questions_total)
                # Map synthetic-index back to real index
                if isinstance(parsed2.get("answer_index"), int) and 0 <= parsed2["answer_index"] < len(remaining):
                    real_idx = options.index(remaining[parsed2["answer_index"]])
                    if await _click_option(tab.tab_id, options[real_idx]["selector"]):
                        answered += 1
                        log.append({"q_index": q_index, "status": "multi_picked", "answer_index": real_idx,
                                    "chosen_text": options[real_idx].get("text", "")[:120]})
                    remaining.pop(parsed2["answer_index"])
                else:
                    # LLM said "no more"
                    break

        # If this is a single-question-per-page quiz, click Next
        if is_single_question_per_page and q_index < questions_total - 1:
            nav_resp_raw = await handle_navigate_quiz(action="next", tab_id=tab.tab_id, allow_unsafe=False)
            try:
                nav_resp = json.loads(nav_resp_raw)
                if nav_resp.get("code") != "QUIZ_NAVIGATION_DONE":
                    log.append({"q_index": q_index, "status": "next_failed", "detail": nav_resp})
                    break
            except Exception:
                break

    # Final submit (best-effort; not all quizzes have one)
    submit_status = "skipped"
    try:
        submit_raw = await handle_navigate_quiz(action="submit", tab_id=tab.tab_id, allow_unsafe=allow_unsafe)
        submit_json = json.loads(submit_raw)
        submit_status = submit_json.get("code", "skipped")
    except Exception as e:
        submit_status = f"failed: {e}"

    summary = f"Answered {answered}, skipped {skipped} of {questions_total} question(s)."
    return {
        "status": "success",
        "code": "QUIZ_SOLVED",
        "message": summary,
        "data": {
            "quiz_title": title,
            "questions_total": questions_total,
            "answered": answered,
            "skipped": skipped,
            "submit": submit_status,
            "log": log,
        },
    }