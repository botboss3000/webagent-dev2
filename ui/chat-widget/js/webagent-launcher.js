'use strict';

// ── WebAgent launcher — global floating bot icon (THIN WRAPPER) ─────────────
// This file used to own the whole launcher (bot SVG, animations, drag, hover
// corner menu, sessions popup, element pickup). All of that logic now lives in
// the SHARED component ui/chat-widget/js/chat-launcher.js (createChatLauncher),
// which any page can reuse with its own icon / agent / corner buttons / widget
// options. This module keeps only the GLOBAL instance's wiring:
//   • the admin gate (widget_launcher_visible in ui-config),
//   • the chat_widget admin config (corners, agent, prompt, detection toggles),
//   • the #webagent-launcher id + default WebAgent agent resolution,
//   • the corner-button config from chat_ui.json (launcher.corner_buttons),
//   • the legacy 'webagent-launcher-position' storage key + element pickup.
// Behavior is byte-for-byte what the old singleton did — that is the regression
// test for the shared factory.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { ensureWebagentAgent } from '../../chat/js/session-agent.js';
import { _widgetChatUiProfile } from './chat-widget.js';
import { createChatLauncher } from './chat-launcher.js';

const STORAGE_KEY = 'webagent-launcher-position';

/**
 * Mount the global WebAgent launcher (once). No-op for signed-out visitors or
 * when an admin disabled the widget launcher in App Settings.
 * @returns {object|null} the launcher api ({el, open, close, destroy, …})
 */
export async function initWebagentLauncher() {
  if (document.getElementById('webagent-launcher') || !app.currentUserId) return null;

  // Gate: widget launcher is hidden unless an admin toggled it on in App Settings.
  // Also read the admin's chat_widget config (corners, agent, prompt, detection).
  let widgetCfg = {};
  try {
    const r = await window.__fetchUiConfig({ headers: { ...authHeaders() } });
    if (r.ok) {
      const cfg = await r.json();
      if (cfg.widget_launcher_visible !== true) return null;
      widgetCfg = cfg.chat_widget;
      if (!widgetCfg || typeof widgetCfg !== 'object') widgetCfg = {};
    }
  } catch (_) { /* if config fetch fails, default to hidden */ return null; }

  // ── Detection toggles ──
  // Admin can disable pickup entirely or switch to surroundings mode.
  // Both off → no pickup at all; surroundings on → composite N/E/S/W mode;
  // only 12oclock on (default) → existing single-point-above behavior.
  const pickup12 = widgetCfg.pickup_12oclock !== false;      // default ON
  const pickupSurroundings = widgetCfg.pickup_surroundings === true;
  const elementPickup = pickup12 || pickupSurroundings;
  const pickupMode = pickupSurroundings ? 'surroundings' : '12oclock';

  // ── Agent resolution ──
  // Admin-configured agent id wins; blank → default WebAgent.
  const configuredAgentId = (widgetCfg.agent_id || '').trim();

  // ── Corner config ──
  // Admin-configured corner_buttons win; absent → fall back to chat_ui.json.
  const adminCorners = widgetCfg.corner_buttons && typeof widgetCfg.corner_buttons === 'object'
    ? widgetCfg.corner_buttons : null;

  // ── Prompt ──
  const prependPrompt = (widgetCfg.prompt || '').trim();

  const launcher = createChatLauncher({
    id: 'webagent-launcher',
    mountEl: document.body,
    position: 'fixed',
    icon: 'bot',
    iconSize: 27,
    ariaLabel: 'Open WebAgent chat',
    storageKey: STORAGE_KEY,
    elementPickup,
    pickupMode: elementPickup ? pickupMode : '12oclock',
    resolveAgent: configuredAgentId
      ? async () => {
          try {
            const res = await fetch(
              apiPath(`/api/v1/agents?user_id=${encodeURIComponent(app.currentUserId)}`),
              { headers: authHeaders() },
            );
            if (res.ok) {
              const data = await res.json();
              const found = (data.agents || []).find((a) => a.id === configuredAgentId);
              if (found) return found;
            }
          } catch (_) {}
          return { id: configuredAgentId, name: configuredAgentId, icon: 'bot' };
        }
      : resolveWebagent,
    // Corner buttons: admin config first, then fall back to chat_ui.json.
    cornerButtonsProvider: async () => {
      if (adminCorners) return adminCorners;
      const profile = await _widgetChatUiProfile();
      return profile?.launcher?.corner_buttons;
    },
    widget: {
      transformMessage: prependPrompt
        ? async (text) => prependPrompt + '\n\n' + text
        : undefined,
    },
  });

  // Resolve the agent name for the aria-label (best-effort).
  const agentResolver = configuredAgentId
    ? async () => {
        try {
          const res = await fetch(
            apiPath(`/api/v1/agents?user_id=${encodeURIComponent(app.currentUserId)}`),
            { headers: authHeaders() },
          );
          if (res.ok) {
            const data = await res.json();
            const found = (data.agents || []).find((a) => a.id === configuredAgentId);
            if (found) return found;
          }
        } catch (_) {}
        return { id: configuredAgentId, name: configuredAgentId, icon: 'bot' };
      }
    : resolveWebagent;
  agentResolver().then((value) => {
    launcher.button.setAttribute('aria-label', `Open ${value.name || 'WebAgent'} chat`);
  }).catch(() => {});

  return launcher;
}

// ── Agent resolver (unchanged) ──────────────────────────────────────────────

async function resolveWebagent() {
  const userId = app.currentUserId;
  const id = await ensureWebagentAgent(userId);
  try {
    const res = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`), { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      const found = (data.agents || []).find((item) => item.id === id);
      if (found) return found;
    }
  } catch (_) {}
  return { id, name: 'WebAgent', icon: 'bot' };
}
