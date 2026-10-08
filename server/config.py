"""
config.py — Central configuration for the Browser Automation MCP Server.

The server is a thin, LLM-free bridge: the MCP client's own model drives the
browser through four universal tools (browser_see / browser_act /
browser_js / browser_tabs). No sampling, no API keys, no in-server agent.
"""

import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── Identity ─────────────────────────────────────────────────────────────────
SERVER_NAME = "browser-automation-mcp"


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("browser-automation-mcp")
    except Exception:
        return "7.0.0"


SERVER_VERSION = _package_version()

# ── Timeouts ─────────────────────────────────────────────────────────────────
WS_RESPONSE_TIMEOUT = 30.0
SCREENSHOT_TIMEOUT = 10.0
WS_PING_INTERVAL = 25.0
JS_TIMEOUT = 15.0

# ── WebSocket server for the browser extension (localhost only) ──────────────
WS_HOST = os.getenv("BROWSER_WS_HOST", "127.0.0.1")
WS_PORT = int(os.getenv("BROWSER_WS_PORT", "8000"))
# Optional shared secret. When empty (default) any token is accepted — the
# bridge only listens on localhost, so this is fine for personal use. Set
# BROWSER_WS_AUTH_TOKEN and paste the same token into the extension to
# enforce it.
WS_AUTH_TOKEN = os.getenv("BROWSER_WS_AUTH_TOKEN", "").strip()

# ── Limits ───────────────────────────────────────────────────────────────────
MAX_TEXT_SUMMARY_CHARS = 12_000
MAX_ACTION_STEPS = 20
MAX_JS_RESULT_CHARS = 100_000
