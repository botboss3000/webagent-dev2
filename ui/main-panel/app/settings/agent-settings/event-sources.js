'use strict';

/**
 * Event-sources engine panel — runtime status, source plugins, recent deliveries.
 *
 * Relocated from the former App Config "Event Sources" tab; now rendered inside
 * Agent Settings → "Automation Engine" (the event-trigger half). Wired by
 * agent-settings.js, which calls init() once and load() lazily on first expand.
 */

import { _fetch, _qs, _esc } from '../utils.js';

async function _loadEvents() {
  await Promise.all([
    _loadEventsRuntime(),
    _loadEventsSources(),
    _loadEventsDeliveries(),
  ]);
}

export function init() {
  _qs('ac-events-refresh')?.addEventListener('click', _loadEvents);
}

function _adminUserId() {
  return (window.app && window.app.currentUserId) || localStorage.getItem('webagent_active_user_id') || '';
}

export async function load() {
  await Promise.all([
    _loadEventsRuntime(),
    _loadEventsSources(),
    _loadEventsDeliveries(),
  ]);
}

async function _loadEventsRuntime() {
  const badge = _qs('ac-events-runtime-badge');
  const info  = _qs('ac-events-runtime-info');
  if (!badge || !info) return;
  badge.textContent = 'loading…';
  try {
    const r = await fetch('/admin/events/runtime-status?requesting_user_id=' + encodeURIComponent(_adminUserId()));
    if (!r.ok) {
      badge.textContent = r.status === 403 ? 'admin only' : `HTTP ${r.status}`;
      info.innerHTML = '';
      return;
    }
    const d = await r.json();
    // Runtime health = the two in-process loops that drive event triggers: the
    // poller (poll-only sources) + the renewer (refreshes push subscriptions).
    const poller = !!d.poller_running, renewer = !!d.renewer_running;
    const running = poller && renewer;
    badge.textContent = running ? 'running' : (poller || renewer ? 'partial' : 'idle');
    badge.classList.remove('ac-fg-success','ac-fg-warning','ac-fg3'); badge.classList.add(running ? 'ac-fg-success' : (poller || renewer ? 'ac-fg-warning' : 'ac-fg3'));
    info.innerHTML =
      `<div class="es-detail-row">· Poller: <span class="es-poller-status">${poller ? 'running' : 'stopped'}</span></div>` +
      `<div class="es-detail-row">· Renewer: <span class="es-renewer-status">${renewer ? 'running' : 'stopped'}</span></div>` +
      `<div class="es-detail-row-muted">${d.subscriptions_expiring_within_24h || 0} subscription(s) expiring within 24h</div>`;
    const pollerSpan = info.querySelector('.es-poller-status');
    if (pollerSpan) { pollerSpan.classList.remove('ac-fg-success','ac-fg3'); pollerSpan.classList.add(poller ? 'ac-fg-success' : 'ac-fg3'); }
    const renewerSpan = info.querySelector('.es-renewer-status');
    if (renewerSpan) { renewerSpan.classList.remove('ac-fg-success','ac-fg3'); renewerSpan.classList.add(renewer ? 'ac-fg-success' : 'ac-fg3'); }
  } catch (e) {
    badge.textContent = 'error';
    badge.classList.add('ac-fg-danger'); badge.classList.remove('ac-fg-success','ac-fg-warning','ac-fg3');
    info.innerHTML = `<div class="es-detail-row es-status-err">${_esc(e.message)}</div>`;
  }
}

async function _loadEventsSources() {
  const wrap = _qs('ac-events-sources');
  if (!wrap) return;
  try {
    const r = await fetch('/admin/events/sources?requesting_user_id=' + encodeURIComponent(_adminUserId()));
    if (!r.ok) { wrap.innerHTML = `<div>${r.status === 403 ? 'Admin access required.' : 'Could not load event sources.'}</div>`; return; }
    const d = await r.json();
    const rows = d.sources || [];
    if (!rows.length) { wrap.innerHTML = '<div>(no event sources registered)</div>'; return; }
    // _renderEventSourceRow returns a DOM node (it wires button click handlers),
    // so append the nodes — never `.map().join('')`, which would stringify each
    // node to "[object HTMLDivElement]".
    wrap.innerHTML = '';
    rows.forEach(s => wrap.appendChild(_renderEventSourceRow(s)));
  } catch (e) { wrap.innerHTML = `<div>Error: ${e.message}</div>`; }
}

