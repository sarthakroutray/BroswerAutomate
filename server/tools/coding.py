"""
coding.py — Coding-platform tool handlers.

Tools:
  browser_get_coding_problem — extract problem statement, I/O, constraints, samples
  browser_set_code_editor    — inject code into editor (ACE/Monaco/CodeMirror)
  browser_get_code_editor    — read current code from editor
  browser_compile_and_run    — click compile, wait for results, return test outcomes
  browser_get_test_results   — parse test results from page
  browser_submit_solution    — click submit and capture results
"""

import re
import json
import asyncio
import logging
import html as html_module
from typing import Optional

from ..browser_state import browser_manager, send_with_retries, click_first_selector
from ..config import COMPILE_BUTTON_SELECTORS, SUBMIT_BUTTON_SELECTORS
from ..errors import format_tool_result
from ..policy import evaluate_action

logger = logging.getLogger("browser-agent")


def parse_compilation_summary(full_text: str) -> dict:
    output = {"status": "unknown", "passed": 0, "total": 0, "compiler_errors": [], "test_cases": []}
    result_match = re.search(r'(\d+)/(\d+)\s+(?:Sample\s+)?[Tt]estcase(?:s)?\s+[Pp]assed', full_text)
    if result_match:
        output["passed"] = int(result_match.group(1))
        output["total"] = int(result_match.group(2))
        output["status"] = "all_passed" if output["passed"] == output["total"] else "some_failed"

    passed_match = re.search(r'(\d+)/(\d+)\s+Testcases\s+Passed', full_text)
    if passed_match:
        output["passed"] = int(passed_match.group(1))
        output["total"] = int(passed_match.group(2))
        output["status"] = "all_passed" if output["passed"] == output["total"] else "some_failed"

    error_match = re.search(r'Compiler Message\s+(.+?)(?=Sample Testcase|Testcase|\Z)', full_text, re.DOTALL)
    if error_match:
        error_text = error_match.group(1).strip()
        if error_text and 'error' in error_text.lower():
            output["status"] = "compilation_error"
            output["compiler_errors"] = [line.strip() for line in error_text.split('\n') if line.strip()][:15]

    for tc_num, tc_status in re.findall(r'Testcase\s+(\d+)\s*[-\u2013]\s*(Passed|Failed)', full_text, re.IGNORECASE):
        output["test_cases"].append({"number": tc_num, "status": tc_status})

    ea_pairs = re.findall(r'Expected Output\s+(.+?)(?:Your Output|Output)\s+(.+?)(?=Testcase|\Z)', full_text, re.DOTALL)
    for idx, (expected, actual) in enumerate(ea_pairs):
        if idx < len(output["test_cases"]):
            output["test_cases"][idx]["expected"] = expected.strip()[:300]
            output["test_cases"][idx]["actual"] = actual.strip()[:300]
    return output


