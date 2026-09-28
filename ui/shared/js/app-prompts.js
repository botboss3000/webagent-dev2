'use strict';

// UI message catalog — the user-facing chat strings (the welcome bubble, the
// "new session" / "switched agent" system bubbles, and the composer pill
// placeholder) are authored in data/config/chat_ui.json under
// "chat_common.messages" and surface-specific message overrides, served in
// the public auth UI config.
//
// This module fetches the FULL chat_ui.json at boot (loadUiMessages, called
// from ui/shared/js/main.js) and keeps it. Per-agent overrides are fetched
// from the agent list (window.__agentsSharedData.agents) and deep-merged on
// demand so each agent can have completely custom chat chrome.
//
// Consumers:
//   • ui/chat/js/session-load.js   (welcome + restricted new-session)
//   • ui/chat/js/session-init.js   (new-session + switched-agent)
//   • ui/chat/js/chat-send.js       (pill placeholder)
//   • ui/chat/js/chat-ui.js         (full chat chrome rendering)
//   • ui/chat/chat-side-panel.html  (static fallbacks, patched here)

import { apiPath } from './config.js';
import { app } from './state.js';
import { authHeaders } from './left-login.js';

// Fallback defaults — KEEP IN SYNC with chat_ui.json chat_common.messages.
const _DEFAULTS = {
  welcome_bubble: 'Welcome to WebAgent',
  new_session_bubble: 'New session. Start typing below.',
  switched_agent_bubble: 'Switched agent. New session started.',
  pill_placeholder: 'Chat',
  session_deleted_notice: 'Session not found — it was deleted.',
};

// The live shared_default singleton predates template-level chat_ui metadata on
// many installs. Keep its effective starter prompts available client-side until
// an admin explicitly saves an override (including [] to show none).
export const DEFAULT_WEBAGENT_SUGGESTION_PROMPTS = Object.freeze([
  'Help me plan what to work on next',
  'Review this project and suggest the highest-impact improvement',
]);

// The FULL app-wide chat_ui.json (all surfaces). Loaded once at boot.
// Stays null until the server responds — the pipeline MUST wait for agents
// to load before applying any config (see chat-controls-config.js).
let _fullChatUi = null;
// The flattened active-surface messages (chat_common.messages merged with the
// current surface's messages). Same shape as _DEFAULTS; kept for chatMsg().
let _chat = { ..._DEFAULTS };

/** Synchronous accessor for the app-wide chat UI string. Always usable. */
export function chatMsg(key) {
  return (_chat && _chat[key]) || _DEFAULTS[key] || '';
}

/**
 * Check if debug mode is active for the current (or given) agent.
 * Returns true only when chat_ui.json chat_common.debug.enabled is true.
 * A legacy boolean `debug: true` remains supported for existing overrides.
 */
export function isDebugMode(agentId) {
  const ui = getAgentChatUi(agentId || (app && app.currentAgentId));
  const debug = ui.chat_common?.debug;
  return debug === true || debug?.enabled === true;
}

/**
 * Read a boolean flag from the current agent's deep-merged chat_ui config
 * (chat_common.<key>). Falls back to `defaultVal` when absent/unreadable.
 */
export function chatUiFlag(key, defaultVal) {
  try {
    const ui = getAgentChatUi();
    const common = ui && ui.chat_common;
    if (common && common[key] !== undefined) return common[key];
    return defaultVal;
  } catch (_) { return defaultVal; }
}

// ── Per-agent chat UI ────────────────────────────────────────────────────────

/** Deep-merge two objects (nested dicts merged; scalars/arrays replaced).
 *  Returns a new object; neither argument is mutated. */
function _deepMerge(base, overrides) {
  if (!overrides || typeof overrides !== 'object') return base;
  const out = { ...base };
  for (const key of Object.keys(overrides)) {
    const ov = overrides[key];
    const bv = base[key];
    if (ov && typeof ov === 'object' && !Array.isArray(ov) && bv && typeof bv === 'object' && !Array.isArray(bv)) {
      out[key] = _deepMerge(bv, ov);
    } else {
      out[key] = ov;
    }
  }
  return out;
}

