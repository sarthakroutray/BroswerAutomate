/**
 * background.js — Service Worker v4 (Manifest V3)
 *
 * Manages the WebSocket connection to the MCP bridge server and executes
 * server requests against the browser: DOM extraction, human-like action
 * batches, screenshots, code-editor control (ACE/Monaco/CodeMirror),
 * arbitrary MAIN-world JavaScript, and tab management.
 *
 * Centralized MSG registry of all wire-string message types.
 */

// ─── Constants ───────────────────────────────────────────────────────────────

const DEFAULT_MCP_SERVER_URL = "ws://localhost:8000";
const AUTO_RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_ATTEMPTS = 3;
const OVERLAY_HIDE_DELAY_MS = 3000;

// Canonical message-type registry. Every message that flows through
// chrome.runtime / chrome.tabs / WS is keyed by one of these strings.
// Chrome MV3 doesn't allow module imports across the service-worker /
// content-script / popup boundaries, so this constant is duplicated
// verbatim in content.js, overlay.js, and popup.js. A test in
// tests/test_message_types.py asserts every literal message-type string
// used in those files appears here, and that the duplicates stay in sync.
//
// Direction legend:
//   EXT  = from popup → background (chrome.runtime.sendMessage)
//   BG   = from background → content script (chrome.tabs.sendMessage)
//   WS↑  = from background → MCP server (WebSocket, outgoing)
//   WS↓  = from MCP server → background (WebSocket, incoming)
const MSG = Object.freeze({
  // popup → background
  CONNECT: "CONNECT",
  DISCONNECT: "DISCONNECT",
  GET_STATUS: "GET_STATUS",
  SET_SERVER_URL: "SET_SERVER_URL",
  SET_AUTH_TOKEN: "SET_AUTH_TOKEN",
  EMERGENCY_STOP: "EMERGENCY_STOP",
  DEBUGGER_CLICK: "DEBUGGER_CLICK",
  // background → content script
  PING: "PING",
  PONG: "PONG",
  ACK: "ACK",
  OVERLAY_CONTROL: "OVERLAY_CONTROL",
  EXTRACT_DOM: "EXTRACT_DOM",
  EXTRACT_HTML: "EXTRACT_HTML",
  EXTRACT_TEXT: "EXTRACT_TEXT",
  EXTRACT_CODING_PROBLEM: "EXTRACT_CODING_PROBLEM",
  EXTRACT_QUIZ_STRUCTURE: "EXTRACT_QUIZ_STRUCTURE",
  EXTRACT_PAGE_CONTEXT: "EXTRACT_PAGE_CONTEXT",
  FIND_BY_TEXT: "FIND_BY_TEXT",
  EXECUTE_ACTIONS: "EXECUTE_ACTIONS",
  EXECUTE_JS: "EXECUTE_JS",
  GET_ELEMENT_INFO: "GET_ELEMENT_INFO",
  WAIT_FOR_ELEMENT: "WAIT_FOR_ELEMENT",
  SET_CODE: "SET_CODE",
  GET_CODE: "GET_CODE",
  // content → background (responses)
  DOM_UPDATE: "DOM_UPDATE",
  ACTION_COMPLETE: "ACTION_COMPLETE",
  SCREENSHOT_RESULT: "SCREENSHOT_RESULT",
  HTML_RESULT: "HTML_RESULT",
  TEXT_EXTRACT_RESULT: "TEXT_EXTRACT_RESULT",
  CODING_PROBLEM_RESULT: "CODING_PROBLEM_RESULT",
  QUIZ_STRUCTURE_RESULT: "QUIZ_STRUCTURE_RESULT",
  PAGE_CONTEXT_RESULT: "PAGE_CONTEXT_RESULT",
  FIND_BY_TEXT_RESULT: "FIND_BY_TEXT_RESULT",
  ELEMENT_INFO_RESULT: "ELEMENT_INFO_RESULT",
  ELEMENT_WAIT_RESULT: "ELEMENT_WAIT_RESULT",
  JS_RESULT: "JS_RESULT",
  SET_CODE_RESULT: "SET_CODE_RESULT",
  GET_CODE_RESULT: "GET_CODE_RESULT",
  DEBUGGER_CLICK_RESULT: "DEBUGGER_CLICK_RESULT",
  // background → MCP server (WebSocket outgoing)
  AUTH: "AUTH",
  AUTH_OK: "AUTH_OK",
  TAB_SNAPSHOT: "TAB_SNAPSHOT",
  TAB_CREATED: "TAB_CREATED",
  TAB_CLOSED: "TAB_CLOSED",
  TAB_SWITCHED: "TAB_SWITCHED",
  TAB_OPENED: "TAB_OPENED",
  REQUEST_DOM: "REQUEST_DOM",
  REQUEST_HTML: "REQUEST_HTML",
  OPEN_TAB: "OPEN_TAB",
  CLOSE_TAB: "CLOSE_TAB",
  SWITCH_TAB: "SWITCH_TAB",
  TAKE_SCREENSHOT: "TAKE_SCREENSHOT",
  // background → popup (broadcasts)
  CONNECTION_STATUS: "CONNECTION_STATUS",
  TAB_LIST_UPDATED: "TAB_LIST_UPDATED",
});

