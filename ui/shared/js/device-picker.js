/* device-picker.js — the shared "target device" picker for the multi-device fleet.
 *
 * One source of truth for (a) fetching the list of WebAgent instances ("devices")
 * that share this database and (b) rendering the little floating menu used to pick
 * one. Reused by THREE surfaces so they stay consistent:
 *   • the chat composer target pill            (ui/chat/js/chat-ui.js)
 *   • the automations "Run on" column          (ui/main-panel/automations/js/automations.js)
 *   • the sessions device badge (label lookup)  (ui/main-panel/agents/sessions/js/sessions-page.js)
 *
 * Public API on window.DevicePicker:
 *   load(force)            -> Promise<device[]>      (cached ~8s; [{instance_id,label,online,last_seen,is_self}])
 *   selfId()               -> string                 (this device's own fleet instance-id, '' if not yet loaded)
 *   labelFor(instanceId)   -> string                 ('This device' for '' or this device's id, else the device's label)
 *   claudeReady(device)    -> bool                   (can this device run a Claude-Code agent — `claude` CLI installed)
 *   open(anchorEl, opts)   -> opens the floating menu near anchorEl
 *   closeAll()             -> dismiss any open menu
 *
 * open() opts: { currentInstance, currentOffline, showOffline, includeThisDevice,
 *                title, force, onSelect(sel) }   where sel = {instance_id,label,is_self,offline}
 *
 * KEEP: window.DevicePicker is the cross-surface contract. Backed by GET
 * /api/v1/devices (app/api/devices.py). Styling lives in ui/shared/css/app1.css
 * under "Shared target-device picker".
 */