function _applyEnforcedTierGate(config) {
  const enforced = config?._tier_enforced;
  if (!enforced || enforced.denied) return config;
  let out = _deepMerge(config || {}, enforced.override || {});
  const denied = new Set(enforced.controls || []);
  const walk = (value) => {
    if (Array.isArray(value)) return value.filter(item => !(typeof item === 'string' && denied.has(item))).map(walk);
    if (!value || typeof value !== 'object') return value;
    const next = {};
    for (const [key, child] of Object.entries(value)) {
      if (key === 'controls' && child && typeof child === 'object' && !Array.isArray(child)) {
        next[key] = {};
        for (const [name, cfg] of Object.entries(child)) {
          const resolved = walk(cfg);
          next[key][name] = denied.has(name)
            ? _deepMerge(resolved && typeof resolved === 'object' ? resolved : {}, { enabled: false })
            : resolved;
        }
      } else if (denied.has(key)) {
        // Named booleans such as session_dropdown.menu.session_id are controls
        // too; clamp them after per-agent overrides just like controls maps.
        next[key] = false;
      } else {
        next[key] = walk(child);
      }
    }
    return next;
  };
  return walk(out);
}

// Per-agent override cache: agentId → chat_ui override dict (from agents list).
const _agentOverrideCache = new Map();

/** Find a cached agent's per-agent chat UI override map. Agents are fetched once
 *  into window.__agentsSharedData.agents (each carries a `chat_ui` object served
 *  by app/api/agents.py _safe_agent); a blank/missing value means "use default". */
function _agentChatUiOverride(agentId) {
  if (!agentId) return null;
  let cached = _agentOverrideCache.get(agentId);
  if (cached !== undefined) return cached;
  let ov = null;
  try {
    const list = window.__agentsSharedData && window.__agentsSharedData.agents;
    if (Array.isArray(list)) {
      const a = list.find(x => x && x.id === agentId);
      if (a && a.chat_ui && typeof a.chat_ui === 'object' && !Array.isArray(a.chat_ui) && Object.keys(a.chat_ui).length > 0) {
        ov = a.chat_ui;
      }
    }
  } catch (_) { /* non-fatal */ }
  // Only cache if we found agents data — otherwise leave uncached so
  // a subsequent call after agents load will find the real override.
  if (window.__agentsSharedData && window.__agentsSharedData.agents) {
    _agentOverrideCache.set(agentId, ov);
  }
  return ov;
}

/** Clear the per-agent override cache — call after agents list refreshes. */
export function clearAgentChatUiCache() {
  _agentOverrideCache.clear();
}

/**
 * Return the FULL deep-merged chat_ui config for a specific agent.
 *
 * Starts with the app-wide chat_ui.json, then deep-merges the agent's
 * metadata.chat_ui override on top. Returns the full JSON (all surfaces,
 * chat_common, chat_pill, launcher, etc.) so the chat panel can render
 * per-agent chrome.
 */
export function getAgentChatUi(agentId) {
  const id = agentId || (app && app.currentAgentId) || '';
  const override = _agentChatUiOverride(id);
  if (!override) {
    // No per-agent override — return server config or a minimal stub that
    // always has chat_common so the pipeline doesn't bail early.
    const base = _fullChatUi || {};
    if (!base.chat_common) base.chat_common = {};
    return _applyEnforcedTierGate(base);
  }
  const merged = _applyEnforcedTierGate(_deepMerge(_fullChatUi || {}, override));
  // Surface-only overrides (chat_mobile/chat_desktop without chat_common)
  // must still carry chat_common so mergeProfile() has a base to build from.
  if (!merged.chat_common) merged.chat_common = {};
  return merged;
}

