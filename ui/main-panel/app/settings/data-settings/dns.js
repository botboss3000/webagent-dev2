'use strict';

/**
 * Domains — App Configuration → Data Settings → Domains card.
 *
 * The sibling of deploy.js: where Deployment CREATES a server, this POINTS a
 * domain at one. Talks to /admin/dns/* : lists the discovered DNS providers +
 * their connect forms, saves the (non-secret) settings + the API key into the
 * encrypted vault, tests the connection, lists the account's domains, and streams
 * the "point a domain at a server" action as NDJSON into a live log — then runs a
 * provider-independent resolution check.
 *
 * DNS providers are drop-ins (app/dns/providers/); the whole card renders from the
 * /catalog payload, so adding a provider needs NO change here. The keyless "Other
 * / manual" provider swaps the connect + domain-picker UI for a plain domain box +
 * an instruction card (it prints the exact record to set, then Verify confirms it).
 *
 * Colours use design-system variables so the card is correct in dark + light.
 * Mirrors the structure of deploy.js / ui/shared/js/remote-access.js.
 */

import { apiPath } from '../../../../shared/js/config.js';
import { authHeaders, isAdmin } from '../../../../shared/js/left-login.js?v=253';
import { _refreshLucideIcons } from '../../../../shared/js/dom-utils.js';

let _catalog = null;        // last /catalog payload
let _busy = false;          // a point stream is running

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _provider() { return (_qs('ac-dns-provider') || {}).value || ''; }
function _current() { return (_catalog && (_catalog.providers || []).find(p => p.id === _provider())) || null; }

