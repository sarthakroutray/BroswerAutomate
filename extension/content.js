/**
 * content.js — Enhanced Content Script v4
 *
 * Core responsibilities:
 *   1. DOM EXTRACTION: Full page snapshot including checkboxes, radios, tables,
 *      images, forms — everything the AI needs to understand and interact with.
 *   2. ACTION EXECUTION: 20 action types with human-like simulation, including
 *      set_code/get_code (routed to background for MAIN-world editor access).
 *   3. ELEMENT HIGHLIGHTING: Visual indicator for AI interactions.
 *   4. TEXT EXTRACTION: Search & extract text from the page
 *   5. ELEMENT INFO: Detailed element inspection
 *   6. WAIT FOR ELEMENT: Poll until element appears
 *   7. PAGE CONTEXT / QUIZ / CODING extraction for structured problem solving
 *
 * Arbitrary JavaScript runs in background.js (MAIN world) — see browser_js.
 */

if (window.__aiAgentContentScriptLoaded) {
  // Already injected
} else {
  window.__aiAgentContentScriptLoaded = true;

  const MAX_TEXT_SUMMARY_CHARS = 12000;

  // ═══════════════════════════════════════════════════════════════════════════
  // ─── SECTION 1: DOM EXTRACTION ────────────────────────────────────────────
  // ═══════════════════════════════════════════════════════════════════════════

  function isElementVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none") return false;
    if (style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  function buildSelector(el) {
    // 1. ID
    if (el.id && /^[a-zA-Z][\w-]*$/.test(el.id)) {
      if (document.querySelectorAll(`#${CSS.escape(el.id)}`).length === 1) {
        return `#${CSS.escape(el.id)}`;
      }
    }

    // 2. Name + type combo
    if (el.name && el.tagName) {
      const tag = el.tagName.toLowerCase();
      const type = el.getAttribute("type");
      if (type) {
        const selector = `${tag}[name="${CSS.escape(el.name)}"][type="${CSS.escape(type)}"]`;
        if (document.querySelectorAll(selector).length === 1) return selector;
      }
      const selector = `${tag}[name="${CSS.escape(el.name)}"]`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }

    // 3. data-testid
    const testId = el.getAttribute("data-testid");
    if (testId) {
      const selector = `[data-testid="${CSS.escape(testId)}"]`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }

    // 4. Unique attribute combos
    if (el.tagName) {
      const tag = el.tagName.toLowerCase();
      const type = el.getAttribute("type");
      const placeholder = el.getAttribute("placeholder");
      const ariaLabel = el.getAttribute("aria-label");
      const value = el.getAttribute("value");
      const forAttr = el.getAttribute("for");

      if (type && placeholder) {
        const s = `${tag}[type="${CSS.escape(type)}"][placeholder="${CSS.escape(placeholder)}"]`;
        if (document.querySelectorAll(s).length === 1) return s;
      }
      if (ariaLabel) {
        const s = `${tag}[aria-label="${CSS.escape(ariaLabel)}"]`;
        if (document.querySelectorAll(s).length === 1) return s;
      }
      if (placeholder) {
        const s = `${tag}[placeholder="${CSS.escape(placeholder)}"]`;
        if (document.querySelectorAll(s).length === 1) return s;
      }
      if (type && value && (type === "radio" || type === "checkbox")) {
        const name = el.getAttribute("name");
        if (name) {
          const s = `${tag}[name="${CSS.escape(name)}"][value="${CSS.escape(value)}"]`;
          if (document.querySelectorAll(s).length === 1) return s;
        }
      }
      if (forAttr) {
        const s = `${tag}[for="${CSS.escape(forAttr)}"]`;
        if (document.querySelectorAll(s).length === 1) return s;
      }
    }

    // 5. Hierarchical nth-of-type path (fallback)
    const path = [];
    let current = el;
    while (current && current !== document.body && current !== document.documentElement) {
      let segment = current.tagName.toLowerCase();
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          (c) => c.tagName === current.tagName
        );
        if (siblings.length > 1) {
          const index = siblings.indexOf(current) + 1;
          segment += `:nth-of-type(${index})`;
        }
      }
      path.unshift(segment);
      current = parent;
    }
    return "body > " + path.join(" > ");
  }

  function findLabel(el) {
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return label.textContent.trim();
    }
    const parentLabel = el.closest("label");
    if (parentLabel) {
      const clone = parentLabel.cloneNode(true);
      clone.querySelectorAll("input, select, textarea").forEach(e => e.remove());
      const text = clone.textContent.trim();
      if (text) return text;
    }
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) return ariaLabel;
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const labelEl = document.getElementById(labelledBy);
      if (labelEl) return labelEl.textContent.trim();
    }
    const prev = el.previousElementSibling;
    if (prev && ["LABEL", "SPAN", "DIV", "P"].includes(prev.tagName)) {
      const text = prev.textContent.trim();
      if (text.length > 0 && text.length < 100) return text;
    }
    return null;
  }

  function findContext(el) {
    let current = el.parentElement;
    for (let i = 0; i < 5 && current; i++) {
      const headings = current.querySelectorAll("h1, h2, h3, h4, h5, h6, p, legend, .question, .question-text");
      for (const heading of headings) {
        if (heading.contains(el)) continue;
        const text = heading.textContent.trim();
        if (text.length > 5 && text.length < 500) return text;
      }
      current = current.parentElement;
    }
    return null;
  }

  function extractDOM() {
    const snapshot = {
      url: window.location.href,
      title: document.title,
      timestamp: Date.now(),
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        scrollY: Math.round(window.scrollY),
        scrollHeight: document.documentElement.scrollHeight,
      },
      inputs: [],
      checkboxes: [],
      radioButtons: [],
      buttons: [],
      links: [],
      selects: [],
      headings: [],
      tables: [],
      images: [],
      textSummary: "",
      domHash: "",
      runtimeSignals: {
        iframeCount: 0,
        sameOriginIframeCount: 0,
        crossOriginIframeCount: 0,
        shadowHostCount: 0,
        modalLikeCount: 0,
      },
    };

    // Runtime page complexity signals used by the agent for strategy selection.
    const frameEls = Array.from(document.querySelectorAll("iframe"));
    let sameOrigin = 0;
    let crossOrigin = 0;
    for (const frame of frameEls) {
      try {
        if (frame.contentDocument) sameOrigin++;
      } catch {
        crossOrigin++;
      }
    }
    let shadowHosts = 0;
    try {
      shadowHosts = Array.from(document.querySelectorAll("*")).filter((el) => !!el.shadowRoot).length;
    } catch { }
    let modalLikeCount = 0;
    try {
      modalLikeCount = document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="overlay"]').length;
    } catch { }
    snapshot.runtimeSignals = {
      iframeCount: frameEls.length,
      sameOriginIframeCount: sameOrigin,
      crossOriginIframeCount: crossOrigin,
      shadowHostCount: shadowHosts,
      modalLikeCount,
    };

    // ── Text Inputs
    const inputElements = document.querySelectorAll(
      'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), textarea, [contenteditable="true"]'
    );
    for (const el of inputElements) {
      if (!isElementVisible(el)) continue;
      snapshot.inputs.push({
        selector: buildSelector(el),
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute("type") || (el.tagName === "TEXTAREA" ? "textarea" : "text"),
        name: el.name || null,
        id: el.id || null,
        placeholder: el.placeholder || null,
        label: findLabel(el),
        value: el.value || el.textContent || "",
        required: el.required || false,
        disabled: el.disabled || false,
        readOnly: el.readOnly || false,
      });
    }

    // ── Checkboxes
    const checkboxElements = document.querySelectorAll('input[type="checkbox"]');
    for (const el of checkboxElements) {
      if (!isElementVisible(el)) continue;
      snapshot.checkboxes.push({
        selector: buildSelector(el),
        name: el.name || null,
        id: el.id || null,
        value: el.value || null,
        label: findLabel(el),
        checked: el.checked,
        disabled: el.disabled,
        context: findContext(el),
      });
    }

    // ── Radio Buttons
    const radioElements = document.querySelectorAll('input[type="radio"]');
    for (const el of radioElements) {
      // Note: Some radio buttons are hidden with opacity:0 or size:0
      // In such cases, we need to provide the label selector for clicking
      const radioData = {
        selector: buildSelector(el),
        name: el.name || null,
        id: el.id || null,
        value: el.value || null,
        label: findLabel(el),
        checked: el.checked,
        disabled: el.disabled,
        context: findContext(el),
      };

      // If radio is hidden, try to find and provide the clickable label selector
      const rect = el.getBoundingClientRect();
      const isHidden = rect.width === 0 || rect.height === 0 || parseFloat(window.getComputedStyle(el).opacity) === 0;

      if (isHidden) {
        // Find associated label element
        let labelEl = null;
        if (el.id) {
          labelEl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        }
        if (!labelEl) {
          labelEl = el.closest("label");
        }

        if (labelEl && isElementVisible(labelEl)) {
          radioData.labelSelector = buildSelector(labelEl);
          radioData.clickableSelector = radioData.labelSelector; // Prefer clicking the label
        }
      }

      snapshot.radioButtons.push(radioData);
    }

    // ── Select Dropdowns
    const selectElements = document.querySelectorAll("select");
    for (const el of selectElements) {
      if (!isElementVisible(el)) continue;
      const options = Array.from(el.options).map((opt) => ({
        value: opt.value,
        text: opt.textContent.trim(),
        selected: opt.selected,
      }));
      snapshot.selects.push({
        selector: buildSelector(el),
        name: el.name || null,
        id: el.id || null,
        label: findLabel(el),
        options,
        currentValue: el.value,
        disabled: el.disabled,
      });
    }

    // ── Buttons
    const buttonElements = document.querySelectorAll(
      'button, input[type="submit"], input[type="button"], input[type="reset"], [role="button"]'
    );
    for (const el of buttonElements) {
      if (!isElementVisible(el)) continue;
      const text =
        el.textContent?.trim() ||
        el.value ||
        el.getAttribute("aria-label") ||
        el.getAttribute("title") ||
        "";
      if (!text) continue;
      snapshot.buttons.push({
        selector: buildSelector(el),
        text: text.substring(0, 100),
        type: el.getAttribute("type") || "button",
        disabled: el.disabled || false,
      });
    }

    // ── Links
    const linkElements = document.querySelectorAll("a[href]");
    let linkCount = 0;
    for (const el of linkElements) {
      if (linkCount >= 50) break;
      if (!isElementVisible(el)) continue;
      const text = el.textContent?.trim();
      if (!text) continue;
      snapshot.links.push({
        selector: buildSelector(el),
        text: text.substring(0, 80),
        href: el.href,
      });
      linkCount++;
    }

    // ── Headings
    const headingElements = document.querySelectorAll("h1, h2, h3, h4, h5, h6");
    for (const el of headingElements) {
      if (!isElementVisible(el)) continue;
      const text = el.textContent?.trim();
      if (!text) continue;
      snapshot.headings.push({
        tag: el.tagName.toLowerCase(),
        text: text.substring(0, 200),
      });
    }

    // ── Tables
    const tableElements = document.querySelectorAll("table");
    let tableCount = 0;
    for (const table of tableElements) {
      if (tableCount >= 5) break;
      if (!isElementVisible(table)) continue;

      const caption = table.querySelector("caption")?.textContent?.trim() || null;
      const headers = [];
      const headerRow = table.querySelector("thead tr") || table.querySelector("tr");
      if (headerRow) {
        for (const th of headerRow.querySelectorAll("th")) {
          headers.push(th.textContent.trim().substring(0, 50));
        }
      }

      const rows = [];
      const bodyRows = table.querySelectorAll("tbody tr");
      const dataRows = bodyRows.length > 0 ? bodyRows : table.querySelectorAll("tr");
      let rowCount = 0;
      for (const tr of dataRows) {
        if (rowCount >= 15) break;
        if (tr === headerRow && headers.length > 0) continue;
        const cells = [];
        for (const td of tr.querySelectorAll("td, th")) {
          cells.push(td.textContent.trim().substring(0, 100));
        }
        if (cells.length > 0) {
          rows.push(cells);
          rowCount++;
        }
      }

      if (headers.length > 0 || rows.length > 0) {
        snapshot.tables.push({
          selector: buildSelector(table),
          caption,
          headers,
          rows,
        });
        tableCount++;
      }
    }

    // ── Images
    const imageElements = document.querySelectorAll("img[src]");
    let imgCount = 0;
    for (const el of imageElements) {
      if (imgCount >= 15) break;
      if (!isElementVisible(el)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 30 || rect.height < 30) continue;
      snapshot.images.push({
        selector: buildSelector(el),
        alt: el.alt || null,
        src: el.src?.substring(0, 200),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      });
      imgCount++;
    }

    // ── Shadow DOM enumeration (one level deep)
    // Walk open shadow roots and pull interactive elements into the snapshot.
    // Selectors are prefixed with the host's canonical CSS selector + " >>> "
    // so resolveElement can later traverse them via querySelectorAcrossShadowPath.
    let shadowInputs = 0, shadowButtons = 0;
    try {
      const allEls = document.querySelectorAll("*");
      for (const host of allEls) {
        if (!host.shadowRoot) continue;
        const hostSel = buildSelector(host);
        const sr = host.shadowRoot;

        // Inputs
        sr.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), textarea, [contenteditable="true"]').forEach((el) => {
          if (!isElementVisible(el)) return;
          snapshot.inputs.push({
            selector: hostSel + " >>> " + buildSelector(el),
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute("type") || (el.tagName === "TEXTAREA" ? "textarea" : "text"),
            name: el.name || null, id: el.id || null,
            placeholder: el.placeholder || null,
            label: findLabel(el), value: el.value || el.textContent || "",
            required: el.required || false, disabled: el.disabled || false, readOnly: el.readOnly || false,
            shadow: true,
          });
          shadowInputs++;
        });

        // Buttons
        sr.querySelectorAll('button, input[type="submit"], input[type="button"], input[type="reset"], [role="button"]').forEach((el) => {
          if (!isElementVisible(el)) return;
          const text = el.textContent?.trim() || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || "";
          if (!text) return;
          snapshot.buttons.push({
            selector: hostSel + " >>> " + buildSelector(el),
            text: text.substring(0, 100),
            type: el.getAttribute("type") || "button",
            disabled: el.disabled || false,
            shadow: true,
          });
          shadowButtons++;
        });

        // Links
        sr.querySelectorAll("a[href]").forEach((el) => {
          if (!isElementVisible(el)) return;
          const text = el.textContent?.trim();
          if (!text) return;
          snapshot.links.push({
            selector: hostSel + " >>> " + buildSelector(el),
            text: text.substring(0, 80),
            href: el.href,
            shadow: true,
          });
        });

        // Checkboxes
        sr.querySelectorAll('input[type="checkbox"]').forEach((el) => {
          if (!isElementVisible(el)) return;
          snapshot.checkboxes.push({
            selector: hostSel + " >>> " + buildSelector(el),
            name: el.name || null, id: el.id || null, value: el.value || null,
            label: findLabel(el), checked: el.checked, disabled: el.disabled,
            context: findContext(el), shadow: true,
          });
        });

        // Radio buttons
        sr.querySelectorAll('input[type="radio"]').forEach((el) => {
          snapshot.radioButtons.push({
            selector: hostSel + " >>> " + buildSelector(el),
            name: el.name || null, id: el.id || null, value: el.value || null,
            label: findLabel(el), checked: el.checked, disabled: el.disabled,
            context: findContext(el), shadow: true,
          });
        });

        // Selects
        sr.querySelectorAll("select").forEach((el) => {
          if (!isElementVisible(el)) return;
          const options = Array.from(el.options).map((opt) => ({
            value: opt.value, text: opt.textContent.trim(), selected: opt.selected,
          }));
          snapshot.selects.push({
            selector: hostSel + " >>> " + buildSelector(el),
            name: el.name || null, id: el.id || null,
            label: findLabel(el), options, currentValue: el.value,
            disabled: el.disabled, shadow: true,
          });
        });
      }
    } catch (e) { /* shadow DOM walk is best-effort */ }

    // ── Visible Text Summary (capped)
    const bodyText = document.body?.innerText || "";
    const cleaned = bodyText.replace(/\s+/g, " ").trim();
    snapshot.textSummary = cleaned.substring(0, MAX_TEXT_SUMMARY_CHARS);
    snapshot.domHash = simpleHash(`${snapshot.url}|${snapshot.title}|${snapshot.textSummary.substring(0, 600)}|${snapshot.inputs.length}|${snapshot.buttons.length}|${snapshot.links.length}`);
    snapshot.runtimeSignals.shadowInputs = shadowInputs;
    snapshot.runtimeSignals.shadowButtons = shadowButtons;

    return snapshot;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // ─── SECTION 2: ACTION EXECUTION ──────────────────────────────────────────
  // ═══════════════════════════════════════════════════════════════════════════

  const ALLOWED_ACTIONS = new Set([
    "type", "click", "click_at", "select", "scroll", "navigate", "wait",
    "check", "uncheck", "press_key", "hover", "clear",
    "submit", "double_click", "focus",
    "set_code", "get_code",
    "go_back", "go_forward", "reload",
  ]);
  const MAX_STEPS_PER_BATCH = 20;
  const ACTION_TIMEOUT_MS = 10000;
  const CLICK_MAX_ATTEMPTS = 3;
  const BLOCKER_DISMISS_PATTERN = /close|dismiss|accept|agree|ok|got\s*it|continue|skip|allow|not\s*now|no\s*thanks/i;
  const DOM_STABILITY_TIMEOUT_MS = 2400;
  const DOM_STABILITY_QUIET_MS = 260;
  const SAME_ORIGIN_IFRAME_MAX_DEPTH = 4;
  const IDEMPOTENCY_TOKEN_TTL_MS = 12000;
  const recentIdempotencyTokens = new Map();

  function simpleHash(input) {
    const text = String(input || "");
    let hash = 0;
    for (let i = 0; i < text.length; i++) {
      hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
    }
    return String(hash >>> 0);
  }

  function cleanupIdempotencyCache() {
    const now = Date.now();
    for (const [token, ts] of recentIdempotencyTokens.entries()) {
      if (now - ts > IDEMPOTENCY_TOKEN_TTL_MS) {
        recentIdempotencyTokens.delete(token);
      }
    }
    if (recentIdempotencyTokens.size <= 120) return;
    const entries = Array.from(recentIdempotencyTokens.entries()).sort((a, b) => a[1] - b[1]);
    for (const [token] of entries.slice(0, recentIdempotencyTokens.size - 120)) {
      recentIdempotencyTokens.delete(token);
    }
  }

  function checkAndRecordIdempotencyToken(token) {
    if (!token || typeof token !== "string") return true;
    cleanupIdempotencyCache();
    const now = Date.now();
    const previous = recentIdempotencyTokens.get(token);
    if (previous && (now - previous) <= IDEMPOTENCY_TOKEN_TTL_MS) {
      return false;
    }
    recentIdempotencyTokens.set(token, now);
    return true;
  }

  function capturePageFingerprint() {
    const bodyText = (document.body?.innerText || "").replace(/\s+/g, " ").trim();
    return {
      url: window.location.href,
      title: document.title,
      readyState: document.readyState,
      scrollY: Math.round(window.scrollY || 0),
      inputCount: document.querySelectorAll("input, textarea, select").length,
      buttonCount: document.querySelectorAll("button, [role='button']").length,
      textHash: simpleHash(bodyText.substring(0, 1500)),
      domHash: simpleHash(`${document.documentElement?.childElementCount || 0}|${document.body?.childElementCount || 0}|${bodyText.substring(0, 600)}`),
    };
  }

  function didFingerprintChange(before, after) {
    if (!before || !after) return true;
    return (
      before.url !== after.url ||
      before.readyState !== after.readyState ||
      before.scrollY !== after.scrollY ||
      before.inputCount !== after.inputCount ||
      before.buttonCount !== after.buttonCount ||
      before.textHash !== after.textHash ||
      before.domHash !== after.domHash
    );
  }

  async function waitForDomStability(timeoutMs = DOM_STABILITY_TIMEOUT_MS, quietMs = DOM_STABILITY_QUIET_MS) {
    const timeout = Math.max(400, Math.min(Number(timeoutMs) || DOM_STABILITY_TIMEOUT_MS, 10000));
    const quietWindow = Math.max(120, Math.min(Number(quietMs) || DOM_STABILITY_QUIET_MS, 1000));
    let lastMutation = Date.now();
    let observer = null;

    try {
      observer = new MutationObserver(() => {
        lastMutation = Date.now();
      });
      const target = document.documentElement || document.body;
      if (target) {
        observer.observe(target, {
          childList: true,
          subtree: true,
          attributes: true,
          characterData: true,
        });
      }
    } catch { }

    const start = Date.now();
    while (Date.now() - start < timeout) {
      const quietFor = Date.now() - lastMutation;
      const ready = document.readyState === "complete" || document.readyState === "interactive";
      if (quietFor >= quietWindow && ready) {
        break;
      }
      await sleep(70);
    }

    if (observer) {
      try { observer.disconnect(); } catch { }
    }
  }

  // ─── MutationObserver: Auto-dismiss dynamically injected overlays ──────────

  const OVERLAY_SELECTORS = [
    '[class*="modal"]', '[class*="overlay"]', '[class*="popup"]',
    '[class*="cookie"]', '[class*="consent"]', '[class*="gdpr"]',
    '[class*="banner"]', '[class*="notification"]', '[class*="dialog"]',
    '[role="dialog"]', '[aria-modal="true"]',
  ];
  const OVERLAY_SELECTOR_STR = OVERLAY_SELECTORS.join(',');

  let _overlayDismissObserver = null;

  function startOverlayDismissObserver() {
    if (_overlayDismissObserver) return;

    _overlayDismissObserver = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          const el = /** @type {Element} */ (node);

          // Check if the added element itself is an overlay
          let isOverlay = false;
          try {
            isOverlay = el.matches(OVERLAY_SELECTOR_STR);
          } catch { }

          // Also check if it contains an overlay child
          if (!isOverlay) {
            try {
              isOverlay = !!el.querySelector(OVERLAY_SELECTOR_STR);
            } catch { }
          }

          if (!isOverlay) continue;

          // Check if it's a full-screen or large overlay (not a tiny toast)
          const rect = el.getBoundingClientRect();
          const vw = window.innerWidth;
          const vh = window.innerHeight;
          const coversSignificantArea = (rect.width > vw * 0.3 && rect.height > vh * 0.1) ||
            (rect.width > vw * 0.6) || (rect.height > vh * 0.5);

          if (!coversSignificantArea) continue;

          // Delay slightly to let the overlay fully render
          setTimeout(() => {
            try {
              const dismissBtn = findDismissButton(el) || findGlobalDismissButton();
              if (dismissBtn && isElementVisible(dismissBtn)) {
                dismissBtn.click();
                return;
              }
              // Fallback: try Escape key
              const esc = { key: 'Escape', code: 'Escape', bubbles: true, cancelable: true };
              document.dispatchEvent(new KeyboardEvent('keydown', esc));
              document.dispatchEvent(new KeyboardEvent('keyup', esc));
            } catch { }
          }, 400);
        }
      }
    });

    _overlayDismissObserver.observe(document.body, { childList: true, subtree: true });
  }

  // Start watching for overlays as soon as body is available
  if (document.body) {
    startOverlayDismissObserver();
  } else {
    document.addEventListener('DOMContentLoaded', startOverlayDismissObserver, { once: true });
  }
  const SHADOW_SELECTOR_DELIMITER = ">>>";

  function querySelectorAcrossShadowPath(selector) {
    if (!selector.includes(SHADOW_SELECTOR_DELIMITER)) return null;
    const parts = selector
      .split(SHADOW_SELECTOR_DELIMITER)
      .map((s) => s.trim())
      .filter(Boolean);
    if (parts.length === 0) return null;

    let root = document;
    let current = null;
    for (let i = 0; i < parts.length; i++) {
      if (!root || typeof root.querySelector !== "function") return null;
      current = root.querySelector(parts[i]);
      if (!current) return null;
      if (i < parts.length - 1) {
        if (!current.shadowRoot) return null;
        root = current.shadowRoot;
      }
    }
    return current;
  }

  function querySelectorDeep(selector, root = document) {
    const visitedRoots = new Set();

    function walk(currentRoot) {
      if (!currentRoot || visitedRoots.has(currentRoot)) return null;
      visitedRoots.add(currentRoot);
      if (typeof currentRoot.querySelector !== "function") return null;

      try {
        const direct = currentRoot.querySelector(selector);
        if (direct) return direct;
      } catch {
        return null;
      }

      let nodes = [];
      try {
        nodes = currentRoot.querySelectorAll("*");
      } catch {
        nodes = [];
      }

      for (const node of nodes) {
        if (node && node.shadowRoot) {
          const nested = walk(node.shadowRoot);
          if (nested) return nested;
        }
      }
      return null;
    }

    return walk(root);
  }

  function querySelectorAcrossSameOriginIframes(selector, rootWindow = window, depth = 0) {
    if (depth > SAME_ORIGIN_IFRAME_MAX_DEPTH) return null;
    let frames = [];
    try {
      frames = rootWindow.document ? Array.from(rootWindow.document.querySelectorAll("iframe")) : [];
    } catch {
      return null;
    }

    for (const frame of frames) {
      try {
        const frameDoc = frame.contentDocument;
        const frameWin = frame.contentWindow;
        if (!frameDoc || !frameWin) continue;

        const direct = querySelectorDeep(selector, frameDoc);
        if (direct) return direct;

        const nested = querySelectorAcrossSameOriginIframes(selector, frameWin, depth + 1);
        if (nested) return nested;
      } catch {
        // Cross-origin frame or inaccessible frame context.
      }
    }
    return null;
  }

  function getSelectorMissHint(selector) {
    const frames = Array.from(document.querySelectorAll("iframe")).filter((frame) => {
      try {
        return isElementVisible(frame);
      } catch {
        return false;
      }
    });

    if (frames.length === 0) {
      return `Element not found: ${selector}`;
    }

    let sameOriginCount = 0;
    let crossOriginCount = 0;
    for (const frame of frames) {
      try {
        const _doc = frame.contentDocument;
        if (_doc) sameOriginCount++;
      } catch {
        crossOriginCount++;
      }
    }

    if (crossOriginCount > 0) {
      return `Element not found: ${selector}. It may be inside a cross-origin iframe, which browser security prevents extensions from scripting.`;
    }
    if (sameOriginCount > 0) {
      return `Element not found: ${selector}. It may be inside an iframe and require frame-aware selectors or coordinate fallback.`;
    }
    return `Element not found: ${selector}`;
  }

  function resolveAndValidate(selector) {
    if (!selector || typeof selector !== "string") {
      throw new Error("Missing or invalid selector");
    }

    const el = resolveElement(selector);

    if (!el) {
      throw new Error(getSelectorMissHint(selector));
    }
    if (!isElementVisible(el)) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.getBoundingClientRect();
    }
    return el;
  }

  function selectorCandidatesFromStep(step) {
    const candidates = [];
    if (step && typeof step.selector === "string" && step.selector.trim()) {
      candidates.push(step.selector.trim());
    }
    if (step && Array.isArray(step.selector_fallbacks)) {
      for (const sel of step.selector_fallbacks) {
        if (typeof sel === "string" && sel.trim()) {
          candidates.push(sel.trim());
        }
      }
    }
    return Array.from(new Set(candidates));
  }

  function resolveAndValidateWithFallback(step) {
    const selectors = selectorCandidatesFromStep(step);
    if (selectors.length === 0) {
      throw new Error("Missing or invalid selector");
    }
    let lastErr = null;
    for (const selector of selectors) {
      try {
        const el = resolveAndValidate(selector);
        return { element: el, selector };
      } catch (err) {
        lastErr = err;
      }
    }
    throw (lastErr || new Error(`Element not found: ${selectors[0]}`));
  }

  function resolveElement(selector) {
    if (!selector || typeof selector !== "string") return null;
    const trimmed = selector.trim();
    if (!trimmed) return null;
    try {
      // Text-based selectors: text="Remove" | text^="Save" | text$="button" | text*="partial" | text~="word"
      // Lets the LLM write semantic selectors without relying on positional :nth-child.
      const textSel = parseTextSelector(trimmed);
      if (textSel) {
        const result = findElementByText(textSel);
        if (result && !result.error && result.primary) {
          // Re-resolve through buildSelector to get a robust canonical CSS selector,
          // then resolve that. This keeps the rest of the pipeline using standard selectors.
          const resolved = resolveElement(result.primary.selector);
          if (resolved) return resolved;
        }
        return null;
      }
      if (trimmed.startsWith('/') || trimmed.startsWith('(/')) {
        const result = document.evaluate(
          trimmed,
          document,
          null,
          XPathResult.FIRST_ORDERED_NODE_TYPE,
          null
        );
        return result.singleNodeValue || null;
      }
      const shadowPathMatch = querySelectorAcrossShadowPath(trimmed);
      if (shadowPathMatch) return shadowPathMatch;

      const direct = document.querySelector(trimmed);
      if (direct) return direct;

      const deep = querySelectorDeep(trimmed);
      if (deep) return deep;

      return querySelectorAcrossSameOriginIframes(trimmed);
    } catch {
      return null;
    }
  }

  async function waitForInteractable(el, timeoutMs = 1800) {
    const timeout = Math.max(300, Math.min(Number(timeoutMs) || 1800, 5000));
    const start = Date.now();

    while (Date.now() - start < timeout) {
      if (!el || !el.isConnected) return false;
      const style = window.getComputedStyle(el);
      const enabled = !el.disabled && el.getAttribute("aria-disabled") !== "true";
      const pointerReady = style.pointerEvents !== "none";
      if (isElementVisible(el) && enabled && pointerReady) return true;
      await sleep(80);
    }
    return false;
  }

  function setNativeInputValue(el, value) {
    if (!el) return;
    if (el.tagName === "TEXTAREA") {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
      if (descriptor && typeof descriptor.set === "function") {
        descriptor.set.call(el, value);
        return;
      }
    }
    if (el.tagName === "INPUT") {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      if (descriptor && typeof descriptor.set === "function") {
        descriptor.set.call(el, value);
        return;
      }
    }
    el.value = value;
  }

  function dispatchInputLikeEvent(el, inputType, data) {
    try {
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        composed: true,
        inputType: inputType || "insertText",
        data: data || null,
      }));
    } catch {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  function isSameOrContains(root, node) {
    return !!(root && node && (root === node || root.contains(node)));
  }

  function findTopElementAtPoint(x, y) {
    try {
      return document.elementFromPoint(x, y);
    } catch {
      return null;
    }
  }

  function getElementCenter(el) {
    const rect = el.getBoundingClientRect();
    return {
      rect,
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    };
  }

  function getClickablePoints(rect) {
    const padX = Math.max(2, Math.min(rect.width * 0.2, 16));
    const padY = Math.max(2, Math.min(rect.height * 0.2, 16));
    const points = [
      [rect.left + rect.width / 2, rect.top + rect.height / 2],
      [rect.left + padX, rect.top + padY],
      [rect.right - padX, rect.top + padY],
      [rect.left + padX, rect.bottom - padY],
      [rect.right - padX, rect.bottom - padY],
    ];
    return points
      .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y))
      .map(([x, y]) => ({ x, y }));
  }

  function findDismissButton(blocker) {
    if (!blocker) return null;
    const roots = [blocker, blocker.closest("[role='dialog'], [aria-modal='true'], .modal, .popup")].filter(Boolean);
    for (const root of roots) {
      const candidates = root.querySelectorAll(
        "button, [role='button'], [aria-label], [aria-label*='close' i], .close, [class*='close'], .btn, a"
      );
      for (const candidate of candidates) {
        if (!isElementVisible(candidate)) continue;
        const text = `${candidate.textContent || ""} ${candidate.getAttribute("aria-label") || ""}`.trim();
        if (BLOCKER_DISMISS_PATTERN.test(text)) {
          return candidate;
        }
      }
    }
    return null;
  }

  function findGlobalDismissButton() {
    const candidates = document.querySelectorAll(
      "button, [role='button'], [aria-label], [aria-label*='close' i], .close, [class*='close'], [data-testid*='close'], a"
    );
    for (const candidate of candidates) {
      if (!isElementVisible(candidate)) continue;
      const text = `${candidate.textContent || ""} ${candidate.getAttribute("aria-label") || ""}`.trim();
      if (BLOCKER_DISMISS_PATTERN.test(text)) {
        return candidate;
      }
    }
    return null;
  }

  async function dismissInterferenceIfPossible(target, blockerHint = null) {
    if (!target) return false;
    const { x, y } = getElementCenter(target);
    const topEl = blockerHint || findTopElementAtPoint(x, y);
    if (!topEl || isSameOrContains(target, topEl) || isSameOrContains(topEl, target)) {
      return false;
    }

    const dismissButton = findDismissButton(topEl) || findGlobalDismissButton();
    if (dismissButton) {
      try {
        await humanClick(dismissButton, { attempts: 1, detectBlockers: false });
        await sleep(160);
        const refreshedTop = findTopElementAtPoint(x, y);
        if (!refreshedTop || isSameOrContains(target, refreshedTop) || isSameOrContains(refreshedTop, target)) {
          return true;
        }
      } catch { }
    }

    try {
      const escOpts = { key: "Escape", code: "Escape", bubbles: true, cancelable: true };
      document.dispatchEvent(new KeyboardEvent("keydown", escOpts));
      document.dispatchEvent(new KeyboardEvent("keyup", escOpts));
      await sleep(120);
      const refreshedTop = findTopElementAtPoint(x, y);
      if (!refreshedTop || isSameOrContains(target, refreshedTop) || isSameOrContains(refreshedTop, target)) {
        return true;
      }
    } catch { }

    return false;
  }

  // ─── Human-Like Simulators ─────────────────────────────────────────────────

  async function humanType(el, text) {
    const ready = await waitForInteractable(el);
    if (!ready) {
      throw new Error("Element is not interactable");
    }

    el.focus();
    el.click();
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      setNativeInputValue(el, "");
      dispatchInputLikeEvent(el, "deleteContentBackward", null);
    }

    const typeText = String(text ?? "");
    for (let i = 0; i < typeText.length; i++) {
      const char = typeText[i];
      el.dispatchEvent(new KeyboardEvent("keydown", {
        key: char, code: `Key${char.toUpperCase()}`, bubbles: true,
      }));
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        setNativeInputValue(el, `${el.value || ""}${char}`);
      } else if (el.isContentEditable) {
        if (typeof document.execCommand === "function") {
          document.execCommand("insertText", false, char);
        } else {
          el.textContent = `${el.textContent || ""}${char}`;
        }
      }
      dispatchInputLikeEvent(el, "insertText", char);
      el.dispatchEvent(new KeyboardEvent("keyup", {
        key: char, code: `Key${char.toUpperCase()}`, bubbles: true,
      }));
      let delay = 15 + Math.random() * 60;
      if (Math.random() < 0.08) delay += 150 + Math.random() * 150;
      await sleep(delay);
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  async function humanClick(el, options = {}) {
    const attempts = Math.max(1, Math.min(options.attempts || CLICK_MAX_ATTEMPTS, 5));
    const detectBlockers = options.detectBlockers !== false;
    const useDebuggerFallback = options.useDebuggerFallback === true;

    let lastError = null;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        if (!el || !el.isConnected) {
          throw new Error("Target element is detached from DOM");
        }

        el.scrollIntoView({ behavior: "smooth", block: "center" });
        const ready = await waitForInteractable(el, 2200);
        if (!ready) {
          throw new Error("Target is present but not interactable");
        }
        await sleep(120 + Math.random() * 180);
        highlightElement(el);

        const { rect, x, y } = getElementCenter(el);
        if (rect.width <= 0 || rect.height <= 0) {
          throw new Error("Element has no clickable area");
        }

        let clickPoint = { x, y };
        let blockerAtPoint = null;
        const candidatePoints = getClickablePoints(rect);
        for (const candidate of candidatePoints) {
          const topEl = findTopElementAtPoint(candidate.x, candidate.y);
          if (!topEl) continue;
          if (isSameOrContains(el, topEl) || isSameOrContains(topEl, el)) {
            clickPoint = candidate;
            blockerAtPoint = null;
            break;
          }
          if (!blockerAtPoint) blockerAtPoint = topEl;
        }

        // Also check for invisible obstruction via ancestor styles
        if (!blockerAtPoint && detectBlockers) {
          try {
            let ancestor = el.parentElement;
            while (ancestor && ancestor !== document.body) {
              const style = window.getComputedStyle(ancestor);
              if (style.pointerEvents === 'none' || parseFloat(style.opacity) === 0 || style.visibility === 'hidden') {
                blockerAtPoint = ancestor;
                break;
              }
              ancestor = ancestor.parentElement;
            }
          } catch { }
        }

        if (detectBlockers && blockerAtPoint) {
          if (blockerAtPoint.tagName === "IFRAME") {
            throw new Error("Target is occluded by an iframe. Cross-origin frame content cannot be automated from this context.");
          }
          const dismissed = await dismissInterferenceIfPossible(el, blockerAtPoint);
          if (dismissed) {
            await sleep(100);
            continue;
          }
        }

        // Strategy 1: Synthetic events (standard approach)
        const jitteredX = clickPoint.x + (Math.random() - 0.5) * 4;
        const jitteredY = clickPoint.y + (Math.random() - 0.5) * 4;
        const topAtJitteredPoint = findTopElementAtPoint(jitteredX, jitteredY);
        const eventTarget = (
          topAtJitteredPoint &&
          (isSameOrContains(el, topAtJitteredPoint) || isSameOrContains(topAtJitteredPoint, el))
        )
          ? topAtJitteredPoint
          : el;
        const mouseOpts = {
          bubbles: true, cancelable: true, view: window,
          clientX: jitteredX, clientY: jitteredY,
        };
        const pointerOpts = {
          ...mouseOpts,
          pointerId: 1,
          isPrimary: true,
          pointerType: "mouse",
          buttons: 1,
        };

        if (typeof PointerEvent === "function") {
          eventTarget.dispatchEvent(new PointerEvent("pointerover", pointerOpts));
          eventTarget.dispatchEvent(new PointerEvent("pointermove", pointerOpts));
        }
        eventTarget.dispatchEvent(new MouseEvent("mouseover", mouseOpts));
        eventTarget.dispatchEvent(new MouseEvent("mousemove", mouseOpts));
        await sleep(30 + Math.random() * 70);
        if (typeof PointerEvent === "function") {
          eventTarget.dispatchEvent(new PointerEvent("pointerdown", pointerOpts));
        }
        eventTarget.dispatchEvent(new MouseEvent("mousedown", mouseOpts));
        await sleep(25 + Math.random() * 50);
        if (typeof PointerEvent === "function") {
          eventTarget.dispatchEvent(new PointerEvent("pointerup", { ...pointerOpts, buttons: 0 }));
        }
        eventTarget.dispatchEvent(new MouseEvent("mouseup", mouseOpts));
        eventTarget.dispatchEvent(new MouseEvent("click", mouseOpts));

        await sleep(70 + Math.random() * 140);
        return;
      } catch (err) {
        lastError = err;

        // Strategy 2: Debugger trusted click fallback via background.js
        if (useDebuggerFallback && attempt === attempts) {
          try {
            const { rect: fallbackRect } = getElementCenter(el);
            const fbX = fallbackRect.left + fallbackRect.width / 2;
            const fbY = fallbackRect.top + fallbackRect.height / 2;
            const debuggerResult = await new Promise((resolve) => {
              chrome.runtime.sendMessage(
                { type: 'DEBUGGER_CLICK', x: fbX, y: fbY },
                (response) => {
                  if (chrome.runtime.lastError) {
                    resolve({ success: false, error: chrome.runtime.lastError.message });
                  } else {
                    resolve(response || { success: false });
                  }
                }
              );
            });
            if (debuggerResult && debuggerResult.success) {
              await sleep(100);
              return; // Debugger click succeeded
            }
          } catch { }
        }

        if (attempt < attempts) {
          await sleep(120 + attempt * 80);
          continue;
        }
      }
    }

    throw (lastError || new Error("Click failed after retries"));
  }

  async function humanDoubleClick(el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    await sleep(150 + Math.random() * 200);
    highlightElement(el);
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const mouseOpts = { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y };
    el.dispatchEvent(new MouseEvent("mousedown", mouseOpts));
    el.dispatchEvent(new MouseEvent("mouseup", mouseOpts));
    el.dispatchEvent(new MouseEvent("click", mouseOpts));
    await sleep(80);
    el.dispatchEvent(new MouseEvent("mousedown", mouseOpts));
    el.dispatchEvent(new MouseEvent("mouseup", mouseOpts));
    el.dispatchEvent(new MouseEvent("click", mouseOpts));
    el.dispatchEvent(new MouseEvent("dblclick", mouseOpts));
    await sleep(100);
  }

  async function humanClickAt(x, y) {
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      throw new Error("click_at requires numeric x and y coordinates");
    }

    const clampedX = Math.min(Math.max(0, x), Math.max(0, viewportWidth - 1));
    const clampedY = Math.min(Math.max(0, y), Math.max(0, viewportHeight - 1));

    let target = document.elementFromPoint(clampedX, clampedY);
    if (!target) {
      throw new Error(`No element found at (${clampedX}, ${clampedY})`);
    }
    if (target.tagName === "IFRAME") {
      throw new Error("Coordinate resolves to an iframe. Cross-origin frame content cannot be automated from this context.");
    }

    if (!isElementVisible(target)) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      await sleep(120);
      const refreshedTarget = document.elementFromPoint(clampedX, clampedY);
      if (refreshedTarget) target = refreshedTarget;
    }

    highlightElement(target);
    const mouseOpts = {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: clampedX,
      clientY: clampedY,
    };

    target.dispatchEvent(new MouseEvent("mouseover", mouseOpts));
    target.dispatchEvent(new MouseEvent("mousemove", mouseOpts));
    await sleep(40 + Math.random() * 70);
    target.dispatchEvent(new MouseEvent("mousedown", mouseOpts));
    await sleep(25 + Math.random() * 60);
    target.dispatchEvent(new MouseEvent("mouseup", mouseOpts));
    target.dispatchEvent(new MouseEvent("click", mouseOpts));

    try { target.click(); } catch { }
    await sleep(80 + Math.random() * 130);

    const targetSelector = target.id
      ? `#${target.id}`
      : target.className
        ? `${target.tagName.toLowerCase()}.${String(target.className).trim().split(/\s+/).slice(0, 2).join('.')}`
        : target.tagName.toLowerCase();

    return {
      x: clampedX,
      y: clampedY,
      targetTag: target.tagName.toLowerCase(),
      targetSelector,
    };
  }

  async function humanSelect(el, value) {
    el.focus();
    await sleep(100 + Math.random() * 100);
    let matched = false;
    for (const option of el.options) {
      if (
        option.value === value ||
        option.textContent.trim().toLowerCase() === value.toLowerCase() ||
        option.textContent.trim().toLowerCase().includes(value.toLowerCase())
      ) {
        el.value = option.value;
        matched = true;
        break;
      }
    }
    if (!matched) {
      throw new Error(`Option "${value}" not found in select`);
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(100 + Math.random() * 150);
  }

  async function humanScroll(direction, amount) {
    const distance = amount || 500;
    const multiplier = direction === "up" ? -1 : 1;
    const startY = window.scrollY;
    const targetY = Math.max(0, startY + distance * multiplier);
    const duration = 350 + Math.random() * 200;
    const startTime = performance.now();
    return new Promise((resolve) => {
      function step(currentTime) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = progress < 0.5
          ? 4 * progress * progress * progress
          : 1 - Math.pow(-2 * progress + 2, 3) / 2;
        window.scrollTo(0, startY + (targetY - startY) * eased);
        if (progress < 1) {
          requestAnimationFrame(step);
        } else {
          resolve();
        }
      }
      requestAnimationFrame(step);
    });
  }

  /**
   * FIXED: humanCheck now uses click-only approach to properly
   * toggle checkboxes and trigger framework change detection.
   * The old approach set .checked = true then dispatched click which toggled it back.
   */
  async function humanCheck(el) {
    highlightElement(el);
    if (!el.checked) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      await sleep(100 + Math.random() * 150);
      // Use click to toggle — this properly handles both native and framework checkboxes
      el.click();
      // Verify it was checked. If not, force it.
      if (!el.checked) {
        el.checked = true;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    await sleep(80);
  }

  /**
   * FIXED: humanUncheck now uses click-only approach.
   */
  async function humanUncheck(el) {
    highlightElement(el);
    if (el.checked) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      await sleep(100 + Math.random() * 150);
      el.click();
      // Verify it was unchecked. If not, force it.
      if (el.checked) {
        el.checked = false;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    await sleep(80);
  }

  async function humanPressKey(key, el) {
    const target = el || document.activeElement || document.body;
    const keyMap = {
      enter: { key: "Enter", code: "Enter", keyCode: 13 },
      tab: { key: "Tab", code: "Tab", keyCode: 9 },
      escape: { key: "Escape", code: "Escape", keyCode: 27 },
      backspace: { key: "Backspace", code: "Backspace", keyCode: 8 },
      delete: { key: "Delete", code: "Delete", keyCode: 46 },
      arrowup: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
      arrowdown: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
      arrowleft: { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 },
      arrowright: { key: "ArrowRight", code: "ArrowRight", keyCode: 39 },
      space: { key: " ", code: "Space", keyCode: 32 },
      home: { key: "Home", code: "Home", keyCode: 36 },
      end: { key: "End", code: "End", keyCode: 35 },
      pageup: { key: "PageUp", code: "PageUp", keyCode: 33 },
      pagedown: { key: "PageDown", code: "PageDown", keyCode: 34 },
    };
    const mapped = keyMap[key.toLowerCase()] || { key, code: `Key${key}`, keyCode: 0 };
    const opts = { bubbles: true, cancelable: true, ...mapped };
    target.dispatchEvent(new KeyboardEvent("keydown", opts));
    await sleep(50);
    target.dispatchEvent(new KeyboardEvent("keyup", opts));
    if (key.toLowerCase() === "enter" && target.form) {
      target.form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    }
    await sleep(100);
  }

  async function humanHover(el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    await sleep(100);
    highlightElement(el);
    const rect = el.getBoundingClientRect();
    const opts = {
      bubbles: true, clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
    };
    el.dispatchEvent(new MouseEvent("mouseenter", opts));
    el.dispatchEvent(new MouseEvent("mouseover", opts));
    el.dispatchEvent(new MouseEvent("mousemove", opts));
    await sleep(300 + Math.random() * 200);
  }

  async function humanClear(el) {
    el.focus();
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      el.value = "";
    } else if (el.isContentEditable) {
      el.textContent = "";
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(50);
  }

  async function humanSubmit(el) {
    const form = el.tagName === "FORM" ? el : el.closest("form");
    if (form) {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      try { form.requestSubmit(); } catch { }
    } else {
      await humanClick(el);
    }
    await sleep(200);
  }

  // ─── Element Highlighting ──────────────────────────────────────────────────

  function highlightElement(el) {
    try {
      el.setAttribute("data-ai-target-active", "");
      el.setAttribute("data-ai-pulse", "");
      setTimeout(() => {
        el.removeAttribute("data-ai-target-active");
        el.removeAttribute("data-ai-pulse");
      }, 1500);
    } catch { }
  }

  // ─── Action Router ─────────────────────────────────────────────────────────

  async function waitForNavigationOutcome(previousUrl, timeoutMs = 5500, allowSameUrl = false) {
    const deadline = Date.now() + Math.max(800, timeoutMs);
    while (Date.now() < deadline) {
      const urlChanged = window.location.href !== previousUrl;
      const domReady = document.readyState === "complete" || document.readyState === "interactive";
      if (urlChanged || (allowSameUrl && domReady)) {
        await waitForDomStability(1800, 220);
        return true;
      }
      await sleep(100);
    }
    return false;
  }

  function deriveActionToken(step) {
    // Only explicitly provided tokens dedupe — never auto-generate them, so
    // intentionally repeated actions (double-clicking "+" etc.) always run.
    if (step && typeof step.idempotency_token === "string" && step.idempotency_token.trim()) {
      return step.idempotency_token.trim();
    }
    return "";
  }

  function actionRequiresStateChange(action, step) {
    if (["submit", "navigate", "go_back", "go_forward", "reload"].includes(action)) {
      return true;
    }
    const expected = String(step?.expected_change || "").toLowerCase();
    if (expected) {
      return expected !== "none";
    }
    return false;
  }

  function actionPostWaitMs(action) {
    if (["navigate", "go_back", "go_forward", "reload", "submit"].includes(action)) return 3200;
    if (["click", "click_at", "double_click", "select", "check", "uncheck"].includes(action)) return 1700;
    if (["type", "clear", "press_key"].includes(action)) return 700;
    if (action === "scroll") return 900;
    return 450;
  }

  function canRetryAction(action, errorMessage) {
    if (!["click", "click_at", "type", "check", "submit", "select", "navigate"].includes(action)) {
      return false;
    }
    const lowered = String(errorMessage || "").toLowerCase();
    if (/(cross-origin|unsafe url|disallowed|requires)/.test(lowered)) return false;
    return true;
  }

  async function executeSingleAction(step) {
    const action = String(step?.action || "").toLowerCase();
    const value = step?.value;
    if (!ALLOWED_ACTIONS.has(action)) {
      return { success: false, action, error: `Disallowed action: ${action}` };
    }
    try {
      switch (action) {
        case "type": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          if (el.readOnly || el.disabled) {
            return { success: false, action, selector: used, error: "Element is read-only or disabled" };
          }
          await humanType(el, value || "");
          return { success: true, action, selector: used };
        }
        case "click": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          await humanClick(el, { attempts: CLICK_MAX_ATTEMPTS, detectBlockers: true });
          return { success: true, action, selector: used };
        }
        case "click_at": {
          let x = step.x;
          let y = step.y;

          if ((!Number.isFinite(x) || !Number.isFinite(y)) && typeof value === "string") {
            const match = value.match(/^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$/);
            if (match) {
              x = Number(match[1]);
              y = Number(match[2]);
            }
          }

          if (!Number.isFinite(x) || !Number.isFinite(y)) {
            return { success: false, action, error: "click_at requires numeric x and y (or value as 'x,y')" };
          }

          const info = await humanClickAt(Number(x), Number(y));
          return { success: true, action, ...info };
        }
        case "double_click": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          await humanDoubleClick(el);
          return { success: true, action, selector: used };
        }
        case "select": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          if (el.tagName !== "SELECT") {
            return { success: false, action, selector: used, error: "Element is not a <select>" };
          }
          await humanSelect(el, value || "");
          return { success: true, action, selector: used };
        }
        case "check": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          if (el.type !== "checkbox" && el.type !== "radio") {
            await humanClick(el);
          } else {
            await humanCheck(el);
          }
          return { success: true, action, selector: used };
        }
        case "uncheck": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          if (el.type === "checkbox") {
            await humanUncheck(el);
          } else {
            await humanClick(el);
          }
          return { success: true, action, selector: used };
        }
        case "press_key": {
          let el = null;
          if (step?.selector) {
            try { el = resolveAndValidate(step.selector); } catch { }
          }
          await humanPressKey(value || "Enter", el);
          return { success: true, action };
        }
        case "scroll": {
          await humanScroll(value || "down", step.amount || 500);
          return { success: true, action };
        }
        case "hover": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          await humanHover(el);
          return { success: true, action, selector: used };
        }
        case "clear": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          await humanClear(el);
          return { success: true, action, selector: used };
        }
        case "focus": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          el.focus();
          el.scrollIntoView({ behavior: "smooth", block: "center" });
          return { success: true, action, selector: used };
        }
        case "submit": {
          const { element: el, selector: used } = resolveAndValidateWithFallback(step);
          await humanSubmit(el);
          return { success: true, action, selector: used };
        }
        case "navigate": {
          if (!value || typeof value !== "string") {
            return { success: false, action, error: "Navigate requires a URL" };
          }
          if (!value.startsWith("http://") && !value.startsWith("https://")) {
            return { success: false, action, error: `Unsafe URL: ${value}` };
          }
          const beforeUrl = window.location.href;
          window.location.href = value;
          const ok = await waitForNavigationOutcome(beforeUrl, 6200, false);
          if (!ok) {
            return { success: false, action, error: "Navigation did not complete within expected time" };
          }
          return { success: true, action };
        }
        case "wait": {
          if (step?.selector) {
            const waitMs = Math.min(parseInt(value, 10) || 10000, 30000);
            const waited = await waitForElement(step.selector, waitMs, true);
            return waited.found
              ? { success: true, action, selector: step.selector }
              : { success: false, action, selector: step.selector, error: `Timed out waiting for ${step.selector}` };
          }
          const ms = Math.min(parseInt(value, 10) || 1000, 10000);
          await sleep(ms);
          return { success: true, action };
        }
        case "set_code": {
          if (typeof value !== "string" || !value) {
            return { success: false, action, error: "set_code requires the source code in 'value'" };
          }
          const setResult = await new Promise((resolve) => {
            chrome.runtime.sendMessage({ type: "SET_CODE", code: value }, (response) => {
              if (chrome.runtime.lastError) {
                resolve({ success: false, message: chrome.runtime.lastError.message });
              } else {
                resolve(response || { success: false, message: "No response from background" });
              }
            });
          });
          return { success: !!setResult.success, action, ...setResult };
        }
        case "get_code": {
          const getCodeResult = await new Promise((resolve) => {
            chrome.runtime.sendMessage({ type: "GET_CODE" }, (response) => {
              if (chrome.runtime.lastError) {
                resolve({ success: false, message: chrome.runtime.lastError.message });
              } else {
                resolve(response || { success: false, message: "No response from background" });
              }
            });
          });
          return { success: !!getCodeResult.success, action, ...getCodeResult };
        }
        case "go_back": {
          const beforeUrl = window.location.href;
          window.history.back();
          const ok = await waitForNavigationOutcome(beforeUrl, 4500, false);
          return ok ? { success: true, action } : { success: false, action, error: "go_back did not change page state" };
        }
        case "go_forward": {
          const beforeUrl = window.location.href;
          window.history.forward();
          const ok = await waitForNavigationOutcome(beforeUrl, 4500, false);
          return ok ? { success: true, action } : { success: false, action, error: "go_forward did not change page state" };
        }
        case "reload": {
          const beforeUrl = window.location.href;
          window.location.reload();
          const ok = await waitForNavigationOutcome(beforeUrl, 5200, true);
          return ok ? { success: true, action } : { success: false, action, error: "reload did not complete within expected time" };
        }
        default:
          return { success: false, action, error: `Unknown: ${action}` };
      }
    } catch (err) {
      const message = err?.message || String(err);
      const trustedHint = (
        ["click", "double_click", "type", "press_key", "submit"].includes(action) &&
        !/cross-origin iframe|not found/i.test(message)
      )
        ? `${message}. Some sites require trusted user gestures and cannot be fully automated from content scripts.`
        : message;
      return { success: false, action, error: trustedHint };
    }
  }

  async function executeActionBatch(steps) {
    if (!Array.isArray(steps)) {
      return { error: "Steps must be an array" };
    }
    const safeSteps = steps.slice(0, MAX_STEPS_PER_BATCH);
    const results = [];

    for (let idx = 0; idx < safeSteps.length; idx++) {
      const step = safeSteps[idx] || {};
      const action = String(step.action || "").toLowerCase();
      const token = deriveActionToken(step);
      if (token && !checkAndRecordIdempotencyToken(token)) {
        results.push({
          success: false,
          action,
          error: "Duplicate action blocked by idempotency guard",
        });
        continue;
      }

      const before = capturePageFingerprint();
      let result = await Promise.race([
        executeSingleAction(step),
        sleep(ACTION_TIMEOUT_MS).then(() => ({
          success: false, action, error: "Action timed out",
        })),
      ]);

      if (!result.success && canRetryAction(action, result.error)) {
        if (/occluded|overlay|blocked/i.test(String(result.error || ""))) {
          try { await humanPressKey("Escape"); } catch { }
        }
        await waitForDomStability(900, 180);
        result = await Promise.race([
          executeSingleAction(step),
          sleep(ACTION_TIMEOUT_MS).then(() => ({
            success: false, action, error: "Action timed out (retry)",
          })),
        ]);
      }

      if (result.success) {
        const settleMs = actionPostWaitMs(action);
        await waitForDomStability(settleMs, Math.max(180, Math.min(450, Math.floor(settleMs / 6))));
        const after = capturePageFingerprint();
        if (actionRequiresStateChange(action, step) && !didFingerprintChange(before, after)) {
          result = {
            success: false,
            action,
            error: "Action executed but produced no observable page state change",
          };
        }
      }

      results.push(result);
      if (!result.success && action === "navigate") break;
      await sleep(70 + Math.random() * 120);
    }

    return { results };
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // ─── SECTION 3: NEW CAPABILITIES ──────────────────────────────────────────
  // ═══════════════════════════════════════════════════════════════════════════

  /**
   * Extract text from the page, optionally searching or targeting a selector.
   */
  function extractText(query, selector) {
    let source = "";
    if (selector) {
      const el = resolveElement(selector);
      if (!el) return { error: `Element not found: ${selector}` };
      source = el.innerText || el.textContent || "";
    } else {
      source = document.body?.innerText || "";
    }

    if (query) {
      // Find matching lines/paragraphs
      const lines = source.split(/\n+/);
      const queryLower = query.toLowerCase();
      const matches = lines.filter(l => l.toLowerCase().includes(queryLower)).map(l => l.trim()).filter(Boolean);
      return { matches, total: matches.length };
    }

    return { text: source.substring(0, 100000) };
  }

  /**
   * Get detailed information about a specific element.
   */
  function getElementInfo(selector) {
    const el = resolveElement(selector);
    if (!el) return { error: `Element not found: ${selector}` };

    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);

    // Collect attributes
    const attributes = {};
    for (const attr of el.attributes) {
      attributes[attr.name] = attr.value;
    }

    return {
      info: {
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        classes: Array.from(el.classList),
        text: (el.innerText || el.textContent || "").substring(0, 1000),
        attributes,
        rect: {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          top: rect.top,
          right: rect.right,
          bottom: rect.bottom,
          left: rect.left,
        },
        styles: {
          display: style.display,
          visibility: style.visibility,
          opacity: style.opacity,
          position: style.position,
          overflow: style.overflow,
          color: style.color,
          backgroundColor: style.backgroundColor,
          fontSize: style.fontSize,
        },
        visible: isElementVisible(el),
        enabled: !el.disabled,
        value: el.value || null,
        checked: el.checked !== undefined ? el.checked : null,
      },
    };
  }

  /**
   * Wait for an element to appear in the DOM, polling periodically.
   */
  async function waitForElement(selector, timeoutMs, requireVisible = true) {
    const maxWait = Math.min(timeoutMs || 10000, 30000);
    const pollInterval = 250;
    const startTime = Date.now();

    while (Date.now() - startTime < maxWait) {
      const el = resolveElement(selector);
      if (el) {
        const visible = isElementVisible(el);
        const interactable = await waitForInteractable(el, 500);
        if (requireVisible && !visible) {
          await sleep(pollInterval);
          continue;
        }
        await waitForDomStability(900, 180);
        return {
          found: true,
          tag: el.tagName.toLowerCase(),
          text: (el.innerText || el.textContent || "").substring(0, 200),
          visible,
          interactable,
          elapsed: Date.now() - startTime,
        };
      }
      await sleep(pollInterval);
    }

    return { found: false, elapsed: maxWait };
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // ─── SECTION 3b: PAGE CONTEXT (structured extraction) ──────────────────────
  // ═══════════════════════════════════════════════════════════════════════════

  // Detect whether the page is a quiz, a coding challenge, or generic.
  // Heuristic but works for the common platforms (HackerRank, LeetCode,
  // Unstop, Google Forms, Testbook, etc.).
  function detectPageKind() {
    const url = location.href.toLowerCase();
    const title = (document.title || "").toLowerCase();
    const body = (document.body?.innerText || "").toLowerCase();
    const hasEditor = !!document.querySelector(".ace_editor, .monaco-editor, .CodeMirror, .cm-editor, textarea[name*='code'], textarea[class*='code']");
    const hasCompile = !!document.querySelector("button[id*='compile'], button.compile, .compile-btn, [data-action='compile'], [id*='run'], button[id*='run']");
    const radioCount = document.querySelectorAll("input[type='radio']").length;
    const checkboxCount = document.querySelectorAll("input[type='checkbox']").length;
    const clickableOptions = document.querySelectorAll(".option, .answer-option, .quiz-option, [class*='option'], [data-option], [role='option']").length;
    const quizNav = /question\s*(?:no\.?\s*)?:?\s*\d+\s*(?:\/|of)\s*\d+/i.test(body) ||
                    /\d+\s*\/\s*\d+\s*(?:questions?|q)/i.test(body);
    const codingSignals = (hasEditor && hasCompile) ||
      /problem\s+statement|sample\s+input|sample\s+output|input\s+format|output\s+format/i.test(body) ||
      /leetcode\.com|hackerrank\.com|codechef\.com|geeksforgeeks\.org|codingninjas\.com/i.test(url);
    const quizSignals = (radioCount >= 2 || checkboxCount >= 2 || clickableOptions >= 2) &&
      (quizNav || /quiz|assessment|test|exam/i.test(title + " " + url));

    if (codingSignals && !quizSignals) return "coding";
    if (quizSignals && !codingSignals) return "quiz";
    if (codingSignals && quizSignals) return "mixed"; // rare, treat as coding first
    return "generic";
  }

  // Detect login/auth walls, captchas, and blocking modals so the model can
  // decide whether it can proceed or has to stop.
  function detectBlockers() {
    const body = (document.body?.innerText || "").toLowerCase();
    const html = document.documentElement.innerHTML.toLowerCase();
    const hasLogin =
      /sign\s*in|log\s*in|login|continue\s+with\s+(google|github|facebook)/i.test(body) &&
      !!document.querySelector("input[type='password'], input[name*='password'], input[id*='password']");
    const hasCaptcha =
      /recaptcha|hcaptcha|turnstile|hcaptcha/i.test(html) ||
      !!document.querySelector(".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], iframe[src*='recaptcha'], iframe[src*='hcaptcha']");
    const hasTwoFactor =
      /two[\s-]?factor|2fa|verification\s+code|otp|one[\s-]time\s+password/i.test(body) &&
      !!document.querySelector("input[name*='otp'], input[id*='otp'], input[autocomplete='one-time-code']");
    const modalOpen = !!document.querySelector("[role='dialog'][aria-modal='true'], .modal.show, [class*='modal'][class*='open'], .modal-open");
    return { has_login: hasLogin, has_captcha: hasCaptcha, has_two_factor: hasTwoFactor, modal_open: modalOpen };
  }

  function buildPageContext() {
    const dom = extractDOM();
    const kind = detectPageKind();
    const blockers = detectBlockers();
    const quiz = kind === "quiz" || kind === "mixed" ? extractQuizStructure() : null;
    const coding = kind === "coding" || kind === "mixed" ? extractCodingProblem() : null;
    return {
      url: location.href,
      title: document.title,
      kind,
      blockers,
      dom,
      quiz,
      coding,
    };
  }

  // Find an element by visible text. Supports:
  //   text="Remove"          exact match
  //   text^="Save"           starts with
  //   text$="button"         ends with
  //   text*="partial"        contains (default if just text="partial" without anchors)
  //   text~="word"           whitespace-separated word match
  // If multiple elements match, returns up to 5 candidates so the LLM can pick.
  // Parse text-based selectors like:
//   text="Remove"           exact match
//   text^="Save"            starts-with
//   text$="button"          ends-with
//   text*="partial"         contains (most common; equivalent to text="partial")
//   text~="word"            word-boundary
//   text~/regex/            case-insensitive regex (literal slashes inside need escaping)
// Returns { text, mode, tag, role } or null if the selector is not text-based.
function parseTextSelector(selector) {
    const m = selector.match(/^text([\^$*~]?=)\/((?:[^\/\\]|\\.)*)\/([a-z]*)$/);
    if (!m) {
      // also accept unquoted needles: text="X" works in both forms
      const simple = selector.match(/^text([\^$*~]?=)(.+)$/);
      if (!simple) return null;
      return { text: unescapeTextNeedle(simple[2]), mode: modeFromOp(simple[1]), tag: null, role: null };
    }
    return { text: unescapeTextNeedle(m[2]), mode: modeFromOp(m[1]), tag: null, role: null };
  }
  function modeFromOp(op) {
    if (op === "=") return "exact";
    if (op === "^=") return "startsWith";
    if (op === "$=") return "endsWith";
    if (op === "*=") return "contains";
    if (op === "~=") return "word";
    if (op === "/=") return "regex";
    return "contains";
  }
  function unescapeTextNeedle(s) {
    return String(s || "").replace(/\\(.)/g, "$1");
  }

  function findElementByText({ text, mode = "contains", tag = null, role = null } = {}) {
    const needle = String(text ?? "").trim();
    if (!needle) return { error: "Empty text selector", candidates: [] };
    const test = (elText) => {
      const haystack = String(elText || "").trim();
      if (!haystack) return false;
      switch (mode) {
        case "exact": return haystack === needle;
        case "startsWith": return haystack.startsWith(needle);
        case "endsWith": return haystack.endsWith(needle);
        case "word": return new RegExp(`\\b${needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(haystack);
        case "regex":
          try { return new RegExp(needle, "i").test(haystack); } catch { return false; }
        case "contains":
        default: return haystack.toLowerCase().includes(needle.toLowerCase());
      }
    };
    const pool = [];
    const selector = tag
      ? `${tag}, ${tag} *`
      : "button, a, [role='button'], label, span, div, li, td, th, p, h1, h2, h3, h4, h5, h6, option, [role='option'], [data-option]";
    document.querySelectorAll(selector).forEach((el) => {
      // Skip if role filter doesn't match
      if (role && el.getAttribute("role") !== role) return;
      // Skip if element has no own visible text and is just a wrapper
      const own = Array.from(el.childNodes).filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join("").trim();
      const txt = own || (el.getAttribute("aria-label")) || (el.title) || "";
      if (!test(txt)) return;
      if (!isElementVisible(el)) return;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;
      pool.push({
        tag: el.tagName.toLowerCase(),
        text: txt.substring(0, 200),
        selector: buildSelector(el),
        role: el.getAttribute("role"),
        aria_label: el.getAttribute("aria-label"),
      });
    });
    // De-dup by selector
    const seen = new Set();
    const candidates = [];
    for (const c of pool) {
      if (seen.has(c.selector)) continue;
      seen.add(c.selector);
      candidates.push(c);
      if (candidates.length >= 5) break;
    }
    if (candidates.length === 0) return { error: `No element found with text '${needle}'`, candidates: [] };
    return { candidates, primary: candidates[0] };
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // ─── SECTION 4: MESSAGE HANDLER ───────────────────────────────────────────
  // ═══════════════════════════════════════════════════════════════════════════

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    (async () => {
      try {
        switch (message.type) {
          case "PING": {
            sendResponse({ pong: true });
            break;
          }
          case "EXTRACT_DOM": {
            const domState = extractDOM();
            sendResponse(domState);
            break;
          }
          case "EXTRACT_HTML": {
            const html = document.documentElement.outerHTML;
            sendResponse({ html, url: window.location.href });
            break;
          }
          case "EXTRACT_CODING_PROBLEM": {
            // Structured extraction of coding problem content for assessment platforms
            try {
              const result = extractCodingProblem();
              sendResponse(result);
            } catch (e) {
              sendResponse({ error: e.message });
            }
            break;
          }
          case "EXECUTE_ACTIONS": {
            const { steps } = message;
            const result = await executeActionBatch(steps);
            sendResponse(result);
            break;
          }
          case "EXTRACT_TEXT": {
            const result = extractText(message.query || "", message.selector || "");
            sendResponse(result);
            break;
          }
          case "EXTRACT_PAGE_CONTEXT": {
            // One-shot structured extraction for browser_see(mode="context").
            // Returns: { kind, dom, quiz, coding, blockers }
            try {
              const ctx = buildPageContext();
              sendResponse(ctx);
            } catch (e) {
              sendResponse({ error: e.message, kind: "unknown" });
            }
            break;
          }
          case "FIND_BY_TEXT": {
            // Resolve a text-based selector and return the canonical CSS selector
            // for the matching element. Falls back to walking the DOM and finding
            // the first element whose visible text matches the criteria.
            const result = findElementByText(message);
            sendResponse(result);
            break;
          }
          case "GET_ELEMENT_INFO": {
            const result = getElementInfo(message.selector);
            sendResponse(result);
            break;
          }
          case "WAIT_FOR_ELEMENT": {
            const result = await waitForElement(
              message.selector,
              message.timeout,
              message.require_visible !== false,
            );
            sendResponse(result);
            break;
          }
          case "OVERLAY_CONTROL": {
            if (typeof window.handleOverlayControl === "function") {
              window.handleOverlayControl(message);
            }
            sendResponse({ success: true });
            break;
          }
          case "EXTRACT_QUIZ_STRUCTURE": {
            try {
              const result = extractQuizStructure();
              sendResponse(result);
            } catch (e) {
              sendResponse({ error: e.message });
            }
            break;
          }
          default:
            sendResponse({ error: `Unknown message type: ${message.type}` });
        }
      } catch (err) {
        console.error("[Content] Error:", err);
        sendResponse({ error: err.message });
      }
    })();
    return true;
  });

  // ─── Utility ───────────────────────────────────────────────────────────────

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ─── Quiz Structure Extraction ──────────────────────────────────────────────

  function extractQuizStructure() {
    const result = {
      url: window.location.href,
      title: document.title,
      questions: [],
      quiz_type: "unknown", // single_question, multi_question
      question_number: null,
      total_questions: null,
      has_navigation: false,
      navigation: {},
    };

    // Detect quiz position from text patterns
    const bodyText = document.body.innerText || "";
    const posPatterns = [
      /Question\s*(?:No\.?\s*)?:?\s*(\d+)\s*(?:\/|of)\s*(\d+)/i,
      /(\d+)\s*(?:\/|of)\s*(\d+)\s*(?:questions?|Q)/i,
      /Q\s*(\d+)\s*(?:\/|of)\s*(\d+)/i,
    ];
    for (const pat of posPatterns) {
      const m = bodyText.match(pat);
      if (m) {
        result.question_number = parseInt(m[1], 10);
        result.total_questions = parseInt(m[2], 10);
        break;
      }
    }

    // Detect navigation buttons
    const navSelectors = {
      next: ['button[id*="next"]', '.next-btn', 'a.next', '[data-action="next"]'],
      previous: ['button[id*="prev"]', '.prev-btn', 'a.prev', '[data-action="previous"]'],
      submit: ['button[id*="submit"]', '.submit-btn', '[data-action="submit"]'],
    };
    for (const [action, selectors] of Object.entries(navSelectors)) {
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && isElementVisible(el)) {
          result.has_navigation = true;
          result.navigation[action] = buildSelector(el);
          break;
        }
      }
    }
    // Also check text-based nav buttons
    const allButtons = document.querySelectorAll('button, [role="button"], a.btn');
    for (const btn of allButtons) {
      if (!isElementVisible(btn)) continue;
      const text = (btn.textContent || "").trim().toLowerCase();
      if (!result.navigation.next && /^(next|continue|forward)\s*(question)?$/i.test(text)) {
        result.has_navigation = true;
        result.navigation.next = buildSelector(btn);
      }
      if (!result.navigation.previous && /^(prev|previous|back)\s*(question)?$/i.test(text)) {
        result.has_navigation = true;
        result.navigation.previous = buildSelector(btn);
      }
      if (!result.navigation.submit && /^(submit|finish|complete)\s*(test|quiz|exam)?$/i.test(text)) {
        result.has_navigation = true;
        result.navigation.submit = buildSelector(btn);
      }
    }

    // Extract questions from radio button groups
    const radioGroups = {};
    for (const radio of document.querySelectorAll('input[type="radio"]')) {
      const name = radio.getAttribute("name") || "__unnamed__";
      if (!radioGroups[name]) radioGroups[name] = [];
      radioGroups[name].push(radio);
    }

    // Extract questions from checkbox groups
    const checkboxGroups = {};
    for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
      const name = cb.getAttribute("name") || "__unnamed__";
      // Skip non-quiz checkboxes (terms, cookies, etc.)
      const label = getOptionLabel(cb);
      if (/agree|terms|cookie|newsletter|subscribe|remember/i.test(label)) continue;
      if (!checkboxGroups[name]) checkboxGroups[name] = [];
      checkboxGroups[name].push(cb);
    }

    // Process radio groups into questions
    for (const [groupName, radios] of Object.entries(radioGroups)) {
      if (radios.length < 2) continue;
      const question = extractQuestionFromOptions(radios);
      const options = radios.map(radio => ({
        text: getOptionLabel(radio),
        selector: buildSelector(radio),
        value: radio.value || "",
        selected: radio.checked,
        type: "radio",
      }));
      result.questions.push({
        question_text: question,
        group_name: groupName,
        options,
        type: "single_choice",
        answered: options.some(o => o.selected),
      });
    }

    // Process checkbox groups into questions
    for (const [groupName, checkboxes] of Object.entries(checkboxGroups)) {
      if (checkboxes.length < 2) continue;
      const question = extractQuestionFromOptions(checkboxes);
      const options = checkboxes.map(cb => ({
        text: getOptionLabel(cb),
        selector: buildSelector(cb),
        value: cb.value || "",
        selected: cb.checked,
        type: "checkbox",
      }));
      result.questions.push({
        question_text: question,
        group_name: groupName,
        options,
        type: "multi_choice",
        answered: options.some(o => o.selected),
      });
    }

    // Detect clickable option containers (common in modern quiz UIs)
    if (result.questions.length === 0) {
      const optionContainerSelectors = [
        '.option', '.answer-option', '.quiz-option', '.choice',
        '[class*="option"]', '[class*="answer"]', '[class*="choice"]',
        '[data-option]', '[data-answer]', '[role="option"]',
        'li[class*="option"]',
      ];
      const optionContainers = document.querySelectorAll(optionContainerSelectors.join(','));
      if (optionContainers.length >= 2) {
        const options = [];
        for (const container of optionContainers) {
          if (!isElementVisible(container)) continue;
          const text = (container.innerText || container.textContent || "").trim();
          if (!text || text.length > 500) continue;
          const isSelected = container.classList.contains('selected') ||
            container.classList.contains('active') ||
            container.classList.contains('chosen') ||
            container.getAttribute('aria-selected') === 'true';
          options.push({
            text: text.substring(0, 300),
            selector: buildSelector(container),
            value: "",
            selected: isSelected,
            type: "clickable",
          });
        }
        if (options.length >= 2) {
          const questionText = extractQuestionFromOptions(
            Array.from(optionContainers).filter(c => isElementVisible(c))
          );
          result.questions.push({
            question_text: questionText,
            group_name: "clickable_options",
            options,
            type: options.length <= 6 ? "single_choice" : "multi_choice",
            answered: options.some(o => o.selected),
          });
        }
      }
    }

    result.quiz_type = result.questions.length > 1 ? "multi_question" : "single_question";

    // Add page text context (truncated)
    result.page_context = bodyText.substring(0, 3000);

    return result;
  }

  function getOptionLabel(inputEl) {
    // 1. Check for associated label via 'for' attribute
    if (inputEl.id) {
      const label = document.querySelector(`label[for="${CSS.escape(inputEl.id)}"]`);
      if (label) return (label.innerText || label.textContent || "").trim();
    }

    // 2. Check parent label
    const parentLabel = inputEl.closest("label");
    if (parentLabel) {
      const clone = parentLabel.cloneNode(true);
      clone.querySelectorAll("input").forEach(i => i.remove());
      return (clone.innerText || clone.textContent || "").trim();
    }

    // 3. Check next sibling text
    let sibling = inputEl.nextSibling;
    while (sibling) {
      if (sibling.nodeType === Node.TEXT_NODE) {
        const text = sibling.textContent.trim();
        if (text) return text;
      }
      if (sibling.nodeType === Node.ELEMENT_NODE) {
        const text = (sibling.innerText || sibling.textContent || "").trim();
        if (text) return text;
        break;
      }
      sibling = sibling.nextSibling;
    }

    // 4. Check closest container
    const container = inputEl.closest("li, div, td, .option, .answer");
    if (container) {
      const clone = container.cloneNode(true);
      clone.querySelectorAll("input").forEach(i => i.remove());
      const text = (clone.innerText || clone.textContent || "").trim();
      if (text) return text;
    }

    return inputEl.value || "";
  }

  function extractQuestionFromOptions(optionElements) {
    if (!optionElements || optionElements.length === 0) return "";

    // Walk up from the first option to find a question container
    const firstOption = optionElements[0];
    let container = firstOption.closest(
      ".question, .quiz-item, .question-container, .quiz-question, " +
      "[class*='question'], [class*='quiz-item'], [data-question], " +
      "fieldset, .form-group, li, article, section"
    );

    if (!container) {
      container = firstOption.parentElement?.parentElement;
    }

    if (container) {
      // Look for question text in headings, paragraphs, legend, or question-specific elements
      const questionSelectors = [
        "h1, h2, h3, h4, h5, h6",
        ".question-text, .question-title, .question-body",
        "[class*='question-text'], [class*='questionText']",
        "legend",
        "p",
        "label:not(:has(input))",
      ];
      for (const sel of questionSelectors) {
        const candidates = container.querySelectorAll(sel);
        for (const candidate of candidates) {
          // Skip if candidate contains an option
          if (optionElements.some(opt => candidate.contains(opt))) continue;
          const text = (candidate.innerText || candidate.textContent || "").trim();
          if (text && text.length > 5 && text.length < 2000) return text;
        }
      }
    }

    // Fallback: find text content before the first option within the common ancestor
    const parent = firstOption.parentElement;
    if (parent) {
      let questionParts = [];
      for (const child of parent.parentElement?.children || []) {
        if (child === parent) break;
        const text = (child.innerText || child.textContent || "").trim();
        if (text && text.length > 3) questionParts.push(text);
      }
      if (questionParts.length > 0) return questionParts.join(" ");
    }

    return "";
  }

  // ─── Coding Problem Extraction ─────────────────────────────────────────────

  function elementText(el) {
    if (!el) return "";
    const raw = el.innerText ?? el.textContent ?? "";
    return typeof raw === "string" ? raw : String(raw);
  }

  function extractCodingProblem() {
    const result = {
      url: window.location.href,
      title: document.title,
      problem_statement: "",
      input_format: "",
      output_format: "",
      constraints: "",
      sample_inputs: [],
      sample_outputs: [],
      pre_blocks: [],
      detected_language: "",
      editor_type: "",
      has_editor: false,
      compile_button: "",
      submit_button: "",
      full_text: "",
      question_number: "",
    };

    // Detect editor type
    if (document.querySelector('.ace_editor')) {
      result.editor_type = "ace";
      result.has_editor = true;
    } else if (document.querySelector('.monaco-editor')) {
      result.editor_type = "monaco";
      result.has_editor = true;
    } else if (document.querySelector('.CodeMirror')) {
      result.editor_type = "codemirror5";
      result.has_editor = true;
    } else if (document.querySelector('.cm-editor')) {
      result.editor_type = "codemirror6";
      result.has_editor = true;
    }

    // Extract all pre/code blocks
    const preElements = document.querySelectorAll('pre');
    preElements.forEach((el, i) => {
      const text = elementText(el).trim();
      if (text) result.pre_blocks.push({ index: i, text: text });
    });

    // Extract code blocks too
    const codeElements = document.querySelectorAll('code:not(pre code)');
    codeElements.forEach((el, i) => {
      const text = elementText(el).trim();
      if (text && text.length > 1) {
        result.pre_blocks.push({ index: preElements.length + i, text: text, tag: 'code' });
      }
    });

    // Detect language from dropdowns, labels, or selected options
    const langSelectors = [
      'select[id*="lang"]', 'select[class*="lang"]', 'select[id*="programme"]',
      '.language-select', '[data-language]', '.lang-dropdown',
      'select option[selected]',
    ];
    for (const sel of langSelectors) {
      const el = document.querySelector(sel);
      if (el) {
        if (el.tagName === 'SELECT') {
          const selected = el.options[el.selectedIndex];
          if (selected) result.detected_language = selected.textContent.trim();
        } else if (el.tagName === 'OPTION') {
          result.detected_language = el.textContent.trim();
        } else {
          result.detected_language = el.textContent.trim() || el.getAttribute('data-language') || "";
        }
        break;
      }
    }

    // Detect language from visible text labels
    if (!result.detected_language) {
      const langMatch = elementText(document.body).match(/(?:Language|Compiler)\s*:?\s*(C\+\+|Python\s*3?|Java|JavaScript|C)\b/i);
      if (langMatch) result.detected_language = langMatch[1];
    }

    // Detect compile button
    const compileSelectors = [
      '#programme-compile', 'button[id*="compile"]',
      'button.compile', '.compile-btn', '[data-action="compile"]',
    ];
    for (const sel of compileSelectors) {
      const el = document.querySelector(sel);
      if (el) {
        result.compile_button = sel;
        break;
      }
    }
    // XPath fallback for compile
    if (!result.compile_button) {
      const buttons = document.querySelectorAll('button');
      for (const btn of buttons) {
        if (/compile|run/i.test(btn.textContent)) {
          result.compile_button = btn.id ? `#${btn.id}` : `button:nth-of-type(${Array.from(buttons).indexOf(btn) + 1})`;
          break;
        }
      }
    }

    // Detect submit button
    const submitSelectors = [
      '#tt-footer-submit-answer', 'button[id*="submit"]',
      'button.submit', '.submit-btn', '[data-action="submit"]',
    ];
    for (const sel of submitSelectors) {
      const el = document.querySelector(sel);
      if (el) {
        result.submit_button = sel;
        break;
      }
    }
    if (!result.submit_button) {
      const buttons = document.querySelectorAll('button');
      for (const btn of buttons) {
        if (/submit\s*(code|answer|solution)?/i.test(btn.textContent)) {
          result.submit_button = btn.id ? `#${btn.id}` : `button:nth-of-type(${Array.from(buttons).indexOf(btn) + 1})`;
          break;
        }
      }
    }

    // Extract full problem text - try common containers first
    const problemContainers = [
      '.problem-statement', '.question-content', '.coding-desc',
      '.problem-description', '.question-text', '#problem-statement',
      '.programme-question', '.question-section', '.problem-section',
      '[class*="question"]', '[class*="problem"]', '[class*="programme"]',
      '.content-area', 'main', 'article',
    ];

    let problemEl = null;
    for (const sel of problemContainers) {
      const el = document.querySelector(sel);
      if (el && elementText(el).trim().length > 50) {
        problemEl = el;
        break;
      }
    }

    if (problemEl) {
      result.full_text = elementText(problemEl).trim();
    } else {
      // Fallback: get the largest text block that's not the editor
      result.full_text = elementText(document.body).substring(0, 10000);
    }

    // Try to parse sections from the text
    const text = result.full_text;

    // Input format
    const inputMatch = text.match(/Input\s*(?:Format|Description|Specification)?\s*:?\s*\n([\s\S]*?)(?=Output|Constraints|Sample|Example|\n\n)/i);
    if (inputMatch) result.input_format = inputMatch[1].trim();

    // Output format
    const outputMatch = text.match(/Output\s*(?:Format|Description|Specification)?\s*:?\s*\n([\s\S]*?)(?=Constraints|Sample|Example|Input|\n\n)/i);
    if (outputMatch) result.output_format = outputMatch[1].trim();

    // Constraints
    const constraintMatch = text.match(/Constraints?\s*:?\s*\n([\s\S]*?)(?=Sample|Example|Input|Output|\n\n)/i);
    if (constraintMatch) result.constraints = constraintMatch[1].trim();

    // Sample I/O from labeled sections
    const sampleInputMatches = text.match(/Sample\s*Input\s*\d*\s*:?\s*\n([\s\S]*?)(?=Sample\s*Output|Expected\s*Output|\n\n)/gi);
    if (sampleInputMatches) {
      sampleInputMatches.forEach(m => {
        const content = m.replace(/Sample\s*Input\s*\d*\s*:?\s*\n/i, '').trim();
        if (content) result.sample_inputs.push(content);
      });
    }

    const sampleOutputMatches = text.match(/(?:Sample|Expected)\s*Output\s*\d*\s*:?\s*\n([\s\S]*?)(?=Sample\s*Input|Explanation|Note|\n\n|$)/gi);
    if (sampleOutputMatches) {
      sampleOutputMatches.forEach(m => {
        const content = m.replace(/(?:Sample|Expected)\s*Output\s*\d*\s*:?\s*\n/i, '').trim();
        if (content) result.sample_outputs.push(content);
      });
    }

    // Question number
    const qNumMatch = text.match(/(?:Question|Problem|Q)\s*(\d+)/i);
    if (qNumMatch) result.question_number = qNumMatch[1];

    // Problem statement (first substantial paragraph)
    const lines = text.split('\n').filter(l => l.trim().length > 0);
    const stmtLines = [];
    let started = false;
    for (const line of lines) {
      if (!started && line.trim().length > 30 && !/^(input|output|constraint|sample|example|question\s*\d)/i.test(line.trim())) {
        started = true;
      }
      if (started) {
        if (/^(Input|Output|Constraint|Sample|Example)\s*(Format|Description)?/i.test(line.trim())) break;
        stmtLines.push(line.trim());
      }
      if (stmtLines.length > 15) break;
    }
    result.problem_statement = stmtLines.join('\n');

    return result;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // ─── SECTION 5: SMART PAGE DETECTION ──────────────────────────────────────
  // ═══════════════════════════════════════════════════════════════════════════

  function detectPageContext() {
    const radios = document.querySelectorAll('input[type="radio"]');
    const title = document.title.toLowerCase();
    const url = window.location.href.toLowerCase();

    const isQuiz = radios.length >= 4 ||
      title.includes("quiz") ||
      title.includes("test") ||
      title.includes("exam") ||
      title.includes("assessment") ||
      url.includes("quiz") ||
      url.includes("assessment");

    if (isQuiz && radios.length > 0) {
      const radioGroups = new Set();
      radios.forEach(r => r.name && radioGroups.add(r.name));

      if (radioGroups.size >= 2) {
        showSmartHint("quiz", {
          questionCount: radioGroups.size,
          title: document.title
        });
      }
    }
  }

  function showSmartHint(type, data) {
    const hintKey = `aiagent_hint_${type}_${window.location.pathname}`;
    if (sessionStorage.getItem(hintKey)) return;
    sessionStorage.setItem(hintKey, "shown");

    const hint = document.createElement("div");
    hint.id = "ai-agent-hint";
    hint.style.cssText = `
      position: fixed;
      top: 20px;
      right: 20px;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: white;
      padding: 16px 20px;
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
      z-index: 999999;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 14px;
      max-width: 350px;
      animation: aiHintSlideIn 0.3s ease-out;
      cursor: pointer;
    `;

    let message = "";
    if (type === "quiz") {
      message = `
        <div style="display: flex; align-items: center; gap: 12px;">
          <span style="font-size: 24px;">🤖</span>
          <div>
            <div style="font-weight: 600; margin-bottom: 4px;">Quiz Detected!</div>
            <div style="opacity: 0.9; font-size: 13px;">
              Found ${data.questionCount} questions. I can complete this automatically!
            </div>
            <div style="margin-top: 8px; font-size: 12px; opacity: 0.8;">
              💡 Ask me: "Complete the quiz"
            </div>
          </div>
        </div>
      `;
    }

    hint.innerHTML = message;

    const style = document.createElement("style");
    style.textContent = `
      @keyframes aiHintSlideIn {
        from { transform: translateX(400px); opacity: 0; }
        to { transform: translateX(0); opacity: 1; }
      }
    `;
    document.head.appendChild(style);

    setTimeout(() => {
      if (hint.parentNode) {
        hint.style.animation = "aiHintSlideIn 0.3s ease-in reverse";
        setTimeout(() => hint.remove(), 300);
      }
    }, 10000);

    hint.addEventListener("click", () => {
      hint.style.animation = "aiHintSlideIn 0.3s ease-in reverse";
      setTimeout(() => hint.remove(), 300);
    });

    document.body.appendChild(hint);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      setTimeout(detectPageContext, 1000);
    });
  } else {
    setTimeout(detectPageContext, 1000);
  }

  console.log("[AI Agent] Content script v4 loaded on", window.location.href);
}
