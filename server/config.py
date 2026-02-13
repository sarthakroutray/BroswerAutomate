"""
config.py — centralised configuration for the Browser Automation MCP Server v5.
"""

import os
import sys
from typing import Optional


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── Identity ─────────────────────────────────────────────────────────────────
SERVER_NAME = "browser-automation-mcp"
SERVER_VERSION = "5.0.0"

# ── Timeouts ─────────────────────────────────────────────────────────────────
WS_RESPONSE_TIMEOUT = 30.0
SCREENSHOT_TIMEOUT = 10.0
WS_PING_INTERVAL = 25.0
MCP_SAMPLING_TIMEOUT = 45.0

# ── Agent defaults ───────────────────────────────────────────────────────────
DEFAULT_MAX_STEPS = 30
AGENT_STEP_DELAY = 1.5
MAX_TEXT_SUMMARY_CHARS = 12_000
MAX_TASK_HISTORY = 50

# ── Reliability / hardening flags ────────────────────────────────────────────
ENABLE_STRUCTURED_TOOL_RESPONSES = _env_bool("BROWSER_STRUCTURED_TOOL_RESPONSES", True)
STRICT_TOOL_VALIDATION = _env_bool("BROWSER_STRICT_TOOL_VALIDATION", True)
ENABLE_IDEMPOTENCY_GUARDS = _env_bool("BROWSER_ENABLE_IDEMPOTENCY_GUARDS", True)
ENABLE_AGENT_ACTION_VERIFICATION = _env_bool("BROWSER_ENABLE_AGENT_ACTION_VERIFICATION", True)
ENABLE_AGENT_FAILURE_CLASSIFICATION = _env_bool("BROWSER_ENABLE_AGENT_FAILURE_CLASSIFICATION", True)
ENABLE_AGENT_SAFE_STOP = _env_bool("BROWSER_ENABLE_AGENT_SAFE_STOP", True)
JS_EXPRESSION_STRICT_MODE = _env_bool("BROWSER_JS_EXPRESSION_STRICT_MODE", True)

# ── Reliability / guardrail parameters ───────────────────────────────────────
IDEMPOTENCY_WINDOW_SECONDS = float(os.getenv("BROWSER_IDEMPOTENCY_WINDOW_SECONDS", "4"))
AGENT_STAGNATION_LIMIT = int(os.getenv("BROWSER_AGENT_STAGNATION_LIMIT", "4"))
AGENT_MAX_RETRY_PER_STEP = int(os.getenv("BROWSER_AGENT_MAX_RETRY_PER_STEP", "3"))
AGENT_MAX_RETRY_PER_CATEGORY = int(os.getenv("BROWSER_AGENT_MAX_RETRY_PER_CATEGORY", "3"))
AGENT_DOM_STABILIZE_WAIT_MS = int(os.getenv("BROWSER_AGENT_DOM_STABILIZE_WAIT_MS", "1800"))

# ── Context compression ─────────────────────────────────────────────────────
HISTORY_VERBATIM_STEPS = 3          # keep last N steps full
HISTORY_SUMMARY_MAX_CHARS = 600     # compressed narrative max
SCREENSHOT_MAX_WIDTH = 1024
SCREENSHOT_JPEG_QUALITY = 85

# ── MCP model hints ──────────────────────────────────────────────────────────
DEFAULT_MCP_MODEL_HINTS = [
    "gpt-4o",
    "gpt-4.1",
    "claude-3-5-sonnet-20241022",
]


def get_mcp_model_hints() -> list[str]:
    env = os.getenv("BROWSER_MCP_MODEL_HINTS", "").strip()
    if not env:
        return list(DEFAULT_MCP_MODEL_HINTS)
    hints = [h.strip() for h in env.split(",") if h.strip()]
    return hints or list(DEFAULT_MCP_MODEL_HINTS)


# ── Tool profiles ────────────────────────────────────────────────────────────
SUPPORTED_TOOL_PROFILES = {"full", "coding", "minimal", "manual"}