/**
 * Per-agent chat UI string. Looks up `key` inside the active surface's
 * messages block of the agent's deep-merged chat_ui, falling back to the
 * app-wide default. `agentId` defaults to the current chat agent.
 *
 * Use this (not chatMsg) for the welcome / new-session / switched-agent
 * bubbles and the composer pill placeholder.
 */
export function agentChatMsg(key, agentId) {
  const id = agentId || (app && app.currentAgentId) || '';
  // Try the agent's merged surface messages first
  const agentUi = getAgentChatUi(id);
  const surfaceKey = window.__CHAT_PORTAL__
    ? 'chat_widget'
    : (window.innerWidth <= 768 ? 'chat_mobile' : 'chat_desktop');
  const surface = agentUi[surfaceKey] || {};
  const surfaceMsgs = surface.messages || {};
  const commonMsgs = (agentUi.chat_common && agentUi.chat_common.messages) || {};
  const v = surfaceMsgs[key] || commonMsgs[key];
  if (typeof v === 'string' && v.trim()) return v;
  return chatMsg(key);
}

// ── Boot-time load ───────────────────────────────────────────────────────────

/**
 * Fetch the live strings once and merge them over the defaults. Resolves even
 * on failure (keeps the fallbacks) so it can be awaited on the boot path
 * without risk of stalling the app.
 */
export async function loadUiMessages(agentId) {
  try {
    // Keep the global config as the immutable merge base. Fetching an already
    // agent-resolved response here would make that override leak into the next
    // agent when getAgentChatUi() treats _fullChatUi as its base.
    // Per-agent chat_ui is merged exclusively from the current agent record.
    const res = await window.__fetchUiConfig({ headers: { ...authHeaders() } });
    if (!res.ok) return null;
    const data = await res.json();
    const ui = data?.chat_ui;
    if (ui) {
      _fullChatUi = ui;
      window.__chatUiSnapshot = ui;  // snapshot for pill defaults before agents load
      const surface = window.__CHAT_PORTAL__
        ? ui.chat_widget
        : (window.innerWidth <= 768 ? ui.chat_mobile : ui.chat_desktop);
      _applyStaticComposerGeometry(ui);
      document.body.dataset.chatUiReady = 'true';
      const chat = { ...(ui.chat_common?.messages || {}), ...(surface?.messages || {}) };
      if (chat && typeof chat === 'object') {
        _chat = { ..._DEFAULTS, ...chat };
        _applyStaticStrings();
      }
    }
    return data;
  } catch (_) { return null; /* unreachable config — keep fallbacks */ }
}

// Patch the strings that live in static markup (the composer pill placeholder)
// once the JSON arrives, covering the window before any JS render. The welcome
// bubble is re-rendered from chatMsg() by session-load.js on session load, so
// it needs no static patch here.
// Apply only geometry that the static boot composer needs. This runs as soon as
// chat_ui.json arrives, before the footer builder reparents any controls.
function _applyStaticComposerGeometry(ui) {
  const surfaceKey = window.__CHAT_PORTAL__
    ? 'chat_widget'
    : (window.innerWidth <= 768 ? 'chat_mobile' : 'chat_desktop');
  const profile = { ...(ui?.chat_common || {}), ...(ui?.[surfaceKey] || {}) };
  const idle = profile.idle_footer?.chat_pill;
  const active = profile.active_footer?.chat_pill;
  const width = idle?.pill_width || active?.pill_width;
  const radius = idle?.pill_radius || active?.pill_radius;
  const panel = document.getElementById('chat-panel');
  const area = document.getElementById('chat-input-area');
  if (width && panel) {
    panel.style.setProperty('--chat-pill-width', width);
    panel.style.setProperty('--chat-pill-current-width', width);
  }
  if (width && area) area.style.setProperty('--idle-pill-width', width);
  if (radius && panel) panel.style.setProperty('--chat-pill-radius', radius);
}

function _applyStaticStrings() {
  try {
    const input = document.getElementById('chat-input');
    if (input && !input.disabled) input.placeholder = chatMsg('pill_placeholder');
  } catch (_) { /* DOM not ready / non-fatal */ }
}
