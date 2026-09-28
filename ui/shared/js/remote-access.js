'use strict';

/**
 * Remote Access — App Configuration → Data Settings → Remote Access card
 *   (the card sits directly below the Database card; moved out of App Settings).
 *
 * Lets an admin reach this server from a phone / other device. Talks to
 * /admin/remote-access/* : shows the same-network address (+ QR), starts/stops
 * the app-managed tunnels (ngrok / cloudflared), surfaces Tailscale + manual
 * port-forward status, and manages the signpost "phone bookmark" that always
 * forwards to the PC's current address.
 *
 * Each method carries step-by-step setup guidance (a numbered "How to set this
 * up" list) plus per-field "?" help badges — the same affordance the Deploy →
 * Cloud VM form uses. The badge + shared floating bubble are re-implemented
 * self-contained here (rather than importing deploy.js's _tipBadge) because this
 * is a SHARED module and must not depend on an admin-tools feature folder; it
 * reuses the SAME global CSS classes (.ac-field-tip / .ac-tip-pop in app3.css)
 * so it looks identical.
 *
 * Colours use design-system variables so the card is correct in dark + light.
 */

import { apiPath } from './config.js';
import { isAdmin } from './left-login.js';
import { _refreshLucideIcons, _esc } from './dom-utils.js';
import { copyText } from './clipboard.js';
import { showQrPopup, closeQrPopup } from './qr-popup.js';

let _status = null;

// A safe inline link for step text (author-controlled strings only — see the
// innerHTML note in _renderMethodPanel).
function _lnk(url, label) {
  return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
}