// ─── State ───────────────────────────────────────────────────────────────────

const state = {
  ws: null,
  connected: false,
  authenticated: false,
  connecting: false, // guard against connection races
  reconnectTimer: null,
  reconnectAttempts: 0,
  manualReconnect: false,
  trackedTabs: new Set(),
  overlayHideTimer: null,
  activeTabId: null,
  serverUrl: DEFAULT_MCP_SERVER_URL,
  wsAuthToken: "",
  debuggerAttached: new Set(), // tabs with debugger currently attached
};

function normalizeServerUrl(rawUrl) {
  const trimmed = String(rawUrl || "").trim();
  if (!trimmed) return DEFAULT_MCP_SERVER_URL;
  if (/^https?:\/\//i.test(trimmed)) {
    return trimmed.replace(/^http/i, "ws").replace(/\/+$/, "");
  }
  const withScheme = /^wss?:\/\//i.test(trimmed) ? trimmed : `ws://${trimmed}`;
  return withScheme.replace(/\/+$/, "");
}

function extractServerConfig(rawUrl, fallbackToken = "") {
  const normalized = normalizeServerUrl(rawUrl);
  try {
    const parsed = new URL(normalized);
    const token = parsed.searchParams.get("token") || fallbackToken || "";
    parsed.search = "";
    parsed.hash = "";
    return {
      serverUrl: parsed.toString().replace(/\/+$/, ""),
      wsAuthToken: token,
    };
  } catch {
    return {
      serverUrl: normalized.replace(/[?#].*$/, ""),
      wsAuthToken: fallbackToken || "",
    };
  }
}

async function loadServerUrlSetting() {
  try {
    const stored = await chrome.storage.local.get(["mcpServerUrl", "wsAuthToken"]);
    const config = extractServerConfig(stored.mcpServerUrl, stored.wsAuthToken || "");
    state.serverUrl = config.serverUrl;
    state.wsAuthToken = config.wsAuthToken;
  } catch {
    state.serverUrl = DEFAULT_MCP_SERVER_URL;
    state.wsAuthToken = "";
  }
}

// ─── Debugger Click (Trusted Events via CDP) ─────────────────────────────────

async function debuggerClick(tabId, x, y) {
  const target = { tabId };
  const wasAttached = state.debuggerAttached.has(tabId);

  try {
    if (!wasAttached) {
      await chrome.debugger.attach(target, "1.3");
      state.debuggerAttached.add(tabId);
    }

    // Mouse pressed at coordinates
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      clickCount: 1,
    });

    // Brief delay to mimic human timing
    await sleep(50 + Math.random() * 40);

    // Mouse released
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      clickCount: 1,
    });

    return { success: true };
  } catch (err) {
    return { success: false, error: err.message };
  } finally {
    // Auto-detach after a short delay to avoid interfering with DevTools
    if (!wasAttached) {
      setTimeout(async () => {
        try {
          await chrome.debugger.detach(target);
        } catch { }
        state.debuggerAttached.delete(tabId);
      }, 300);
    }
  }
}

// ─── Code Editor Control (MAIN world: ACE / Monaco / CodeMirror) ─────────────

async function editorSetCode(tabId, codeStr) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: (code) => {
      try {
        // ACE Editor — try multiple detection methods
        // Method 1: Direct ace.edit() lookup
        if (typeof ace !== 'undefined') {
          const aceEls = document.querySelectorAll('.ace_editor');
          for (const el of aceEls) {
            try {
              const editor = ace.edit(el);
              if (editor && typeof editor.setValue === 'function') {
                editor.setValue(code, -1);
                editor.clearSelection();
                editor.gotoLine(1, 0, false);
                // Trigger session change events
                editor.session._signal("change");
                return { success: true, message: 'Code set in ACE editor (ace.edit)', lines: code.split('\n').length };
              }
            } catch (e) { /* try next */ }
          }
        }

        // Method 2: ACE via element's env property
        const aceEl2 = document.querySelector('.ace_editor');
        if (aceEl2 && aceEl2.env && aceEl2.env.editor) {
          const editor = aceEl2.env.editor;
          editor.setValue(code, -1);
          editor.clearSelection();
          editor.gotoLine(1, 0, false);
          return { success: true, message: 'Code set in ACE editor (env.editor)', lines: code.split('\n').length };
        }

        // Method 3: Search for ACE in iframes
        const iframes = document.querySelectorAll('iframe');
        for (const iframe of iframes) {
          try {
            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
            const iframeAce = iframe.contentWindow.ace;
            if (iframeAce) {
              const iframeAceEl = iframeDoc.querySelector('.ace_editor');
              if (iframeAceEl) {
                const editor = iframeAce.edit(iframeAceEl);
                editor.setValue(code, -1);
                editor.clearSelection();
                return { success: true, message: 'Code set in ACE editor (iframe)', lines: code.split('\n').length };
              }
            }
          } catch (e) { /* cross-origin iframe, skip */ }
        }

        // Monaco Editor
        if (typeof monaco !== 'undefined' && monaco.editor) {
          const models = monaco.editor.getModels();
          if (models && models.length > 0) {
            models[0].setValue(code);
            return { success: true, message: 'Code set in Monaco editor', lines: code.split('\n').length };
          }
          const editors = typeof monaco.editor.getEditors === 'function' ? monaco.editor.getEditors() : [];
          if (editors.length > 0) {
            editors[0].setValue(code);
            return { success: true, message: 'Code set in Monaco editor', lines: code.split('\n').length };
          }
        }

        // CodeMirror 5
        const cm5 = document.querySelector('.CodeMirror');
        if (cm5 && cm5.CodeMirror) {
          cm5.CodeMirror.setValue(code);
          return { success: true, message: 'Code set in CodeMirror 5', lines: code.split('\n').length };
        }

        // CodeMirror 6
        const cm6 = document.querySelector('.cm-editor');
        if (cm6 && cm6.cmView && cm6.cmView.view) {
          const view = cm6.cmView.view;
          view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: code } });
          return { success: true, message: 'Code set in CodeMirror 6', lines: code.split('\n').length };
        }

        // Fallback: look for any textarea inside a code-like container
        const codeTextarea = document.querySelector('.code-area textarea, .editor textarea, [class*="programme"] textarea, textarea[name*="code"]');
        if (codeTextarea) {
          codeTextarea.value = code;
          codeTextarea.dispatchEvent(new Event('input', { bubbles: true }));
          codeTextarea.dispatchEvent(new Event('change', { bubbles: true }));
          return { success: true, message: 'Code set in textarea fallback', lines: code.split('\n').length };
        }

        return { success: false, message: 'No supported code editor found on page (tried ACE, Monaco, CodeMirror, textarea)' };
      } catch (e) {
        return { success: false, message: 'Editor error: ' + e.message };
      }
    },
    args: [codeStr],
  });
  return results && results[0] ? results[0].result : { success: false, message: 'No result from script' };
}