function _renderEventSourceRow(s) {
  const name = s.name || s.source_id || '';
  const row = document.createElement('div');
  row.classList.add('es-source-row-layout');

  const left = document.createElement('div');
  left.classList.add('ac-flex1');
  const stateColor = s.enabled ? 'var(--success)' : 'var(--fg-4)';
  const subInfo = `${s.subscription_enabled || 0}/${s.subscription_total || 0} active`;
  const errInfo = s.subscription_errored
    ? ` · <span class="es-err-count">${s.subscription_errored} errored</span>` : '';
  left.innerHTML = `<div class="es-source-name">${_esc(name)}</div>`
    + `<div class="es-source-desc">${_esc(s.description || '')}</div>`
    + `<div class="es-source-state-row"><span class="es-source-state">${s.enabled ? 'enabled' : 'disabled'}</span> · ${subInfo}${errInfo}</div>`;
  const stateSpan = left.querySelector('.es-source-state');
  if (stateSpan) { stateSpan.classList.remove('ac-fg-success','ac-fg4','ac-fg3'); stateSpan.classList.add(stateColor === 'var(--success)' ? 'ac-fg-success' : 'ac-fg4'); }

  const right = document.createElement('div');
  right.classList.add('ac-flex-gap8');

  const result = document.createElement('span');
  result.classList.add('es-result-label');

  // Both actions operate on the whole source (re-register / unregister every
  // subscription on it) and take requesting_user_id as a QUERY param.
  const rerunBtn = document.createElement('button');
  rerunBtn.className = 'ac-btn ac-btn-xs';
  rerunBtn.textContent = 'Re-register';
  rerunBtn.title = 'Re-register every enabled subscription on this source (after recreating provider infrastructure)';
  rerunBtn.addEventListener('click', async () => {
    rerunBtn.disabled = true; rerunBtn.textContent = '…';
    try {
      const rr = await fetch(`/admin/events/sources/${encodeURIComponent(name)}/re-register-all?requesting_user_id=${encodeURIComponent(_adminUserId())}`, { method: 'POST' });
      if (!rr.ok) throw new Error(`HTTP ${rr.status}`);
      const d = await rr.json();
      result.textContent = `✓ ${d.succeeded || 0} ok`;
      result.classList.add('ac-fg-success'); result.classList.remove('ac-fg-danger','ac-fg-warning','ac-fg3');
    } catch (e) {
      result.textContent = `✗ ${e.message}`;
      result.classList.add('ac-fg-danger'); result.classList.remove('ac-fg-success','ac-fg-warning','ac-fg3');
    }
    rerunBtn.disabled = false; rerunBtn.textContent = 'Re-register';
  });

  const stopBtn = document.createElement('button');
  stopBtn.className = 'ac-btn ac-btn-xs';
  stopBtn.textContent = 'Stop all';
  stopBtn.title = 'Unregister every subscription on this source (before deleting provider infrastructure)';
  stopBtn.addEventListener('click', async () => {
    stopBtn.disabled = true; stopBtn.textContent = '…';
    try {
      const rr = await fetch(`/admin/events/sources/${encodeURIComponent(name)}/stop-all?requesting_user_id=${encodeURIComponent(_adminUserId())}`, { method: 'POST' });
      if (!rr.ok) throw new Error(`HTTP ${rr.status}`);
      const d = await rr.json();
      result.textContent = `✓ ${d.stopped || 0} stopped`;
      result.classList.add('ac-fg-warning'); result.classList.remove('ac-fg-success','ac-fg-danger','ac-fg3');
    } catch (e) {
      result.textContent = `✗ ${e.message}`;
      result.classList.add('ac-fg-danger'); result.classList.remove('ac-fg-success','ac-fg-warning','ac-fg3');
    }
    stopBtn.disabled = false; stopBtn.textContent = 'Stop all';
  });

  right.appendChild(rerunBtn);
  right.appendChild(stopBtn);
  right.appendChild(result);
  row.appendChild(left);
  row.appendChild(right);
  return row;
}

async function _loadEventsDeliveries() {
  const wrap = _qs('ac-events-deliveries');
  if (!wrap) return;
  try {
    const r = await fetch(`/admin/events/deliveries?requesting_user_id=${encodeURIComponent(_adminUserId())}&limit=50`);
    if (!r.ok) {
      wrap.innerHTML = `<div>${r.status === 403 ? 'Admin access required.' : 'Could not load deliveries.'}</div>`;
      return;
    }
    const d = await r.json();
    const rows = d.deliveries || [];
    if (!rows.length) {
      wrap.innerHTML = '<div>(no deliveries yet)</div>';
      return;
    }
    const colorByStatus = {
      ok: 'var(--success)',
      test: 'var(--brand)',
      duplicate: 'var(--fg-3)',
      pending: 'var(--warning)',
      error: 'var(--danger)',
    };
    wrap.innerHTML = rows.map(r => {
      const errPart = r.error ? `  ${r.error.substr(0, 80)}` : '';
      const t = (r.created_at || '').replace('T', ' ').substr(0, 19);
      return `<div class="es-event-row"><span class="es-event-time">${t}</span>  <span class="es-event-status">${r.status.padEnd(9)}</span> <span class="es-event-source">${r.source}.${r.event_type}</span> evt=${(r.event_external_id || '').substr(0, 36)} sub=${(r.subscription_id || '').substr(0, 8)}${errPart}</div>`;
    }).join('');
    // Apply dynamic colours per row
    wrap.querySelectorAll('.es-event-status').forEach((el, i) => {
      const c = colorByStatus[rows[i].status] || 'var(--fg-3)'; el.classList.remove('ac-fg-success','ac-fg-danger','ac-fg-brand','ac-fg-warning','ac-fg3'); el.classList.add(c === 'var(--success)' ? 'ac-fg-success' : c === 'var(--danger)' ? 'ac-fg-danger' : c === 'var(--brand)' ? 'ac-fg-brand' : c === 'var(--warning)' ? 'ac-fg-warning' : 'ac-fg3');
    });
  } catch (e) {
    wrap.innerHTML = `<div>Error: ${e.message}</div>`;
  }
}

export function stop() {
  // Event Sources has no persistent state to clean up.
}