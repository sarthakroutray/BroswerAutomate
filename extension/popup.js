/* popup.js — Bridge console: connection state, controls, settings */

(function () {
  'use strict';

  // ── Elements ─────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);

  const board = $('#board');
  const stateTitle = $('#stateTitle');
  const stateMeta = $('#stateMeta');
  const primaryBtn = $('#primaryBtn');

  const endpointInput = $('#endpoint');
  const copyBtn = $('#copyBtn');
  const urlSaved = $('#urlSaved');

  const tokenInput = $('#token');
  const tokenToggle = $('#tokenToggle');
  const tokenSaved = $('#tokenSaved');

  const notice = $('#notice');

  // ── State ────────────────────────────────────────────────
  let status = 'checking'; // checking | connecting | connected | disconnected | error
  let tabCount = 0;
  let lastEndpoint = '';
  let savedTimer = null;
  let noticeTimer = null;

  const SAVED_MS = 1800;
  const NOTICE_MS = 9000;

  // ── Messaging ────────────────────────────────────────────
  function send(message) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ error: chrome.runtime.lastError.message });
        } else {
          resolve(response || {});
        }
      });
    });
  }

  // ── Rendering ────────────────────────────────────────────
  function hostFrom(rawUrl) {
    const value = String(rawUrl || '').trim();
    if (!value) return 'ws://localhost:8000';
    try {
      const parsed = new URL(value.includes('://') ? value : `ws://${value}`);
      return parsed.host + (parsed.pathname !== '/' ? parsed.pathname : '');
    } catch {
      return value.replace(/[?#].*$/, '');
    }
  }

  function render(next) {
    status = next;

    const running = status === 'checking' || status === 'connecting';
    board.dataset.state = running ? 'connecting' : status;

    if (status === 'connected') {
      stateTitle.textContent = 'Connected';
      stateMeta.textContent = `${hostFrom(lastEndpoint)} · ${tabCount} tab${tabCount === 1 ? '' : 's'}`;
    } else if (status === 'connecting') {
      stateTitle.textContent = 'Connecting';
      stateMeta.textContent = hostFrom(lastEndpoint);
    } else if (status === 'checking') {
      stateTitle.textContent = 'Checking connection';
      stateMeta.textContent = hostFrom(lastEndpoint);
    } else if (status === 'error') {
      stateTitle.textContent = 'Connection failed';
      stateMeta.textContent = hostFrom(lastEndpoint);
    } else {
      stateTitle.textContent = 'Not connected';
      stateMeta.textContent = hostFrom(lastEndpoint);
    }

    if (running) {
      primaryBtn.className = 'btn btn--primary';
      primaryBtn.textContent = 'Connecting';
      primaryBtn.disabled = true;
      primaryBtn.setAttribute('aria-busy', 'true');
    } else if (status === 'connected') {
      primaryBtn.className = 'btn btn--quiet btn--danger-quiet';
      primaryBtn.textContent = 'Disconnect';
      primaryBtn.disabled = false;
      primaryBtn.removeAttribute('aria-busy');
    } else {
      primaryBtn.className = 'btn btn--primary';
      primaryBtn.textContent = 'Connect';
      primaryBtn.disabled = false;
      primaryBtn.removeAttribute('aria-busy');
    }
  }

  function showSaved(chip) {
    chip.textContent = 'Saved';
    chip.dataset.show = 'true';
    if (savedTimer) clearTimeout(savedTimer);
    savedTimer = setTimeout(() => {
      chip.dataset.show = 'false';
      setTimeout(() => { chip.textContent = ''; }, 160);
    }, SAVED_MS);
  }

  function showNotice(text) {
    notice.textContent = text;
    notice.dataset.show = 'true';
    if (noticeTimer) clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice.dataset.show = 'false'; }, NOTICE_MS);
  }

  function clearNotice() {
    if (noticeTimer) clearTimeout(noticeTimer);
    notice.dataset.show = 'false';
  }

  // ── Primary action ───────────────────────────────────────
  primaryBtn.addEventListener('click', async () => {
    clearNotice();

    if (status === 'connected') {
      await send({ type: 'DISCONNECT' });
      tabCount = 0;
      render('disconnected');
      return;
    }

    render('connecting');
    let tabId = null;
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      tabId = tab ? tab.id : null;
    } catch { }

    if (tabId == null) {
      render('error');
      showNotice('No active tab to bridge. Open a page, then connect.');
      return;
    }

    const res = await send({ type: 'CONNECT', tabId });
    if (res.error) {
      render('error');
      showNotice(res.error);
      return;
    }
    if (res.connected) {
      render('connected');
    } else {
      render('error');
      showNotice('Could not reach the bridge. Is the MCP server running?');
    }
  });

  // ── Endpoint setting ─────────────────────────────────────
  async function saveEndpoint() {
    const candidate = endpointInput.value.trim();
    if (!candidate || candidate === lastEndpoint) {
      endpointInput.value = lastEndpoint || endpointInput.value;
      return;
    }
    const res = await send({ type: 'SET_SERVER_URL', serverUrl: candidate });
    if (res.error || !res.success) {
      endpointInput.value = lastEndpoint;
      showNotice(res.error || 'Could not save the endpoint.');
      return;
    }
    lastEndpoint = res.serverUrl || candidate;
    endpointInput.value = lastEndpoint;
    render(status);
    showSaved(urlSaved);
  }

  endpointInput.addEventListener('blur', saveEndpoint);
  endpointInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      endpointInput.blur();
    }
  });

  copyBtn.addEventListener('click', async () => {
    const value = endpointInput.value.trim();
    if (!value) return;
    let copied = false;
    try {
      await navigator.clipboard.writeText(value);
      copied = true;
    } catch {
      try {
        const scratch = document.createElement('textarea');
        scratch.value = value;
        scratch.setAttribute('readonly', '');
        scratch.style.position = 'fixed';
        scratch.style.opacity = '0';
        document.body.appendChild(scratch);
        scratch.select();
        copied = document.execCommand('copy');
        document.body.removeChild(scratch);
      } catch { copied = false; }
    }
    if (!copied) {
      showNotice('Could not copy. Select the endpoint and copy manually.');
      return;
    }
    copyBtn.textContent = 'Copied';
    copyBtn.dataset.done = 'true';
    copyBtn.setAttribute('aria-label', 'Endpoint copied');
    setTimeout(() => {
      copyBtn.textContent = 'Copy';
      copyBtn.dataset.done = 'false';
      copyBtn.setAttribute('aria-label', 'Copy endpoint');
    }, 1400);
  });

  // ── Auth token setting ───────────────────────────────────
  async function saveToken() {
    const token = tokenInput.value.trim();
    const res = await send({ type: 'SET_AUTH_TOKEN', wsAuthToken: token });
    if (res.error) {
      showNotice(res.error);
      return;
    }
    showSaved(tokenSaved);
  }

  tokenInput.addEventListener('blur', saveToken);
  tokenInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      tokenInput.blur();
    }
  });

  tokenToggle.addEventListener('click', () => {
    const revealed = tokenInput.type === 'text';
    tokenInput.type = revealed ? 'password' : 'text';
    tokenToggle.textContent = revealed ? 'Show' : 'Hide';
    tokenToggle.setAttribute('aria-pressed', String(!revealed));
    tokenToggle.setAttribute('aria-label', revealed ? 'Show auth token' : 'Hide auth token');
    tokenInput.focus();
  });

  // ── Background messages ──────────────────────────────────
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type !== 'CONNECTION_STATUS') return;
    if (msg.serverUrl) {
      lastEndpoint = msg.serverUrl;
      endpointInput.value = msg.serverUrl;
    }
    tabCount = msg.tabCount || 0;
    render(msg.connected ? 'connected' : 'disconnected');
  });

  // ── Initial load ─────────────────────────────────────────
  (async () => {
    const res = await send({ type: 'GET_STATUS' });
    if (res.error) {
      render('error');
      showNotice('Extension background is unavailable. Reopen the popup.');
      return;
    }
    lastEndpoint = res.serverUrl || lastEndpoint;
    if (lastEndpoint) endpointInput.value = lastEndpoint;
    if (res.wsAuthToken) tokenInput.value = res.wsAuthToken;
    tabCount = res.tabCount || 0;
    render(res.connected ? 'connected' : 'disconnected');
  })();
})();
