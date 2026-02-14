# AI Browser Agent — MCP-Powered Autonomous Browser Automation

A Chrome extension + MCP server that lets your LLM see, navigate, and interact with web pages autonomously. **No API keys needed** — it uses the LLM your MCP client (Claude Desktop, etc.) is already connected to.

## How It Works

```
MCP Client (Claude Desktop)
       │  natural language instructions
       ▼
  MCP Server (run_server.py)
       │  WebSocket
       ▼
  Chrome Extension
       │  DOM extraction + screenshots + action execution
       ▼
  Browser Tab
```

The LLM drives the loop: get page state → decide actions → execute → repeat.
When using `browser_run_task`, the server uses **MCP Sampling** to ask the connected LLM for decisions — zero config, no separate API keys.

## MCP Tools

| Tool | Description |
|------|-------------|
| `browser_get_page_state` | Get structured DOM (inputs, buttons, links, checkboxes, radios, tables, images) |
| `browser_take_screenshot` | Capture visible tab as base64 PNG |
| `browser_execute_actions` | Execute actions: click, type, select, check, uncheck, scroll, navigate, press_key, hover, clear, submit, double_click, focus, wait |
| `browser_run_task` | Autonomous agent loop — give it a goal and it works through it step by step |
| `browser_list_tabs` | List all connected browser tabs |
| `browser_open_tab` | Open a new tab with a URL |
| `browser_close_tab` | Close a specific tab |
| `browser_switch_tab` | Switch active tab |

## Setup

### 1. Install Python dependencies

From the project root:

```bash
pip install -r server/requirements.txt
```

### 2. Load the Chrome Extension

1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `extension/` folder

### 3. Configure your MCP client

Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "browser-automation": {
      "command": "C:/path/to/BroswerAutomate/.venv/Scripts/python.exe",
      "args": ["C:/path/to/BroswerAutomate/run_server.py"]
    }
  }
}
```

### Optional: Faster Tool Profiles

To reduce MCP tool-selection overhead, you can start the server with a smaller tool profile:

- `full`: core browser automation tools
- `coding`: same core browser toolset (compatibility alias)
- `minimal` (default): core browser automation tools

Specialized quiz/coding tools are not exposed; use the core browser tools for these flows.

Example:

```json
{
  "mcpServers": {
    "browser-automation": {
      "command": "C:/path/to/BroswerAutomate/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/BroswerAutomate/run_server.py",
        "--tool-profile",
        "minimal"
      ]
    }
  }
}
```

You can also set `BROWSER_TOOL_PROFILE=minimal` as an environment variable.

### 4. Connect

1. Click the extension icon in Chrome
2. Click **Connect** (connects to the server's WebSocket on port 8000)
3. Tell your LLM: *"Get the page state"* or *"Fill this form and submit it"*

### Connection Troubleshooting

- Ensure your MCP config starts `run_server.py` from the repository root. `server/server.py` is not a valid entrypoint in this version.
- If you change `BROWSER_WS_HOST` or `BROWSER_WS_PORT`, update the extension **Settings → Server URL** to match (for example `ws://localhost:9000`).
- Default values are compatible out of the box: server listens on `127.0.0.1:8000`, extension connects to `ws://localhost:8000`.

## Usage Examples

**Direct tool use** (LLM drives the loop):
> "Look at the current page and tell me what you see"
> "Click the Submit button"
> "Fill in the email field with test@example.com"

**Autonomous mode** (agent runs a multi-step loop):
> "Open this dashboard and click into the latest report"
> "Fill out the registration form with realistic test data"
> "Navigate to google.com and search for 'MCP protocol'"

## Popup UI

The extension popup lets you type a goal and run it directly. The server uses MCP Sampling to ask the connected LLM, or falls back to env-var API keys (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`) if no MCP session is active.

### MCP Sampling Notes (No API Key Mode)

- MCP sampling works without API keys only when an MCP client session is active.
- If you start tasks from the popup and no sampling session exists, the task now fails fast with a clear error instead of hanging.
- To activate sampling, invoke a tool from your MCP client first (for example `browser_list_tabs`), then run `browser_run_task` or use the popup.
- If your MCP client reports model endpoint errors (for example `Endpoint not found for model auto`), set `BROWSER_MCP_MODEL_HINTS` to a comma-separated list your client supports, for example `gpt-4o,gpt-4.1`.
- If you prefer standalone popup usage, set one of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`.

## Reliability Hardening

The action engine includes resilience features for dynamic or obstructed pages:

- Selector clicks now retry automatically and detect element obstruction at click point.
- Common dismissible overlays/popups (close/accept/ok/skip patterns) are auto-dismissed before retrying clicks.
- `browser_execute_actions` supports coordinate-based fallback clicks with `click_at` (`x`/`y`), useful when selectors are unstable.
- Background `EXECUTE_ACTIONS` now re-injects/retries content-script messaging once on transient delivery failures.
- Action batches now support selector fallback ranking (`selector_fallbacks`) and step idempotency tokens (`idempotency_token`) to prevent duplicate submissions.
- Dynamic pages now use DOM-stability checks (MutationObserver quiet-window) plus post-action verification before proceeding.
- MCP tool calls now support `response_mode` (`legacy`, `structured`, `dual`) and include a structured envelope for deterministic error handling.
- `browser_eval_js` now permits only restricted read-only property-path expressions (no arbitrary eval / network / storage access).

Example `browser_execute_actions` payload using coordinate fallback:

```json
{
  "actions": [
    { "action": "click_at", "x": 1220, "y": 740 }
  ]
}
```

Note: this improves reliability against common UI blockers, but it does not bypass hard security controls such as CAPTCHAs, cross-origin iframe restrictions, or server-side bot defenses.

## Standalone HTTP Mode

For development/testing without MCP:

```bash
# Default (MCP stdio mode)
python run_server.py

# Standalone HTTP Mode
python run_server.py --http --port 8000
```

Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY` env var for the autonomous agent fallback.

## Architecture

- **run_server.py** — Main entry point (at root). Supports MCP (default) and `--http` modes.
- **server/** — Core package containing modular logic:
    - **browser_state.py** — WebSocket comms and tab management.
    - **config.py** — Centralized settings and tool profiles.
    - **observability.py** — Event logging and telemetry.
    - **llm/** — Prompt management and unified provider interface.
    - **tools/** — Specialized tool handlers and dispatch logic.
    - **agent/** — Autonomous orchestrator loop.
    - **transport.py** — FastAPI and MCP server wiring.
    - **legacy_server.py** — Original monolithic implementation (backup).
- **extension/** — Chrome extension source.
