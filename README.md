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
| `browser_execute_actions` | Execute actions: click, click_at (coordinates, CDP-backed), type, select, check, uncheck, scroll, navigate, go_back, go_forward, reload, press_key, hover, clear, submit, double_click, focus, wait |
| `browser_run_task` | Autonomous agent loop — give it a goal and it works through it step by step. Optional `max_steps` (1-100, default 30) and `allow_unsafe` flag. |
| `browser_list_tabs` | List all connected browser tabs |
| `browser_navigate` | Unified navigation: goto URL, back, forward, reload, new_tab, close_tab, switch_tab |
| `browser_extract_text` | Extract or search text content from the page |
| `browser_wait_for_element` | Wait for an element to appear on the page |
| `browser_execute_script` | Evaluate restricted read-only JS property paths |

Tab management (open, close, switch) is handled through `browser_navigate` with the `action` parameter set to `new_tab`, `close_tab`, or `switch_tab`.

Additional tools are available in the `coding` and `full` profiles:

| Tool | Description |
|------|-------------|
| `browser_navigate_quiz` | Navigate quiz pages: next, previous, submit |
| `browser_get_coding_problem` | Extract coding problem statement and test cases |
| `browser_set_code_editor` | Insert code into ACE/Monaco/CodeMirror editors |
| `browser_get_code_editor` | Read current code from the editor |
| `browser_compile_and_run` | Click compile, wait for and return test results |
| `browser_get_test_results` | Parse test results from the page |
| `browser_submit_solution` | Submit coding solution |

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
      "args": ["C:/path/to/BroswerAutomate/run_server.py"],
      "env": {
        "BROWSER_WS_AUTH_TOKEN": "your-secret-token"
      }
    }
  }
}
```

If `BROWSER_WS_AUTH_TOKEN` is not set, the server generates a random token at startup and prints it to stderr. Set the same token in the extension **Settings → Auth Token** (or embed it in the server URL as `ws://localhost:8000?token=your-secret-token`).

### Optional: Faster Tool Profiles

To reduce MCP tool-selection overhead, you can start the server with a smaller tool profile:

- `full` (default): all tools including quiz, coding helpers, `browser_run_task`, and `browser_execute_script`
- `coding`: core browser tools plus all coding/quiz tools (no `browser_run_task` or `browser_execute_script`)
- `minimal`: core navigation and action tools only — `browser_get_page_state`, `browser_take_screenshot`, `browser_execute_actions`, `browser_run_task`, `browser_list_tabs`, `browser_navigate`
- `manual`: all tools except `browser_run_task` (human drives every action)

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
2. Open **Settings** and set the **Auth Token** to match `BROWSER_WS_AUTH_TOKEN` (or paste the token printed to server stderr on first run)
3. Click **Connect**
4. Tell your LLM: *"Get the page state"* or *"Fill this form and submit it"*

> **Tip:** You can embed the token directly in the server URL: `ws://localhost:8000?token=your-secret-token`. The extension will extract and store it automatically.

### Connection Troubleshooting

- Ensure your MCP config starts `run_server.py` from the repository root. `server/server.py` is not a valid entrypoint.
- WebSocket authentication is mandatory. Connection will be rejected if the token doesn't match. Check that both the server `BROWSER_WS_AUTH_TOKEN` and the extension **Settings → Auth Token** are identical.
- If you change `BROWSER_WS_HOST` or `BROWSER_WS_PORT`, update the extension **Settings → Server URL** to match (for example `ws://localhost:9000`).
- Default values are compatible out of the box: server listens on `127.0.0.1:8000`, extension connects to `ws://localhost:8000`.
- The extension auto-reconnects up to 3 times on unexpected disconnects.

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