async function editorGetCode(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: () => {
      try {
        // ACE Editor — multiple detection methods
        if (typeof ace !== 'undefined') {
          const aceEls = document.querySelectorAll('.ace_editor');
          for (const el of aceEls) {
            try {
              const editor = ace.edit(el);
              if (editor && typeof editor.getValue === 'function') {
                return { success: true, code: editor.getValue(), editor: 'ace' };
              }
            } catch (e) { /* try next */ }
          }
        }
        // ACE via env property
        const aceEl2 = document.querySelector('.ace_editor');
        if (aceEl2 && aceEl2.env && aceEl2.env.editor) {
          return { success: true, code: aceEl2.env.editor.getValue(), editor: 'ace' };
        }
        // Check iframes for ACE
        const iframes = document.querySelectorAll('iframe');
        for (const iframe of iframes) {
          try {
            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
            const iframeAce = iframe.contentWindow.ace;
            if (iframeAce) {
              const iframeAceEl = iframeDoc.querySelector('.ace_editor');
              if (iframeAceEl) {
                const editor = iframeAce.edit(iframeAceEl);
                return { success: true, code: editor.getValue(), editor: 'ace (iframe)' };
              }
            }
          } catch (e) { /* cross-origin */ }
        }
        // Monaco
        if (typeof monaco !== 'undefined' && monaco.editor) {
          const models = monaco.editor.getModels();
          if (models && models.length > 0) {
            return { success: true, code: models[0].getValue(), editor: 'monaco' };
          }
        }
        // CodeMirror 5
        const cm5 = document.querySelector('.CodeMirror');
        if (cm5 && cm5.CodeMirror) {
          return { success: true, code: cm5.CodeMirror.getValue(), editor: 'codemirror5' };
        }
        // CodeMirror 6
        const cm6 = document.querySelector('.cm-editor');
        if (cm6 && cm6.cmView && cm6.cmView.view) {
          return { success: true, code: cm6.cmView.view.state.doc.toString(), editor: 'codemirror6' };
        }
        // Fallback: textarea
        const codeTextarea = document.querySelector('.code-area textarea, .editor textarea, [class*="programme"] textarea, textarea[name*="code"]');
        if (codeTextarea) {
          return { success: true, code: codeTextarea.value, editor: 'textarea' };
        }
        return { success: false, code: '', message: 'No editor found (tried ACE, Monaco, CodeMirror, textarea)' };
      } catch (e) {
        return { success: false, code: '', message: e.message };
      }
    },
    args: [],
  });
  return results && results[0] ? results[0].result : { success: false, message: 'No result' };
}

// ─── Arbitrary JavaScript (MAIN world: full page privileges) ─────────────────

async function runPageScript(tabId, script) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: async (code) => {
      const serialize = (value) => {
        if (value === undefined) return "undefined";
        if (value === null) return "null";
        if (typeof value === "function") return `[Function ${value.name || "anonymous"}]`;
        if (value instanceof Element) {
          return `<${value.tagName.toLowerCase()} id="${value.id || ""}" class="${value.className || ""}">`;
        }
        try {
          const text = JSON.stringify(value, null, 2);
          return text === undefined ? String(value) : text;
        } catch {
          return String(value);
        }
      };
      try {
        let out;
        try {
          // Expression form: return (expr)
          out = await (0, eval)(`(async () => (${code}\n))()`);
        } catch (e) {
          if (!(e instanceof SyntaxError)) throw e;
          // Statement-body form: use `return` to produce a value
          out = await (0, eval)(`(async () => {\n${code}\n})()`);
        }
        return { result: serialize(out) };
      } catch (e) {
        return { error: String((e && e.message) || e) };
      }
    },
    args: [script],
  });
  return results && results[0] ? results[0].result : { error: 'No result from script' };
}

