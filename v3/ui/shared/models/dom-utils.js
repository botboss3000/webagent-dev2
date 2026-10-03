'use strict';
// Reference save-feedback functions only; unrelated v2 chat imports are not migrated.
export function _saveOverlay(container) {
  if (!container) return null;
  if (container._saveOv && container.contains(container._saveOv)) return container._saveOv;
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
  const ov = document.createElement('span');
  ov.className = 'ac-save-overlay';
  ov.setAttribute('aria-hidden', 'true');
  container.appendChild(ov);
  container._saveOv = ov;
  return ov;
}

// Minimum time the spinner stays visible, so even an instant save/failure shows
// a perceptible spinner → check/triangle sequence rather than a blink.
const _MIN_SPIN_MS = 400;

/** Show the in-flight spinner OVER `container` (cancels any prior flash/timer). */
export function _markSaving(container) {
  const ov = _saveOverlay(container);
  if (!ov) return;
  if (ov._fade) { clearTimeout(ov._fade); ov._fade = null; }
  if (ov._delay) { clearTimeout(ov._delay); ov._delay = null; }
  ov._savingAt = Date.now();
  ov.classList.remove('ok', 'err');
  ov.classList.add('show');
  ov.title = 'Saving…';
  ov.innerHTML = '<span class="agents-spinner"></span>';
}

/** Settle the overlay OVER `container` to a green check ✓ (ok) or an orange
 *  warning triangle ⚠ (fail), hold, then fade away. `errMsg` is the failure
 *  tooltip. The CALLER reverts the control's value when `ok` is false. */
export function _flashSaveCheck(container, ok, errMsg) {
  const ov = _saveOverlay(container);
  if (!ov) return;
  // Hold the spinner for at least _MIN_SPIN_MS so it's always seen before the
  // check/triangle, even if the save resolved/failed instantly.
  const since = ov._savingAt ? (Date.now() - ov._savingAt) : _MIN_SPIN_MS;
  if (since < _MIN_SPIN_MS) {
    if (ov._delay) clearTimeout(ov._delay);
    ov._delay = setTimeout(() => { ov._savingAt = 0; _flashSaveCheck(container, ok, errMsg); }, _MIN_SPIN_MS - since);
    return;
  }
  if (ov._fade) { clearTimeout(ov._fade); ov._fade = null; }
  ov._savingAt = 0;
  ov.classList.add('show');
  ov.innerHTML = '';
  _clearSavePop(ov);
  if (ok) {
    ov.classList.remove('err'); ov.classList.add('ok');
    ov.textContent = '✓';       // ✓ check
    ov.title = 'Saved';
    ov._fade = setTimeout(() => { ov.classList.remove('show', 'ok'); ov.textContent = ''; ov.title = ''; }, 1600);
  } else {
    ov.classList.remove('ok'); ov.classList.add('err');
    ov.textContent = '⚠';       // ⚠ warning triangle
    ov.title = errMsg || 'Save failed — reverted';
    // Small 1–2 word popup beside the triangle so the failure is unmissable.
    _showSavePop(ov, _shortFailLabel(errMsg));
    ov._fade = setTimeout(() => { ov.classList.remove('show', 'err'); ov.textContent = ''; ov.title = ''; _clearSavePop(ov); }, 2600);
  }
}

/** Condense a failure message to a 1–2 word popup label. */
function _shortFailLabel(errMsg) {
  if (!errMsg) return 'Not saved';
  const m = String(errMsg).toLowerCase();
  if (m.includes('clear')) return 'Not cleared';
  return 'Not saved';
}

/** Pop a tiny warning label next to the overlay (auto-removed by _clearSavePop). */
function _showSavePop(ov, text) {
  _clearSavePop(ov);
  const pop = document.createElement('span');
  pop.className = 'ac-save-pop';
  pop.setAttribute('aria-hidden', 'true');
  pop.textContent = text;
  ov.appendChild(pop);
  ov._pop = pop;
  // Trigger the entrance transition on the next frame.
  requestAnimationFrame(() => { if (ov._pop === pop) pop.classList.add('show'); });
}

/** Remove any popup label attached to the overlay. */
function _clearSavePop(ov) {
  if (ov && ov._pop) {
    if (ov._pop.parentNode) ov._pop.parentNode.removeChild(ov._pop);
    ov._pop = null;
  }
}

