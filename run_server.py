#!/usr/bin/env python3
"""
run_server.py — Entry point for the FastMCP Browser Automation Server.

Architecture (LLM-free bridge):
  server/config.py        — Constants, timeouts, WS settings
  server/browser_state.py — BrowserManager, BrowserTab, WebSocket state
  server/errors.py        — Structured tool response envelope
  server/tools/           — The four universal tools:
                            browser_see / browser_act / browser_js / browser_tabs
  server/transport.py     — FastMCP + WebSocket server wiring

The MCP client's own model drives everything — no sampling, no API keys,
no in-server agent.
"""

import sys
import asyncio
import logging

# Configure logging before imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)

from server.transport import run_mcp_server  # noqa: E402

if __name__ == "__main__":
    asyncio.run(run_mcp_server())
