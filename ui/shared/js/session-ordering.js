'use strict';

function _sessionActivityTime(session) {
  // Terminal runs display their completion time in the picker. Use that same
  // timestamp for sorting so a visible “10m ago” row cannot appear below a
  // visible “12m ago” row because its older interaction timestamp won instead.
  const terminalRun = ['complete', 'interrupted', 'error'].includes(session?.run_status);
  const timestamp = (terminalRun && session?.run_updated_at)
    || session?.activity_at || session?.last_active
    || session?.updated_at || session?.created_at || 0;
  return Date.parse(timestamp) || 0;
}

/**
 * Sort sessions strictly by their most recent activity, newest first.
 * Session IDs make ties deterministic without introducing a secondary ranking.
 */
export function compareSessionsByRecentActivity(a, b) {
  return _sessionActivityTime(b) - _sessionActivityTime(a)
    || String(a?.id || '').localeCompare(String(b?.id || ''));
}

/**
 * The shared SESSION LIST ORDER: live pinned sessions appear first in their
 * user-defined database order, and every other session follows newest-first.
 *
 * This is the ONE comparator the sessions page's table
 * (ui/main-panel/agents/sessions/js/sessions-page.js) and the session activity
 * chart above that table both rank by, so a lane's position and a table row's
 * position cannot drift apart. The chat session picker ranks by it too, so all
 * three surfaces agree on what "the order of the sessions list" means.
 *
 * A binned session loses its pinned standing — but callers describe a bin
 * differently: the picker and the chart's endpoint carry `recycled`, while the
 * sessions table tags rows with `_origin: 'bin'`. A binned row keeps its pin flag
 * (so restoring brings the pin back), so EITHER marker must disqualify it, and an
 * absent marker is simply not a bin. Callers that carry neither are unaffected.
 */
export function compareSessionsByPinnedOrder(a, b) {
  const aPinned = !!(a?.pinned && a?._origin !== 'bin' && !a?.recycled);
  const bPinned = !!(b?.pinned && b?._origin !== 'bin' && !b?.recycled);
  if (aPinned !== bPinned) return aPinned ? -1 : 1;
  if (aPinned && bPinned) {
    const aOrder = Number.isFinite(a?.sort_order) ? a.sort_order : Number.MAX_SAFE_INTEGER;
    const bOrder = Number.isFinite(b?.sort_order) ? b.sort_order : Number.MAX_SAFE_INTEGER;
    if (aOrder !== bOrder) return aOrder - bOrder;
  }
  return compareSessionsByRecentActivity(a, b);
}
