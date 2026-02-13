"""
coding.py — Coding-platform tool handlers.

Tools: code_extract_problem, code_inject, code_read_editor,
       code_compile_run, code_get_results, code_submit
"""

import re
import json
import asyncio
import logging
import html as html_module

from ..browser_state import browser_manager, send_with_retries, click_first_selector
from ..config import COMPILE_BUTTON_SELECTORS, SUBMIT_BUTTON_SELECTORS

logger = logging.getLogger("browser-agent")

try:
    from mcp.types import TextContent
except ImportError:
    from dataclasses import dataclass
    @dataclass
    class TextContent:
        type: str
        text: str


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

    for tc_num, tc_status in re.findall(r'Testcase\s+(\d+)\s*[-–]\s*(Passed|Failed)', full_text, re.IGNORECASE):
        output["test_cases"].append({"number": tc_num, "status": tc_status})

    ea_pairs = re.findall(r'Expected Output\s+(.+?)(?:Your Output|Output)\s+(.+?)(?=Testcase|\Z)', full_text, re.DOTALL)
    for idx, (expected, actual) in enumerate(ea_pairs):
        if idx < len(output["test_cases"]):
            output["test_cases"][idx]["expected"] = expected.strip()[:300]
            output["test_cases"][idx]["actual"] = actual.strip()[:300]
    return output


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def handle_extract_problem(arguments: dict) -> list:
    """code_extract_problem — extract coding problem from page."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))

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
    return [TextContent(type="text", text=json.dumps(problem, indent=2))]


async def handle_code_inject(arguments: dict) -> list:
    """code_inject — inject code into editor."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    code = arguments.get("code", "")
    if not code:
        return [TextContent(type="text", text="Error: code required")]

    resp = await browser_manager.send(
        {"type": "SET_CODE", "code": code, "tab_id": tab.tab_id}, timeout=15.0,
    )
    success = resp.get("success", False)
    msg = resp.get("message", "Unknown")
    lines = resp.get("lines", 0)

    if not success:
        return [TextContent(type="text", text=f"Failed to set code: {msg}")]

    await asyncio.sleep(0.5)

    try:
        verify_resp = await send_with_retries({"type": "GET_CODE", "tab_id": tab.tab_id}, timeout=10.0)
        verify_code = verify_resp.get("code", "")
        verify_lines = len(verify_code.strip().split('\n')) if verify_code.strip() else 0
        expected_lines = len(code.strip().split('\n'))
        if verify_lines >= expected_lines - 2:
            return [TextContent(type="text", text=f"Code inserted ({lines} lines, {len(code)} chars) via {msg}. Verified: {verify_lines} lines.")]
        return [TextContent(type="text", text=f"WARNING: Expected {expected_lines} lines but editor has {verify_lines}. Try again.")]
    except Exception:
        return [TextContent(type="text", text=f"Code inserted ({lines} lines, {len(code)} chars) — {msg}")]


async def handle_code_read_editor(arguments: dict) -> list:
    """code_read_editor — read code from editor."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    resp = await send_with_retries({"type": "GET_CODE", "tab_id": tab.tab_id}, timeout=10.0)
    success = resp.get("success", False)
    code = resp.get("code", "")
    editor = resp.get("editor", "unknown")
    if not success:
        return [TextContent(type="text", text=f"Could not read editor: {resp.get('message', 'unknown error')}")]
    line_count = len(code.split('\n')) if code else 0
    return [TextContent(type="text", text=f"Editor: {editor} ({line_count} lines)\n\n```\n{code}\n```")]


async def handle_compile_run(arguments: dict) -> list:
    """code_compile_run — click compile and wait for results."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    wait_time = max(2, min(int(arguments.get("wait_time", 10)), 45))

    await asyncio.sleep(1.0)

    clicked = await click_first_selector(tab.tab_id, COMPILE_BUTTON_SELECTORS, timeout=5.0)
    if not clicked:
        return [TextContent(type="text", text="Error: Could not find compile/run button")]

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
    summary = f"Status: {output['status']}\n"
    if output["total"]:
        summary += f"Passed: {output['passed']}/{output['total']}\n"
    if output["compiler_errors"]:
        summary += f"\nCompiler Errors:\n" + '\n'.join(output["compiler_errors"][:5]) + "\n"
    for tc in output["test_cases"]:
        summary += f"\nTestcase {tc['number']}: {tc['status']}"
        if tc.get('expected') and tc['status'] == 'Failed':
            summary += f"\n  Expected: {tc['expected'][:100]}"
            summary += f"\n  Got: {tc.get('actual', 'N/A')[:100]}"
    return [TextContent(type="text", text=summary)]


async def handle_get_results(arguments: dict) -> list:
    """code_get_results — parse test results from page."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
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
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def handle_submit(arguments: dict) -> list:
    """code_submit — click submit and capture results."""
    tab = browser_manager.resolve_tab(arguments.get("tab_id"))
    clicked = await click_first_selector(tab.tab_id, SUBMIT_BUTTON_SELECTORS, timeout=5.0)
    if not clicked:
        return [TextContent(type="text", text="Error: Could not find submit button")]

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

    summary = f"Submission: {result['status']}\n"
    if result["total"]:
        summary += f"Result: {result['passed']}/{result['total']} testcases passed\n"
    for td in result["test_details"]:
        summary += f"  Test {td['case']}: {td['status']}\n"
    return [TextContent(type="text", text=summary)]