async def handle_get_coding_problem(tab_id: Optional[str] = None) -> str:
    """Extract coding problem from the page: statement, I/O format, constraints, samples, language.

    USE THIS TOOL:
    - To read and understand a coding problem before writing a solution
    - To get sample test cases for validation
    - To detect the programming language and editor type

    DO NOT USE THIS TOOL:
    - For non-coding pages (use browser_extract_text instead)
    - After you already have the problem data

    Returns: JSON with problem_statement, input_format, output_format, constraints,
             sample_inputs, sample_outputs, language, and editor info.
    """
    tab = browser_manager.resolve_tab(tab_id)

    structured = {}
    try:
        structured = await send_with_retries(
            {"type": "EXTRACT_CODING_PROBLEM", "tab_id": tab.tab_id}, timeout=15.0,
        )
    except Exception:
        pass

    resp = await send_with_retries(
        {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "", "selector": ""}, timeout=15.0,
    )
    full_text = structured.get("full_text", "") or resp.get("text", "")

    raw_resp = await send_with_retries({"type": "REQUEST_HTML", "tab_id": tab.tab_id}, timeout=15.0)
    raw_html = raw_resp.get("html", "")
    pre_contents = re.findall(r'<pre[^>]*>(.*?)</pre>', raw_html, re.DOTALL)
    pre_texts = [html_module.unescape(re.sub(r'<[^>]+>', '', p)).strip() for p in pre_contents]

    problem = {
        "url": structured.get("url", ""),
        "title": structured.get("title", ""),
        "question_number": "",
        "total_questions": "",
        "problem_statement": structured.get("problem_statement", ""),
        "input_format": structured.get("input_format", ""),
        "output_format": structured.get("output_format", ""),
        "constraints": structured.get("constraints", ""),
        "sample_inputs": structured.get("sample_inputs", []),
        "sample_outputs": structured.get("sample_outputs", []),
        "pre_blocks": pre_texts,
        "language": structured.get("detected_language", "") or "C",
        "editor_type": structured.get("editor_type", ""),
        "has_editor": structured.get("has_editor", False),
        "compile_button": structured.get("compile_button", ""),
        "submit_button": structured.get("submit_button", ""),
    }

    q_match = re.search(r'Question\s*No\s*:?\s*(\d+)\s*/\s*(\d+)', full_text)
    if q_match:
        problem["question_number"] = q_match.group(1)
        problem["total_questions"] = q_match.group(2)

    if not problem["problem_statement"]:
        sm = re.search(r'Problem Statement\s*[:\-]?\s*(.+?)(?=Input format|Marks|$)', full_text, re.DOTALL)
        if sm:
            problem["problem_statement"] = sm.group(1).strip()

    if not problem["input_format"]:
        im = re.search(r'Input format\s*[:\-]?\s*(.+?)(?=Output format|Code constraints|$)', full_text, re.DOTALL)
        if im:
            problem["input_format"] = im.group(1).strip()

    if not problem["output_format"]:
        om = re.search(r'Output format\s*[:\-]?\s*(.+?)(?=Code constraints|Sample test|$)', full_text, re.DOTALL)
        if om:
            problem["output_format"] = om.group(1).strip()

    if not problem["constraints"]:
        cm = re.search(r'Code constraints\s*[:\-]?\s*(.+?)(?=Sample test|$)', full_text, re.DOTALL)
        if cm:
            problem["constraints"] = cm.group(1).strip()

    if not problem["sample_inputs"]:
        ss = re.search(r'Sample test cases\s*:?\s*(.+?)(?=Note\s*:|Fill your code|Marks\s*:|$)', full_text, re.DOTALL)
        if ss:
            io_pairs = re.findall(
                r'Input\s*\d+\s*:?\s*(.+?)Output\s*\d+\s*:?\s*(.+?)(?=Input\s*\d+\s*:|$)',
                ss.group(1), re.DOTALL,
            )
            for inp, out in io_pairs:
                problem["sample_inputs"].append(inp.strip())
                problem["sample_outputs"].append(out.strip())

    if problem["language"] == "C" and not structured.get("detected_language"):
        lm = re.search(r'(?:C\+\+|Python|Java|JavaScript|C)\s*(?:\(\d+\)|\(GCC\))', full_text)
        if lm:
            problem["language"] = lm.group(0).split('(')[0].strip()

    problem["full_text"] = full_text[:6000]
    return format_tool_result(
        status="success",
        code="CODING_PROBLEM_READY",
        message="Extracted coding problem",
        data={"tab_id": tab.tab_id, "problem": problem},
    )


