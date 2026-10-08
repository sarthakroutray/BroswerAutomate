"""
server.solvers.coding — Solve coding challenges end-to-end.

Strategy
--------
1. Call EXTRACT_PAGE_CONTEXT to get problem statement, I/O, constraints,
   samples, detected language, and editor info.
2. For each attempt (up to max_attempts):
   a. Ask the LLM for a complete solution in the detected language.
      Provide the problem text, constraints, samples, the previous attempt
      (if any), and the compiler output.
   b. Strip markdown fences if the LLM emitted them.
   c. Inject the code via browser_set_code_editor.
   d. Click Compile & Run via browser_compile_and_run.
   e. Parse the result. If all tests pass, submit. Otherwise, loop with the
      compiler output as feedback.
3. Final submit (best-effort).

The LLM call here is also focused — no screenshot, no full DOM — so it
gets much better code than the generic browser_run_task loop, which has to
fit the problem text plus the entire DOM dump plus a screenshot into a
single prompt.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from ..browser_state import browser_manager, send_with_retries

logger = logging.getLogger("browser-agent")


CODING_SOLVE_SYSTEM_PROMPT = """You are an expert competitive programmer solving a coding challenge.

Given the problem statement, constraints, sample input/output, the detected
programming language, and (on retries) the previous attempt + compiler
output, produce a complete solution.

Output strict JSON in this shape:

{
  "language": "<C|C++|Java|Python|JavaScript|...>",
  "code": "<complete source code as a single string — use \\n for newlines>",
  "explanation": "<one short sentence explaining your approach>"
}

Rules:
- Output ONLY raw source code inside the JSON string. No markdown fences.
- The code must read from stdin and write to stdout, matching the exact
  output format shown in the samples (every space, every newline matters).
- Handle edge cases. If the previous attempt failed, fix the actual error
  reported by the compiler — do not rewrite from scratch unless necessary.
