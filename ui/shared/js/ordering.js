'use strict';

/**
 * ordering.js — shared row-ordering helpers for the chat-header dropdowns.
 *
 * Two concerns live here so the agent dropdown (sessions.js) and the Agents
 * page (agents.js) stay in sync:
 *
 *   1. Agent pin state + display sort. Pins are a per-user localStorage flag
 *      (kept here, formerly in sessions.js). `sortAgentsForDisplay` floats
 *      pinned agents to the top while otherwise preserving the order the
 *      server already returned (which is the synced `sort_order` column).
 *      Both the dropdown and the Agents page call it, so they match exactly.
 *
 *   2. Drag-to-reorder. `makeRowsReorderable` wires a pointer-based (mouse +
 *      touch) drag from a handle on each row (either a dedicated grip element or
 *      the row itself for full-row drag), then hands the resulting top-to-bottom
 *      id list back to the caller, which persists it to the DB.
 *
 * The persisted order is account-level (DB), so it follows the user across
 * devices: agents via POST /api/v1/agents/reorder. For sessions, only pinned
 * rows are manually reorderable; unpinned rows always follow recent activity.
 * Pinned order persists via POST /api/v1/db/sessions/reorder.
 */

import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

// ── Agent pin state (per-user, localStorage) ──────────────────────────────────

function _pinnedAgentsKey(userId) {
  return `pinnedAgents:${userId || 'anon'}`;
}

export function getPinnedAgents(userId) {
  try {
    const raw = localStorage.getItem(_pinnedAgentsKey(userId));
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (_) { return new Set(); }
}

function setPinnedAgents(userId, set) {
  try {
    localStorage.setItem(_pinnedAgentsKey(userId), JSON.stringify(Array.from(set)));
  } catch (_) {}
}

/** Toggle an agent's pin and return the updated Set. */
function toggleAgentPin(userId, agentId) {
  const pinned = getPinnedAgents(userId);
  if (pinned.has(agentId)) pinned.delete(agentId);
  else pinned.add(agentId);
  setPinnedAgents(userId, pinned);
  return pinned;
}

/**
 * Return a new array with pinned agents first, otherwise preserving the input
 * order (which is already the server's sort_order order). Stable: equal-pin
 * rows keep their incoming relative order.
 */
export function sortAgentsForDisplay(agents, userId) {
  const pinned = getPinnedAgents(userId);
  return agents
    .map((a, i) => ({ a, i }))
    .sort((x, y) => {
      const px = pinned.has(x.a.id) ? 0 : 1;
      const py = pinned.has(y.a.id) ? 0 : 1;
      if (px !== py) return px - py;
      return x.i - y.i;
    })
    .map((o) => o.a);
}

// ── Persistence ───────────────────────────────────────────────────────────────

async function persistAgentOrder(userId, orderedIds) {
  if (!userId || !Array.isArray(orderedIds) || !orderedIds.length) return;
  try {
    await fetch(apiPath('/api/v1/agents/reorder'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: userId, order: orderedIds }),
    });
  } catch (e) {
    console.warn('Failed to persist agent order:', e);
  }
}

export async function persistSessionOrder(userId, orderedIds) {
  if (!userId || !Array.isArray(orderedIds) || !orderedIds.length) return true;
  const res = await fetch(apiPath('/api/v1/db/sessions/reorder?db=user.db'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ user_id: userId, order: orderedIds }),
  });
  if (!res.ok) throw new Error(`Session reorder failed (HTTP ${res.status})`);
  const data = await res.json();
  if (!data.success || data.updated !== orderedIds.length) {
    throw new Error(`Session reorder saved ${data.updated || 0} of ${orderedIds.length} rows`);
  }
  return true;
}

// ── Pointer-based drag-to-reorder ──────────────────────────────────────────────
//
// Works for mouse and touch. Drag is initiated only from a grip handle inside a
// row (so row-body clicks still select and the kebab still opens). The handle
// must carry `touch-action: none` in CSS so the browser doesn't treat the touch
// as a scroll. Delegated from the container, so it survives innerHTML rebuilds.

const _DRAG_THRESHOLD = 4;   // px the pointer must travel before a drag "arms"
const _EDGE = 28;            // px from a scroll edge that triggers auto-scroll
const _LONG_PRESS_MS = 500;  // press-and-hold duration before a long-press fires
const _LONG_PRESS_MOVE = 8;  // px of movement that cancels a pending long-press