async def handle_set_code_editor(code: str, tab_id: Optional[str] = None) -> str:
    """Insert complete source code into the active code editor (ACE/Monaco/CodeMirror).

    USE THIS TOOL:
    - To inject a solution into the code editor
    - After reading the problem with browser_get_coding_problem
    - To replace existing code in the editor

    DO NOT USE THIS TOOL:
    - For non-code text input (use browser_execute_actions with type action)
    - Without first understanding the problem

    Args:
        code: Complete source code to insert. Must be valid for the target language.

    Returns: Confirmation with line count and verification status.
    """
    tab = browser_manager.resolve_tab(tab_id)
    if not code:
        return format_tool_result(
            status="error",
            code="MISSING_REQUIRED_FIELD",
            message="code required",
            data={"tab_id": tab.tab_id},
        )

    resp = await browser_manager.send(
        {"type": "SET_CODE", "code": code, "tab_id": tab.tab_id}, timeout=15.0,
    )
    success = resp.get("success", False)
    msg = resp.get("message", "Unknown")
    lines = resp.get("lines", 0)

    if not success:
        return format_tool_result(
            status="error",
            code="SET_CODE_FAILED",
            message=f"Failed to set code: {msg}",
            data={"tab_id": tab.tab_id, "lines": lines},
        )

    await asyncio.sleep(0.5)

    try:
        verify_resp = await send_with_retries({"type": "GET_CODE", "tab_id": tab.tab_id}, timeout=10.0)
        verify_code = verify_resp.get("code", "")
        verify_lines = len(verify_code.strip().split('\n')) if verify_code.strip() else 0
        expected_lines = len(code.strip().split('\n'))
        if verify_lines >= expected_lines - 2:
            return format_tool_result(
                status="success",
                code="CODE_SET",
                message=f"Code inserted via {msg}. Verified {verify_lines} lines.",
                data={"tab_id": tab.tab_id, "lines": lines, "chars": len(code), "verified_lines": verify_lines},
            )
        return format_tool_result(
            status="error",
            code="CODE_VERIFICATION_FAILED",
            message=f"Expected {expected_lines} lines but editor has {verify_lines}. Try again.",
            data={"tab_id": tab.tab_id, "expected_lines": expected_lines, "verified_lines": verify_lines},
        )
    except Exception:
        return format_tool_result(
            status="success",
            code="CODE_SET",
            message=f"Code inserted ({lines} lines, {len(code)} chars) -- {msg}",
            data={"tab_id": tab.tab_id, "lines": lines, "chars": len(code), "verified": False},
        )


async def handle_get_code_editor(tab_id: Optional[str] = None) -> str:
    """Read the current code from the active code editor (ACE/Monaco/CodeMirror).

    USE THIS TOOL:
    - To verify what code is currently in the editor
    - To read existing code before making modifications
    - To check if code injection was successful

    DO NOT USE THIS TOOL:
    - For page text extraction (use browser_extract_text)

    Returns: The current source code in the editor with line count.
    """
    tab = browser_manager.resolve_tab(tab_id)
    resp = await send_with_retries({"type": "GET_CODE", "tab_id": tab.tab_id}, timeout=10.0)
    success = resp.get("success", False)
    code = resp.get("code", "")
    editor = resp.get("editor", "unknown")
    if not success:
        return format_tool_result(
            status="error",
            code="GET_CODE_FAILED",
            message=f"Could not read editor: {resp.get('message', 'unknown error')}",
            data={"tab_id": tab.tab_id, "editor": editor},
        )
    line_count = len(code.split('\n')) if code else 0
    return format_tool_result(
        status="success",
        code="CODE_READY",
        message=f"Read code editor '{editor}'",
        data={"tab_id": tab.tab_id, "editor": editor, "line_count": line_count, "code": code},
    )