// Per-method guidance shown in the "Reach it from anywhere" configurator:
//   • blurb  — the one-line summary under the method dropdown.
//   • steps  — a numbered "How to set this up" list (rendered as .ac-tx-steps
//              above the inputs, mirroring the Deploy card's manual-install
//              steps). Strings are AUTHORED HTML and may hold links (_lnk).
//   • fields — the inputs; each may carry a `tip` (a plain string, OR a rich
//              {text, link:{url,label}} descriptor) that becomes a circled "?"
//              help badge beside its label (see _tipBadge below).
const METHOD_META = {
  same_network: {
    blurb: 'No remote tunnel — only devices on the same Wi-Fi can reach this PC (using the address above).',
    steps: [
      'On a phone or laptop joined to the <strong>same Wi-Fi</strong> as this PC, open the address shown under “Same network” above.',
      'Secure-only browser features (Google sign-in, camera, microphone) don’t work over plain http — pick a tunnel method below for an https address.',
    ],
    fields: [],
  },
  ngrok: {
    blurb: 'App-managed outbound tunnel. With a reserved domain the address is stable (a paid ngrok feature); without one it changes each restart and the phone bookmark keeps up.',
    steps: [
      'Install ngrok on this PC (once) — ' + _lnk('https://ngrok.com/download', 'download ngrok') + '.',
      'Create a free account, then copy your personal authtoken from the ' + _lnk('https://dashboard.ngrok.com/get-started/your-authtoken', 'ngrok dashboard') + '.',
      'Paste that authtoken in the field below and click “Save token” — a one-time login the app runs into ngrok for you.',
      '(Optional) Reserve a domain for an address that survives restarts; otherwise it changes each time.',
      'Click Start (▶) — the live https address then appears below.',
    ],
    fields: [
      { key: 'domain', label: 'Reserved domain (optional — stable address)', placeholder: 'your-name.ngrok.app', full: true,
        tip: { text: 'A domain you reserve in your ngrok dashboard so the address stays the same on every restart (a paid ngrok feature). Leave blank for a free address that changes each time.', link: { url: 'https://dashboard.ngrok.com/domains', label: 'Reserve a domain' } } },
      { key: 'region', label: 'Region (optional)', placeholder: 'us, eu, ap, au, sa, jp, in',
        tip: 'Which ngrok edge to route through — pick the one nearest you for lower latency (us, eu, ap, au, sa, jp, in). Blank uses ngrok’s default.' },
      { key: 'bin_path', label: 'ngrok path (optional)', placeholder: 'blank = find on PATH',
        tip: 'Where the ngrok program lives on this PC. Leave blank to let the app find it on your system PATH — only set this if you installed ngrok somewhere unusual.' },
      { key: '__authtoken', label: 'ngrok authtoken (saved into ngrok, one-time)', placeholder: 'paste token, then Save token', action: 'authtoken', full: true,
        tip: { text: 'Your personal ngrok login token. Paste it and click “Save token”: the app runs ngrok’s one-time login for you. WebAgent never stores it — ngrok keeps it in its own config.', link: { url: 'https://dashboard.ngrok.com/get-started/your-authtoken', label: 'Get your authtoken' } } },
    ],
  },
  cloudflare: {
    blurb: 'App-managed Cloudflare tunnel. A named tunnel gives a fixed https hostname on a domain you own; tick Quick for a throwaway trycloudflare.com address instead.',
    steps: [
      'Install cloudflared on this PC (once) — ' + _lnk('https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/', 'download cloudflared') + '.',
      '<strong>Quick way (no account):</strong> tick “Quick tunnel” below, then Start — you get a throwaway https://…trycloudflare.com address. Great for a first test.',
      '<strong>Stable way (your own domain):</strong> add a domain to Cloudflare, run “cloudflared tunnel login”, then ' + _lnk('https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/', 'create a named tunnel') + ' and route a hostname to it.',
      'Enter that tunnel’s name and its hostname in the fields below.',
      'Click Start (▶) — the fixed https hostname goes live.',
    ],
    // `quick` comes first: it's the quick-vs-stable choice. When ticked, the
    // named-tunnel fields below are hidden (they only apply to a stable tunnel)
    // — see _applyCloudflareMode. The background process follows this flag:
    // ticked → `cloudflared tunnel --url http://localhost:<port>`; unticked →
    // `cloudflared tunnel run <name>`.
    fields: [
      { key: 'quick', label: 'Quick tunnel — random trycloudflare.com address, no account', type: 'checkbox', full: true,
        tip: 'Tick for the no-account throwaway option: Cloudflare hands out a random https://…trycloudflare.com address that lasts until you stop the tunnel. Untick to use your own named tunnel + fixed hostname below.' },
      { key: 'tunnel', label: 'Named tunnel', placeholder: 'my-pc',
        tip: { text: 'The name of the tunnel you made with “cloudflared tunnel create”. Only for the stable, named-tunnel setup — leave blank if you’re using a Quick tunnel.', link: { url: 'https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/', label: 'Create a named tunnel' } } },
      { key: 'hostname', label: 'Fixed hostname', placeholder: 'pc.webagent.live',
        tip: 'The https address on your own domain that you routed to this tunnel (e.g. pc.example.com). Devices open this to reach the PC. Set once with “cloudflared tunnel route dns”.' },
      { key: 'bin_path', label: 'cloudflared path (optional)', placeholder: 'blank = find on PATH',
        tip: 'Where the cloudflared program lives on this PC. Leave blank to let the app find it on your system PATH — only set this if you installed it somewhere unusual.' },
    ],
  },
  tailscale: {
    blurb: 'Private mesh — only your own enrolled devices can reach this PC (most secure). Nothing is exposed to the public internet.',
    steps: [
      'Install Tailscale on this PC and on your phone / other device — ' + _lnk('https://tailscale.com/download', 'download Tailscale') + '.',
      'Sign <strong>both</strong> devices into the <strong>same</strong> Tailscale account.',
      'Make sure Tailscale is connected (run “tailscale up” if needed) — this PC’s private address then appears below.',
      'On your other device, open that address to reach this PC. Only your enrolled devices can.',
    ],
    fields: [],
  },
  manual: {
    blurb: 'You run your own port-forward / dynamic-DNS. Enter the public address it exposes; Test checks whether it answers.',
    steps: [
      'On your router, forward an external port to this PC’s local address + port (look for “port forwarding” in the router’s settings).',
      '(Optional) If your home IP changes over time, set up dynamic DNS so a fixed name always points to it.',
      'Enter the full public address it exposes in the field below (include http:// or https://).',
      'Click Test (the activity icon) to confirm it reaches this PC.',
    ],
    fields: [
      { key: 'public_url', label: 'Your public address', placeholder: 'https://home.example.com', full: true,
        tip: 'The address your own port-forward or dynamic-DNS exposes to the internet, including http:// or https://. Test checks whether it actually reaches this PC.' },
    ],
  },
};

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }

function _setStatus(msg, kind) {
  const el = _qs('ac-ra-status');
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
  if (el) { el.style.width = '152px'; el.style.height = '152px'; el.style.display = 'block'; }
}

/** Render the Same-network addresses as a uniform stacked list, one row per
 *  URL: <code>address</code> + Copy + QR. The QR button opens a popup panel
 *  (rather than showing a big code inline) so the list stays compact. */
