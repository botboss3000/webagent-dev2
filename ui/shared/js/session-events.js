'use strict';

/**
 * Shared "a session changed" notifier.
 *
 * One small app-wide signal so the places that LIST sessions (the chat side
 * panel's session list + the admin Sessions table) stay in sync no matter
 * where a session is mutated. Whenever a session is deleted, recycled,
 * restored, renamed, pinned, or hidden — from the chat header, a session-row
 * menu, or the Sessions page toolbar — the mutating code calls
 * notifySessionsChanged(); every listener re-fetches its own view.
 *
 * Built on a plain window CustomEvent (same pattern as `webagent-auth-expired`)
 * so there's no shared state to keep consistent — just fire-and-forget. Only
 * MUTATIONS dispatch; listeners merely re-read, so there is no feedback loop.
 */

export const SESSIONS_CHANGED = 'webagent-sessions-changed';

// The chat panel owns the active session. This separate signal keeps every
// session-listing surface aligned with that single source of truth without
// treating a session switch as a database mutation.
export const ACTIVE_SESSION_CHANGED = 'webagent-active-session-changed';

// Fire after a successful session mutation. `detail` is optional context
// (e.g. { action: 'delete', ids: [...] }) — listeners may ignore it and just
// reload.
export function notifySessionsChanged(detail = {}) {
  try {
    window.dispatchEvent(new CustomEvent(SESSIONS_CHANGED, { detail }));
  } catch (_) { /* CustomEvent unsupported — ignore */ }
}

// Fire immediately when the chat panel accepts a different current session.
// Consumers should reconcile their existing DOM rather than refetching data.
export function notifyActiveSessionChanged(sessionId) {
  if (!sessionId) return;
  try {
    window.dispatchEvent(new CustomEvent(ACTIVE_SESSION_CHANGED, { detail: { sessionId } }));
  } catch (_) { /* CustomEvent unsupported — ignore */ }
}

// Subscribe to session-change notifications. Returns an unsubscribe fn.
export function onSessionsChanged(cb) {
  if (typeof cb !== 'function') return () => {};
  window.addEventListener(SESSIONS_CHANGED, cb);
  return () => window.removeEventListener(SESSIONS_CHANGED, cb);
}

// Subscribe to chat-panel active-session changes. The event detail is
// `{ sessionId }`, emitted only after the chat has accepted that session.
export function onActiveSessionChanged(cb) {
  if (typeof cb !== 'function') return () => {};
  window.addEventListener(ACTIVE_SESSION_CHANGED, cb);
  return () => window.removeEventListener(ACTIVE_SESSION_CHANGED, cb);
}