// ─── WebSocket Connection ─────────────────────────────────────────────────────

async function connectToMCPServer(tabId, injectOnAuth = false) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    if (tabId) state.trackedTabs.add(tabId);
    return;
  }

  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }

  // Prevent overlapping connect attempts
  if (state.connecting) return;
  state.connecting = true;

  try {
    state.serverUrl = normalizeServerUrl(state.serverUrl);
    const ws = new WebSocket(`${state.serverUrl}/ws/browser`);
    let pendingConnectTabId = tabId || null;

    ws.onopen = async () => {
      state.ws = ws;
      state.connected = false;
      state.authenticated = false;
      state.connecting = false;
      state.reconnectAttempts = 0;
      ws.send(JSON.stringify({ type: "AUTH", token: state.wsAuthToken || "" }));
    };

    ws.onmessage = async (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "AUTH_OK") {
          state.authenticated = true;
          state.connected = true;
          chrome.storage.local.set({ autoConnect: true });

          if (pendingConnectTabId) {
            state.trackedTabs.add(pendingConnectTabId);
            state.activeTabId = pendingConnectTabId;
            if (injectOnAuth) {
              try {
                await ensureContentScript(pendingConnectTabId);
              } catch (err) {
                console.warn("[Background] Content script injection failed on connect:", err.message);
              }
            }
          }

          try {
            const allTabs = await chrome.tabs.query({});
            const tabSnapshot = [];
            for (const tab of allTabs) {
              if (tab.url && !tab.url.startsWith("chrome://") && !tab.url.startsWith("chrome-extension://")) {
                state.trackedTabs.add(tab.id);
                tabSnapshot.push({ tab_id: tab.id, url: tab.url, title: tab.title || "" });
              }
            }
            const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
            if (activeTab) state.activeTabId = activeTab.id;
            sendToServer({ type: "TAB_SNAPSHOT", tabs: tabSnapshot, active_tab_id: state.activeTabId });
          } catch { }

          broadcastStatus(true);
          return;
        }
        if (message.type === "ACK") return;
        await handleServerMessage(message);
      } catch (err) {
        console.error("[Background] Message error:", err);
      }
    };

    ws.onerror = () => {
      state.connecting = false;
    };

    ws.onclose = () => {
      console.log("[Background] Disconnected from server");
      const shouldReconnectImmediately = state.manualReconnect;
      state.manualReconnect = false;
      state.connected = false;
      state.authenticated = false;
      state.connecting = false;
      state.ws = null;
      broadcastStatus(false);

      if (shouldReconnectImmediately) {
        const firstTab = state.activeTabId || Array.from(state.trackedTabs)[0];
        if (firstTab) {
          connectToMCPServer(firstTab);
        }
        return;
      }

      if (state.trackedTabs.size > 0) {
        if (state.reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
          state.reconnectAttempts++;
          console.log(`[Background] Reconnect attempt ${state.reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}`);
          state.reconnectTimer = setTimeout(() => {
            const firstTab = state.activeTabId || Array.from(state.trackedTabs)[0];
            connectToMCPServer(firstTab);
          }, AUTO_RECONNECT_DELAY_MS);
        } else {
          console.log("[Background] Max reconnect attempts reached. Disconnecting fully.");
          disconnectFromMCPServer();
        }
      }
    };

    state.ws = ws;
  } catch (err) {
    console.error("[Background] Connect failed:", err);
    state.connected = false;
    state.connecting = false;
    broadcastStatus(false);
  }
}

function disconnectFromMCPServer() {
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  state.manualReconnect = false;
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  state.connected = false;
  state.authenticated = false;
  state.connecting = false;
  state.trackedTabs.clear();
  state.activeTabId = null;
  broadcastStatus(false);
  chrome.storage.local.set({ autoConnect: false });
}

// ─── Content Script Injection ────────────────────────────────────────────────

async function pingContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "PING" });
    return true;
  } catch {
    return false;
  }
}

async function ensureContentScript(tabId) {
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (await pingContentScript(tabId)) return true;
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: ["overlay.js", "content.js"],
      });
      try {
        await chrome.scripting.insertCSS({ target: { tabId }, files: ["styles.css"] });
      } catch { }
    } catch (err) {
      lastError = err;
    }
    // Let an in-flight navigation settle before re-checking the listener.
    await sleep(150 * (attempt + 1));
  }
  if (await pingContentScript(tabId)) return true;
  throw new Error(
    `Could not inject content script into tab ${tabId}` +
    (lastError ? `: ${lastError.message}` : "") +
    ". Click the extension icon (Connect) on that tab, then retry."
  );
}

