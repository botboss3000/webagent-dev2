'use strict';

// Chat message visibility — independent show/hide per message lane type.
//
// The transcript is classified into lane types (main / progress / tool /
// summary / system — see messageTypeOf below, which mirrors the backend's
// `message_type` field in db_viewer.py). Every render path asks
// isMessageTypeVisible(type) before painting a lane, so hiding a type keeps
// storage complete but removes its bubbles everywhere: cold load, reconcile
// poll, live WS events, and virtual-scroll recycling.
//
// Defaults come from chat_ui.json → chat_common.message_visibility.defaults.
// The user's per-device overrides live in localStorage, written by the header
// filter popover (ui/chat/elements/visibility). A change fires listeners
// (the popover re-renders the transcript from the cache).

import { getAgentChatUi } from './app-prompts.js';
import { app } from './state.js';

const LS_KEY = 'chat.messageVisibility';
const _listeners = new Set();
// Keyed by the current agent id: config defaults come from chat_ui.json +
// the agent's metadata.chat_ui override, so a cached merge from agent A must
// not leak into agent B after a switch. (localStorage stays global — it holds
// only the user's manual toggles.)
const _cacheByAgent = new Map();

const _FALLBACK_DEFAULTS = {
  main: true,
  progress: true,
  tool: true,
  summary: true,
  system: true,
};

/** Config defaults from chat_ui.json (empty object when absent/unreadable). */
function _configDefaults() {
  try {
    const ui = getAgentChatUi();
    const d = ui && ui.chat_common && ui.chat_common.message_visibility
      && ui.chat_common.message_visibility.defaults;
    if (d && typeof d === 'object' && !Array.isArray(d)) return { ...d };
  } catch (_) { /* non-fatal */ }
  return {};
}

/** Merged map: fallback defaults ← config defaults ← localStorage override. */
function _load() {
  const agentKey = (app && app.currentAgentId) || '';
  const cached = _cacheByAgent.get(agentKey);
  if (cached) return cached;
  const merged = { ..._FALLBACK_DEFAULTS, ..._configDefaults() };
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) Object.assign(merged, JSON.parse(raw));
  } catch (_) { /* non-fatal */ }
  _cacheByAgent.set(agentKey, merged);
  return merged;
}

/** Is this lane type currently visible? Unknown types default to visible. */
export function isMessageTypeVisible(type) {
  return _load()[type] !== false;
}

/** Current visibility map (copy — safe to mutate). */
export function getMessageVisibility() {
  return { ..._load() };
}

/** Set one lane's visibility, persist the override, notify listeners. */
export function setMessageTypeVisible(type, visible) {
  const merged = _load();
  merged[type] = !!visible;
  try { localStorage.setItem(LS_KEY, JSON.stringify(merged)); } catch (_) { /* non-fatal */ }
  _listeners.forEach(fn => {
    try { fn(type, !!visible, getMessageVisibility()); } catch (_) { /* non-fatal */ }
  });
}

/** Subscribe to visibility changes; returns an unsubscribe function. */
export function onMessageVisibilityChange(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

/** Clear the per-device override (back to config defaults). */
export function resetMessageVisibility() {
  _cacheByAgent.clear();
  try { localStorage.removeItem(LS_KEY); } catch (_) { /* non-fatal */ }
  _listeners.forEach(fn => {
    try { fn(null, null, getMessageVisibility()); } catch (_) { /* non-fatal */ }
  });
}

/**
 * Is this row an Output Closer recap (the 'Closer' lane bubble)?
 * Checks the server's `source` (system:closer / legacy system:summary /
 * system:overview), the
 * server-derived `message_type`, and — the rescue for legacy CACHED rows whose
 * `source` was dropped by an older cache writer — the metadata `kind:'summary'`
 * marker the closer persists alongside the row.
 */
export function isSummaryRow(msg) {
  if (!msg || msg.role !== 'system') return false;
  const src = msg.source || '';
  if (src === 'system:summary' || src === 'system:overview' || src === 'system:closer') return true;
  if (msg.message_type === 'summary') return true;
  try {
    const meta = typeof msg.metadata === 'string' ? JSON.parse(msg.metadata) : msg.metadata;
    return !!(meta && meta.kind === 'summary');
  } catch (_) { /* non-fatal */ }
  return false;
}

/**
 * Classify one message row (DB payload or live WS event shape) into a lane
 * type. Uses the server's derived `message_type` when present; otherwise
 * derives from the same axes the backend uses (role / source / status / phase).
 * Returns null for rows that have no lane (shouldn't normally happen).
 */
export function messageTypeOf(msg) {
  if (!msg || typeof msg !== 'object') return null;
  if (msg.message_type) return msg.message_type; // server-derived, authoritative
  const role = msg.role || '';
  if (role === 'user') return 'user';
  if (role === 'tool' || role === 'tool_result') return 'tool';
  if (role === 'system') {
    return isSummaryRow(msg) ? 'summary' : 'system';
  }
  if (role === 'assistant') {
    if (msg.status === 'interrupted' || msg.status === 'error') return 'system';
    if (msg.status === 'streaming') return 'main';
    const renderKind = String(msg.chat_render_kind || 'activity').toLowerCase();
    if (renderKind === 'promoted_answer' || renderKind === 'notice') return 'main';
    if (renderKind === 'error') return 'system';
    return 'progress';
  }
  return null;
}
