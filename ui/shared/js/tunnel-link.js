'use strict';

/**
 * Tunnel Link — App Configuration → App Settings → Tunnel card.
 *
 * The WebAgent side of the tunnel.webagent.live feature. The admin enters the
 * hosted relay address and their Cloudflare tunnel address; this panel registers
 * the instance with the relay (keeping the address current) and, on demand, mints
 * a short-lived pairing code shown as a QR. A phone scans that QR at the relay to
 * link itself and then view this app.
 *
 * Talks to /admin/tunnel-link/* . Colours use design-system variables so the card
 * is correct in dark + light. Mirrors the structure of remote-access.js.
 */

import { apiPath } from './config.js';
import { isAdmin } from './left-login.js';
import { copyText } from './clipboard.js';

let _status = null;

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }

function _setStatus(msg, kind) {
  const el = _qs('ac-tl-status');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'ok' ? 'var(--success)' : kind === 'err' ? 'var(--danger)' : '';
}

async function _post(path, body) {
  const res = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

function _copy(text) {
  if (!text || text === '—') return;
  // copyText (shared/js/clipboard.js) handles insecure http://<ip> contexts
  // (e.g. a phone on the LAN) via an execCommand fallback — navigator.clipboard
  // is undefined there.
  copyText(text)
    .then(() => _setStatus('Copied', 'ok'))
    .catch(() => _setStatus('Copy failed — select the text manually', 'err'));
}

function _injectQr(containerId, svg) {
  const box = _qs(containerId);
  if (!box) return;
  if (!svg) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.innerHTML = svg;
  // QR must read as dark-on-light to scan; functional white plate in both themes.
  box.style.display = 'inline-block';
  box.style.background = '#ffffff';
  box.style.padding = '8px';
  box.style.borderRadius = '8px';
  const el = box.querySelector('svg');
  if (el) { el.style.width = '180px'; el.style.height = '180px'; el.style.display = 'block'; }
}

async function _load() {
  if (!isAdmin()) return;
  try {
    const res = await fetch(apiPath('/admin/tunnel-link/status?requesting_user_id=' + encodeURIComponent(_userId())));
    if (!res.ok) return; // 403 for non-admins, etc. — leave the card quiet
    _status = await res.json();
  } catch { return; }
  _render();
}

function _render() {
  if (!_status) return;
  const s = _status;

  if (_qs('ac-tl-relay') && document.activeElement !== _qs('ac-tl-relay')) _qs('ac-tl-relay').value = s.relay_base_url || '';
  if (_qs('ac-tl-url') && document.activeElement !== _qs('ac-tl-url')) _qs('ac-tl-url').value = s.tunnel_url || '';
  if (_qs('ac-tl-label') && document.activeElement !== _qs('ac-tl-label')) _qs('ac-tl-label').value = s.label || '';

  const badge = _qs('ac-tl-active-badge');
  if (badge) badge.textContent = s.registered ? 'linked' : 'not set up';

  const viewer = _qs('ac-tl-viewer');
  if (viewer) viewer.textContent = s.viewer_url || '—';

  // Pairing block is only meaningful once registered with an address.
  const ready = !!s.registered && !!s.tunnel_url;
  const pairBtn = _qs('ac-tl-pair');
  if (pairBtn) pairBtn.disabled = !ready;
  const pairHint = _qs('ac-tl-pair-hint');
  if (pairHint) {
    pairHint.textContent = ready
      ? 'Generate a code, then scan it with your phone at the address above.'
      : 'Save your relay and tunnel addresses first, then you can generate a pairing QR.';
  }
}

async function _save() {
  if (!isAdmin()) return;
  _setStatus('Saving…');
  try {
    _status = await _post('/admin/tunnel-link/save', {
      relay_base_url: (_qs('ac-tl-relay') || {}).value || '',
      tunnel_url: (_qs('ac-tl-url') || {}).value || '',
      label: (_qs('ac-tl-label') || {}).value || '',
    });
    _render();
    _setStatus(_status.registered ? 'Saved and linked to the relay.' : 'Saved.', 'ok');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _showPairing() {
  if (!isAdmin()) return;
  _setStatus('Generating pairing code…');
  try {
    const r = await _post('/admin/tunnel-link/pairing-code', {});
    const link = _qs('ac-tl-pair-link');
    if (link) link.textContent = r.link || '—';
    _injectQr('ac-tl-pair-qr', r.qr_svg);
    const exp = _qs('ac-tl-pair-expiry');
    if (exp) {
      const mins = r.ttl_seconds ? Math.round(r.ttl_seconds / 60) : 2;
      exp.textContent = 'This code is single-use and expires in about ' + mins +
        (mins === 1 ? ' minute' : ' minutes') + '. Generate a new one if it runs out.';
    }
    // If the relay produced no QR (qrcode lib missing), the link is still shown to copy.
    if (!r.qr_svg && link) _setStatus('Pairing link ready — copy it to your phone.', 'ok');
    else _setStatus('Scan the QR with your phone now.', 'ok');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _reset() {
  if (!isAdmin()) return;
  _setStatus('Forgetting relay registration…');
  try {
    _status = await _post('/admin/tunnel-link/reset', {});
    _injectQr('ac-tl-pair-qr', null);
    if (_qs('ac-tl-pair-link')) _qs('ac-tl-pair-link').textContent = '—';
    if (_qs('ac-tl-pair-expiry')) _qs('ac-tl-pair-expiry').textContent = '';
    _render();
    _setStatus('Forgotten. Save again to re-link.', 'ok');
  } catch (e) { _setStatus(e.message, 'err'); }
}

export function initTunnelLink() {
  _qs('ac-tl-save')?.addEventListener('click', _save);
  _qs('ac-tl-reset')?.addEventListener('click', _reset);
  _qs('ac-tl-pair')?.addEventListener('click', _showPairing);
  _qs('ac-tl-pair-copy')?.addEventListener('click', () => _copy((_qs('ac-tl-pair-link') || {}).textContent));
  _qs('ac-tl-viewer-copy')?.addEventListener('click', () => _copy((_qs('ac-tl-viewer') || {}).textContent));

  // Re-load whenever the App Settings section is shown (wired in nav.js).
  window.__refreshTunnelLink = _load;
}

/** Fetch tunnel-link status. Called when the App Settings view becomes visible
 *  (from applySidebarView in files.js). */
export function startTunnelLink() {
  _load();
}