// ── Fetch helpers (mirror deploy.js) ─────────────────────────────────────────
async function _post(path, body) {
  const res = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

function _setStatus(elId, msg, kind) {
  const el = _qs(elId);
  if (!el) return;
  el.textContent = msg || '';
  el.classList.remove('ac-fg-success','ac-fg-danger'); if (kind === 'ok') el.classList.add('ac-fg-success'); else if (kind === 'err') el.classList.add('ac-fg-danger');
}

function _logLine(text, level) {
  const log = _qs('ac-dns-log');
  if (!log) return;
  log.hidden = false;
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ ' : level === 'warn' ? '! ' : '· ';
  log.textContent += mark + text + '\n';
  log.scrollTop = log.scrollHeight;
}

// NDJSON stream reader — same shape as deploy.js `_stream`.
async function _stream(path, body, onDone) {
  const res = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  const ctype = res.headers.get('content-type') || '';
  if (!ctype.includes('ndjson') || !res.body || !res.body.getReader) {
    const obj = await res.json();
    onDone(obj.result || obj);
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const drain = (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.phase === 'done') onDone(ev.result || {});
      else if (ev.message) _logLine(ev.message, ev.level);
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    drain(dec.decode(value, { stream: true }));
  }
  drain(dec.decode());
}

// ── Field builder (a trimmed twin of deploy.js `_buildField`) ────────────────
function _fieldLink(link) {
  const a = document.createElement('a');
  a.href = link.url; a.target = '_blank'; a.rel = 'noopener';
  a.className = 'ac-field-link ac-dns-field-link';
  a.textContent = link.label || link.url;
  return a;
}

function _tipBadge(tip) {
  if (!tip) return null;
  const text = typeof tip === 'string' ? tip : (tip.text || '');
  if (!text) return null;
  const b = document.createElement('span');
  b.textContent = 'ⓘ';
  b.title = text;
  b.className = 'ac-dns-tip-badge';
  return b;
}

// `showTip` — when true, the field's `tip` is spelled out as a visible help
// line under the input (the "how to get this" instructions), instead of hiding
// behind the hover ⓘ badge. dns.js turns this on only while the provider isn't
// connected yet, so setup reads as step-by-step and collapses once it's done.
function _buildField(f, value, kind, isSet, showTip) {
  const wrap = document.createElement('div');
  if (f.type === 'textarea' || f.full) wrap.classList.add('ac-dns-field-full');

  if (f.type !== 'checkbox') {
    const lab = document.createElement('label');
    lab.className = 'ac-label';
    lab.textContent = f.label + (f.required ? ' *' : '');
    // Keep the hover badge only when we're NOT showing the tip inline.
    if (!showTip) {
      const tip = _tipBadge(f.tip);
      if (tip) lab.appendChild(tip);
    }
    wrap.appendChild(lab);
    if (f.link && f.link.url) wrap.appendChild(_fieldLink(f.link));
  }

  let inp;
  if (f.type === 'select') {
    inp = document.createElement('select');
    inp.className = 'ac-input';
    (f.options || []).forEach(o => {
      const opt = document.createElement('option');
      if (typeof o === 'string') { opt.value = o; opt.textContent = o; }
      else { opt.value = o.value; opt.textContent = o.label || o.value; }
      inp.appendChild(opt);
    });
    inp.value = (value != null ? value : (f.default != null ? f.default : ''));
  } else if (f.type === 'checkbox') {
    const row = document.createElement('label');
    row.style.cssText = 'display:flex;align-items:center;gap:8px;';
    inp = document.createElement('input');
    inp.type = 'checkbox';
    inp.checked = (value != null ? !!value : !!f.default);
    inp.dataset.type = 'checkbox';
    row.appendChild(inp);
    const span = document.createElement('span');
    span.className = 'ac-label'; span.style.margin = '0'; span.textContent = f.label;
    row.appendChild(span);
    wrap.appendChild(row);
  } else if (f.type === 'textarea') {
    inp = document.createElement('textarea');
    inp.className = 'ac-input';
    inp.rows = 4;
    inp.value = value != null ? value : '';
    if (isSet && !inp.value) inp.placeholder = '•••••••• saved — leave blank to keep it';
    else if (f.placeholder) inp.placeholder = f.placeholder;
  } else {
    inp = document.createElement('input');
    inp.type = f.type === 'password' ? 'password' : (f.type === 'number' ? 'number' : 'text');
    inp.className = 'ac-input';
    inp.autocomplete = 'off';
    inp.spellcheck = false;
    inp.value = value != null ? value : '';
    if (f.secret && isSet && !inp.value) inp.placeholder = '•••••••• saved — leave blank to keep it';
    else if (f.placeholder) inp.placeholder = f.placeholder;
  }

  if (inp) {
    inp.dataset.key = f.key;
    inp.dataset.kind = kind;
    if (f.type !== 'checkbox') wrap.appendChild(inp);
  }

  // Inline "how to get this" instructions, shown while setting the provider up.
  if (showTip) {
    const tipText = typeof f.tip === 'string' ? f.tip : (f.tip && f.tip.text) || '';
    if (tipText) {
      const help = document.createElement('div');
      help.className = 'ac-field-help';
      help.textContent = tipText;
      wrap.appendChild(help);
    }
  }
  return wrap;
}

function _gather(kind) {
  const out = {};
  document.querySelectorAll(`#ac-dns-main-row [data-kind="${kind}"]`).forEach(el => {
    const key = el.dataset.key;
    if (!key) return;
    out[key] = el.dataset.type === 'checkbox' ? !!el.checked : el.value;
  });
  return out;
}

// ── Load + render ────────────────────────────────────────────────────────────
async function _load() {
  if (!isAdmin()) return;
  try {
    const res = await fetch(apiPath('/admin/dns/catalog?requesting_user_id=' + encodeURIComponent(_userId())), { headers: { ...authHeaders() } });
    if (!res.ok) return;          // 403 for non-admins — leave the card quiet
    _catalog = await res.json();
  } catch { return; }
  _renderAll();
}

function _renderAll() {
  if (!_catalog) return;
  const sel = _qs('ac-dns-provider');
  if (!sel) return;
  const providers = _catalog.providers || [];
  const keep = sel.value || _catalog.active_provider || (providers[0] && providers[0].id) || '';
  sel.innerHTML = '';
  providers.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.display_name + (p.available ? '' : ' (unavailable)');
    sel.appendChild(opt);
  });
  sel.value = providers.some(p => p.id === keep) ? keep : (providers[0] && providers[0].id) || '';
  _renderTargets();
  _renderProvider();
  _renderBadge();
}

function _renderBadge() {
  const badge = _qs('ac-dns-badge');
  if (!badge) return;
  // Show the most recently pointed domain across providers, if any.
  let best = null;
  (_catalog.providers || []).forEach(p => {
    const pt = p.pointing || {};
    if (pt.host && (!best || (pt.updated_at || 0) > (best.updated_at || 0))) best = pt;
  });
  if (best) badge.textContent = best.host + (best.verified ? ' ✓' : ' · pending');
  else badge.textContent = 'Point a domain';
}