// ─── Overlay (visual automation indicator) ───────────────────────────────────

function overlayShow(tabId, goal) {
  if (state.overlayHideTimer) {
    clearTimeout(state.overlayHideTimer);
    state.overlayHideTimer = null;
  }
  sendToContentScript(tabId, { type: "OVERLAY_CONTROL", command: "show", goal: goal || "" }).catch(() => { });
}

function overlayLog(tabId, message) {
  sendToContentScript(tabId, { type: "OVERLAY_CONTROL", command: "log", message: message || "" }).catch(() => { });
}

function overlayHideSoon(tabId) {
  if (state.overlayHideTimer) clearTimeout(state.overlayHideTimer);
  state.overlayHideTimer = setTimeout(() => {
    state.overlayHideTimer = null;
    sendToContentScript(tabId, { type: "OVERLAY_CONTROL", command: "hide" }).catch(() => { });
  }, OVERLAY_HIDE_DELAY_MS);
}

// ─── Server Message Handler ──────────────────────────────────────────────────

function resolveTabId(tab_id) {
  return tab_id ? Number(tab_id) : (state.activeTabId || Array.from(state.trackedTabs)[0]);
}

async function handleServerMessage(message) {
  const { type, tab_id } = message;

  switch (type) {

    case "PING": {
      sendToServer({ type: "PONG" });
      break;
    }

    case "EXECUTE_ACTIONS": {
      const targetTabId = resolveTabId(tab_id);
      const { steps, request_id } = message;

      // Check for actions that need native Chrome API
      const nativeActions = [];
      const contentActions = [];
      for (const step of (steps || [])) {
        if (["go_back", "go_forward", "reload"].includes(step.action)) {
          nativeActions.push(step);
        } else {
          contentActions.push(step);
        }
      }

      try {
        overlayShow(targetTabId, `${(steps || []).length} action(s)`);

        // Execute native actions first
        for (const step of nativeActions) {
          if (step.action === "go_back") {
            await chrome.tabs.goBack(targetTabId);
          } else if (step.action === "go_forward") {
            await chrome.tabs.goForward(targetTabId);
          } else if (step.action === "reload") {
            const bypassCache = step.value === "hard";
            await chrome.tabs.reload(targetTabId, { bypassCache });
          }
          // Brief wait for navigation
          await sleep(500);
        }

        // Execute content-script actions
        let result = { results: [] };
        if (contentActions.length > 0) {
          // Native nav actions (e.g. reload) wipe the content script, so ensure
          // it is injected again after they run and before sending actions.
          await ensureContentScript(targetTabId);
          try {
            result = await sendToContentScript(targetTabId, {
              type: "EXECUTE_ACTIONS",
              steps: contentActions,
            });
          } catch (firstErr) {
            // One re-inject + retry to ride out navigation races.
            await ensureContentScript(targetTabId);
            result = await sendToContentScript(targetTabId, {
              type: "EXECUTE_ACTIONS",
              steps: contentActions,
            });
          }
        }

        // Merge native action results
        const nativeResults = nativeActions.map(a => ({ action: a.action, success: true }));
        const allResults = [...nativeResults, ...(result.results || [])];
        const failed = allResults.filter(r => !r.success).length;

        overlayLog(targetTabId, `${allResults.length} action(s): ${allResults.length - failed} ok, ${failed} failed`);
        overlayHideSoon(targetTabId);

        sendToServer({
          type: "ACTION_COMPLETE",
          tab_id: targetTabId,
          request_id,
          result: { results: allResults },
        });
      } catch (err) {
        overlayLog(targetTabId, `Error: ${err.message}`);
        overlayHideSoon(targetTabId);
        sendToServer({
          type: "ACTION_COMPLETE",
          tab_id: targetTabId,
          request_id,
          result: { error: err.message },
        });
      }
      break;
    }

    case "REQUEST_DOM": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id } = message;
      try {
        await ensureContentScript(targetTabId);
        const domState = await requestDOMExtraction(targetTabId);
        sendToServer({
          type: "DOM_UPDATE",
          tab_id: targetTabId,
          request_id,
          dom_state: domState,
        });
      } catch (err) {
        sendToServer({
          type: "DOM_UPDATE",
          tab_id: targetTabId,
          request_id,
          dom_state: {},
          error: err.message,
        });
      }
      break;
    }

    case "REQUEST_HTML": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, { type: "EXTRACT_HTML" });
        sendToServer({
          type: "HTML_RESULT",
          tab_id: targetTabId,
          request_id,
          html: response.html,
          url: response.url,
        });
      } catch (err) {
        sendToServer({
          type: "HTML_RESULT",
          tab_id: targetTabId,
          request_id,
          html: "",
          error: err.message,
        });
      }
      break;
    }

    case "EXTRACT_CODING_PROBLEM": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, { type: "EXTRACT_CODING_PROBLEM" });
        sendToServer({
          type: "CODING_PROBLEM_RESULT",
          tab_id: targetTabId,
          request_id,
          ...response,
        });
      } catch (err) {
        sendToServer({
          type: "CODING_PROBLEM_RESULT",
          tab_id: targetTabId,
          request_id,
          error: err.message,
        });
      }
      break;
    }

    case "EXTRACT_TEXT": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id, query, selector } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, {
          type: "EXTRACT_TEXT", query: query || "", selector: selector || "",
        });
        sendToServer({ type: "TEXT_EXTRACT_RESULT", tab_id: targetTabId, request_id, ...response });
      } catch (err) {
        sendToServer({ type: "TEXT_EXTRACT_RESULT", tab_id: targetTabId, request_id, error: err.message });
      }
      break;
    }

    case "GET_ELEMENT_INFO": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id, selector } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, {
          type: "GET_ELEMENT_INFO", selector,
        });
        sendToServer({ type: "ELEMENT_INFO_RESULT", tab_id: targetTabId, request_id, ...response });
      } catch (err) {
        sendToServer({ type: "ELEMENT_INFO_RESULT", tab_id: targetTabId, request_id, error: err.message });
      }
      break;
    }

    case "WAIT_FOR_ELEMENT": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id, selector, timeout, require_visible } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, {
          type: "WAIT_FOR_ELEMENT",
          selector,
          timeout: timeout || 10000,
          require_visible,
        });
        sendToServer({ type: "ELEMENT_WAIT_RESULT", tab_id: targetTabId, request_id, ...response });
      } catch (err) {
        sendToServer({ type: "ELEMENT_WAIT_RESULT", tab_id: targetTabId, request_id, found: false, error: err.message });
      }
      break;
    }

    case "EXECUTE_JS": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id, script, expression } = message;
      try {
        const response = await runPageScript(targetTabId, script != null ? script : expression);
        sendToServer({ type: "JS_RESULT", tab_id: targetTabId, request_id, ...response });
      } catch (err) {
        sendToServer({ type: "JS_RESULT", tab_id: targetTabId, request_id, error: err.message });
      }
      break;
    }

    case "SET_CODE": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id, code } = message;
      try {
        const response = await editorSetCode(targetTabId, code);
        sendToServer({ type: "SET_CODE_RESULT", tab_id: targetTabId, request_id, ...response });
      } catch (err) {
        sendToServer({ type: "SET_CODE_RESULT", tab_id: targetTabId, request_id, success: false, message: err.message });
      }
      break;
    }

    case "GET_CODE": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id } = message;
      try {
        const response = await editorGetCode(targetTabId);
        sendToServer({ type: "GET_CODE_RESULT", tab_id: targetTabId, request_id, ...response });
      } catch (err) {
        sendToServer({ type: "GET_CODE_RESULT", tab_id: targetTabId, request_id, success: false, message: err.message });
      }
      break;
    }

    case "TAKE_SCREENSHOT": {
      const targetTabId = tab_id ? Number(tab_id) : null;
      const { request_id } = message;
      try {
        const screenshot = await captureScreenshot(targetTabId);
        sendToServer({
          type: "SCREENSHOT_RESULT",
          tab_id: targetTabId,
          request_id,
          screenshot,
        });
      } catch (err) {
        console.error("[Background] Screenshot error:", err);
        sendToServer({
          type: "SCREENSHOT_RESULT",
          tab_id: targetTabId,
          request_id,
          screenshot: null,
          error: err.message,
        });
      }
      break;
    }

    case "OPEN_TAB": {
      const { url, active, request_id } = message;
      try {
        const newTab = await chrome.tabs.create({
          url: url,
          active: active !== false,
        });
        state.trackedTabs.add(newTab.id);
        if (active !== false) state.activeTabId = newTab.id;
        sendToServer({
          type: "TAB_OPENED",
          tab_id: newTab.id,
          request_id,
          url: newTab.url,
        });
      } catch (err) {
        console.error("[Background] Open tab error:", err);
        sendToServer({
          type: "TAB_OPENED",
          tab_id: null,
          request_id,
          error: err.message,
        });
      }
      break;
    }

    case "CLOSE_TAB": {
      const targetTabId = tab_id ? Number(tab_id) : null;
      const { request_id } = message;
      if (targetTabId) {
        try {
          await chrome.tabs.remove(targetTabId);
          state.trackedTabs.delete(targetTabId);
          if (state.activeTabId === targetTabId) state.activeTabId = null;
          sendToServer({ type: "TAB_CLOSED", tab_id: targetTabId, request_id });
        } catch (err) {
          console.error("[Background] Close tab error:", err);
          sendToServer({ type: "TAB_CLOSED", tab_id: targetTabId, request_id, error: err.message });
        }
      } else {
        sendToServer({ type: "TAB_CLOSED", tab_id: null, request_id, error: "No tab_id provided" });
      }
      break;
    }

    case "SWITCH_TAB": {
      const switchTabId = tab_id ? Number(tab_id) : null;
      const { request_id } = message;
      if (switchTabId) {
        try {
          await chrome.tabs.update(switchTabId, { active: true });
          const tabInfo = await chrome.tabs.get(switchTabId);
          if (tabInfo.windowId) {
            await chrome.windows.update(tabInfo.windowId, { focused: true });
          }
          state.activeTabId = switchTabId;
          sendToServer({
            type: "TAB_SWITCHED",
            tab_id: switchTabId,
            request_id,
            url: tabInfo.url || "",
            title: tabInfo.title || "",
          });
        } catch (err) {
          sendToServer({ type: "TAB_SWITCHED", tab_id: switchTabId, request_id, error: err.message });
        }
      }
      break;
    }

    case "DEBUGGER_CLICK": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id, x, y } = message;
      const result = await debuggerClick(targetTabId, x, y);
      sendToServer({ type: "DEBUGGER_CLICK_RESULT", tab_id: targetTabId, request_id, ...result });
      break;
    }

    case "EXTRACT_QUIZ_STRUCTURE": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, { type: "EXTRACT_QUIZ_STRUCTURE" });
        sendToServer({
          type: "QUIZ_STRUCTURE_RESULT",
          tab_id: targetTabId,
          request_id,
          ...response,
        });
      } catch (err) {
        sendToServer({
          type: "QUIZ_STRUCTURE_RESULT",
          tab_id: targetTabId,
          request_id,
          error: err.message,
        });
      }
      break;
    }

    case "EXTRACT_PAGE_CONTEXT": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, { type: "EXTRACT_PAGE_CONTEXT" });
        sendToServer({
          type: "PAGE_CONTEXT_RESULT",
          tab_id: targetTabId,
          request_id,
          ...response,
        });
      } catch (err) {
        sendToServer({
          type: "PAGE_CONTEXT_RESULT",
          tab_id: targetTabId,
          request_id,
          error: err.message,
          kind: "unknown",
        });
      }
      break;
    }

    case "FIND_BY_TEXT": {
      const targetTabId = resolveTabId(tab_id);
      const { request_id, text, mode, tag, role } = message;
      try {
        await ensureContentScript(targetTabId);
        const response = await sendToContentScript(targetTabId, {
          type: "FIND_BY_TEXT",
          text, mode, tag, role,
        });
        sendToServer({
          type: "FIND_BY_TEXT_RESULT",
          tab_id: targetTabId,
          request_id,
          ...response,
        });
      } catch (err) {
        sendToServer({
          type: "FIND_BY_TEXT_RESULT",
          tab_id: targetTabId,
          request_id,
          error: err.message,
        });
      }
      break;
    }

    default:
      console.debug("[Background] Unhandled server message:", type);
  }
}

