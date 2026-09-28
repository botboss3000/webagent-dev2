'use strict';

// Chat control: stats — renders every stat the config enabled (token-bar /
// ctx / ctx-max / cost) side by side in the stats strip. Which stats appear is
// driven by chat_ui.json → controls.stats.visible (resolved by applyStatsConfig
// in chat-pill-config.js); each stat also hides itself at zero via its own
// renderer (chat-activity.js for the panel, chat-surface.js for the widget).
//
// The strip is an overflow-x:auto flex row, so there's no show/hide cycling or
// localStorage here — the chevrons scroll the strip when its content overflows
// (same behaviour as the widget's wireStatsCarousel in chat-surface.js).
//
// Targets: pill — init() called from chat-controls-config.js after pill layout.

let _stripEl = null;          // cached strip reference so helpers don't re-query

/** Toggle chevron visibility from real scroll overflow (the CSS edge-fade
 *  masks and chevron buttons key off the .visible class). */
function _updateChevrons() {
  if (!_stripEl) return;
  const chevLeft = document.querySelector('.chat-stats-chev.left');
  const chevRight = document.querySelector('.chat-stats-chev.right');
  const overflow = _stripEl.scrollWidth - _stripEl.clientWidth > 1;
  if (chevLeft) chevLeft.classList.toggle('visible', overflow && _stripEl.scrollLeft > 1);
  if (chevRight) chevRight.classList.toggle('visible', overflow && _stripEl.scrollLeft < _stripEl.scrollWidth - _stripEl.clientWidth - 1);
}

function _onChevLeft(e) {
  e.stopPropagation();  // don't open the context panel (click target is #chat-pill-stats)
  if (!_stripEl) return;
  _stripEl.scrollBy({ left: -Math.max(60, Math.floor(_stripEl.clientWidth * 0.5)), behavior: 'smooth' });
}

function _onChevRight(e) {
  e.stopPropagation();
  if (!_stripEl) return;
  _stripEl.scrollBy({ left: Math.max(60, Math.floor(_stripEl.clientWidth * 0.5)), behavior: 'smooth' });
}

export function init() {
  _stripEl = document.getElementById('chat-pill-stats-strip');
  if (!_stripEl) return;

  const chevLeft = document.querySelector('.chat-stats-chev.left');
  const chevRight = document.querySelector('.chat-stats-chev.right');
  if (chevLeft) chevLeft.addEventListener('click', _onChevLeft);
  if (chevRight) chevRight.addEventListener('click', _onChevRight);
  _stripEl.addEventListener('scroll', _updateChevrons, { passive: true });

  requestAnimationFrame(_updateChevrons);
  if (typeof ResizeObserver !== 'undefined') {
    let rp = false;
    const ro = new ResizeObserver(() => { if (!rp) { rp = true; requestAnimationFrame(() => { rp = false; _updateChevrons(); }); } });
    ro.observe(_stripEl);
    window.addEventListener('resize', _updateChevrons);
  }

  // Store cleanup reference
  _stripEl._statsCleanup = () => {
    if (chevLeft) chevLeft.removeEventListener('click', _onChevLeft);
    if (chevRight) chevRight.removeEventListener('click', _onChevRight);
    _stripEl.removeEventListener('scroll', _updateChevrons);
  };
}

export function destroy() {
  if (_stripEl && typeof _stripEl._statsCleanup === 'function') {
    _stripEl._statsCleanup();
    delete _stripEl._statsCleanup;
  }
  _stripEl = null;
}

export function style() { return ''; }
