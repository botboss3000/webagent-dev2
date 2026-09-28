"use strict";
// RECORDER-HOOK: generic optional application frontend loader.
// Catalog: app/application_plugins.py; recorder consumer: plugins/recorder/frontend.
// Remove this file and its index.html script together if no application plugins
// remain. No imports from optional packages belong on chat's critical boot path.
import { authHeaders, isAdmin } from "./left-login.js?v=253";
import { apiPath } from "./config.js";
import { partialsReady } from "./partial-loader.js";
const loaded = new Set();
let busy = false;
let retry = null;
function retryDiscovery() {
  if (!retry) retry = setTimeout(() => { retry = null; load(); }, 5000);
}
// Match main.js auth module identity so a previously resolved admin is visible.
let adminResolved = isAdmin();
async function load() {
  // Cached module loads can outrun injected DOM during a warm reload.
  try { await partialsReady; } catch (_) { return; }
  // Admin resolution dispatches before main.js applies its CSS class.
  // Use its event detail: legacy versioned auth modules may have separate caches.
  if (busy || !(adminResolved || document.body.classList.contains("is-admin"))) return;
  busy = true;
  try {
    const response = await fetch(apiPath("/api/v1/application-plugins"), {headers:authHeaders()});
    if (!response.ok) { if (response.status !== 403) retryDiscovery(); return; }
    const data = await response.json();
    for (const plugin of data.plugins || []) {
      if (loaded.has(plugin.id)) continue;
      const url = new URL(plugin.entry_url, location.origin);
      if (url.origin !== location.origin || !url.pathname.startsWith("/plugin-assets/")) continue;
      try { await import(url.href); loaded.add(plugin.id); }
      catch (_) { console.warn("DEBUG-TAG:application-plugin-unavailable", plugin.id); retryDiscovery(); }
    }
  } catch (_) { retryDiscovery(); /* Optional discovery never prevents normal app use. */ }
  finally { busy = false; }
}
window.addEventListener("admin-status-loaded", event => {
  adminResolved = !!event.detail?.is_admin;
  load();
});
window.addEventListener("auth-changed", load);
window.addEventListener("webagent-app-ready", load);
load();
