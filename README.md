# BroswerAutomate — Universal Browser Control for MCP

A Chrome extension + MCP server that lets your LLM do **anything** in the browser: fill forms, complete quizzes and courses, solve coding challenges, automate workflows, buy stuff. Six universal tools, no API keys, no in-server LLM — the model in your MCP client (Claude Desktop, etc.) drives everything.

## How It Works

```
MCP Client (Claude Desktop / any MCP client)
       │  the client's own LLM plans and reasons
       ▼
  MCP Server (run_server.py)          ← LLM-free bridge
       │  WebSocket (localhost:8000)
       ▼
  Chrome Extension
       │  DOM extraction + screenshots + human-like actions + JS
       ▼
  Browser Tab (Chrome / Edge / Brave / any Chromium browser)
```

There is **no MCP sampling and no API key fallback** — the server never calls an LLM. Your MCP client's model does all the thinking through the six tools below.

## MCP Tools — 6 tools that can do everything

| Tool | Description |
|------|-------------|
| `browser_see` | **Observe.** Structured DOM with exact selectors for every input/button/link/checkbox/radio/select + page analysis. Modes: `page` (default, paginate with `limit`/`offset`), `context` (one-shot page kind + login/captcha/2FA blockers + quiz/coding data), `text` (searchable), `html`, `element` (detailed info), `editor` (read ACE/Monaco/CodeMirror code), `quiz` (questions + option selectors), `coding` (problem statement + samples), `find` (search by visible text via `query` + `find_mode` exact/contains/startsWith/endsWith/word/regex). `screenshot=true` adds a PNG (`clip_selector` for element region, `full_page` for whole page). |
| `browser_act` | **Act.** Batched human-like actions: `click`, `click_at`, `double_click`, `right_click`, `drag`, `type`, `clear`, `select` (multi with `multiple:true`), `check`, `uncheck`, `press_key` (incl. chords like `Ctrl+C`), `scroll`, `scroll_element`, `hover`, `focus`, `submit`, `navigate`, `wait`, `upload`, `dialog_accept`, `dialog_dismiss`, `set_code`, `get_code`, `go_back`, `go_forward`, `reload`. Supports CSS/XPath/shadow-path/text selectors (`text="Buy now"`), selector fallbacks, same-origin `frame` scope, per-step `timeout_ms`/`auto_wait`, and optional idempotency tokens. `trusted:true` on clicks uses CDP input for canvas/bot-walled targets. |
| `browser_js` | **Escape hatch.** Arbitrary JavaScript in the page's MAIN world — framework internals, fetch, storage, DOM surgery. Expression or async body; the (awaited) result is returned. Anything `browser_act` can't express, express here. |
| `browser_tabs` | **Navigate.** `goto`, `back`, `forward`, `reload`, `new_tab`, `close_tab`, `switch_tab`, `list`. `wait_until: load|dom|networkidle` controls confirmation; unconfirmed transitions return `partial:true` instead of misleading success. |
| `browser_wait` | **Wait observably.** `selector`/`text` waits with `require_visible`/`require_interactable`, or pure DOM stability via `require_stable`. Returns found/visible/interactable/elapsed detail. |
| `browser_session` | **Bridge health.** `status` (connection + tabs), `stop` (cancel in-flight batch), `reconnect` (re-sync guidance). |

Everything else — solving a 30-question quiz, filling a checkout form, submitting a coding solution, completing a course — is composition of these six by the model.

### Selector formats accepted by `browser_act`

- CSS: `#email`, `input[name="q"]`
- XPath: `//button[contains(text(),'Submit')]`
- Shadow DOM: `host-selector >>> inner-selector`
- Text: `text="Buy now"` (exact), `text*="partial"`, `text^="starts"`, `text$="ends"`, `text~="word"`, `text~/regex/`

### Code editors

`browser_act` with `{"action": "set_code", "value": "<full source>"}` writes into ACE, Monaco, CodeMirror 5/6, or code textareas (and fires the right change events). `{"action": "get_code"}` / `browser_see(mode="editor")` reads it back.

## Setup

### 1. Install the server

No cloning needed — install straight from GitHub:

```bash
# Option A: run without a permanent install (needs uv)
uvx --from git+https://github.com/sarthakroutray/BroswerAutomate browser-automation-mcp

# Option B: persistent isolated install (recommended)
pipx install git+https://github.com/sarthakroutray/BroswerAutomate

# Option C: plain pip
pip install git+https://github.com/sarthakroutray/BroswerAutomate
```

From source (for development):

```bash
git clone https://github.com/sarthakroutray/BroswerAutomate
cd BroswerAutomate
pip install -e ".[dev]"
# or: pip install -r server/requirements.txt
```

