'use strict';

/**
 * QR Popup — a shared, compact draggable QR-code popover used by Instances
 * Overview, Remote Access, and New Deployment. One popup at a time.
 *
 * Breadcrumb: ui/shared/js/qr-popup.js
 *   CSS: ui/shared/css/app3.css (.qr-pop)
 *   Callers: ui/admin-tools/instances/instances.js,
 *            ui/shared/js/remote-access.js,
 *            ui/admin-tools/instances/app-config/data-settings/deploy.js
 */

import { _refreshLucideIcons } from './dom-utils.js';
import { copyText } from './clipboard.js';

// ── Module state (one popup at a time) ──────────────────────────────────────
let _panel = null;
let _anchor = null;
let _dragging = false;
let _dragStartX = 0, _dragStartY = 0;
let _panelStartLeft = 0, _panelStartTop = 0;
const _DRAG_THRESHOLD = 3;

// ── Viewport-fit positioning ────────────────────────────────────────────────

function _place(panel, anchor) {
  const a = anchor.getBoundingClientRect();
  const pw = panel.offsetWidth, ph = panel.offsetHeight;
  const gap = 4, margin = 6;
  const vw = window.innerWidth, vh = window.innerHeight;

  // Try below the anchor, right-aligned; flip above if it would clip.
  let left = a.right - pw;
  let top = a.bottom + gap;
  if (top + ph > vh - margin) top = a.top - ph - gap;
  // Clamp so the entire panel is always inside the viewport.
  left = Math.max(margin, Math.min(left, vw - pw - margin));
  top = Math.max(margin, Math.min(top, vh - ph - margin));
  panel.style.left = Math.round(left) + 'px';
  panel.style.top = Math.round(top) + 'px';
}

// ── Drag (toolbar only) ─────────────────────────────────────────────────────

function _onPointerDown(e) {
  if (e.button !== 0) return;
  _dragging = false;
  _dragStartX = e.clientX;
  _dragStartY = e.clientY;
  const r = _panel.getBoundingClientRect();
  _panelStartLeft = r.left;
  _panelStartTop = r.top;
  _panel.setPointerCapture(e.pointerId);
  _panel.addEventListener('pointermove', _onPointerMove);
  _panel.addEventListener('pointerup', _onPointerUp);
  _panel.addEventListener('pointercancel', _onPointerUp);
}
function _onPointerMove(e) {
  const dx = e.clientX - _dragStartX, dy = e.clientY - _dragStartY;
  if (!_dragging && (Math.abs(dx) > _DRAG_THRESHOLD || Math.abs(dy) > _DRAG_THRESHOLD)) {
    _dragging = true;
    _panel.classList.add('qr-pop-dragging');
  }
  if (_dragging) {
    _panel.style.left = Math.round(_panelStartLeft + dx) + 'px';
    _panel.style.top = Math.round(_panelStartTop + dy) + 'px';
  }
}
function _onPointerUp(e) {
  _panel.removeEventListener('pointermove', _onPointerMove);
  _panel.removeEventListener('pointerup', _onPointerUp);
  _panel.removeEventListener('pointercancel', _onPointerUp);
  _panel.releasePointerCapture(e.pointerId);
  _panel.classList.remove('qr-pop-dragging');
}

// ── Public API ──────────────────────────────────────────────────────────────

/**
 * @param {Object} opts
 * @param {string} [opts.url]    - text to encode in the QR
 * @param {string} [opts.svg]    - server-rendered SVG (takes precedence over url)
 * @param {Element} opts.anchor  - the button / element to position next to
 * @param {string} [opts.label]  - override the URL label below the QR
 * @param {string} [opts.loading] - HTML to show in the plate while content loads
 * @param {string} [opts.className] - extra CSS class(es) on the panel
 * @returns {{ panel: Element, setPlate: (html:string) => void }}
 */
export function showQrPopup({ url, svg, anchor, label, loading, className }) {
  closeQrPopup();

  const panel = document.createElement('div');
  panel.className = 'qr-pop' + (className ? ' ' + className : '');

  // ── Toolbar (draggable handle) ──
  const toolbar = document.createElement('div');
  toolbar.className = 'qr-pop-toolbar';

  const closeBtn = document.createElement('button');
  closeBtn.className = 'qr-pop-close';
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'Close QR popup');
  closeBtn.innerHTML = '<i data-lucide="x"></i>';
  closeBtn.addEventListener('click', closeQrPopup);
  toolbar.appendChild(closeBtn);
  panel.appendChild(toolbar);

  // ── White plate (QR image / SVG / loading) ──
  const plate = document.createElement('div');
  plate.className = 'qr-pop-plate';
  if (svg) {
    plate.innerHTML = svg;
    const el = plate.querySelector('svg');
    if (el) { el.style.width = '100%'; el.style.height = 'auto'; el.style.display = 'block'; }
  } else if (loading) {
    plate.innerHTML = loading;
  } else if (url) {
    const img = document.createElement('img');
    img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=160x160&data=' + encodeURIComponent(url);
    img.alt = 'QR Code';
    img.loading = 'lazy';
    img.draggable = false;
    img.style.display = 'block';
    img.style.width = '100%';
    img.style.height = 'auto';
    plate.appendChild(img);
  }
  panel.appendChild(plate);

  // ── URL label (truncated, click to copy) ──
  const labelText = label || url || '';
  const urlLabel = document.createElement('div');
  urlLabel.className = 'qr-pop-url';
  urlLabel.textContent = labelText;
  if (labelText) {
    urlLabel.title = labelText + ' — click to copy';
    urlLabel.addEventListener('click', (e) => {
      e.stopPropagation();
      copyText(labelText).then(() => {
        const orig = urlLabel.textContent;
        urlLabel.textContent = 'Copied!';
        urlLabel.classList.add('qr-pop-copied');
        setTimeout(() => {
          urlLabel.textContent = orig;
          urlLabel.classList.remove('qr-pop-copied');
        }, 1200);
      }).catch(() => {});
    });
  }
  panel.appendChild(urlLabel);

  document.body.appendChild(panel);
  _panel = panel;
  _anchor = anchor;

  _refreshLucideIcons(panel);
  _place(panel, anchor);

  // Drag from toolbar and QR plate
  toolbar.addEventListener('pointerdown', _onPointerDown);
  plate.addEventListener('pointerdown', _onPointerDown);

  // Dismissal
  const onDocClick = (ev) => {
    if (panel.contains(ev.target) || anchor.contains(ev.target)) return;
    closeQrPopup();
  };
  const onKey = (ev) => { if (ev.key === 'Escape') closeQrPopup(); };
  const onReflow = () => { if (_panel && !_dragging) _place(_panel, _anchor); };
  document.addEventListener('mousedown', onDocClick, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onReflow, true);
  window.addEventListener('scroll', onReflow, true);
  panel._onDocClick = onDocClick;
  panel._onKey = onKey;
  panel._onReflow = onReflow;

  return {
    panel,
    setPlate(html) {
      plate.innerHTML = html;
      const el = plate.querySelector('svg');
      if (el) { el.style.width = '100%'; el.style.height = 'auto'; el.style.display = 'block'; }
    },
  };
}

export function closeQrPopup() {
  if (!_panel) return;
  document.removeEventListener('mousedown', _panel._onDocClick, true);
  document.removeEventListener('keydown', _panel._onKey, true);
  window.removeEventListener('resize', _panel._onReflow, true);
  window.removeEventListener('scroll', _panel._onReflow, true);
  _panel.remove();
  _panel = null;
  _anchor = null;
  _dragging = false;
}
