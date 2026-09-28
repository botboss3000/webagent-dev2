'use strict';

import { authHeaders } from '../../../../shared/js/left-login.js?v=253';

/**
 * Storage Routing driver — renders the Browser / Server / Remote checkbox
 * table inside the Data Settings → Database card.
 *
 * Styled to match the model-selection table (ac-ability-row / ac-saved-cap /
 * ac-saved-th) from ui/shared/js/model-table.js. One row per data function,
 * three columns (B / S / R), each column is a single checkbox.
 *
 * Talks to GET /admin/storage/routing (read) and POST /admin/storage/routing (save).
 */

// ── Row definitions ─────────────────────────────────────────────────────────
const ROWS = [
  { key: 'session_data',  label: 'Session Data' },
  { key: 'session_tools', label: 'Session Tool Use' },
  { key: 'session_cache', label: 'Session Cache' },
  { key: 'agent_data',    label: 'Agent Data' },
  { key: 'user_data',     label: 'User Data' },
  { key: 'vault',         label: 'Vault' },
  { key: 'genui_pages',   label: 'Gen UI Pages' },
  { key: 'attachments',   label: 'Attachments' },
  { key: 'local_instance',  label: 'Local Instance', desc: 'App-plane state pinned to this machine — background leader, device presence &amp; jobs, instances, storage layout &amp; migrations, app meta, tenant keys, linking codes.' },
  { key: 'app_shared_data', label: 'App Data Shared', desc: 'App-plane data to share remotely — user accounts &amp; profiles, channel identities, agent templates &amp; catalog, tools, billing, wallets, platform ledger.' },
];

const COLS = [
  { key: 'browser',  label: 'B', title: 'Browser — IndexedDB, instant, zero server cost' },
  { key: 'server',   label: 'S', title: 'Server — SQLite, fast, single-host' },
  { key: 'postgres', label: 'R', title: 'Remote — durable, cross-device sync' },
];

// ── Grid columns (matches model-table SAVED_GRID) ──────────────────────────
// Drag-grip · Label · B   · S   · R
const GRID = '20px minmax(0, 1fr) 24px 24px 24px';

let _routing = {};
let _capabilities = {};
let _saveTimer = null;

// ── Helpers ───────────────────────────────────────────────────────────────

function qs(id) { return document.getElementById(id); }

async function _loadAndRender() {
  const list = qs('ac-sr-list');
  if (!list) return;

  // 1. Fetch config
  try {
    const resp = await fetch('/admin/storage/routing', { headers: authHeaders() });
    if (resp.ok) {
      const data = await resp.json();
      _routing = data.routing || {};
      _capabilities = data.capabilities || {};
    }
  } catch (e) {
    console.warn('[StorageRouting] Fetch failed:', e);
  }

  // 2. Clear placeholder
  list.innerHTML = '';

  // 3. Build header row (mirrors _buildSavedHead)
  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-saved-head ac-saved-row';
  head.style.cssText = 'display:grid;grid-template-columns:' + GRID + ';align-items:center;';
  // Empty drag-grip column
  head.appendChild(document.createElement('span'));
  // Label header
  const headLabel = document.createElement('span');
  headLabel.className = 'ac-ability-label';
  headLabel.innerHTML = '<span class="ac-saved-th">Data Function</span>';
  head.appendChild(headLabel);
  // Column headers (B, S, P)
  for (const col of COLS) {
    const th = document.createElement('span');
    th.className = 'ac-saved-cap ac-saved-th';
    th.textContent = col.label;
    th.title = col.title;
    head.appendChild(th);
  }
  list.appendChild(head);

  // 4. Build data rows
  for (const row of ROWS) {
    const current = _routing[row.key] || 'server';

    const r = document.createElement('div');
    r.className = 'ac-ability-row ac-saved-row';
    r.style.cssText = 'display:grid;grid-template-columns:' + GRID + ';align-items:center;';

    // Drag grip placeholder (empty span, same as model-table)
    const grip = document.createElement('span');
    grip.style.cssText = 'width:20px;';
    r.appendChild(grip);

    // Label cell (name + optional description stacked)
    const labelCell = document.createElement('span');
    labelCell.className = 'ac-sr-label';
    let labelHtml = '<span class="ac-fw600-fg1">' + row.label + '</span>';
    if (row.desc) {
      labelHtml += '<span class="ac-sr-desc">' + row.desc + '</span>';
    }
    labelCell.innerHTML = labelHtml;
    r.appendChild(labelCell);

    // Checkbox cells (one per column) — styled div checkboxes matching
    // the automations/sessions page pattern (.auto-check-cell / .sessions-check-cell).
    for (const col of COLS) {
      const cell = document.createElement('span');
      cell.className = 'ac-saved-cap';
      cell.addEventListener('click', function(e) { e.stopPropagation(); });

      const cb = document.createElement('span');
      cb.className = 'ac-sr-check-cell';
      if (current === col.key) cb.classList.add('checked');
      cb.title = col.title + ' — ' + row.label;
      if (col.key === 'browser') {
        const enabled = (
          row.key === 'session_data' && _capabilities.browser_authority === true
        ) || (
          row.key === 'session_cache' && _capabilities.browser_session_cache === true
        );
        if (!enabled) {
          cb.classList.add('disabled');
          cb.title = `Browser storage is disabled for ${row.label}`;
        }
      }
      cb.addEventListener('click', function(e) {
        e.stopPropagation();
        if (cb.classList.contains('disabled')) return;
        _onCheckChange(row.key, col.key, cb);
      });
      cell.appendChild(cb);
      r.appendChild(cell);
    }

    list.appendChild(r);
  }
}

// ── Checkbox logic — single-select per row, auto-save ──────────────────────

function _onCheckChange(rowKey, colKey, clickedCb) {
  // Single-select: only the clicked column is checked; uncheck siblings
  const rowEl = clickedCb.closest('.ac-saved-row');
  if (!rowEl) return;

  const allCbs = rowEl.querySelectorAll('.ac-sr-check-cell');
  for (const cb of allCbs) {
    if (cb === clickedCb) {
      cb.classList.add('checked');
    } else {
      cb.classList.remove('checked');
    }
  }

  _routing[rowKey] = colKey;

  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(_saveAll, 400);
}

async function _saveAll() {
  try {
    const resp = await fetch('/admin/storage/routing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ routing: _routing }),
    });
    if (resp.ok) {
      _flashSaved();
    } else {
      console.warn('[StorageRouting] Save failed:', resp.status);
    }
  } catch (e) {
    console.warn('[StorageRouting] Save error:', e);
  }
}

function _flashSaved() {
  const el = qs('ac-sr-saved');
  if (!el) return;
  el.classList.remove('ac-hidden');
  clearTimeout(el._hideTimer);
  el._hideTimer = setTimeout(function() { el.classList.add('ac-hidden'); }, 2000);
}

// ── Public entry ───────────────────────────────────────────────────────────

export function initStorageRouting() {
  _loadAndRender();
}