// ─── Screenshot Capture ──────────────────────────────────────────────────────

async function captureScreenshot(tabId) {
  if (tabId) {
    try {
      const tab = await chrome.tabs.get(tabId);
      await chrome.tabs.update(tabId, { active: true });
      if (tab.windowId) {
        await chrome.windows.update(tab.windowId, { focused: true });
      }
      await sleep(300);
    } catch { }
  }

  const dataUrl = await chrome.tabs.captureVisibleTab(null, {
    format: "png",
    quality: 85,
  });

  if (dataUrl && dataUrl.startsWith("data:image/png;base64,")) {
    return dataUrl.replace("data:image/png;base64,", "");
  }
  return dataUrl;
}

// ─── Content Script Communication ────────────────────────────────────────────

async function sendToContentScript(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve(response);
      }
    });
  });
}

async function requestDOMExtraction(tabId) {
  return await sendToContentScript(tabId, { type: "EXTRACT_DOM" });
}

// ─── Server Communication ────────────────────────────────────────────────────

function sendToServer(data) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN && state.authenticated) {
    state.ws.send(JSON.stringify(data));
    return true;
  }
  return false;
}

// ─── Utility ─────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ─── Broadcasting ────────────────────────────────────────────────────────────

function broadcastStatus(connected) {
  broadcastToAll({
    type: "CONNECTION_STATUS",
    connected,
    serverUrl: state.serverUrl,
    tabCount: state.trackedTabs.size,
    trackedTabs: Array.from(state.trackedTabs),
  });
}