function _renderSameNetwork(sn) {
  const host = _qs('ac-ra-sn-urls');
  if (!host) return;
  host.innerHTML = '';

  // Prefer per-URL entries (each carries its own qr_svg); fall back to the flat
  // urls list (only the primary has a QR in that older shape).
  let entries = Array.isArray(sn.entries) && sn.entries.length ? sn.entries : null;
  if (!entries) {
    entries = (sn.urls || []).map(u => ({
      url: u,
      qr_svg: u === sn.primary_url ? sn.qr_svg : null,
    }));
  }

  if (!entries.length) {
    host.innerHTML = '<div class="ac-hint">No same-network address detected.</div>';
    return;
  }

  entries.forEach(e => {
    const row = document.createElement('div');
    row.className = 'ac-ra-sn-row';

    const code = document.createElement('code');
    code.className = 'ac-ra-code';
    code.textContent = e.url;
    row.appendChild(code);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'ac-btn ac-ra-icon-btn';
    copyBtn.type = 'button';
    copyBtn.title = 'Copy address';
    copyBtn.setAttribute('aria-label', 'Copy address');
    copyBtn.innerHTML = '<i data-lucide="copy"></i>';
    copyBtn.addEventListener('click', () => _copy(e.url));
    row.appendChild(copyBtn);

    const qrBtn = document.createElement('button');
    qrBtn.className = 'ac-btn ac-ra-icon-btn';
    qrBtn.type = 'button';
    qrBtn.title = 'Show QR code';
    qrBtn.setAttribute('aria-label', 'Show QR code');
    qrBtn.innerHTML = '<i data-lucide="qr-code"></i>';
    qrBtn.addEventListener('click', () => _toggleQrPopup(e.url, e.qr_svg, qrBtn));
    row.appendChild(qrBtn);

    host.appendChild(row);
  });

  // Swap the [data-lucide] placeholders for real SVGs (scoped to this host).
  _refreshLucideIcons(host);
}

// ── QR popup (shared: ui/shared/js/qr-popup.js) ──────────────────────────
// The Same-network QR codes arrive pre-rendered per URL from the status endpoint.
// The "Reach it from anywhere" address, by contrast, is dynamic (a live tunnel
// URL that only exists once started), so it has no baked-in SVG — we fetch its
// QR on demand from the shared server-side generator (POST /admin/storage/qr,
// the same one the Deploy config-share QR uses) and cache it per address.
const _qrCache = {};
async function _fetchQrSvg(text) {
  if (Object.prototype.hasOwnProperty.call(_qrCache, text)) return _qrCache[text];
  try {
    const r = await _post('/admin/storage/qr', { text });
    _qrCache[text] = r && r.ok ? r.qr_svg : null;
  } catch { _qrCache[text] = null; }
  return _qrCache[text];
}

/** Shared toggle: reuse the shared module — close if this anchor already open. */
let _lastQrAnchor = null;
function _toggleQrPopup(url, svg, anchor) {
  if (_lastQrAnchor === anchor) { closeQrPopup(); _lastQrAnchor = null; return; }
  _lastQrAnchor = anchor;
  showQrPopup({ url, svg, anchor });
}

async function _toggleRemoteQr(url, anchor) {
  if (_lastQrAnchor === anchor) { closeQrPopup(); _lastQrAnchor = null; return; }
  const svg = await _fetchQrSvg(url);
  if (!svg) { _setStatus('QR unavailable on this server (qrcode package missing)', 'err'); return; }
  _lastQrAnchor = anchor;
  showQrPopup({ url, svg, anchor });
}

async function _load() {
  if (!isAdmin()) return;
  try {
    const res = await fetch(apiPath('/admin/remote-access/status?requesting_user_id=' + encodeURIComponent(_userId())));
    if (!res.ok) return; // 403 for non-admins, etc. — leave the card quiet
    _status = await res.json();
  } catch { return; }
  _renderAll();
}