/**
 * Block the click the browser synthesises after a pointer sequence (a drag drop,
 * a handle tap, or a long-press) so it doesn't also fall through to the row's
 * own click handler (which would select / switch). Capture phase + stopPropagation
 * so it pre-empts the bubble-phase row handler bound on the same container.
 * Self-removes after one click or 350ms, whichever comes first.
 */
function _suppressNextClick(container) {
  const handler = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    container.removeEventListener('click', handler, true);
  };
  container.addEventListener('click', handler, true);
  setTimeout(() => container.removeEventListener('click', handler, true), 350);
}

/**
 * Press-and-hold detection on a row, delegated from the container so it survives
 * innerHTML rebuilds. Used for "long-press the row to rename". A press that moves
 * past a small threshold (a scroll/drag) or releases early (a tap → select) is
 * not a long-press. On a genuine hold the trailing click is suppressed so the row
 * isn't also selected.
 *
 * @param {HTMLElement} container
 * @param {Object} opts
 * @param {string} opts.rowSelector       CSS selector for each row.
 * @param {string} [opts.ignoreSelector]  Presses starting on a match here are
 *        ignored (e.g. the drag handle and the action buttons, which have their
 *        own gestures).
 * @param {string} [opts.requireSelector] If set, the press must START on an
 *        element matching this selector (e.g. only the title, not the whole row).
 *        Presses landing elsewhere in the row are ignored.
 * @param {(rowId: string, rowEl: HTMLElement) => void} opts.onLongPress
 * @param {number} [opts.longPressMs]
 */
export function attachRowLongPress(container, opts) {
  if (!container || container.__longPressBound) return;
  container.__longPressBound = true;
  container.dataset.hasLongPress = 'true';  // so sendToAgent can skip this zone

  const {
    rowSelector,
    ignoreSelector,
    requireSelector,
    onLongPress,
    longPressMs = _LONG_PRESS_MS,
  } = opts || {};

  let timer = null;
  let row = null;
  let startX = 0;
  let startY = 0;

  function cancel() {
    if (timer) { clearTimeout(timer); timer = null; }
    row = null;
  }

  container.addEventListener('pointerdown', (e) => {
    // Left button / touch / pen only — ignore right- and middle-clicks.
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (ignoreSelector && e.target.closest(ignoreSelector)) return;
    if (requireSelector && !e.target.closest(requireSelector)) return;
    const r = e.target.closest(rowSelector);
    if (!r || !container.contains(r)) return;
    row = r;
    startX = e.clientX;
    startY = e.clientY;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      const target = row;
      row = null;
      if (!target) return;
      _suppressNextClick(container);
      if (typeof onLongPress === 'function') onLongPress(target.dataset.id, target);
    }, longPressMs);
  });

  container.addEventListener('pointermove', (e) => {
    if (!row) return;
    if (Math.abs(e.clientX - startX) > _LONG_PRESS_MOVE ||
        Math.abs(e.clientY - startY) > _LONG_PRESS_MOVE) {
      cancel();
    }
  }, { passive: true });

  container.addEventListener('pointerup', cancel);
  container.addEventListener('pointercancel', cancel);
}

/**
 * @param {HTMLElement} container  The scrollable menu element (rows live inside).
 * @param {Object} opts
 * @param {string} opts.rowSelector     CSS selector matching each reorderable row.
 * @param {string} opts.handleSelector  CSS selector for the drag trigger. Can be the
 *        row itself (for full-row drag) or a child handle element. When matching the
 *        row, plain taps still select; drags fire onReorder. Pass ignoreSelector to
 *        exclude interactive children from initiating a drag.
 * @param {string} [opts.ignoreSelector] CSS selector for interactive elements that
 *        should NOT initiate a drag (kebab, delete, input, etc.). Pointer events on
 *        these are ignored.
 * @param {HTMLElement} [opts.dropBefore] Fixed child that dragged rows must stay
 *        before (for example, a footer following a reorderable list).
 * @param {(orderedIds: string[]) => void} opts.onReorder  Called on drop with the
 *        new top-to-bottom list of row `data-id` values.
 * @param {(rowId: string) => void} [opts.onHandleLongPress]  Called when the grip
 *        is pressed and held still (no drag). Used for "long-press to pin".
 * @param {number} [opts.longPressMs]
 */
