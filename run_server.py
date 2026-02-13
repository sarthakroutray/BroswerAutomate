#!/usr/bin/env python3
"""
run_server.py — Thin entry point for the modular MCP Browser Automation Server.

All logic lives in the 'server/' package:
  server/config.py          — Constants, timeouts, tool profiles
  server/browser_state.py   — BrowserManager, BrowserTab, WebSocket state
  server/observability.py   — EventBus telemetry
  server/llm/prompts.py     — System prompts
  server/llm/provider.py    — Unified LLM provider
  server/tools/             — Tool schemas, handlers, registry
  server/agent/             — AutonomousAgent orchestrator
  server/transport.py       — FastAPI + WebSocket + MCP Server wiring
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

from server.transport import app, run_mcp_server  # noqa: E402

if __name__ == "__main__":
    if "--http" in sys.argv:
        import uvicorn
        port = 8000
        if "--port" in sys.argv:
            idx = sys.argv.index("--port") + 1
            if idx < len(sys.argv):
                port = int(sys.argv[idx])
        logging.getLogger("browser-agent").info(f"Starting HTTP server on port {port}")
        uvicorn.run("server.transport:app", host="0.0.0.0", port=port, reload=True, log_level="info")
    else:
        asyncio.run(run_mcp_server())