BASIC_TOOL_NAMES = {
    # observation
    "browser_observe", "browser_screenshot", "browser_extract_content",
    "browser_inspect_element", "browser_wait_element",
    # navigation
    "browser_navigate", "browser_list_tabs",
    # interaction
    "browser_act", "browser_fill_form", "browser_eval_js",
    # agent
    "browser_run_task",
}

FULL_TOOL_NAMES = set(BASIC_TOOL_NAMES)
CODING_TOOL_NAMES = set(BASIC_TOOL_NAMES)
MINIMAL_TOOL_NAMES = set(BASIC_TOOL_NAMES)
MANUAL_TOOL_NAMES = set(BASIC_TOOL_NAMES)

DEFAULT_TOOL_PROFILE = "minimal"


def resolve_tool_profile() -> str:
    profile = os.getenv("BROWSER_TOOL_PROFILE", "").strip().lower()
    if "--tool-profile" in sys.argv:
        idx = sys.argv.index("--tool-profile") + 1
        if idx < len(sys.argv):
            profile = sys.argv[idx].strip().lower()
    if not profile:
        profile = DEFAULT_TOOL_PROFILE
    if profile not in SUPPORTED_TOOL_PROFILES:
        print(
            f"[browser-agent] Invalid tool profile '{profile}', "
            f"falling back to '{DEFAULT_TOOL_PROFILE}'",
            file=sys.stderr,
        )
        profile = DEFAULT_TOOL_PROFILE
    return profile


ACTIVE_TOOL_PROFILE = resolve_tool_profile()


def get_enabled_tool_names() -> Optional[set[str]]:
    mapping = {
        "full": FULL_TOOL_NAMES,
        "coding": CODING_TOOL_NAMES,
        "minimal": MINIMAL_TOOL_NAMES,
        "manual": MANUAL_TOOL_NAMES,
    }
    return mapping.get(ACTIVE_TOOL_PROFILE, MANUAL_TOOL_NAMES)


# ── Selector banks (quiz / coding buttons) ───────────────────────────────────
COMPILE_BUTTON_SELECTORS = [
    "#programme-compile",
    "button[id*='compile']",
    "//button[contains(text(),'Compile')]",
    "//button[contains(text(),'Run')]",
    "//button[contains(text(),'Compile & Run')]",
    ".compile-btn",
    "[data-action='compile']",
]

SUBMIT_BUTTON_SELECTORS = [
    "#tt-footer-submit-answer",
    "button[id*='submit']",
    "//button[contains(text(),'Submit Code')]",
    "//button[contains(text(),'Submit')]",
    ".submit-btn",
]

QUIZ_NAV_SELECTORS: dict[str, list[str]] = {
    "next": [
        "#next-btn", "button[id*='next']", "[data-action='next']",
        "button.next", ".next-btn", "a.next",
        "//button[contains(normalize-space(),'Next')]",
        "//a[contains(normalize-space(),'Next')]",
        "//*[@role='button' and contains(normalize-space(),'Next')]",
        "//*[contains(translate(@class,'NEXT','next'),'next')]",
    ],
    "previous": [
        "#prev-btn", "button[id*='prev']",
        "[data-action='previous']", "[data-action='prev']",
        "button.prev", ".prev-btn", "a.prev",
        "//button[contains(normalize-space(),'Previous')]",
        "//button[contains(normalize-space(),'Prev')]",
        "//a[contains(normalize-space(),'Previous')]",
        "//*[@role='button' and contains(normalize-space(),'Prev')]",
        "//*[contains(translate(@class,'PREV','prev'),'prev')]",
    ],
    "submit": [
        "#tt-header-submit", "#tt-footer-submit-answer",
        "button[id*='submit']", "[data-action='submit']", ".submit-btn",
        "//button[contains(normalize-space(),'Submit Test')]",
        "//button[contains(normalize-space(),'Submit Code')]",
        "//button[contains(normalize-space(),'Submit')]",
        "//a[contains(normalize-space(),'Submit')]",
        "//*[@role='button' and contains(normalize-space(),'Submit')]",
    ],
}
