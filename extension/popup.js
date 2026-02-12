/* popup.js — v4.0 Popup Controller */
(function () {
  'use strict';

  // ── State ───────────────────────────────────────────────
  let connected = false;
  let running = false;
  let startupWatchdog = null;

  // ── Elements ────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];

  const connDot = $('#connDot');
  const connText = $('#connText');
  const connTabsCount = $('#connTabsCount');
  const connBtn = $('#connBtn');
  const errorBox = $('#errorBox');
  const taskInput = $('#taskInput');
  const charCount = $('#charCount');
  const runBtn = $('#runBtn');
  const stopBtn = $('#stopBtn');
  const quickBtns = $$('.quick-btn');
  const progressPanel = $('#progressPanel');
  const progressStep = $('#progressStep');
  const progressFill = $('#progressFill');
  const progressLog = $('#progressLog');
  const progressResult = $('#progressResult');
  const historyList = $('#historyList');
  const clearHistoryBtn = $('#clearHistoryBtn');
  const stepsSlider = $('#stepsSlider');
  const stepsValue = $('#stepsValue');
  const tabs = $$('.header-tab');
  const tabPages = $$('.tab-page');

  // ── Tab Switching ───────────────────────────────────────
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const id = tab.dataset.tab;
      tabs.forEach(t => t.classList.toggle('active', t === tab));
      tabPages.forEach(p => p.classList.toggle('active', p.id === `tab-${id}`));
      if (id === 'history') loadHistory();
    });
  });

  // ── Settings ────────────────────────────────────────────
  stepsSlider.addEventListener('input', () => {
    stepsValue.textContent = stepsSlider.value;
    chrome.storage.local.set({ maxSteps: parseInt(stepsSlider.value) });
  });

  chrome.storage.local.get(['maxSteps'], (res) => {
    if (res.maxSteps) {
      stepsSlider.value = res.maxSteps;
      stepsValue.textContent = res.maxSteps;
    }
  });

  // ── Char Count ──────────────────────────────────────────
  taskInput.addEventListener('input', () => {
    charCount.textContent = taskInput.value.length;
  });

  // ── Keyboard Shortcut ───────────────────────────────────
  taskInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!runBtn.disabled) runTask();
    }
  });

  // ── Connection ──────────────────────────────────────────
  const connQuality = $('#connQuality');
  const llmBadge = $('#llmBadge');

  function updateConnectionUI(isConnected, tabCount) {
    connected = isConnected;
    connDot.className = 'conn-indicator ' + (isConnected ? 'on' : 'off');
    connText.className = 'conn-text ' + (isConnected ? 'on' : '');
    connText.textContent = isConnected ? 'Connected' : 'Disconnected';
    connTabsCount.textContent = isConnected && tabCount ? `${tabCount} tab${tabCount > 1 ? 's' : ''}` : '';
    connBtn.textContent = isConnected ? 'Disconnect' : 'Connect';
    connBtn.className = 'conn-btn' + (isConnected ? ' danger' : '');

    // Connection quality indicator
    if (connQuality) {
      connQuality.className = 'conn-quality ' + (isConnected ? 'good' : '');
    }

    // LLM badge
    if (llmBadge) {
      llmBadge.textContent = isConnected ? 'MCP ACTIVE' : 'MCP';
      llmBadge.style.opacity = isConnected ? '1' : '0.5';
    }

    updateActionButtons();
  }

  function updateActionButtons() {
    const canRun = connected && !running;
    runBtn.disabled = !canRun;
    quickBtns.forEach(btn => btn.disabled = !canRun);
    stopBtn.disabled = !running;
    taskInput.disabled = running;
  }

  connBtn.addEventListener('click', async () => {
    if (connected) {
      chrome.runtime.sendMessage({ type: 'DISCONNECT' });
    } else {
      // Get current tab and send its ID with CONNECT
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) {
          chrome.runtime.sendMessage({ type: 'CONNECT', tabId: tab.id });
        } else {
          showError('No active tab found');
        }
      } catch (err) {
        showError('Failed to get active tab: ' + err.message);
      }
    }
  });

  // ── Get Status on Open ─────────────────────────────────
  chrome.runtime.sendMessage({ type: 'GET_STATUS' }, (res) => {
    if (chrome.runtime.lastError) {
      updateConnectionUI(false, 0);
      return;
    }
    if (res) {
      updateConnectionUI(res.connected, res.tabCount || 0);
      if (res.running) {
        running = true;
        updateActionButtons();
        showProgress();
      }
    }
  });

  // ── Task Execution ─────────────────────────────────────
  function runTask(goal) {
    const taskGoal = goal || taskInput.value.trim();
    if (!taskGoal) {
      showError('Please enter a task or select a quick action.');
      return;
    }
    clearError();
    running = true;
    updateActionButtons();
    resetProgress();
    showProgress();

    if (startupWatchdog) clearTimeout(startupWatchdog);
    startupWatchdog = setTimeout(() => {
      if (running) {
        showError('Task did not start in time. Check MCP connection/session and try again.');
        finishProgress('error', 'Task start timeout');
      }
    }, 25000);

    const maxSteps = parseInt(stepsSlider.value) || 30;
    chrome.runtime.sendMessage({
      type: 'RUN_TASK',
      goal: taskGoal,
      maxSteps
    }, (res) => {
      if (chrome.runtime.lastError) {
        showError('Failed to send task: ' + chrome.runtime.lastError.message);
        running = false;
        updateActionButtons();
        return;
      }
      if (res && res.error) {
        showError(res.error);
        running = false;
        updateActionButtons();
        if (startupWatchdog) {
          clearTimeout(startupWatchdog);
          startupWatchdog = null;
        }
      }
    });
  }

  runBtn.addEventListener('click', () => runTask());

  // ── Quick Actions ───────────────────────────────────────
  quickBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const goal = btn.dataset.goal;
      if (goal) runTask(goal);
    });
  });

  // ── Stop Task ───────────────────────────────────────────
  stopBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'STOP_TASK' }, (res) => {
      if (chrome.runtime.lastError) return;
    });
  });

  // ── Progress Display ───────────────────────────────────
  function showProgress() {
    progressPanel.classList.add('visible');
  }

  function resetProgress() {
    progressLog.innerHTML = '';
    progressResult.textContent = '';
    progressResult.className = 'progress-result';
    progressStep.textContent = '0/30';
    progressFill.style.width = '0%';
  }

  function addLogEntry(text) {
    const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `<span class="time">${time}</span>${escapeHtml(text)}`;
    progressLog.appendChild(entry);
    progressLog.scrollTop = progressLog.scrollHeight;
  }

  function updateProgress(step, maxSteps, description) {
    const max = maxSteps || 30;
    progressStep.textContent = `${step}/${max}`;
    progressFill.style.width = `${Math.min((step / max) * 100, 100)}%`;
    if (description) addLogEntry(description);
  }

  function finishProgress(status, result) {
    running = false;
    if (startupWatchdog) {
      clearTimeout(startupWatchdog);
      startupWatchdog = null;
    }
    updateActionButtons();
    if (status === 'completed') {
      progressResult.className = 'progress-result success';
      progressResult.textContent = '✓ ' + (result || 'Task completed successfully');
    } else if (status === 'error') {
      progressResult.className = 'progress-result error';
      progressResult.textContent = '✕ ' + (result || 'Task failed');
    } else if (status === 'max_steps') {
      progressResult.className = 'progress-result error';
      progressResult.textContent = '⚠ ' + (result || 'Task stopped at maximum steps');
    } else if (status === 'stopped') {
      progressResult.className = 'progress-result error';
      progressResult.textContent = '⬛ Task stopped by user';
    }
    // Save to history
    saveHistoryEntry(taskInput.value.trim() || 'Quick Action', status);
  }

  // ── Messages from Background ────────────────────────────
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    switch (msg.type) {
      case 'CONNECTION_STATUS':
        updateConnectionUI(msg.connected, msg.tabCount || 0);
        break;

      case 'TASK_PROGRESS':
        if (startupWatchdog) {
          clearTimeout(startupWatchdog);
          startupWatchdog = null;
        }
        if (msg.step !== undefined) {
          updateProgress(msg.step, msg.maxSteps || msg.max_steps, msg.description || msg.message);
        }
        if (msg.status === 'completed' || msg.status === 'error' || msg.status === 'stopped' || msg.status === 'max_steps') {
          finishProgress(msg.status, msg.result || msg.summary || msg.message);
        }
        break;

      case 'TASK_LOG':
        addLogEntry(msg.text);
        break;

      case 'TASK_ERROR':
        showError(msg.error);
        running = false;
        updateActionButtons();
        break;
    }
  });

  // ── Error Display ───────────────────────────────────────
  function showError(text) {
    errorBox.textContent = text;
    errorBox.classList.add('visible');
    setTimeout(() => clearError(), 8000);
  }

  function clearError() {
    errorBox.classList.remove('visible');
  }

  // ── History ─────────────────────────────────────────────
  const MAX_HISTORY = 30;

  function saveHistoryEntry(goal, status) {
    if (!goal) return;
    chrome.storage.local.get(['taskHistory'], (res) => {
      const history = res.taskHistory || [];
      history.unshift({
        goal: goal.substring(0, 200),
        status,
        time: Date.now()
      });
      if (history.length > MAX_HISTORY) history.length = MAX_HISTORY;
      chrome.storage.local.set({ taskHistory: history });
    });
  }

  function loadHistory() {
    chrome.storage.local.get(['taskHistory'], (res) => {
      const history = res.taskHistory || [];
      if (history.length === 0) {
        historyList.innerHTML = '<div class="history-empty">No task history yet</div>';
        return;
      }
      historyList.innerHTML = history.map(item => {
        const ago = timeAgo(item.time);
        const statusClass = item.status === 'completed' ? 'completed'
          : item.status === 'error' ? 'error' : 'stopped';
        return `
          <div class="history-item" data-goal="${escapeAttr(item.goal)}">
            <div class="history-goal">${escapeHtml(item.goal)}</div>
            <div class="history-meta">
              <span class="history-status ${statusClass}">${item.status}</span>
              <span>${ago}</span>
            </div>
          </div>`;
      }).join('');

      // Click to re-use a goal
      historyList.querySelectorAll('.history-item').forEach(el => {
        el.addEventListener('click', () => {
          taskInput.value = el.dataset.goal;
          charCount.textContent = taskInput.value.length;
          // Switch to main tab
          tabs[0].click();
        });
      });
    });
  }

  clearHistoryBtn.addEventListener('click', () => {
    chrome.storage.local.set({ taskHistory: [] }, () => {
      loadHistory();
    });
  });

  function timeAgo(ts) {
    const diff = Date.now() - ts;
    const s = Math.floor(diff / 1000);
    if (s < 60) return 'just now';
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.floor(h / 24);
    return `${d}d ago`;
  }

  // ── Utilities ───────────────────────────────────────────
  function escapeHtml(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
  }

  function escapeAttr(str) {
    return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
})();