function _renderAll() {
  if (!_status) return;
  const s = _status;

  const badge = _qs('ac-ra-active-badge');
  if (badge) {
    const running = s.runtime && s.runtime.running ? ' · running' : '';
    badge.textContent = (s.active_method || 'same_network').replace('_', ' ') + running;
  }

  const env = _qs('ac-ra-env-banner');
  if (env) env.hidden = !s.env_locked;

  // Same network — every LAN address stacked, uniform style, each with its
  // own copy + QR-popup button.
  _renderSameNetwork(s.same_network || {});

  // Method
  const sel = _qs('ac-ra-method');
  if (sel) sel.value = s.active_method || 'same_network';
  _renderMethodPanel(s.active_method || 'same_network');

  // Auto-start
  const auto = _qs('ac-ra-autostart');
  if (auto) auto.checked = !!s.auto_start;

  // Runtime line
  _renderRuntime();

  // Phone bookmark + signpost
  const purl = _qs('ac-ra-phone-url');
  if (purl) purl.textContent = s.phone_url || '—';
  _injectQr('ac-ra-phone-qr', s.phone_qr_svg);
  const sp = s.signpost || {};
  if (_qs('ac-ra-sp-server')) _qs('ac-ra-sp-server').value = sp.server_url || '';
  if (_qs('ac-ra-sp-role')) _qs('ac-ra-sp-role').value = sp.role || 'client';
  if (_qs('ac-ra-sp-enabled')) _qs('ac-ra-sp-enabled').checked = sp.enabled !== false;
}

// ── Field-help "?" badge + shared popover ────────────────────────────────────
// A small circled "?" beside a field label. CLICK / TAP pins a help bubble open;
// hover previews it on a mouse. One shared floating bubble is appended to <body>
// so the panel's scroll box can't clip it. This mirrors the Deploy card's field
// help (deploy.js) but is re-implemented here — remote-access.js is a shared
// module and must not import an admin-tools feature file — reusing the SAME
// global CSS (.ac-field-tip / .ac-tip-pop). Text + one optional link only.
let _tipPop = null;        // the shared floating bubble
let _tipAnchor = null;     // the badge it currently points at
let _tipPinned = false;    // opened by click/tap (stays until dismissed)
let _tipHideTimer = null;  // delayed-hide so the pointer can cross into the bubble

function _ensureTipPop() {
  if (_tipPop) return _tipPop;
  _tipPop = document.createElement('div');
  _tipPop.className = 'ac-tip-pop';
  _tipPop.setAttribute('role', 'tooltip');
  _tipPop.hidden = true;
  document.body.appendChild(_tipPop);
  // Dismiss a pinned bubble on outside click / Escape / scroll / resize. Clicks
  // INSIDE the bubble (its link) don't dismiss, so the link stays reachable.
  document.addEventListener('click', (e) => {
    if (!_tipPinned) return;
    if (e.target === _tipAnchor || _tipPop.contains(e.target)) return;
    _hideTip();
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _hideTip(); });
  window.addEventListener('scroll', (e) => {
    if (_tipPop.hidden) return;
    if (e.target === _tipPop || _tipPop.contains(e.target)) return;
    _hideTip();
  }, true);
  window.addEventListener('resize', () => { if (!_tipPop.hidden) _hideTip(); });
  // Keep a bubble alive while the pointer is over it (so its link is clickable).
  _tipPop.addEventListener('mouseenter', () => clearTimeout(_tipHideTimer));
  _tipPop.addEventListener('mouseleave', () => { if (!_tipPinned) _hideTip(); });
  return _tipPop;
}

function _hideTip() {
  clearTimeout(_tipHideTimer);
  if (_tipAnchor) _tipAnchor.classList.remove('is-open');
  _tipPinned = false;
  _tipAnchor = null;
  if (_tipPop) { _tipPop.hidden = true; _tipPop.classList.remove('show', 'is-rich'); }
}

function _scheduleHideTip() {
  clearTimeout(_tipHideTimer);
  _tipHideTimer = setTimeout(_hideTip, 160);
}

function _showTip(anchor, pinned) {
  clearTimeout(_tipHideTimer);
  const data = anchor._tipData || null;
  const text = data ? (data.text || '') : (anchor.dataset.tip || '');
  const link = data ? (data.link || null) : null;
  if (!text && !link) return;
  const pop = _ensureTipPop();
  pop.classList.toggle('is-rich', !!link);
  pop.textContent = '';
  if (text) {
    const t = document.createElement('div');
    t.className = 'ac-tip-text';
    t.textContent = text;
    pop.appendChild(t);
  }
  if (link) {
    const a = document.createElement('a');
    a.className = 'ac-tip-link';
    a.href = link.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.textContent = link.label || link.url;
    pop.appendChild(a);
  }
  pop.style.maxWidth = '320px';
  pop.hidden = false;                       // measurable before placing
  _tipAnchor = anchor;
  _tipPinned = pinned || _tipPinned;
  anchor.classList.toggle('is-open', _tipPinned);
  // Place centred on the badge, on whichever side has room, clamped on-screen.
  const margin = 8, gap = 8;
  const r = anchor.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  let left = r.left + r.width / 2 - pr.width / 2;
  left = Math.max(margin, Math.min(left, window.innerWidth - pr.width - margin));
  const placeAbove = (window.innerHeight - r.bottom) < (pr.height + gap + margin);
  let top = placeAbove ? (r.top - gap - pr.height) : (r.bottom + gap);
  top = Math.max(margin, top);
  pop.style.left = Math.round(left) + 'px';
  pop.style.top = Math.round(top) + 'px';
  pop.classList.add('show');
}

