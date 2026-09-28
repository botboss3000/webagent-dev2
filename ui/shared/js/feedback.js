/**
 * In-app feedback form — lives INSIDE the user dropdown panel.
 *
 * When "Send feedback" is clicked, the dropdown swaps from the default view
 * to the feedback sub-view (same panel, same position/size). "Cancel" or
 * the back arrow returns to the default dropdown view.
 *
 *   1. On load, fetch /api/v1/feedback/config to learn whether feedback is
 *      enabled and what the Turnstile site key is.
 *   2. If enabled, reveal the "Send feedback" item in the user dropdown.
 *   3. When opened, lazy-load the Turnstile script and render an invisible widget.
 *   4. On submit, POST {type, body, email, turnstile_token} to /api/v1/feedback.
 */

const CONFIG_URL = "/api/v1/feedback/config";
const SUBMIT_URL = "/api/v1/feedback";

let configPromise = null;
let turnstileWidgetId = null;
let turnstileLoaded = false;

function $(id) { return document.getElementById(id); }

function loadConfig() {
  if (!configPromise) {
    configPromise = fetch(CONFIG_URL, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : { enabled: false, turnstile_site_key: "" }))
      .catch(() => ({ enabled: false, turnstile_site_key: "" }));
  }
  return configPromise;
}

function loadTurnstileScript() {
  if (turnstileLoaded) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    s.async = true;
    s.defer = true;
    s.onload = () => { turnstileLoaded = true; resolve(); };
    s.onerror = () => reject(new Error("Failed to load Turnstile"));
    document.head.appendChild(s);
  });
}

async function ensureTurnstileWidget(siteKey) {
  if (!siteKey) return;
  await loadTurnstileScript();
  if (turnstileWidgetId !== null) return;
  const host = $("feedback-turnstile");
  if (!host || !window.turnstile) return;
  turnstileWidgetId = window.turnstile.render(host, {
    sitekey: siteKey,
    appearance: "interaction-only",
    size: "flexible",
  });
}

function resetTurnstile() {
  if (turnstileWidgetId !== null && window.turnstile) {
    try { window.turnstile.reset(turnstileWidgetId); } catch (_) {}
  }
}

function getTurnstileToken() {
  if (turnstileWidgetId !== null && window.turnstile) {
    try { return window.turnstile.getResponse(turnstileWidgetId) || ""; } catch (_) { return ""; }
  }
  return "";
}

function setStatus(msg, kind) {
  const el = $("feedback-status");
  if (!el) return;
  el.textContent = msg || "";
  el.className = "dropdown-feedback-status" + (kind ? " " + kind : "");
}

/** Show the feedback sub-view, hide the default dropdown contents. */
function openFeedbackView() {
  const dropdownMenu = $("user-dropdown-menu");
  const feedbackView = $("dropdown-feedback-view");
  const defaultItems = dropdownMenu?.querySelectorAll(
    ":scope > .user-dropdown-current-row, :scope > .user-dropdown-manage-btn, " +
    ":scope > .user-dropdown-divider, :scope > .user-dropdown-accounts-list, " +
    ":scope > #btn-add-account, :scope > .theme-toggle-section, " +
    ":scope > #btn-send-feedback, :scope > #btn-signout-header, " +
    ":scope > .user-dropdown-legal"
  );
  if (!dropdownMenu || !feedbackView) return;

  // Hide default items
  defaultItems?.forEach(el => el.style.display = "none");

  // Position: make the dropdown wide enough for the feedback form
  dropdownMenu.style.width = "360px";

  // Show feedback view
  feedbackView.style.display = "flex";
  setStatus("");
  loadConfig().then((cfg) => {
    if (cfg.turnstile_site_key) ensureTurnstileWidget(cfg.turnstile_site_key).catch(() => {});
  });
  setTimeout(() => { $("feedback-body")?.focus(); }, 50);
}

