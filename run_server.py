#!/usr/bin/env python3
"""
run_server.py — Entry point for the FastMCP Browser Automation Server.

Architecture (v6 — FastMCP native):
  server/config.py          — Constants, timeouts, tool profiles
  server/browser_state.py   — BrowserManager, BrowserTab, WebSocket state
  server/errors.py          — Structured MCP-compliant error handling
  server/observability.py   — EventBus telemetry
  server/llm/prompts.py     — System prompts
  server/llm/provider.py    — Unified LLM provider
  server/tools/             — FastMCP tool handlers (Pydantic schemas)
  server/agent/             — AutonomousAgent orchestrator
  server/transport.py       — FastMCP + WebSocket server wiring
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