function _toggleTip(b) {
  if (_tipPinned && _tipAnchor === b) _hideTip();
  else _showTip(b, true);
}

// Build a "?" badge for a tip (plain string OR {text, link:{url,label}}).
function _tipBadge(tip) {
  const isObj = tip && typeof tip === 'object';
  const text = isObj ? (tip.text || '') : (tip || '');
  if (!text && !(isObj && tip.link)) return null;
  const b = document.createElement('span');
  b.className = 'ac-field-tip';
  b.textContent = '?';
  b.dataset.tip = text;                     // plain-text fallback (hover + aria)
  if (isObj) b._tipData = tip;              // rich payload consumed by _showTip
  b.tabIndex = 0;
  b.setAttribute('role', 'button');
  b.setAttribute('aria-label', 'Help: ' + (text || 'more information'));
  // preventDefault+stopPropagation so a badge inside a <label> doesn't toggle the
  // control, and the outside-click dismiss doesn't fight the open.
  b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); _toggleTip(b); });
  b.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleTip(b); }
  });
  b.addEventListener('mouseenter', () => { if (!_tipPinned) _showTip(b, false); });
  b.addEventListener('mouseleave', () => { if (!_tipPinned) _scheduleHideTip(); });
  b.addEventListener('blur', () => { if (!_tipPinned) _hideTip(); });
  return b;
}

function _renderMethodPanel(method) {
  const meta = METHOD_META[method] || METHOD_META.same_network;
  const blurb = _qs('ac-ra-blurb');
  const fields = _qs('ac-ra-fields');
  if (blurb) blurb.textContent = meta.blurb;
  if (!fields) return;
  fields.innerHTML = '';

  // "How to set this up" — a numbered steps list above the inputs (mirrors the
  // Deploy card's manual-install steps), spanning both grid columns. Step
  // strings are AUTHORED HTML (they may carry _lnk() links), so innerHTML is
  // safe here — nothing user-supplied reaches this.
  if (Array.isArray(meta.steps) && meta.steps.length) {
    const box = document.createElement('div');
    box.style.gridColumn = '1 / -1';
    const title = document.createElement('div');
    title.className = 'ac-label';
    title.textContent = 'How to set this up';
    box.appendChild(title);
    const ol = document.createElement('ol');
    ol.className = 'ac-tx-steps';
    meta.steps.forEach(s => {
      const li = document.createElement('li');
      li.innerHTML = s;
      ol.appendChild(li);
    });
    box.appendChild(ol);
    fields.appendChild(box);
  }

  const cfg = ((_status.methods || {})[method] || {}).config || {};

  // Tailscale: show detected device status instead of inputs.
  if (method === 'tailscale') {
    const ts = (_status.methods || {}).tailscale || {};
    const div = document.createElement('div');
    div.style.gridColumn = '1 / -1';
    div.className = 'ac-hint';
    if (!ts.available) div.textContent = 'Tailscale is not installed on this PC.';
    else if (!ts.ip) div.textContent = 'Tailscale installed but not connected. Run "tailscale up" and sign in.';
    else div.textContent = 'This device: ' + (ts.name || ts.ip) + '  →  ' + (ts.url || ('http://' + ts.ip));
    fields.appendChild(div);
    return;
  }

  meta.fields.forEach(f => {
    const wrap = document.createElement('div');
    wrap.dataset.fieldWrap = f.key;   // so _applyCloudflareMode can hide it
    if (f.full) wrap.style.gridColumn = '1 / -1';
    const lab = document.createElement('label');
    lab.className = 'ac-label';
    lab.textContent = f.label;
    // Circled "?" help badge (same affordance as the Deploy fields).
    if (f.tip) { const badge = _tipBadge(f.tip); if (badge) lab.appendChild(badge); }
    wrap.appendChild(lab);

    let inp;
    if (f.type === 'checkbox') {
      inp = document.createElement('input');
      inp.type = 'checkbox';
      inp.checked = !!cfg[f.key];
      inp.dataset.type = 'checkbox';
    } else {
      inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'ac-input';
      inp.placeholder = f.placeholder || '';
      inp.value = f.key.startsWith('__') ? '' : (cfg[f.key] == null ? '' : String(cfg[f.key]));
    }
    inp.id = 'ac-ra-fld-' + f.key;
    inp.dataset.key = f.key;

    // Persist on change so the choice survives a refresh (no Save click needed).
    // The authtoken field is excluded — it's saved into ngrok's own config, not
    // ours, and _gatherConfig skips it anyway.
    if (f.action !== 'authtoken' && !f.key.startsWith('__')) {
      inp.addEventListener('change', () => _autoSaveConfig(method));
    }

    if (f.action === 'authtoken') {
      const row = document.createElement('div');
      row.style.display = 'flex';
      row.style.gap = '6px';
      row.style.alignItems = 'center';
      inp.style.flex = '1';
      row.appendChild(inp);
      const btn = document.createElement('button');
      btn.className = 'ac-btn';
      btn.type = 'button';
      btn.textContent = 'Save token';
      btn.addEventListener('click', () => _saveAuthtoken(inp.value.trim()));
      row.appendChild(btn);
      wrap.appendChild(row);
    } else {
      wrap.appendChild(inp);
    }
    fields.appendChild(wrap);
  });

  // Cloudflare: the Quick-tunnel toggle hides the named-tunnel fields (they only
  // apply to a stable tunnel), so the quick-vs-stable choice reads clearly.
  if (method === 'cloudflare') {
    const quick = _qs('ac-ra-fld-quick');
    if (quick) quick.addEventListener('change', _applyCloudflareMode);
    _applyCloudflareMode();
  }
}