- MCP sampling works without API keys when an MCP client session is active.
- The server captures the MCP session automatically when you invoke any tool from your MCP client.
- Once a session is captured, `browser_run_task` and popup-initiated tasks can use MCP sampling.
- If no MCP session exists and no API keys are set, tasks fail fast with a clear error.
- If your MCP client reports model endpoint errors, set `BROWSER_MCP_MODEL_HINTS` to a comma-separated list your client supports, for example `gpt-4o,gpt-4.1`.
- For standalone popup usage without an MCP client, set one of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`.

## Reliability Hardening

The action engine includes resilience features for dynamic or obstructed pages:

- Selector clicks retry automatically and detect element obstruction at click point.
- Common dismissible overlays/popups (close/accept/ok/skip patterns) are auto-dismissed before retrying clicks.
- `browser_execute_actions` supports coordinate-based clicks via `click_at` (`x`/`y`). These use the **Chrome DevTools Protocol (CDP) debugger** to dispatch trusted `mousePressed`/`mouseReleased` events — bypassing `pointer-events: none` and synthetic event guards.
- `go_back`, `go_forward`, and `reload` actions are executed via the native Chrome tabs API instead of the content script, making them reliable even when the content script is unavailable. Pass `value: "hard"` to `reload` for a cache-busting hard refresh.
- Content-script messaging retries once on transient delivery failures before raising an error.
- Action batches support selector fallback ranking (`selector_fallbacks`) and step idempotency tokens (`idempotency_token`) to prevent duplicate submissions.
- Dynamic pages use DOM-stability checks (MutationObserver quiet-window) plus post-action verification before proceeding.
- All MCP tool error responses include `status`, `code`, `message`, `retriable`, and `details` fields for deterministic error handling.
- `browser_execute_script` permits only restricted read-only property-path expressions (no eval, loops, fetch, storage, or side effects).

Example `browser_execute_actions` payload using CDP coordinate click:

```json
{
  "actions": [
    { "action": "click_at", "x": 1220, "y": 740 }
  ]
}
```

Example `browser_execute_actions` payload using hard reload:

```json
{
  "actions": [
    { "action": "reload", "value": "hard" }
  ]
}
```

Note: these features improve reliability against common UI blockers, but do not bypass hard security controls such as CAPTCHAs, cross-origin iframe restrictions, or server-side bot defenses.

## Running the Server

```bash
# MCP stdio mode (used by MCP clients like Claude Desktop)
python run_server.py

# With a custom tool profile
python run_server.py --tool-profile minimal
```

**Key environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_WS_AUTH_TOKEN` | auto-generated | Shared secret for WebSocket auth. Set this to a fixed value so the extension token persists across restarts. |
| `BROWSER_WS_HOST` | `127.0.0.1` | WebSocket bind address |
| `BROWSER_WS_PORT` | `8000` | WebSocket port |
| `BROWSER_TOOL_PROFILE` | `full` | Active tool profile (`full`, `coding`, `minimal`, `manual`) |
| `BROWSER_MCP_MODEL_HINTS` | see config | Comma-separated model hints for MCP sampling |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | — | API key fallback when no MCP session is active |
| `BROWSER_AGENT_STAGNATION_LIMIT` | `4` | Steps with no DOM change before agent stops |
| `BROWSER_AGENT_MAX_RETRY_PER_STEP` | `3` | Max retry attempts per agent step |

## Architecture

- **run_server.py** — Main entry point (at root). Starts FastMCP stdio server with parallel WebSocket server.
- **server/** — Core package containing modular logic:
    - **transport.py** — FastMCP server + WebSocket bridge wiring.
    - **browser_state.py** — WebSocket comms and tab management.
    - **config.py** — Centralized settings and tool profiles.
    - **errors.py** — Structured MCP-compliant error handling.
    - **observability.py** — Event logging and telemetry.
    - **llm/** — Prompt management and unified provider interface (MCP sampling + API key fallback).
    - **tools/** — Specialized tool handlers and dispatch logic.
    - **agent/** — Autonomous orchestrator loop.
- **extension/** — Chrome extension source.