function _renderTargets() {
  const sel = _qs('ac-dns-target');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  const targets = (_catalog && _catalog.targets) || [];
  targets.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.ip;
    opt.textContent = `${t.label} — ${t.ip}`;
    sel.appendChild(opt);
  });
  const custom = document.createElement('option');
  custom.value = '__custom__';
  custom.textContent = targets.length ? 'Type an IP address…' : 'Type the server IP address…';
  sel.appendChild(custom);
  sel.value = targets.some(t => t.ip === prev) ? prev : (targets[0] ? targets[0].ip : '__custom__');
  _syncTargetBox();
}

function _syncTargetBox() {
  const sel = _qs('ac-dns-target');
  const box = _qs('ac-dns-ip');
  if (!sel || !box) return;
  box.hidden = sel.value !== '__custom__';
}

function _renderProvider() {
  const p = _current();
  const summary = _qs('ac-dns-summary');
  const reqs = _qs('ac-dns-requires');
  const cfgHost = _qs('ac-dns-config');
  const credHost = _qs('ac-dns-creds');
  if (!p || !cfgHost || !credHost) return;

  // The three states that drive the whole card's shape:
  //   • manual    — keyless "Other / manual"; no credentials, point form always on.
  //   • connected — an API provider whose key is saved (`configured`).
  //   • setup     — an API provider not connected yet → show instructions, hide
  //                 the point form so the card is one clear step at a time.
  const manual = !!p.manual;
  const connected = !!p.configured;
  const setup = !manual && !connected;

  if (summary) summary.textContent = p.available ? (p.summary || '') : (p.unavailable_reason || 'Unavailable here.');
  if (reqs) {
    reqs.innerHTML = '';
    // Prerequisites are guidance for getting set up — hide them once connected.
    (setup || manual ? (p.requires || []) : []).forEach(r => {
      const li = document.createElement('li'); li.textContent = r; reqs.appendChild(li);
    });
    reqs.style.display = reqs.children.length ? '' : 'none';
  }

  // Lead-in shown only during setup: how to proceed, plus the no-API fallback.
  const guideNote = _qs('ac-dns-guide-note');
  if (guideNote) {
    if (setup) {
      guideNote.textContent = `Follow the steps under each field to get your ${p.display_name} details, then Connect. `
        + `No API access? Pick “Other / manual” above to set the DNS record yourself.`;
      guideNote.hidden = false;
    } else {
      guideNote.hidden = true;
    }
  }

  // Connect block: hidden for the keyless manual provider.
  const connectBlock = _qs('ac-dns-connect-block');
  if (connectBlock) connectBlock.style.display = manual ? 'none' : '';

  cfgHost.innerHTML = '';
  credHost.innerHTML = '';
  // During setup, spell out each field's "how to get this" tip inline.
  (p.config_fields || []).forEach(f => cfgHost.appendChild(_buildField(f, (p.config || {})[f.key], 'cfg', false, setup)));
  (p.credential_fields || []).forEach(f => {
    const isSet = !!(p.credentials_set || {})[f.key];
    credHost.appendChild(_buildField(f, '', 'cred', isSet, setup));
  });

  // The point-a-domain form (STEP 2) only exists once we CAN point: an API
  // provider that's connected, or the keyless manual provider. Otherwise show a
  // short locked hint in its place. This is the "hide it until connected" rule.
  const pointBlock = _qs('ac-dns-point-block');
  const lockedHint = _qs('ac-dns-locked-hint');
  if (pointBlock) pointBlock.style.display = setup ? 'none' : '';
  if (lockedHint) lockedHint.hidden = !setup;

  // Disconnect button appears once a key is stored.
  const disc = _qs('ac-dns-disconnect');
  if (disc) disc.hidden = p.manual || !p.configured;

  // Cloudflare proxy toggle only applies to Cloudflare.
  const proxWrap = _qs('ac-dns-proxied-wrap');
  if (proxWrap) proxWrap.style.display = (p.id === 'cloudflare') ? 'flex' : 'none';

  // Domain input mode: API providers use the <select> picker; manual uses the box.
  const zoneWrap = _qs('ac-dns-zone-wrap');
  const domainBox = _qs('ac-dns-domain');
  if (zoneWrap) zoneWrap.style.display = p.manual ? 'none' : 'flex';
  if (domainBox) domainBox.hidden = !p.manual;

  // Reflect the last-pointed record for this provider (if any).
  const pt = p.pointing || {};
  if (pt.host) {
    if (p.manual && domainBox && !domainBox.value) domainBox.value = pt.domain || pt.host || '';
    const hostBox = _qs('ac-dns-host');
    if (hostBox && !hostBox.value && pt.host && pt.domain && pt.host !== pt.domain) {
      hostBox.value = pt.host.replace('.' + pt.domain, '');
    }
    _showResult(pt.verified
      ? `${pt.host} points at ${pt.ip}. HTTPS issues automatically once your server has the domain configured.`
      : `${pt.host} was set to ${pt.ip} — use Verify to check it has propagated.`, pt.verified ? 'ok' : '');
  } else {
    _showResult('', '');
  }

  // For an already-connected API provider, load its domains straight away.
  if (!p.manual && p.configured) _loadZones();
  else _clearZones();

  _refreshLucideIcons();
}