/** Show the named-tunnel (stable) fields only when Quick tunnel is unticked. */
function _applyCloudflareMode() {
  const quick = _qs('ac-ra-fld-quick');
  if (!quick) return;
  const hide = quick.checked;
  document
    .querySelectorAll('#ac-ra-fields [data-field-wrap="tunnel"], #ac-ra-fields [data-field-wrap="hostname"]')
    .forEach(w => { w.style.display = hide ? 'none' : ''; });
}

// Where to download each managed tunnel's binary if it isn't installed yet.
const _BIN_DOWNLOAD = {
  cloudflared: 'https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/',
  ngrok: 'https://ngrok.com/download',
};

/** Runtime readout for the active method, shown under the action buttons:
 *   1. a status line — for the app-managed tunnels (cloudflare / ngrok) this
 *      reports whether the tunnel program is installed and whether its process
 *      is currently running, as a coloured dot + label;
 *   2. the live/target address, rendered as a real clickable link (opens the
 *      remote address in a new tab) with a copy button beside it;
 *   3. the error + recent tunnel output when a managed start failed, so a bad
 *      start is legible instead of silent.
 */
function _renderRuntime() {
  const row = _qs('ac-ra-public-row');
  if (!row) return;
  row.innerHTML = '';

  const rt = _status.runtime || {};
  const method = _status.active_method || 'same_network';
  const m = (_status.methods || {})[method] || {};

  // Best-known address: the live tunnel URL first, else the configured target.
  let url = rt.public_url || m.url || (m.config && m.config.public_url) || (m.config && m.config.hostname) || '';
  const href = url && !/^https?:\/\//i.test(url) ? 'https://' + url : url;

  const binName = method === 'cloudflare' ? 'cloudflared' : (method === 'ngrok' ? 'ngrok' : '');

  // ── 1. Status line (managed tunnels only) ──
  if (binName) {
    const line = document.createElement('div');
    line.style.cssText = 'display:flex;align-items:center;gap:6px;';
    const dot = document.createElement('span');
    dot.style.cssText = 'width:8px;height:8px;border-radius:50%;flex:0 0 auto;';
    const label = document.createElement('span');
    if (!m.available) {
      dot.style.background = 'var(--danger)';
      label.innerHTML = '<strong>' + _esc(binName) + '</strong> not installed — '
        + _lnk(_BIN_DOWNLOAD[binName], 'download it');
    } else if (rt.running) {
      dot.style.background = 'var(--success)';
      label.innerHTML = '<strong>' + _esc(binName) + '</strong> running';
      label.style.color = 'var(--success)';
    } else {
      dot.style.background = 'var(--muted)';
      label.innerHTML = '<strong>' + _esc(binName) + '</strong> installed · stopped';
    }
    line.appendChild(dot);
    line.appendChild(label);
    row.appendChild(line);
  }

  // ── 2. Address, as a clickable link + copy button ──
  if (href) {
    const line = document.createElement('div');
    line.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:' + (binName ? '4px' : '0') + ';';
    const lead = document.createElement('span');
    lead.textContent = rt.running ? 'Live at ' : 'Address: ';
    line.appendChild(lead);
    const a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = url;
    a.style.cssText = 'font-weight:600;word-break:break-all;color:'
      + (rt.running ? 'var(--success)' : 'var(--accent)') + ';';
    line.appendChild(a);
    const copyBtn = document.createElement('button');
    copyBtn.className = 'ac-btn ac-ra-icon-btn';
    copyBtn.type = 'button';
    copyBtn.title = 'Copy address';
    copyBtn.setAttribute('aria-label', 'Copy address');
    copyBtn.innerHTML = '<i data-lucide="copy"></i>';
    copyBtn.addEventListener('click', () => _copy(href));
    line.appendChild(copyBtn);
    // QR button — mirrors the Same-network rows so the remote address is just as
    // easy to open on a phone (scan) as to copy. The SVG is fetched on demand
    // (the address is dynamic) — see _toggleRemoteQr.
    const qrBtn = document.createElement('button');
    qrBtn.className = 'ac-btn ac-ra-icon-btn';
    qrBtn.type = 'button';
    qrBtn.title = 'Show QR code';
    qrBtn.setAttribute('aria-label', 'Show QR code');
    qrBtn.innerHTML = '<i data-lucide="qr-code"></i>';
    qrBtn.addEventListener('click', () => _toggleRemoteQr(href, qrBtn));
    line.appendChild(qrBtn);
    row.appendChild(line);
    _refreshLucideIcons(line);
  }

  // ── 3. Error + recent tunnel output when a managed start failed ──
  if (!rt.running && rt.error) {
    const err = document.createElement('div');
    err.style.cssText = 'color:var(--danger);margin-top:4px;';
    err.textContent = rt.error + (rt.output ? ' — ' + rt.output : '');
    row.appendChild(err);
  }
}