async def handle_compile_and_run(
    wait_time: int = 10,
    tab_id: Optional[str] = None,
) -> str:
    """Click 'Compile & Run' button, wait for results, return compilation status and test case outcomes.

    USE THIS TOOL:
    - After injecting code with browser_set_code_editor
    - To test your solution against sample test cases
    - To check for compilation errors

    DO NOT USE THIS TOOL:
    - Before writing code (inject code first)
    - For final submission (use browser_submit_solution)

    Args:
        wait_time: Seconds to wait for compilation results (default 10, max 45).

    Returns: Compilation status, test case pass/fail counts, and error details.
    """
    tab = browser_manager.resolve_tab(tab_id)
    wait_time = max(2, min(wait_time, 45))

    await asyncio.sleep(1.0)

    clicked = await click_first_selector(tab.tab_id, COMPILE_BUTTON_SELECTORS, timeout=5.0)
    if not clicked:
        return format_tool_result(
            status="error",
            code="COMPILE_TRIGGER_FAILED",
            message="Could not find compile/run button",
            data={"tab_id": tab.tab_id},
        )

    full_text = ""
    for _ in range(max(1, wait_time // 2)):
        await asyncio.sleep(2)
        results_resp = await send_with_retries(
            {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "", "selector": ""}, timeout=15.0,
        )
        full_text = results_resp.get("text", "")
        if re.search(r'(\d+)/(\d+)\s+(?:Sample\s+)?[Tt]estcase(?:s)?\s+[Pp]assed', full_text):
            break
        if re.search(r'Compiler Message|Compilation Error|\berror\b', full_text, re.IGNORECASE):
            break

    output = parse_compilation_summary(full_text)
    return format_tool_result(
        status="success" if output["status"] != "compilation_error" else "error",
        code="COMPILE_RESULTS_READY" if output["status"] != "compilation_error" else "COMPILATION_ERROR",
        message=f"Compilation status: {output['status']}",
        data={"tab_id": tab.tab_id, "results": output},
    )


async def handle_get_test_results(tab_id: Optional[str] = None) -> str:
    """Parse test results from page: passed/failed count, error messages, expected vs actual output.

    USE THIS TOOL:
    - To re-check test results after compilation
    - When browser_compile_and_run results were unclear
    - To get detailed expected vs actual output comparison

    DO NOT USE THIS TOOL:
    - Before compiling (use browser_compile_and_run first)

    Returns: JSON with passed/failed counts, compiler errors, and per-test-case details.
    """
    tab = browser_manager.resolve_tab(tab_id)
    resp = await browser_manager.send(
        {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "", "selector": ""},
    )
    full_text = resp.get("text", "")

    results = {"passed": 0, "failed": 0, "total": 0, "compiler_errors": [], "test_cases": []}
    rm = re.search(r'Result\s+(\d+)/(\d+)\s+(?:Sample\s+)?testcase(?:s)?\s+passed', full_text, re.IGNORECASE)
    if rm:
        results["passed"] = int(rm.group(1))
        results["total"] = int(rm.group(2))
        results["failed"] = results["total"] - results["passed"]

    em = re.search(r'Compiler Message\s+(.+?)(?=Sample Testcase|Result|$)', full_text, re.DOTALL)
    if em:
        results["compiler_errors"] = em.group(1).strip().split('\n')[:10]

    for tc_num, status, expected, actual in re.findall(
        r'Testcase\s+(\d+)\s+-\s+(Passed|Failed)\s+Expected Output\s+(.+?)Output\s+(.+?)(?=Testcase|$)',
        full_text, re.DOTALL,
    ):
        results["test_cases"].append({
            "number": tc_num, "status": status,
            "expected": expected.strip()[:200], "actual": actual.strip()[:200],
        })
    return format_tool_result(
        status="success",
        code="TEST_RESULTS_READY",
        message="Parsed test results",
        data={"tab_id": tab.tab_id, "results": results},
    )


async def handle_submit_solution(tab_id: Optional[str] = None, allow_unsafe: bool = False) -> str:
    """Submit the coding solution. Clicks 'Submit Code' button and captures submission result.

    USE THIS TOOL:
    - After verifying code passes sample tests with browser_compile_and_run
    - As the final step in the coding workflow

    DO NOT USE THIS TOOL:
    - Before testing the code (compile first)
    - For quiz submission (use browser_navigate_quiz with action='submit')

    Returns: Submission status with test case pass/fail results.
    """
    tab = browser_manager.resolve_tab(tab_id)
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

    clicked = await click_first_selector(tab.tab_id, SUBMIT_BUTTON_SELECTORS, timeout=5.0)
    if not clicked:
        return format_tool_result(
            status="error",
            code="SUBMIT_TRIGGER_FAILED",
            message="Could not find submit button",
            data={"tab_id": tab.tab_id},
        )

    await asyncio.sleep(5)
    results_resp = await send_with_retries(
        {"type": "EXTRACT_TEXT", "tab_id": tab.tab_id, "query": "", "selector": ""}, timeout=15.0,
    )
    full_text = results_resp.get("text", "")

    result = {"status": "submitted", "passed": 0, "total": 0, "test_details": []}
    pm = re.search(r'(\d+)/(\d+)\s+[Tt]estcase(?:s)?\s+[Pp]assed', full_text)
    if pm:
        result["passed"] = int(pm.group(1))
        result["total"] = int(pm.group(2))

    if 'Compilation failed' in full_text or ('Compiler Message' in full_text and 'error' in full_text.lower()):
        result["status"] = "compilation_error"

    for tc_num, tc_status in re.findall(
        r'(?:Test Case|Testcase)\s*(\d+).*?(Passed|Failed|Compilation failed)', full_text, re.IGNORECASE,
    ):
        result["test_details"].append({"case": tc_num, "status": tc_status})
    return format_tool_result(
        status="success" if result["status"] != "compilation_error" else "error",
        code="SUBMIT_COMPLETE" if result["status"] != "compilation_error" else "SUBMIT_ERROR",
        message=f"Submission status: {result['status']}",
        data={"tab_id": tab.tab_id, "results": result},
    )