function _clearZones() {
  const sel = _qs('ac-dns-zone');
  if (sel) sel.innerHTML = '<option value="">Connect your account to list your domains…</option>';
}

function _showResult(msg, kind) {
  const el = _qs('ac-dns-result');
  if (!el) return;
  el.hidden = !msg;
  el.textContent = msg || '';
  el.style.color = kind === 'ok' ? 'var(--success)' : kind === 'err' ? 'var(--danger)' : '';
}

// ── Actions ──────────────────────────────────────────────────────────────────
async function _onProviderChange() {
  _renderProvider();
  _renderBadge();
  if (!isAdmin()) return;
  try { await _post('/admin/dns/select', { provider: _provider() }); } catch {}
}

async function _connect() {
  if (!isAdmin()) return;
  _setStatus('ac-dns-connect-status', 'Saving…');
  try {
    const values = { ..._gather('cfg'), ..._gather('cred') };
    const res = await _post('/admin/dns/connect', { provider: _provider(), values });
    _setStatus('ac-dns-connect-status', 'Connected.', 'ok');
    // Refresh the catalog so `configured`/`credentials_set` update, then list domains.
    await _load();
    await _loadZones();
  } catch (e) {
    _setStatus('ac-dns-connect-status', e.message || 'Could not save.', 'err');
  }
}

async function _test() {
  if (!isAdmin()) return;
  _setStatus('ac-dns-connect-status', 'Testing…');
  try {
    // Save whatever's typed first so the test uses the current values.
    const values = { ..._gather('cfg'), ..._gather('cred') };
    await _post('/admin/dns/connect', { provider: _provider(), values });
    const res = await _post('/admin/dns/test', { provider: _provider() });
    _setStatus('ac-dns-connect-status', res.detail || (res.ok ? 'OK' : 'Failed'), res.ok ? 'ok' : 'err');
    if (res.ok) { await _load(); await _loadZones(); }
  } catch (e) {
    _setStatus('ac-dns-connect-status', e.message || 'Test failed.', 'err');
  }
}

async function _disconnect() {
  if (!isAdmin()) return;
  try {
    await _post('/admin/dns/disconnect', { provider: _provider() });
    _setStatus('ac-dns-connect-status', 'Disconnected.', 'ok');
    await _load();
  } catch (e) {
    _setStatus('ac-dns-connect-status', e.message || 'Failed.', 'err');
  }
}

async function _loadZones() {
  const p = _current();
  if (!p || p.manual) return;
  const sel = _qs('ac-dns-zone');
  if (!sel) return;
  sel.innerHTML = '<option value="">Loading your domains…</option>';
  try {
    const res = await _post('/admin/dns/zones', { provider: _provider() });
    if (!res.ok) { sel.innerHTML = `<option value="">${res.detail || 'Could not list domains'}</option>`; return; }
    const zones = res.zones || [];
    if (!zones.length) { sel.innerHTML = '<option value="">No domains found on this account</option>'; return; }
    sel.innerHTML = '';
    zones.forEach(z => {
      const opt = document.createElement('option');
      opt.value = z.name;
      opt.dataset.id = z.id != null ? z.id : z.name;
      opt.textContent = z.name;
      sel.appendChild(opt);
    });
  } catch (e) {
    sel.innerHTML = `<option value="">${e.message || 'Could not list domains'}</option>`;
  }
}