- Match the detected language. If you must switch, note it in explanation.
- For C/C++ include `#include <stdio.h>` (or the equivalent) — full boilerplate.
- For Java include the class declaration matching the platform's expectations
  (e.g. class Main { public static void main(String[] args) { ... } })."""


def _strip_code_fences(text: str) -> str:
    """LLMs sometimes wrap code in markdown fences even when told not to.
    Strip leading ```python / ```c / ``` blocks."""
    text = text.strip()
    fence = re.match(r"^```(?:[a-zA-Z+#-]+)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


async def _extract_coding_context(tab_id: str) -> dict:
    resp = await send_with_retries({"type": "EXTRACT_PAGE_CONTEXT", "tab_id": tab_id}, timeout=15.0)
    if resp.get("error"):
        return {"kind": "unknown", "error": resp["error"]}
    return resp


def _problem_to_prompt(problem: dict, previous_attempt: Optional[dict] = None) -> str:
    lines = []
    lines.append(f"Problem title: {problem.get('title', 'Unknown')}")
    if problem.get("question_number"):
        lines.append(f"Question: {problem['question_number']}"
                     + (f"/{problem['total_questions']}" if problem.get("total_questions") else ""))
    lang = problem.get("language") or "C"
    lines.append(f"Required language: {lang}")
    lines.append("")
    if problem.get("problem_statement"):
        lines.append("Problem statement:")
        lines.append(problem["problem_statement"].strip())
        lines.append("")
    if problem.get("input_format"):
        lines.append("Input format:")
        lines.append(problem["input_format"].strip())
        lines.append("")
    if problem.get("output_format"):
        lines.append("Output format:")
        lines.append(problem["output_format"].strip())
        lines.append("")
    if problem.get("constraints"):
        lines.append("Constraints:")
        lines.append(problem["constraints"].strip())
        lines.append("")
    if problem.get("sample_inputs") or problem.get("sample_outputs"):
        lines.append("Sample test cases:")
        si = problem.get("sample_inputs") or []
        so = problem.get("sample_outputs") or []
        for idx in range(max(len(si), len(so))):
            if idx < len(si):
                lines.append(f"  Input {idx + 1}:\n{si[idx]}")
            if idx < len(so):
                lines.append(f"  Output {idx + 1}:\n{so[idx]}")
        lines.append("")
    if problem.get("pre_blocks"):
        lines.append("Code-block hints from page (may include starter code or examples):")
        for i, block in enumerate(problem["pre_blocks"][:8]):
            if isinstance(block, str):
                lines.append(f"  --- pre #{i + 1} ---\n{block[:1500]}")
            elif isinstance(block, dict):
                lines.append(f"  --- pre #{i + 1} ---\n{block.get('text', '')[:1500]}")
        lines.append("")
    if previous_attempt:
        lines.append("Previous attempt (failed):")
        lines.append(f"  Language: {previous_attempt.get('language', '?')}")
        lines.append(f"  Code:\n{previous_attempt.get('code', '')}")
        lines.append("")
        lines.append("Compiler / runtime output:")
        lines.append(previous_attempt.get("compile_output", "(no output captured)"))
        lines.append("")
        if previous_attempt.get("passed") is not None and previous_attempt.get("total"):
            lines.append(f"Last test result: {previous_attempt['passed']}/{previous_attempt['total']} passed.")
            lines.append("")
        lines.append("Please fix the code and return a new full solution.")
    lines.append("Respond with strict JSON only.")
    return "\n".join(lines)


def _validate_code_solution(parsed: dict, expected_language: str) -> tuple[Optional[str], Optional[str], str]:
    code = parsed.get("code")
    if not isinstance(code, str) or not code.strip():
        return None, None, "LLM response missing or empty 'code'"
    code = _strip_code_fences(code)
    # Don't strictly enforce language match — sometimes LLM switches to Python
    # when asked for C. Just record what was used.
    lang = str(parsed.get("language") or expected_language or "unknown")
    explanation = str(parsed.get("explanation", ""))[:400]
    return code, lang, explanation


async def _inject_code(tab_id: str, code: str) -> dict:
    """Set the code in the active editor via the existing tool."""
    try:
        resp = await send_with_retries(
            {"type": "SET_CODE", "code": code, "tab_id": tab_id}, timeout=15.0,
        )
        return resp
    except Exception as e:
        return {"success": False, "message": f"SET_CODE failed: {e}"}


async def _compile_and_run(tab_id: str, wait_seconds: int = 12) -> dict:
    """Click compile, wait for results, parse the result text."""
    # Use the existing handle_compile_and_run logic via WS messages so we
    # stay on the same code path. This is what the existing tool does.
    from ..tools.coding import COMPILE_BUTTON_SELECTORS, parse_compilation_summary
    from ..browser_state import click_first_selector
    clicked = await click_first_selector(tab_id, COMPILE_BUTTON_SELECTORS, timeout=5.0)
    if not clicked:
        return {"status": "click_failed"}
    import asyncio
    full_text = ""
    for _ in range(max(1, wait_seconds // 2)):
        await asyncio.sleep(2)
        results_resp = await send_with_retries(
            {"type": "EXTRACT_TEXT", "tab_id": tab_id, "query": "", "selector": ""}, timeout=15.0,
        )
        full_text = results_resp.get("text", "")
        if re.search(r'(\d+)/(\d+)\s+(?:Sample\s+)?[Tt]estcase(?:s)?\s+[Pp]assed', full_text):
            break
        if re.search(r'Compiler Message|Compilation Error|\berror\b', full_text, re.IGNORECASE):
            break
    parsed = parse_compilation_summary(full_text)
    parsed["_full_text"] = full_text[:6000]
    return parsed


async def solve_coding(
    *,
    tab_id: Optional[str] = None,
    max_attempts: int = 5,
    llm,
    allow_unsafe: bool = False,
) -> dict:
    tab = browser_manager.resolve_tab(tab_id)
    log: list[dict] = []

    ctx = await _extract_coding_context(tab.tab_id)
    if ctx.get("kind") not in {"coding", "mixed"}:
        return {
            "status": "error",
            "code": "NOT_A_CODING_PROBLEM",
            "message": f"Page kind is '{ctx.get('kind')}', not a coding challenge.",
            "data": {"page_kind": ctx.get("kind")},
        }
    if ctx.get("blockers", {}).get("has_login"):
        return {"status": "error", "code": "LOGIN_REQUIRED", "message": "Login required first.", "data": {}}
    if not ctx.get("coding", {}).get("has_editor"):
        return {
            "status": "error",
            "code": "NO_EDITOR",
            "message": "No supported code editor (ACE/Monaco/CodeMirror/textarea) found on this page.",
            "data": {"editor_type": ctx.get("coding", {}).get("editor_type")},
        }

    problem = ctx["coding"]
    language = problem.get("language") or "C"
    log.append({"phase": "extracted", "language": language, "title": problem.get("title"),
                "has_samples": bool(problem.get("sample_inputs") or problem.get("sample_outputs"))})

    from ..llm.provider import parse_ai_response

    previous = None
    final_solution = None
    for attempt in range(1, max_attempts + 1):
        prompt = _problem_to_prompt(problem, previous_attempt=previous)
        try:
            raw = await llm.ask(text=prompt, system_prompt=CODING_SOLVE_SYSTEM_PROMPT, max_tokens=4096)
        except Exception as e:
            log.append({"phase": "llm_failed", "attempt": attempt, "error": str(e)})
            break

        parsed = parse_ai_response(raw)
        code, used_language, explanation = _validate_code_solution(parsed, language)
        if not code:
            log.append({"phase": "parse_failed", "attempt": attempt, "raw_response": raw[:500]})
            continue

        log.append({"phase": "llm_returned", "attempt": attempt, "language": used_language,
                    "explanation": explanation, "code_length": len(code)})

        # Inject
        inject_resp = await _inject_code(tab.tab_id, code)
        if not inject_resp.get("success"):
            log.append({"phase": "inject_failed", "attempt": attempt, "error": inject_resp.get("message")})
            previous = {"language": used_language, "code": code, "compile_output": "code injection failed"}
            continue
        log.append({"phase": "injected", "attempt": attempt, "lines": inject_resp.get("lines")})

        # Compile & run
        result = await _compile_and_run(tab.tab_id)
        status = result.get("status", "unknown")
        passed = result.get("passed", 0)
        total = result.get("total", 0)
        compile_output = result.get("_full_text", "")[:3000]
        log.append({"phase": "compiled", "attempt": attempt, "status": status, "passed": passed, "total": total})

        if status == "all_passed":
            final_solution = {
                "language": used_language,
                "code": code,
                "explanation": explanation,
                "passed": passed,
                "total": total,
                "attempts": attempt,
            }
            break

        # Save for next iteration's feedback
        previous = {
            "language": used_language,
            "code": code,
            "compile_output": compile_output,
            "passed": passed,
            "total": total,
        }

    # Submit if we got a passing solution
    submit_status = "skipped"
    if final_solution is not None:
        try:
            from ..tools.coding import SUBMIT_BUTTON_SELECTORS
            from ..browser_state import click_first_selector
            from ..policy import evaluate_action
            tab_now = browser_manager.resolve_tab(tab.tab_id)
            decision = evaluate_action(
                action="submit",
                current_url=tab_now.url or ((tab_now.dom_state or {}).get("url", "")),
                allow_unsafe=allow_unsafe,
            )
            if decision.allowed:
                clicked = await click_first_selector(tab.tab_id, SUBMIT_BUTTON_SELECTORS, timeout=5.0)
                submit_status = "submitted" if clicked else "click_failed"
            else:
                submit_status = f"policy_blocked: {decision.message}"
        except Exception as e:
            submit_status = f"failed: {e}"

    return {
        "status": "success" if final_solution else "error",
        "code": "CODING_SOLVED" if final_solution else "CODING_SOLVER_FAILED",
        "message": (
            f"Solved in {final_solution['attempts']} attempt(s); {final_solution['passed']}/{final_solution['total']} passed"
            if final_solution else
            f"Failed to find a passing solution in {max_attempts} attempt(s)."
        ),
        "data": {
            "language": language,
            "final_solution": final_solution,
            "submit": submit_status,
            "log": log,
        },
    }