### 2. Load the Chrome Extension

1. Download `browser-extension-<version>.zip` from the [latest release](https://github.com/sarthakroutray/BroswerAutomate/releases) and unzip it
   (or use the `extension/` folder if you cloned the repo)
2. Open `chrome://extensions/`
3. Enable **Developer mode**
4. Click **Load unpacked** → select the unzipped folder

(Works in any Chromium browser: Chrome, Edge, Brave, Arc, ...)

### 3. Configure your MCP client

Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "browser-automation": {
      "command": "browser-automation-mcp"
    }
  }
}
```

If you installed with `uvx` (Option A) instead, use:

```json
{
  "mcpServers": {
    "browser-automation": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/sarthakroutray/BroswerAutomate",
        "browser-automation-mcp"
      ]
    }
  }
}
```

That's it — no keys, no profiles. Defaults work out of the box: the bridge listens on `127.0.0.1:8000` and the extension connects to `ws://localhost:8000` with no token.

Optional: set `BROWSER_WS_AUTH_TOKEN` to require a shared secret, and paste the same token into the extension **Settings → Auth Token** (or embed it in the server URL as `ws://localhost:8000?token=your-secret-token`).

### 4. Connect

1. Click the extension icon
2. Click **Connect**
3. Tell your LLM: *"See what's on this page"*, *"Fill this form and submit it"*, *"Complete this quiz"*, *"Solve this coding problem and submit"*, *"Buy the cheapest option and check out"*

## Usage Examples

> "Look at the current page and tell me what you see"
> "Fill in the checkout form, use my saved address, and place the order"
> "This is a 20-question quiz — answer every question and submit"
> "Read the coding problem, write a Python solution, run the tests, and submit"
> "Scrape the table on this page into a summary"

## Reliability Features

The action engine handles dynamic or obstructed pages:

- Human-like typing and clicking (pointer/mouse event sequences with jitter), with a **CDP trusted-click fallback** via `chrome.debugger` for hard cases.
- Auto-dismissal of cookie banners and dismissible overlays that block clicks.
- Selector fallbacks (`selector_fallbacks`), text selectors, shadow-DOM and same-origin iframe traversal.
- DOM-stability checks (MutationObserver quiet-window) and post-action change verification.
- Action batches run in order (up to 20 steps); `wait` can wait for an element (`selector` + `value` = timeout ms).
- Optional `idempotency_token` per step dedupes retried batches — never applied automatically, so repeating an action always works.

Note: these improve reliability against common UI blockers, but do not bypass CAPTCHAs, cross-origin iframe restrictions, or other hard security controls.

## Running the Server

```bash
# MCP stdio mode (used by MCP clients like Claude Desktop)
browser-automation-mcp
```

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_WS_HOST` | `127.0.0.1` | WebSocket bind address |
| `BROWSER_WS_PORT` | `8000` | WebSocket port |
| `BROWSER_WS_AUTH_TOKEN` | *(empty = disabled)* | Optional shared secret for the extension↔server WebSocket |

## Architecture

- **`server/__main__.py`** — Entry point (`browser-automation-mcp` console script / `python -m server`). Starts FastMCP stdio server with the parallel WebSocket bridge.
- **server/** — Core package:
    - **transport.py** — FastMCP server + WebSocket bridge wiring (auth handshake, tab sync).
    - **browser_state.py** — WebSocket comms, request/response correlation, tab management.
    - **tools/** — The six tools: `observation.py` (`browser_see`), `interaction.py` (`browser_act`, `browser_js`), `navigation.py` (`browser_tabs`), `wait.py` (`browser_wait`), `session.py` (`browser_session`), `schemas.py` (typed action schema).
    - **config.py** — Settings and limits.
    - **errors.py** — Structured tool response envelope.
- **extension/** — Chrome extension:
    - **background.js** — WebSocket client, tab management, screenshots, MAIN-world code-editor + JS execution, CDP clicks.
    - **content.js** — DOM extraction (incl. shadow DOM), 26-action human-like executor, quiz/coding/context extractors, text selectors.
    - **overlay.js** — "AI is controlling this page" overlay with emergency stop (Escape).
    - **popup.html / popup.js** — Connection and settings UI.

## Releasing a New Version

1. Bump `version` in `pyproject.toml` and `version` in `extension/manifest.json` (keep them in sync).
2. Commit, then tag: `git tag vX.Y.Z && git push origin main --tags`
3. The `Release` workflow runs tests, zips the extension, and publishes a GitHub Release with the zip attached and auto-generated notes.