/** Return to the default dropdown view. */
function closeFeedbackView() {
  const dropdownMenu = $("user-dropdown-menu");
  const feedbackView = $("dropdown-feedback-view");
  const defaultItems = dropdownMenu?.querySelectorAll(
    ":scope > .user-dropdown-current-row, :scope > .user-dropdown-manage-btn, " +
    ":scope > .user-dropdown-divider, :scope > .user-dropdown-accounts-list, " +
    ":scope > .btn-add-account, :scope > .theme-toggle-section, " +
    ":scope > #btn-send-feedback, :scope > #btn-signout-header, " +
    ":scope > .user-dropdown-legal"
  );
  if (!dropdownMenu || !feedbackView) return;

  feedbackView.style.display = "none";
  defaultItems?.forEach(el => el.style.display = "");
  dropdownMenu.style.width = ""; // reset to default

  // Reset the form
  $("feedback-form")?.reset();
  resetTurnstile();
  setStatus("");
  turnstileWidgetId = null;
}

async function handleSubmit(e) {
  e.preventDefault();
  const typeRadio = document.querySelector('input[name="feedback-type"]:checked');
  const type = typeRadio ? typeRadio.value : "bug";
  const body = $("feedback-body")?.value.trim() || "";
  const email = $("feedback-email")?.value.trim() || "";
  const turnstile_token = getTurnstileToken();
  // Honeypot: real users leave this empty. Sent verbatim; the backend/relay
  // silently drop the submission if it's filled.
  const website = $("feedback-website")?.value || "";

  if (!body) {
    setStatus("Please describe your feedback before sending.", "error");
    return;
  }

  const cfg = await loadConfig();
  if (cfg.turnstile_site_key && !turnstile_token) {
    setStatus("Please complete the captcha first.", "error");
    return;
  }

  const btn = $("feedback-submit-btn");
  if (btn) btn.disabled = true;
  setStatus("Sending…");

  try {
    const r = await fetch(SUBMIT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        type,
        body,
        email: email || null,
        turnstile_token: turnstile_token || null,
        website: website || null,
      }),
    });
    if (r.status === 429) {
      setStatus("Too many submissions. Please try again later.", "error");
      resetTurnstile();
      return;
    }
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json())?.detail || ""; } catch (_) {}
      setStatus(detail || "Could not send feedback. Please try again.", "error");
      resetTurnstile();
      return;
    }
    await r.json().catch(() => ({}));
    setStatus("Thanks — your feedback was received.", "success");
    $("feedback-form")?.reset();
    resetTurnstile();
    setTimeout(closeFeedbackView, 1400);
  } catch (_) {
    setStatus("Network error. Please try again.", "error");
    resetTurnstile();
  } finally {
    if (btn) btn.disabled = false;
  }
}

function wireUp() {
  const openBtn = $("btn-send-feedback");
  const form = $("feedback-form");
  const bodyEl = $("feedback-body");
  const countEl = $("feedback-body-count");

  // Open: "Send feedback" in the dropdown
  if (openBtn) {
    openBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openFeedbackView();
    });
  }

  // Back arrow and Cancel button
  const backBtn = $("btn-feedback-back");
  const cancelBtn = $("btn-feedback-cancel");
  const closeX = $("btn-feedback-close-x");
  const close = () => { closeFeedbackView(); };
  if (backBtn) backBtn.addEventListener("click", close);
  if (cancelBtn) cancelBtn.addEventListener("click", close);
  if (closeX) closeX.addEventListener("click", close);

  // 3-way segmented toggle
  const segEl = $("feedback-segmented");
  if (segEl) {
    segEl.addEventListener("click", (e) => {
      const btn = e.target.closest(".fb-seg-opt");
      if (!btn) return;
      const val = btn.dataset.value;
      segEl.querySelectorAll(".fb-seg-opt").forEach(b => {
        b.classList.remove("active");
        b.setAttribute("aria-checked", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-checked", "true");
      const radio = $(`fb-type-${val}`);
      if (radio) radio.checked = true;
    });
  }

  // Form submit
  if (form) form.addEventListener("submit", handleSubmit);

  // Char counter
  if (bodyEl && countEl) {
    bodyEl.addEventListener("input", () => { countEl.textContent = String(bodyEl.value.length); });
  }

  // Reveal the button only if feedback is enabled on this deployment.
  loadConfig().then((cfg) => {
    if (cfg.enabled && openBtn) openBtn.style.display = "";
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", wireUp);
} else {
  wireUp();
}