function broadcastToAll(message) {
  chrome.runtime.sendMessage(message).catch(() => { });
}

// ─── Message Handler from Popup/Content Scripts ──────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      switch (message.type) {
        case "CONNECT": {
          const { tabId } = message;
          if (!tabId) {
            sendResponse({ success: false, error: "Missing tabId" });
            return;
          }
          await connectToMCPServer(tabId, true);
          // Wait briefly for connection if still connecting
          if (!state.connected) {
            await sleep(1500);
          }
          sendResponse({ success: true, connected: state.connected });
          break;
        }

        case "DISCONNECT": {
          disconnectFromMCPServer();
          sendResponse({ success: true });
          break;
        }

        case "GET_STATUS": {
          sendResponse({
            connected: state.connected,
            authenticated: state.authenticated,
            trackedTabs: Array.from(state.trackedTabs),
            tabCount: state.trackedTabs.size,
            serverUrl: state.serverUrl,
            wsAuthToken: state.wsAuthToken,
          });
          break;
        }

        case "SET_SERVER_URL": {
          const nextConfig = extractServerConfig(message.serverUrl, state.wsAuthToken);
          state.serverUrl = nextConfig.serverUrl;
          state.wsAuthToken = nextConfig.wsAuthToken;
          await chrome.storage.local.set({
            mcpServerUrl: nextConfig.serverUrl,
            wsAuthToken: nextConfig.wsAuthToken,
          });

          if (state.connected && state.ws) {
            state.manualReconnect = true;
            state.ws.close();
          } else {
            broadcastStatus(false);
          }

          sendResponse({ success: true, serverUrl: state.serverUrl });
          break;
        }

        case "SET_AUTH_TOKEN": {
          const token = (message.wsAuthToken || "").trim();
          state.wsAuthToken = token;
          await chrome.storage.local.set({ wsAuthToken: token });

          if (state.connected && state.ws) {
            state.manualReconnect = true;
            state.ws.close();
          } else {
            broadcastStatus(false);
          }

          sendResponse({ success: true });
          break;
        }

        case "SET_CODE": {
          // From content script (browser_act set_code step) — MAIN-world
          // editor objects are only reachable from the service worker.
          const codeTabId = message.tabId || state.activeTabId;
          if (!codeTabId) {
            sendResponse({ success: false, message: "No tab ID" });
            return;
          }
          const setResult = await editorSetCode(codeTabId, message.code || "");
          sendResponse(setResult);
          break;
        }

        case "GET_CODE": {
          const getCodeTabId = message.tabId || state.activeTabId;
          if (!getCodeTabId) {
            sendResponse({ success: false, message: "No tab ID" });
            return;
          }
          const getCodeResult = await editorGetCode(getCodeTabId);
          sendResponse(getCodeResult);
          break;
        }

        case "EMERGENCY_STOP": {
          if (state.overlayHideTimer) {
            clearTimeout(state.overlayHideTimer);
            state.overlayHideTimer = null;
          }
          const stopTabId = state.activeTabId || Array.from(state.trackedTabs)[0];
          if (stopTabId) {
            try {
              await sendToContentScript(stopTabId, { type: "OVERLAY_CONTROL", command: "hide" });
            } catch { }
          }
          sendResponse({ success: true });
          break;
        }

        case "DEBUGGER_CLICK": {
          const { tabId: clickTabId, x, y } = message;
          const resolvedTabId = clickTabId || state.activeTabId;
          if (!resolvedTabId) {
            sendResponse({ success: false, error: "No tab ID" });
            return;
          }
          const clickResult = await debuggerClick(resolvedTabId, x, y);
          sendResponse(clickResult);
          break;
        }

        default:
          sendResponse({ error: `Unknown: ${message.type}` });
      }
    } catch (err) {
      console.error("[Background] Handler error:", err);
      sendResponse({ success: false, error: err.message });
    }
  })();
  return true;
});