export function makeRowsReorderable(container, opts) {
  if (!container || container.__reorderBound) return;
  container.__reorderBound = true;
  container.dataset.hasLongPress = 'true';  // so sendToAgent can skip this zone

  const {
    rowSelector,
    handleSelector,
    onReorder,
    onHandleLongPress,
    ignoreSelector,
    dropBefore,
    longPressMs = _LONG_PRESS_MS,
  } = opts;
  let dragRow = null;     // the row being dragged
  let armed = false;      // true once movement passes the threshold
  let startY = 0;
  let scrollRAF = null;
  let lpTimer = null;     // long-press (pin) timer
  let lpFired = false;    // true once a long-press has pinned this press

  function rowIdList() {
    return Array.from(container.querySelectorAll(rowSelector))
      .map((r) => r.dataset.id)
      .filter(Boolean);
  }

  function autoScroll(clientY) {
    const rect = container.getBoundingClientRect();
    let dy = 0;
    if (clientY < rect.top + _EDGE) dy = -8;
    else if (clientY > rect.bottom - _EDGE) dy = 8;
    if (dy && container.scrollHeight > container.clientHeight) {
      container.scrollTop += dy;
    }
  }

  // Insert the dragged row at the position matching the pointer's Y.
  function reposition(clientY) {
    const rows = Array.from(container.querySelectorAll(rowSelector))
      .filter((r) => r !== dragRow);
    let inserted = false;
    for (const r of rows) {
      const rect = r.getBoundingClientRect();
      if (clientY < rect.top + rect.height / 2) {
        container.insertBefore(dragRow, r);
        inserted = true;
        break;
      }
    }
    if (!inserted) {
      if (dropBefore && container.contains(dropBefore)) container.insertBefore(dragRow, dropBefore);
      else container.appendChild(dragRow);
    }
  }

  function onMove(e) {
    if (!dragRow) return;
    const clientY = e.clientY;
    if (!armed) {
      if (Math.abs(clientY - startY) < _DRAG_THRESHOLD) return;
      armed = true;
      if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } // it's a drag, not a hold
      dragRow.classList.add('dragging');
    }
    e.preventDefault();          // stop touch-scrolling / text selection
    reposition(clientY);
    autoScroll(clientY);
  }

  function endDrag() {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onEnd);
    document.removeEventListener('pointercancel', onEnd);
    if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
    if (scrollRAF) cancelAnimationFrame(scrollRAF);
    if (dragRow) dragRow.classList.remove('dragging');
  }

  function onEnd() {
    const wasArmed = armed;
    const wasLongPress = lpFired;
    const row = dragRow;
    endDrag();
    dragRow = null;
    armed = false;
    lpFired = false;
    // The long-press already pinned + suppressed its own click — nothing else.
    if (wasLongPress) return;
    // If a drag actually happened, suppress the trailing click and fire the
    // reorder callback. For a plain tap (no drag), let the click through so it
    // can select/switch the row.
    if (wasArmed) {
      _suppressNextClick(container);
      if (typeof onReorder === 'function') {
        onReorder(rowIdList());
      }
    }
  }

  container.addEventListener('pointerdown', (e) => {
    const handle = e.target.closest(handleSelector);
    if (!handle || !container.contains(handle)) return;
    const row = handle.closest(rowSelector);
    if (!row) return;
    // If ignoreSelector is set, check the pointer didn't land on an interactive
    // element (kebab, delete, title input, etc.) — these have their own gestures.
    if (ignoreSelector && e.target.closest(ignoreSelector)) return;
    const isRowHandle = handle.matches(rowSelector);
    // Classic handle-based drag: steal the pointer event immediately so the
    // handle tap doesn't also trigger the row's click handler.
    // Full-row drag: let the click through naturally; suppress it only if the
    // user actually drags (see onEnd).
    if (!isRowHandle) {
      e.preventDefault();
      e.stopPropagation();
    }
    dragRow = row;
    armed = false;
    lpFired = false;
    startY = e.clientY;
    // Press-and-hold the grip (without dragging) → pin. Cancelled the moment a
    // drag arms (see onMove) or the press ends (endDrag).
    if (typeof onHandleLongPress === 'function') {
      if (lpTimer) clearTimeout(lpTimer);
      lpTimer = setTimeout(() => {
        lpTimer = null;
        if (armed || !dragRow) return;
        lpFired = true;
        const id = dragRow.dataset.id;
        dragRow = null;               // stop a later move from starting a drag
        _suppressNextClick(container);
        onHandleLongPress(id);
      }, longPressMs);
    }
    document.addEventListener('pointermove', onMove, { passive: false });
    document.addEventListener('pointerup', onEnd);
    document.addEventListener('pointercancel', onEnd);
  });
}
