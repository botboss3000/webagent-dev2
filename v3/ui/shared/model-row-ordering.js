'use strict';
// Adapted from v2_ref ordering.js: pointer ordering only, with explicit teardown.
// No v2 authentication, pin state, or persistence endpoints are imported.
const _EDGE=32, _DRAG_THRESHOLD=5, _LONG_PRESS_MS=500;
function _suppressNextClick(container) {
  const handler=event=>{event.preventDefault();event.stopPropagation();container.removeEventListener('click',handler,true);};
  container.addEventListener('click',handler,true);
  setTimeout(()=>container.removeEventListener('click',handler,true),350);
}
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

  const onDown = (e) => {
    if (typeof opts.canReorder === 'function' && !opts.canReorder()) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
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
  };
  container.addEventListener('pointerdown', onDown);
  return () => { endDrag(); container.removeEventListener('pointerdown', onDown); delete container.__reorderBound; delete container.dataset.hasLongPress; };
}