function _gatherConfig(method) {
  const cfg = {};
  document.querySelectorAll('#ac-ra-fields [data-key]').forEach(inp => {
    const key = inp.dataset.key;
    if (!key || key.startsWith('__')) return; // __authtoken is saved separately
    if (inp.dataset.type === 'checkbox') cfg[key] = !!inp.checked;
    else { const v = inp.value.trim(); cfg[key] = v; }
  });
  return cfg;
}

// ── Actions ──
async function _onMethodChange() {
  const sel = _qs('ac-ra-method');
  if (!sel) return;
  _renderMethodPanel(sel.value);
  if (!isAdmin()) return;
  try {
    await _post('/admin/remote-access/method', { method: sel.value });
    if (_status) _status.active_method = sel.value;
    _renderRuntime();
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _saveConfig() {
  if (!isAdmin()) return;
  const method = (_qs('ac-ra-method') || {}).value || 'same_network';
  _setStatus('Saving…');
  try {
    await _post('/admin/remote-access/config', { method, config: _gatherConfig(method) });
    _setStatus('Saved.', 'ok');
    await _load();
  } catch (e) { _setStatus(e.message, 'err'); }
}

// Auto-save the config for `method` a beat after any field changes, so choices
// (e.g. the Cloudflare Quick-tunnel toggle, the named-tunnel name/hostname)
// persist across a page refresh with no Save click. We keep the local _status
// mirror in sync so switching method away and back doesn't show a stale form,
// and deliberately do NOT re-render (that would steal focus mid-edit).
let _autoSaveTimer = null;
function _autoSaveConfig(method) {
  if (!isAdmin()) return;
  clearTimeout(_autoSaveTimer);
  _autoSaveTimer = setTimeout(async () => {
    const config = _gatherConfig(method);
    try {
      await _post('/admin/remote-access/config', { method, config });
      if (_status && _status.methods && _status.methods[method]) {
        _status.methods[method].config = config;
      }
      _setStatus('Saved.', 'ok');
    } catch (e) { _setStatus(e.message, 'err'); }
  }, 400);
}

async function _saveAuthtoken(token) {
  if (!isAdmin()) return;
  if (!token) { _setStatus('Paste a token first', 'err'); return; }
  _setStatus('Saving authtoken…');
  try {
    const r = await _post('/admin/remote-access/ngrok-authtoken', { authtoken: token });
    _setStatus(r.detail || (r.ok ? 'Saved.' : 'Failed'), r.ok ? 'ok' : 'err');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _start() {
  if (!isAdmin()) return;
  const method = (_qs('ac-ra-method') || {}).value || 'same_network';
  _setStatus('Starting ' + method.replace('_', ' ') + '…');
  try {
    // Persist the current form BEFORE starting so the background process matches
    // what's on screen — e.g. the Cloudflare "Quick tunnel" choice decides
    // whether cloudflared runs `tunnel --url http://localhost:<port>` (quick) or
    // `tunnel run <name>` (stable). Without this, Play would use the last SAVED
    // config and silently ignore an unsaved quick/stable switch.
    const meta = METHOD_META[method];
    if (meta && meta.fields.length) {
      await _post('/admin/remote-access/config', { method, config: _gatherConfig(method) });
    }
    const r = await _post('/admin/remote-access/start', { method });
    if (r.ok) _setStatus(r.public_url ? ('Live at ' + r.public_url) : 'Started.', 'ok');
    else _setStatus((r.error || 'Failed') + (r.output ? (' — ' + r.output) : ''), 'err');
    await _load();
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _stop() {
  if (!isAdmin()) return;
  _setStatus('Stopping…');
  try { await _post('/admin/remote-access/stop', {}); _setStatus('Stopped.', 'ok'); await _load(); }
  catch (e) { _setStatus(e.message, 'err'); }
}

async function _test() {
  const method = (_qs('ac-ra-method') || {}).value || 'same_network';
  _setStatus('Testing…');
  try {
    const r = await _post('/admin/remote-access/test', { method });
    _setStatus(r.detail || (r.ok ? 'OK' : 'Failed'), r.ok ? 'ok' : 'err');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _regenKey() {
  if (!isAdmin()) return;
  _setStatus('Generating new key…');
  try { await _post('/admin/remote-access/regenerate-key', {}); _setStatus('New bookmark key generated.', 'ok'); await _load(); }
  catch (e) { _setStatus(e.message, 'err'); }
}

async function _saveSignpost() {
  if (!isAdmin()) return;
  const config = {
    enabled: !!(_qs('ac-ra-sp-enabled') || {}).checked,
    role: (_qs('ac-ra-sp-role') || {}).value || 'client',
    server_url: ((_qs('ac-ra-sp-server') || {}).value || '').trim(),
  };
  _setStatus('Saving signpost…');
  try { await _post('/admin/remote-access/config', { method: 'signpost', config }); _setStatus('Signpost saved.', 'ok'); await _load(); }
  catch (e) { _setStatus(e.message, 'err'); }
}

async function _toggleAutoStart() {
  if (!isAdmin()) return;
  try { await _post('/admin/remote-access/auto-start', { auto_start: !!(_qs('ac-ra-autostart') || {}).checked }); }
  catch (e) { _setStatus(e.message, 'err'); }
}

export function initRemoteAccess() {
  const sel = _qs('ac-ra-method');
  if (sel && !sel.dataset.wired) {
    sel.dataset.wired = '1';
    sel.addEventListener('change', _onMethodChange);
  }
  _qs('ac-ra-save-cfg')?.addEventListener('click', _saveConfig);
  _qs('ac-ra-start')?.addEventListener('click', _start);
  _qs('ac-ra-stop')?.addEventListener('click', _stop);
  _qs('ac-ra-test')?.addEventListener('click', _test);
  _qs('ac-ra-regen')?.addEventListener('click', _regenKey);
  _qs('ac-ra-sp-save')?.addEventListener('click', _saveSignpost);
  _qs('ac-ra-autostart')?.addEventListener('change', _toggleAutoStart);
  _qs('ac-ra-phone-copy')?.addEventListener('click', () => _copy((_qs('ac-ra-phone-url') || {}).textContent));

  // Re-load whenever the App Settings section is shown (wired in app-config.js).
  window.__refreshRemoteAccess = _load;
  // Data fetching deferred to startRemoteAccess() — runs only when the
  // Remote Access card is visible.
}

/** Fetch remote access status. Called when the Remote Access card becomes
 *  visible (from startAdminTools in files.js). */
export function startRemoteAccess() {
  _load();
}
