/**
 * overlay.js — AI Control Overlay System v4
 *
 * Renders a full-screen overlay with Shadow DOM isolation when automation is active.
 * Features:
 *   - Semi-transparent backdrop blocking user interaction
 *   - Animated gradient border glow around page
 *   - Floating "AI is controlling this page" badge with current goal
 *   - Live action log (streaming what the AI is doing)
 *   - Emergency stop button (always clickable)
 *   - Smooth CSS keyframe animations (fade, pulse)
 *   - Non-blocking performance (composited layers, no layout thrashing)
 *
 * v4 fix: Keyboard blocking now only blocks user-initiated events, not
 * programmatic (isTrusted === false) events from AI typing actions.
 *
 * Uses Shadow DOM to isolate overlay styles from the host page.
 */

// ─── Guard against double-injection ─────────────────────────────────────────

if (!window.__aiAgentOverlayLoaded) {
  window.__aiAgentOverlayLoaded = true;

  // ─── Overlay State ──────────────────────────────────────────────────────

  const overlayState = {
    visible: false,
    hostEl: null,
    shadowRoot: null,
    logEntries: [],
    maxLogEntries: 50,
    currentGoal: "",
  };

  // ─── Shadow DOM Construction ────────────────────────────────────────────

  function createOverlayHost() {
    if (overlayState.hostEl) return;

    const host = document.createElement("div");
    host.id = "__ai-agent-overlay-host";
    host.style.cssText = `
      position: fixed !important;
      top: 0 !important;
      left: 0 !important;
      width: 100vw !important;
      height: 100vh !important;
      z-index: 2147483647 !important;
      pointer-events: none !important;
      display: none !important;
    `;

    const shadow = host.attachShadow({ mode: "closed" });
    overlayState.hostEl = host;
    overlayState.shadowRoot = shadow;

    shadow.innerHTML = getOverlayTemplate();
    attachOverlayEvents(shadow);

    document.documentElement.appendChild(host);
  }

  function getOverlayTemplate() {
    return `
      <style>
        *, *::before, *::after {
          box-sizing: border-box;
          margin: 0;
          padding: 0;
        }

        /* ── Full-screen backdrop ── */
        .ai-overlay-backdrop {
          position: fixed;
          top: 0; left: 0;
          width: 100vw; height: 100vh;
          background: rgba(0, 0, 0, 0.15);
          pointer-events: all;
          opacity: 0;
          transition: opacity 0.5s ease;
          z-index: 1;
        }
        .ai-overlay-backdrop.visible { opacity: 1; }

        /* ── Animated border glow ── */
        .ai-border-glow {
          position: fixed;
          top: 0; left: 0;
          width: 100vw; height: 100vh;
          pointer-events: none;
          z-index: 2;
          opacity: 0;
          transition: opacity 0.5s ease;
        }
        .ai-border-glow.visible { opacity: 1; }

        .ai-border-glow::before {
          content: '';
          position: absolute;
          top: 0; left: 0; right: 0; bottom: 0;
          border: 3px solid transparent;
          border-image: linear-gradient(90deg, #6366f1, #8b5cf6, #a78bfa, #c4b5fd, #8b5cf6, #6366f1) 1;
          animation: borderGlow 3s linear infinite;
        }

        @keyframes borderGlow {
          0%   { border-image-source: linear-gradient(0deg, #6366f1, #8b5cf6, #a78bfa, #c4b5fd, #8b5cf6, #6366f1); }
          25%  { border-image-source: linear-gradient(90deg, #6366f1, #8b5cf6, #a78bfa, #c4b5fd, #8b5cf6, #6366f1); }
          50%  { border-image-source: linear-gradient(180deg, #6366f1, #8b5cf6, #a78bfa, #c4b5fd, #8b5cf6, #6366f1); }
          75%  { border-image-source: linear-gradient(270deg, #6366f1, #8b5cf6, #a78bfa, #c4b5fd, #8b5cf6, #6366f1); }
          100% { border-image-source: linear-gradient(360deg, #6366f1, #8b5cf6, #a78bfa, #c4b5fd, #8b5cf6, #6366f1); }
        }

        .ai-border-glow::after {
          content: '';
          position: absolute;
          top: -2px; left: -2px; right: -2px; bottom: -2px;
          background:
            radial-gradient(ellipse at 0% 0%, rgba(99, 102, 241, 0.3) 0%, transparent 50%),
            radial-gradient(ellipse at 100% 0%, rgba(139, 92, 246, 0.3) 0%, transparent 50%),
            radial-gradient(ellipse at 100% 100%, rgba(99, 102, 241, 0.3) 0%, transparent 50%),
            radial-gradient(ellipse at 0% 100%, rgba(139, 92, 246, 0.3) 0%, transparent 50%);
          animation: cornerPulse 2s ease-in-out infinite alternate;
          pointer-events: none;
        }

        @keyframes cornerPulse {
          0% { opacity: 0.5; }
          100% { opacity: 1; }
        }

        /* ── Floating AI Badge ── */
        .ai-badge {
          position: fixed;
          top: 16px;
          left: 50%;
          transform: translateX(-50%) translateY(-80px);
          background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
          color: #ffffff;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          font-size: 14px;
          font-weight: 600;
          padding: 10px 24px;
          border-radius: 30px;
          box-shadow: 0 4px 24px rgba(79, 70, 229, 0.4), 0 0 0 1px rgba(255,255,255,0.1);
          display: flex;
          align-items: center;
          gap: 10px;
          z-index: 4;
          pointer-events: none;
          transition: transform 0.5s cubic-bezier(0.34, 1.56, 0.64, 1);
          animation: badgePulse 2s ease-in-out infinite;
          max-width: 90vw;
          text-align: center;
        }
        .ai-badge.visible {
          transform: translateX(-50%) translateY(0);
        }

        @keyframes badgePulse {
          0%, 100% { box-shadow: 0 4px 24px rgba(79, 70, 229, 0.4), 0 0 0 1px rgba(255,255,255,0.1); }
          50% { box-shadow: 0 4px 32px rgba(79, 70, 229, 0.6), 0 0 0 1px rgba(255,255,255,0.2); }
        }

        .ai-badge-dot {
          width: 8px; height: 8px;
          border-radius: 50%;
          background: #34d399;
          animation: dotBlink 1.5s ease-in-out infinite;
          flex-shrink: 0;
        }

        @keyframes dotBlink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }

        .ai-badge-text {
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }

        /* ── Action Log Panel ── */
        .ai-log-panel {
          position: fixed;
          bottom: 80px;
          right: 16px;
          width: 380px;
          max-height: 300px;
          background: rgba(15, 15, 25, 0.92);
          backdrop-filter: blur(16px);
          border: 1px solid rgba(99, 102, 241, 0.3);
          border-radius: 12px;
          overflow: hidden;
          z-index: 4;
          pointer-events: all;
          opacity: 0;
          transform: translateY(20px);
          transition: opacity 0.4s ease, transform 0.4s ease;
          font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
        }
        .ai-log-panel.visible {
          opacity: 1;
          transform: translateY(0);
        }

        .ai-log-header {
          padding: 10px 14px;
          font-size: 11px;
          font-weight: 600;
          color: #a5b4fc;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          border-bottom: 1px solid rgba(99, 102, 241, 0.2);
          background: rgba(30, 27, 75, 0.5);
          display: flex;
          align-items: center;
          justify-content: space-between;
        }

        .ai-log-goal {
          font-size: 10px;
          color: #c4b5fd;
          font-weight: 400;
          text-transform: none;
          letter-spacing: normal;
          max-width: 220px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .ai-log-body {
          padding: 8px 0;
          max-height: 250px;
          overflow-y: auto;
          scroll-behavior: smooth;
        }
        .ai-log-body::-webkit-scrollbar { width: 4px; }
        .ai-log-body::-webkit-scrollbar-track { background: transparent; }
        .ai-log-body::-webkit-scrollbar-thumb { background: rgba(99, 102, 241, 0.3); border-radius: 2px; }

        .ai-log-entry {
          padding: 4px 14px;
          font-size: 11px;
          line-height: 1.5;
          color: #c7d2fe;
          border-left: 2px solid transparent;
          animation: logSlideIn 0.3s ease-out;
        }
        .ai-log-entry:last-child {
          color: #e0e7ff;
          border-left-color: #6366f1;
          background: rgba(99, 102, 241, 0.05);
        }

        @keyframes logSlideIn {
          from { opacity: 0; transform: translateX(10px); }
          to { opacity: 1; transform: translateX(0); }
        }

        .ai-log-time {
          color: #6366f1;
          margin-right: 6px;
          font-size: 10px;
        }

        /* ── Emergency Stop Button ── */
        .ai-stop-btn {
          position: fixed;
          bottom: 20px;
          right: 16px;
          background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%);
          color: #ffffff;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          font-size: 13px;
          font-weight: 700;
          padding: 12px 28px;
          border: none;
          border-radius: 30px;
          cursor: pointer;
          z-index: 5;
          pointer-events: all;
          box-shadow: 0 4px 20px rgba(220, 38, 38, 0.4);
          display: flex;
          align-items: center;
          gap: 8px;
          opacity: 0;
          transform: translateY(20px);
          transition: opacity 0.4s ease, transform 0.4s ease, box-shadow 0.2s ease;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }
        .ai-stop-btn.visible {
          opacity: 1;
          transform: translateY(0);
        }
        .ai-stop-btn:hover {
          box-shadow: 0 6px 28px rgba(220, 38, 38, 0.6);
          transform: translateY(-1px);
        }
        .ai-stop-btn:active {
          transform: translateY(1px);
        }
        .ai-stop-icon {
          width: 16px; height: 16px;
          background: #fff;
          border-radius: 3px;
        }

        /* ── Input Blocker ── */
        .ai-input-blocker {
          position: fixed;
          top: 0; left: 0;
          width: 100vw; height: 100vh;
          z-index: 0;
          pointer-events: all;
          cursor: not-allowed;
        }
      </style>

      <div class="ai-input-blocker" id="aiInputBlocker"></div>
      <div class="ai-overlay-backdrop" id="aiBackdrop"></div>
      <div class="ai-border-glow" id="aiBorderGlow"></div>

      <div class="ai-badge" id="aiBadge">
        <div class="ai-badge-dot"></div>
        <span class="ai-badge-text">AI is controlling this page</span>
      </div>

      <div class="ai-log-panel" id="aiLogPanel">
        <div class="ai-log-header">
          <span>Live Action Log</span>
          <span class="ai-log-goal" id="aiLogGoal"></span>
        </div>
        <div class="ai-log-body" id="aiLogBody"></div>
      </div>

      <button class="ai-stop-btn" id="aiStopBtn">
        <div class="ai-stop-icon"></div>
        Emergency Stop
      </button>
    `;
  }

  function attachOverlayEvents(shadow) {
    const stopBtn = shadow.getElementById("aiStopBtn");
    if (stopBtn) {
      stopBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        chrome.runtime.sendMessage({ type: "EMERGENCY_STOP" });
        hideOverlay();
      });
    }

    // Block mouse clicks on the backdrop so users can't click through
    const blocker = shadow.getElementById("aiInputBlocker");
    if (blocker) {
      for (const evt of ["mousedown", "mouseup", "click", "dblclick", "contextmenu"]) {
        blocker.addEventListener(evt, (e) => {
          if (overlayState.visible) {
            e.stopPropagation();
            e.preventDefault();
          }
        });
      }
    }
  }

  // ─── Overlay Visibility Control ─────────────────────────────────────────

  function showOverlay(goal) {
    createOverlayHost();
    const { hostEl, shadowRoot } = overlayState;

    hostEl.style.display = "block";
    overlayState.visible = true;
    overlayState.currentGoal = goal || "";

    // Clear previous log entries
    overlayState.logEntries = [];
    const logBody = shadowRoot.getElementById("aiLogBody");
    if (logBody) logBody.innerHTML = "";

    // Show goal in the log header
    const logGoalEl = shadowRoot.getElementById("aiLogGoal");
    if (logGoalEl && goal) {
      logGoalEl.textContent = goal;
      logGoalEl.title = goal;
    }

    // Animate in
    requestAnimationFrame(() => {
      const backdrop = shadowRoot.getElementById("aiBackdrop");
      const glow = shadowRoot.getElementById("aiBorderGlow");
      const badge = shadowRoot.getElementById("aiBadge");
      const logPanel = shadowRoot.getElementById("aiLogPanel");
      const stopBtn = shadowRoot.getElementById("aiStopBtn");

      if (backdrop) backdrop.classList.add("visible");
      setTimeout(() => { if (glow) glow.classList.add("visible"); }, 100);
      setTimeout(() => { if (badge) badge.classList.add("visible"); }, 200);
      setTimeout(() => { if (logPanel) logPanel.classList.add("visible"); }, 300);
      setTimeout(() => { if (stopBtn) stopBtn.classList.add("visible"); }, 400);
    });

    // Block user keyboard input (but allow programmatic/AI events through)
    document.addEventListener("keydown", blockKeyboard, true);
    document.addEventListener("keyup", blockKeyboard, true);
    document.addEventListener("keypress", blockKeyboard, true);
  }

  function hideOverlay() {
    if (!overlayState.hostEl || !overlayState.shadowRoot) return;

    const { shadowRoot, hostEl } = overlayState;
    overlayState.visible = false;

    const backdrop = shadowRoot.getElementById("aiBackdrop");
    const glow = shadowRoot.getElementById("aiBorderGlow");
    const badge = shadowRoot.getElementById("aiBadge");
    const logPanel = shadowRoot.getElementById("aiLogPanel");
    const stopBtn = shadowRoot.getElementById("aiStopBtn");

    if (stopBtn) stopBtn.classList.remove("visible");
    if (logPanel) logPanel.classList.remove("visible");
    setTimeout(() => { if (badge) badge.classList.remove("visible"); }, 100);
    setTimeout(() => { if (glow) glow.classList.remove("visible"); }, 200);
    setTimeout(() => { if (backdrop) backdrop.classList.remove("visible"); }, 300);

    setTimeout(() => { hostEl.style.display = "none"; }, 800);

    document.removeEventListener("keydown", blockKeyboard, true);
    document.removeEventListener("keyup", blockKeyboard, true);
    document.removeEventListener("keypress", blockKeyboard, true);
  }

  function addLogEntry(message) {
    if (!overlayState.shadowRoot) return;

    const logBody = overlayState.shadowRoot.getElementById("aiLogBody");
    if (!logBody) return;

    const now = new Date();
    const timestamp = `${now.getHours().toString().padStart(2, "0")}:${now.getMinutes().toString().padStart(2, "0")}:${now.getSeconds().toString().padStart(2, "0")}`;

    const entry = document.createElement("div");
    entry.className = "ai-log-entry";
    entry.innerHTML = `<span class="ai-log-time">${timestamp}</span>${escapeHtml(message)}`;

    logBody.appendChild(entry);

    overlayState.logEntries.push(message);
    while (logBody.children.length > overlayState.maxLogEntries) {
      logBody.removeChild(logBody.firstChild);
      overlayState.logEntries.shift();
    }

    logBody.scrollTop = logBody.scrollHeight;
  }

  // ─── Keyboard Blocker ──────────────────────────────────────────────────

  /**
   * FIXED: Only block USER-initiated keyboard events (isTrusted === true).
   * Programmatic events dispatched by the AI's humanType() etc. have
   * isTrusted === false and should pass through to the target elements.
   */
  function blockKeyboard(e) {
    // Always allow Escape to trigger emergency stop
    if (e.key === "Escape") {
      chrome.runtime.sendMessage({ type: "EMERGENCY_STOP" });
      hideOverlay();
      return;
    }

    // Only block user-initiated (isTrusted) keyboard events
    // AI's programmatic keyboard events have isTrusted === false and go through
    if (overlayState.visible && e.isTrusted) {
      e.stopPropagation();
      e.preventDefault();
    }
  }

  // ─── Utility ───────────────────────────────────────────────────────────

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ─── Public API (called from content.js) ───────────────────────────────

  window.handleOverlayControl = function handleOverlayControl(message) {
    const { command } = message;

    switch (command) {
      case "show":
        showOverlay(message.goal || "");
        break;
      case "hide":
        hideOverlay();
        break;
      case "log":
        // Auto-show if not visible yet and we get a log message
        if (!overlayState.visible && message.message) {
          showOverlay(message.goal || overlayState.currentGoal || "");
        }
        addLogEntry(message.message || "");
        break;
      case "status":
        break;
      default:
        console.warn("[Overlay] Unknown command:", command);
    }
  };

  console.log("[AI Agent] Overlay system v4 loaded.");
}