// ─── Tab Lifecycle Listeners ─────────────────────────────────────────────────

chrome.tabs.onActivated.addListener((activeInfo) => {
  state.activeTabId = activeInfo.tabId;
  state.trackedTabs.add(activeInfo.tabId);
  if (state.connected) {
    (async () => {
      const tab = await chrome.tabs.get(activeInfo.tabId).catch(() => null);
      sendToServer({
        type: "TAB_SWITCHED",
        tab_id: activeInfo.tabId,
        url: tab?.url || "",
        title: tab?.title || "",
      });
    })();
  }
});

chrome.webNavigation.onCompleted.addListener(async (details) => {
  if (details.frameId !== 0) return;
  if (!state.connected) return;

  const tab = await chrome.tabs.get(details.tabId).catch(() => null);
  if (!tab?.url || tab.url.startsWith("chrome://") || tab.url.startsWith("chrome-extension://")) return;

  state.trackedTabs.add(details.tabId);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (state.trackedTabs.has(tabId)) {
    state.trackedTabs.delete(tabId);
    if (state.activeTabId === tabId) state.activeTabId = null;
    sendToServer({ type: "TAB_CLOSED", tab_id: tabId });
    if (state.trackedTabs.size === 0 && state.connected) {
      disconnectFromMCPServer();
    }
  }
});

chrome.tabs.onCreated.addListener((tab) => {
  if (state.connected && tab.id) {
    if (!tab.url || tab.url.startsWith("chrome://") || tab.url.startsWith("chrome-extension://")) return;
    state.trackedTabs.add(tab.id);
    sendToServer({
      type: "TAB_CREATED",
      tab_id: tab.id,
      url: tab.url,
      title: tab.title || "",
    });
  }
});

// ─── Auto-Reconnect on SW Restart ────────────────────────────────────────────

async function tryAutoConnect() {
  try {
    const stored = await chrome.storage.local.get(["autoConnect", "mcpServerUrl", "wsAuthToken"]);
    const config = extractServerConfig(stored.mcpServerUrl, stored.wsAuthToken || "");
    state.serverUrl = config.serverUrl;
    state.wsAuthToken = config.wsAuthToken;
    if (stored.autoConnect) {
      const tabs = await chrome.tabs.query({});
      const validTab = tabs.find(
        (t) => t.url && !t.url.startsWith("chrome://") && !t.url.startsWith("chrome-extension://")
      );
      if (validTab) {
        await connectToMCPServer(validTab.id);
      }
    }
  } catch { }
}

loadServerUrlSetting().then(() => tryAutoConnect());

console.log("[AI Agent] Background service worker v4 initialized.");
