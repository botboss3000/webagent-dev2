'use strict';

/**
 * Chat Attachments — App Configuration → Data Management → Chat Attachments card.
 *
 * Drives the per-row attachment storage backend selector (local / browser /
 * s3 / gcs) by talking to /admin/storage/attachments/*. Each
 * attachment row records its own provider, so switching here only affects
 * NEW uploads; existing rows keep working.
 */

import { apiPath } from './config.js';
import { isAdmin } from './left-login.js';

/** Race fetch() against a timeout (default 20s) so badges don't hang on
 *  "loading…" forever when a request stalls. */
async function _fetchWithTimeout(url, opts, ms = 20000) {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { ...(opts || {}), signal: ctrl.signal });
  } finally {
    clearTimeout(id);
  }
}

let _statusCache = null;
let _renderedFor = null;

const PROVIDER_META = {
  local: {
    label: 'This server’s disk',
    blurb: 'Files saved to UPLOAD_DIR on the server (default uploads/), served via /uploads. They follow you across devices ONLY if every device reaches this same server; on a local-first, per-device install they stay on the machine that received them. For true cross-device sync, pick a cloud option below.',
    fields: [
      { key: 'upload_dir', label: 'Upload directory', placeholder: 'uploads',
        help: 'Optional absolute path. Leave blank to use the UPLOAD_DIR env var or default ./uploads.' },
    ],
  },
  browser: {
    label: 'This device only (browser storage)',
    blurb: 'Bytes stay on this one device via the browser (IndexedDB); the server records only the row metadata. Nothing leaves the device — but server-side tools (read_attachment, agent vision) cannot read the bytes, and files do NOT follow you to another device. Pick a cloud option below if you want your files on every device.',
    fields: [],
  },
  s3: {
    label: 'S3 / Cloudflare R2 — synced across your devices',
    blurb: 'Files live in a shared cloud bucket, so they follow you to every device. Works with AWS S3 or any S3-compatible store. RECOMMENDED FREE OPTION: Cloudflare R2 — 10 GB free and no download fees ever. Create an R2 bucket, then put your R2 endpoint (https://<account>.r2.cloudflarestorage.com) in Endpoint URL and its access key / secret in the environment. Requires boto3 installed.',
    fields: [
      { key: 'bucket', label: 'Bucket', placeholder: 'my-webagent-uploads', required: true },
      { key: 'region', label: 'Region', placeholder: 'us-east-1 (use "auto" for R2)' },
      { key: 'prefix', label: 'Key prefix', placeholder: 'attachments/' },
      { key: 'endpoint_url', label: 'Endpoint URL', placeholder: 'https://<account>.r2.cloudflarestorage.com (R2) — blank for AWS' },
    ],
  },
  gcs: {
    label: 'Google Cloud Storage — synced across your devices',
    blurb: 'Files live in a shared Google Cloud bucket, so they follow you to every device. Free tier: 5 GB (US regions). Requires google-cloud-storage installed and GOOGLE_APPLICATION_CREDENTIALS (or run inside GCP with default ADC).',
    fields: [
      { key: 'bucket', label: 'Bucket', placeholder: 'my-webagent-uploads', required: true },
      { key: 'prefix', label: 'Object prefix', placeholder: 'attachments/' },
    ],
  },
};

function _qs(id) { return document.getElementById(id); }
function _userId() {
  try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; }
}

function _setStatus(msg, kind) {
  const el = _qs('ac-attach-status');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'ok' ? 'var(--success)'
    : kind === 'err' ? 'var(--danger)'
    : '';
}

async function _load() {
  _setStatus('Loading…');
  try {
    const res = await _fetchWithTimeout(apiPath('/admin/storage/attachments/status?requesting_user_id=' + encodeURIComponent(_userId())));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    _statusCache = await res.json();
    try { window.__webagentAttachmentBackend = _statusCache.mode; } catch {}
  } catch (e) {
    _setStatus('Could not load: ' + e.message, 'err');
    return;
  }
  _setStatus('');
  _renderOverview();
  _renderProviderDetail(_renderedFor || _statusCache.mode);
}

function _renderOverview() {
  const sel = _qs('ac-attach-mode');
  if (!sel) return;
  sel.innerHTML = '';
  (_statusCache?.available || []).forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = (PROVIDER_META[m]?.label || m) + (m === _statusCache.mode ? ' (active)' : '');
    sel.appendChild(opt);
  });
  sel.value = _renderedFor || _statusCache?.mode || 'local';

  const badge = _qs('ac-attach-active-badge');
  if (badge) badge.textContent = PROVIDER_META[_statusCache?.mode]?.label || _statusCache?.mode || '—';

  const banner = _qs('ac-attach-env-banner');
  if (banner) banner.hidden = !_statusCache?.env_locked;

  const counts = _statusCache?.counts || {};
  const nonZero = Object.entries(counts).filter(([, n]) => n > 0);
  const countsEl = _qs('ac-attach-counts');
  if (countsEl) {
    if (nonZero.length > 1) {
      const lines = nonZero.map(([k, n]) => `${PROVIDER_META[k]?.label || k}: ${n}`).join(' · ');
      countsEl.textContent = 'Existing attachments across backends — ' + lines;
    } else {
      countsEl.textContent = '';
    }
  }

  const locked = !!_statusCache?.env_locked;
  ['ac-attach-save', 'ac-attach-save-cfg'].forEach(id => {
    const btn = _qs(id);
    if (btn) btn.disabled = locked;
  });
}