(function () {
  'use strict';

  const PICKER = {};
  let _cache = null;
  let _cacheAt = 0;
  let _cacheUid = '';
  let _accessDenied = false;
  let _selfId = '';   // this device's own fleet instance-id (from the load response)
  const CACHE_MS = 8000;

  function _apiPath(p) { return (window.apiPath ? window.apiPath(p) : p); }
  function _auth() { try { return window.authHeaders ? window.authHeaders() : {}; } catch (_) { return {}; } }
  function _uid() { return (window.app && window.app.currentUserId) || ''; }
  function _isAnonymousGuest(uid) {
    if (String(uid || '').indexOf('anon_') === 0) return true;
    try { return String(localStorage.getItem('auth_user_id') || '').indexOf('anon_') === 0; }
    catch (_) { return false; }
  }
  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function load(force) {
    const now = Date.now();
    const uid = _uid();
    // Anonymous visitors are intentionally not entitled to the shared device
    // fleet.  Do not probe the authenticated endpoint: several always-mounted
    // surfaces ask the picker to prewarm, which otherwise turns one harmless
    // 401 into a repeating background request storm.
    if (uid !== _cacheUid) {
      _cache = null;
      _cacheAt = 0;
      _selfId = '';
      _cacheUid = uid;
      _accessDenied = false;
    }
    if (_isAnonymousGuest(uid)) {
      _cache = [];
      _cacheAt = now;
      return _cache;
    }
    if (_accessDenied) return _cache || [];
    if (!force && _cache && (now - _cacheAt) < CACHE_MS) return _cache;
    try {
      const url = _apiPath('/api/v1/devices?user_id=' + encodeURIComponent(uid));
      const res = await fetch(url, { headers: { ..._auth() } });
      if (res.status === 401 || res.status === 403) {
        _accessDenied = true;
        _cache = [];
        _cacheAt = now;
        return _cache;
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      _cache = Array.isArray(data.devices) ? data.devices : [];
      // Remember this device's own fleet id so callers can pin "This device" to a
      // concrete identity (the endpoint returns it as `self`; fall back to the
      // is_self row if an older server omits it).
      _selfId = data.self || (( _cache.find(d => d && d.is_self) || {}).instance_id) || _selfId;
      _cacheAt = now;
    } catch (e) {
      console.warn('[DevicePicker] load failed:', e);
      _cache = _cache || [];
    }
    return _cache;
  }

  function selfId() { return _selfId; }

  // Can this device run a Claude-Code agent? True only if it reported the `claude`
  // CLI installed in its presence capabilities. Unknown/older servers (no `claude`
  // key) return true so we never falsely flag a device as unable — the runtime
  // "Claude not accessible on this device" notice is the real safety net.
  function claudeReady(device) {
    const c = device && device.capabilities;
    if (!c || typeof c.claude === 'undefined') return true;
    return c.claude === true;
  }

  function labelFor(instanceId) {
    if (!instanceId || instanceId === _selfId) return 'This device';
    const d = (_cache || []).find(x => x.instance_id === instanceId);
    return d ? (d.label || instanceId) : instanceId;
  }

  function _fmtSeen(iso) {
    if (!iso) return '';
    try {
      const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
      if (s < 60) return 'just now';
      if (s < 3600) return Math.floor(s / 60) + 'm ago';
      if (s < 86400) return Math.floor(s / 3600) + 'h ago';
      return Math.floor(s / 86400) + 'd ago';
    } catch (_) { return ''; }
  }

  function _onKey(e) { if (e.key === 'Escape') closeAll(); }

  function closeAll() {
    document.querySelectorAll('.device-picker-pop, .device-picker-backdrop').forEach(el => el.remove());
    document.removeEventListener('keydown', _onKey, true);
  }

  async function open(anchor, opts) {
    opts = opts || {};
    closeAll();
    const devices = await load(opts.force);

    const showOffline = !!opts.showOffline;
    let offlineSel = (opts.currentOffline === 'skip') ? 'skip' : 'wait';
    const curInstance = opts.currentInstance || '';
    const includeThis = opts.includeThisDevice !== false;

    const selfDev = devices.find(d => d.is_self);
    const others = devices.filter(d => !d.is_self);

    let rows = '';
    if (includeThis) {
      const on = !curInstance;
      rows += `<button type="button" class="device-picker-row${on ? ' selected' : ''}" data-instance="" data-self="1">
        <span class="dp-dot online"></span>
        <span class="dp-label">This device${selfDev && selfDev.label ? '<span class="dp-sub">' + _esc(selfDev.label) + '</span>' : ''}</span>
        ${on ? '<i data-lucide="check" class="dp-check"></i>' : ''}
      </button>`;
    }
    for (const d of others) {
      const on = curInstance && d.instance_id === curInstance;
      const sub = d.online ? 'online' : ('offline' + (d.last_seen ? ' · ' + _fmtSeen(d.last_seen) : ''));
      rows += `<button type="button" class="device-picker-row${on ? ' selected' : ''}" data-instance="${_esc(d.instance_id)}" data-label="${_esc(d.label || '')}">
        <span class="dp-dot ${d.online ? 'online' : 'offline'}"></span>
        <span class="dp-label">${_esc(d.label || d.instance_id)}<span class="dp-sub">${sub}</span></span>
        ${on ? '<i data-lucide="check" class="dp-check"></i>' : ''}
      </button>`;
    }
    if (!others.length) {
      rows += `<div class="device-picker-empty">${includeThis ? 'No other devices yet' : 'No other devices online'}</div>`;
    }

    let offlineHtml = '';
    if (showOffline) {
      offlineHtml = `<div class="device-picker-offline">
        <div class="dp-offline-title">If the chosen device is offline</div>
        <div class="dp-offline-seg">
          <button type="button" class="dp-off-opt${offlineSel === 'wait' ? ' on' : ''}" data-off="wait">Wait</button>
          <button type="button" class="dp-off-opt${offlineSel === 'skip' ? ' on' : ''}" data-off="skip">Skip</button>
        </div>
      </div>`;
    }

    const pop = document.createElement('div');
    pop.className = 'device-picker-pop';
    pop.innerHTML =
      `<div class="device-picker-head">${_esc(opts.title || 'Run on')}</div>
       <div class="device-picker-list">${rows}</div>${offlineHtml}`;

    const backdrop = document.createElement('div');
    backdrop.className = 'device-picker-backdrop';
    backdrop.addEventListener('click', closeAll);

    document.body.appendChild(backdrop);
    document.body.appendChild(pop);

    // Anchor the menu: prefer above, flip below if there's no room; clamp to viewport.
    const r = anchor.getBoundingClientRect();
    pop.style.visibility = 'hidden';
    requestAnimationFrame(() => {
      const pw = pop.offsetWidth, ph = pop.offsetHeight;
      let top = r.top - ph - 8;
      if (top < 8) top = r.bottom + 8;
      let left = r.left;
      if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
      if (left < 8) left = 8;
      pop.style.top = Math.round(top) + 'px';
      pop.style.left = Math.round(left) + 'px';
      pop.style.visibility = 'visible';
    });

    if (showOffline) {
      pop.querySelectorAll('.dp-off-opt').forEach(btn => {
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          offlineSel = btn.getAttribute('data-off') === 'skip' ? 'skip' : 'wait';
          pop.querySelectorAll('.dp-off-opt').forEach(b => b.classList.toggle('on', b === btn));
        });
      });
    }

    pop.querySelectorAll('.device-picker-row').forEach(row => {
      row.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const instance = row.getAttribute('data-instance') || '';
        const isSelf = row.getAttribute('data-self') === '1' || !instance;
        const label = instance ? (row.getAttribute('data-label') || instance) : 'This device';
        try {
          if (typeof opts.onSelect === 'function') {
            opts.onSelect({ instance_id: instance, label: label, is_self: isSelf, offline: offlineSel });
          }
        } finally {
          closeAll();
        }
      });
    });

    document.addEventListener('keydown', _onKey, true);
    if (window.lucide && window.lucide.createIcons) { try { window.lucide.createIcons(); } catch (_) {} }
  }

  PICKER.load = load;
  PICKER.selfId = selfId;
  PICKER.labelFor = labelFor;
  PICKER.claudeReady = claudeReady;
  PICKER.open = open;
  PICKER.closeAll = closeAll;
  window.DevicePicker = PICKER;
})();