function _pointParams() {
  const p = _current();
  const zoneSel = _qs('ac-dns-zone');
  const domainBox = _qs('ac-dns-domain');
  const hostBox = _qs('ac-dns-host');
  const targetSel = _qs('ac-dns-target');
  const ipBox = _qs('ac-dns-ip');

  let zoneName = '', zoneId = '';
  if (p && p.manual) {
    zoneName = (domainBox.value || '').trim();
  } else {
    zoneName = (zoneSel.value || '').trim();
    const opt = zoneSel.selectedOptions[0];
    zoneId = opt ? (opt.dataset.id || zoneName) : zoneName;
  }
  const sub = (hostBox.value || '').trim();
  // Full host: subdomain + zone, or the bare zone when no subdomain given.
  const host = sub ? (sub.includes('.') && sub.endsWith(zoneName) ? sub : `${sub}.${zoneName}`) : zoneName;
  const ip = (targetSel.value === '__custom__') ? (ipBox.value || '').trim() : targetSel.value;

  return {
    provider: _provider(),
    zone_id: zoneId, zone_name: zoneName, host, ip,
    ttl: parseInt(_qs('ac-dns-ttl').value, 10) || 300,
    proxied: !!(_qs('ac-dns-proxied') || {}).checked,
    also_www: !!(_qs('ac-dns-www') || {}).checked,
  };
}

async function _point() {
  if (!isAdmin() || _busy) return;
  const params = _pointParams();
  if (!params.zone_name) { _setStatus('ac-dns-status', 'Choose or enter a domain first.', 'err'); return; }
  if (!params.ip) { _setStatus('ac-dns-status', 'Choose the server (or type its IP) first.', 'err'); return; }

  _busy = true;
  const btn = _qs('ac-dns-point');
  if (btn) btn.disabled = true;
  const log = _qs('ac-dns-log');
  if (log) { log.hidden = false; log.textContent = ''; }
  _setStatus('ac-dns-status', 'Pointing…');
  _showResult('', '');
  try {
    await _stream('/admin/dns/point', params, (result) => {
      if (result.ok) {
        _setStatus('ac-dns-status', result.verified ? 'Pointed ✓' : 'Record set', 'ok');
        _showResult(result.message || 'Done.', result.verified ? 'ok' : '');
      } else {
        _setStatus('ac-dns-status', 'Failed', 'err');
        _showResult(result.message || 'Could not point the domain.', 'err');
      }
    });
    await _load();          // refresh badge + stored pointing
  } catch (e) {
    _setStatus('ac-dns-status', e.message || 'Failed', 'err');
  } finally {
    _busy = false;
    if (btn) btn.disabled = false;
  }
}

async function _verify() {
  if (!isAdmin()) return;
  const params = _pointParams();
  _setStatus('ac-dns-status', 'Checking…');
  try {
    const res = await _post('/admin/dns/verify', { provider: _provider(), host: params.host, ip: params.ip });
    _setStatus('ac-dns-status', res.matches ? 'Resolves ✓' : 'Not yet', res.matches ? 'ok' : '');
    _showResult(res.detail || '', res.matches ? 'ok' : '');
    await _load();
  } catch (e) {
    _setStatus('ac-dns-status', e.message || 'Check failed', 'err');
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────
export function initDns() {
  // The standalone #ac-dns-card wrapper was removed when this row moved inside
  // the New-deployment group; guard on the row id that survives the move.
  const card = _qs('ac-dns-main-row');
  if (!card) return;

  _qs('ac-dns-provider')?.addEventListener('change', _onProviderChange);
  _qs('ac-dns-connect')?.addEventListener('click', _connect);
  _qs('ac-dns-test')?.addEventListener('click', _test);
  _qs('ac-dns-disconnect')?.addEventListener('click', _disconnect);
  _qs('ac-dns-zone-refresh')?.addEventListener('click', _loadZones);
  _qs('ac-dns-target')?.addEventListener('change', _syncTargetBox);
  _qs('ac-dns-point')?.addEventListener('click', _point);
  _qs('ac-dns-verify')?.addEventListener('click', _verify);

  _load();
  // Re-fetch the catalog whenever the section is shown (nav.js calls this).
  window.__refreshDns = _load;
}