function _renderProviderDetail(provider) {
  _renderedFor = provider;
  const meta = PROVIDER_META[provider];
  const blurb = _qs('ac-attach-blurb');
  const fields = _qs('ac-attach-fields');
  const saveBtn = _qs('ac-attach-save-cfg');
  if (!meta || !blurb || !fields) return;
  blurb.textContent = meta.blurb;
  fields.innerHTML = '';
  const cfg = (_statusCache?.providers || {})[provider] || {};

  if (!meta.fields.length) {
    fields.innerHTML = '<div class="ac-hint" style="font-size:11px;">No additional configuration.</div>';
    if (saveBtn) saveBtn.disabled = true;
    return;
  }

  if (saveBtn) saveBtn.disabled = !!_statusCache?.env_locked;
  meta.fields.forEach(f => {
    const wrap = document.createElement('div');
    const lab = document.createElement('label');
    lab.className = 'ac-label';
    lab.textContent = f.label + (f.required ? ' *' : '');
    lab.setAttribute('for', `ac-attach-fld-${provider}-${f.key}`);
    wrap.appendChild(lab);

    let inp;
    if (f.type === 'checkbox') {
      inp = document.createElement('input');
      inp.type = 'checkbox';
      inp.checked = cfg[f.key] !== undefined ? !!cfg[f.key] : true;
      inp.dataset.kind = 'bool';
    } else {
      inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'ac-input';
      inp.placeholder = f.placeholder || '';
      inp.value = cfg[f.key] == null ? '' : String(cfg[f.key]);
    }
    inp.id = `ac-attach-fld-${provider}-${f.key}`;
    inp.dataset.provider = provider;
    inp.dataset.key = f.key;
    inp.disabled = !!_statusCache?.env_locked;
    wrap.appendChild(inp);

    if (f.help) {
      const hint = document.createElement('div');
      hint.className = 'ac-hint';
      hint.style.fontSize = '11px';
      hint.textContent = f.help;
      wrap.appendChild(hint);
    }
    fields.appendChild(wrap);
  });
}

async function _saveMode() {
  if (!isAdmin()) return;
  const sel = _qs('ac-attach-mode');
  if (!sel) return;
  const mode = sel.value;
  _setStatus('Saving…');
  try {
    const res = await fetch(apiPath('/admin/storage/attachments/mode'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _userId(), mode }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    _setStatus('Active backend → ' + (PROVIDER_META[mode]?.label || mode), 'ok');
    await _load();
    setTimeout(() => _setStatus(''), 3000);
  } catch (e) {
    _setStatus('Error: ' + e.message, 'err');
  }
}

async function _saveProviderConfig() {
  if (!isAdmin()) return;
  if (!_renderedFor) return;
  const provider = _renderedFor;
  const cfg = {};
  document.querySelectorAll(`#ac-attach-fields [data-provider="${provider}"]`).forEach(inp => {
    const key = inp.dataset.key;
    if (!key) return;
    if (inp.dataset.kind === 'bool') {
      cfg[key] = !!inp.checked;
    } else {
      const v = inp.value.trim();
      if (v) cfg[key] = v;
    }
  });
  _setStatus('Saving…');
  try {
    const res = await fetch(apiPath('/admin/storage/attachments/provider-config'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _userId(), provider, config: cfg }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    _setStatus('Saved.', 'ok');
    await _load();
    setTimeout(() => _setStatus(''), 3000);
  } catch (e) {
    _setStatus('Error: ' + e.message, 'err');
  }
}

async function _testBackend() {
  const sel = _qs('ac-attach-mode');
  if (!sel) return;
  const mode = sel.value;
  _setStatus('Testing ' + mode + '…');
  try {
    const res = await fetch(apiPath('/admin/storage/attachments/test'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _userId(), mode }),
    });
    const data = await res.json();
    if (data.ok) {
      const note = data.note ? ' — ' + data.note : (data.storage_path ? ' — wrote ' + data.storage_path : '');
      _setStatus('OK' + note, 'ok');
    } else {
      _setStatus('Failed: ' + (data.error || 'unknown'), 'err');
    }
  } catch (e) {
    _setStatus('Error: ' + e.message, 'err');
  }
}

export function initDataManagement() {
  const sel = _qs('ac-attach-mode');
  if (!sel) return;  // DOM not yet rendered — startDataManagement will re-call us
  if (sel && !sel.dataset.wired) {
    sel.dataset.wired = '1';
    sel.addEventListener('change', () => _renderProviderDetail(sel.value));
  }
  _qs('ac-attach-save')?.addEventListener('click', _saveMode);
  _qs('ac-attach-test')?.addEventListener('click', _testBackend);
  _qs('ac-attach-save-cfg')?.addEventListener('click', _saveProviderConfig);

  // Chain into the storage section's refresh hook so this card reloads
  // whenever the Data Management tab is activated.
  const prev = window.__refreshStorageSection;
  window.__refreshStorageSection = function () {
    if (typeof prev === 'function') { try { prev(); } catch {} }
    _load();
  };
  // Data fetching deferred to startDataManagement() — runs only when the
  // Chat Attachments card is visible.
}

/** Fetch attachment backend status. Called when the Data Management card
 *  becomes visible (from startAdminTools in files.js). */
export function startDataManagement() {
  // initDataManagement may have run before the Data Settings template was
  // rendered (from _ensureAdminInit which fires on first Admin Tools open,
  // before initAppConfig renders the template). If so, wiring was skipped
  // because _qs('ac-attach-mode') returned null. Try again now.
  const sel = _qs('ac-attach-mode');
  if (sel && !sel.dataset.wired) {
    initDataManagement();
  }
  _load();
}

// Public helper for attachments.js — sync lookup; cached and refreshed on
// every Chat Attachments tab load. Defaults to 'local' if unknown.
function getActiveAttachmentBackend() {
  if (_statusCache?.mode) return _statusCache.mode;
  try { return window.__webagentAttachmentBackend || 'local'; } catch { return 'local'; }
}
