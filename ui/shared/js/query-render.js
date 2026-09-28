"use strict";

// DB query + table rendering — runs a table query and paints the result grid with
// editable cells (edit.js) and row delete (delete.js). Part of the Database viewer
// (see ui/shared/js/index.js initDbViewer).

import { app } from './state.js';
import { getPKColumns, getDisplayColumns, saveColumnOrder } from "./columns.js";
import { initColumnResize } from "./columnResize.js";
import { formatJsonAsHtml } from './json-tree.js';
import { apiPath } from './config.js';
import { authUrl, isAdmin } from './left-login.js';

const RESTRICTED_EDIT_TIP = 'Restricted: only admins can edit rows';
const RESTRICTED_DELETE_TIP = 'Restricted: only admins can delete rows';

function editBtnHtml() {
  if (isAdmin()) return '<button class="db-cell-edit" title="Edit inline">✎</button>';
  return `<button class="db-cell-edit restricted" disabled title="${RESTRICTED_EDIT_TIP}">✎</button>`;
}

function deleteBtnHtml(ri) {
  if (isAdmin()) return `<button class="db-row-delete" data-ri="${ri}" title="Delete row">🗑</button>`;
  return `<button class="db-row-delete restricted" data-ri="${ri}" disabled title="${RESTRICTED_DELETE_TIP}">🗑</button>`;
}

/**
 * ── Persistence helpers ──
 */
function saveSortState() {
  localStorage.setItem('dbSortState', JSON.stringify(app.dbSortState));
}
function saveExclusions() {
  localStorage.setItem('dbExclusions', JSON.stringify(app.dbExclusions));
}
function saveHiddenCols() {
  localStorage.setItem('dbHiddenCols', JSON.stringify(app.dbHiddenCols));
}

/**
 * Load sort + exclusion state from localStorage on init.
 */
export function loadPersistedDbState() {
  try {
    const sort = localStorage.getItem('dbSortState');
    if (sort) app.dbSortState = JSON.parse(sort);
  } catch (e) { /* ignore */ }
  try {
    const excl = localStorage.getItem('dbExclusions');
    if (excl) app.dbExclusions = JSON.parse(excl);
  } catch (e) { /* ignore */ }
  try {
    const hidden = localStorage.getItem('dbHiddenCols');
    if (hidden) app.dbHiddenCols = JSON.parse(hidden);
    else app.dbHiddenCols = {};
  } catch (e) { app.dbHiddenCols = {}; }
  
  // Load show hidden config
  app.dbShowHidden = localStorage.getItem('dbShowHidden') === 'true';
}

/**
 * Get active sort for current table (from state or default).
 */
function getSortForTable(tableName) {
  const s = app.dbSortState[tableName];
  if (s && s.col && s.dir) return s;
  return { col: 'created_at', dir: 'DESC' };
}

/**
 * Get active exclusions for a column on current table.
 */
function getExclusionsForColumn(tableName, colName) {
  if (app.dbExclusions[tableName] && app.dbExclusions[tableName][colName]) {
    return new Set(app.dbExclusions[tableName][colName]);
  }
  return new Set();
}

/**
 * Close any open column popup.
 */
function closeColPopup() {
  if (app.dbColPopup && app.dbColPopup.el) {
    app.dbColPopup.el.remove();
    app.dbColPopup = null;
  }
  // Also remove any lingering popups
  document.querySelectorAll('.db-col-popup').forEach(el => el.remove());
}

/**
 * Build and show the column filter/sort popup.
 */
async function openColPopup(th, tableName, colName) {
  // Close any existing popup first
  closeColPopup();

  // Indicate opening intent so rapid double clicks can cancel correctly
  const popupIntent = { table: tableName, column: colName, isLoading: true, el: null };
  app.dbColPopup = popupIntent;

  // Fetch distinct values
  let values = [];
  let total = 0;
  try {
    const dbName = document.getElementById('db-select').value;
    const url = authUrl(apiPath(
      `/api/v1/db/column-values?db=${encodeURIComponent(dbName)}&table=${encodeURIComponent(tableName)}&column=${encodeURIComponent(colName)}`
    ));
    const res = await fetch(url);
    if (res.status === 401) {
      localStorage.removeItem('auth_token');
      window.location.reload();
      return;
    }
    const data = await res.json();
    values = data.values || [];
    total = data.total || 0;
  } catch (e) {
    values = [];
    total = 0;
  }

  // Very crucial: Check if user already dismissed or opened another menu while fetching
  if (app.dbColPopup !== popupIntent) return;
  
  // Verify TH is still in the document (avoids zombie popups during layout refreshes)
  if (!document.body.contains(th)) return;

  const excluded = getExclusionsForColumn(tableName, colName);
  const sort = getSortForTable(tableName);

  // Build popup element
  const popup = document.createElement('div');
  popup.className = 'db-col-popup';

  // Sort section
  const sortAscActive = sort.col === colName && sort.dir === 'ASC';
  const sortDescActive = sort.col === colName && sort.dir === 'DESC';

  popup.innerHTML = `
    <div class="db-popup-sort">
      <button class="db-popup-sort-btn ${sortAscActive ? 'active' : ''}" data-sort="ASC">
        <span class="db-popup-sort-arrow">▲</span> Sort Ascending
      </button>
      <button class="db-popup-sort-btn ${sortDescActive ? 'active' : ''}" data-sort="DESC">
        <span class="db-popup-sort-arrow">▼</span> Sort Descending
      </button>
      <button class="db-popup-unsort-btn">✕ Unsort</button>
    </div>
    <div class="db-popup-search-wrap">
      <input type="text" class="db-popup-search" placeholder="Filter by values..." />
    </div>
    <div class="db-popup-select-header">
      <a href="#" class="db-popup-select-all">Select all</a>
      <a href="#" class="db-popup-clear">Clear</a>
    </div>
    <div class="db-popup-items"></div>
    <div class="db-popup-status"></div>
    <div class="db-popup-hide-row">
      <button class="db-popup-hide-btn">Hide column «${colName}»</button>
    </div>
    <div class="db-popup-actions">
      <button class="db-popup-apply-btn">Apply</button>
    </div>
  `;

  // Position the popup
  const thRect = th.getBoundingClientRect();
  const viewer = document.getElementById('db-table-view');
  const viewerRect = viewer.getBoundingClientRect();
  
  // Calculate horizontal position
  let leftPos = thRect.left - viewerRect.left;
  const minWidth = Math.max(180, thRect.width);
  // Max width defined in CSS is 300px
  const expectedWidth = Math.min(300, minWidth); 
  
  // If placing it starting from the left edge of the th pushes it past the 
  // right edge of the table viewer, align it to the right edge of the th instead
  if (leftPos + expectedWidth > viewerRect.width) {
    leftPos = (thRect.right - viewerRect.left) - expectedWidth;
    // Fallback if th is larger than the window or weirdly positioned: keep it inside bounds
    if (leftPos < 0) leftPos = 0;
  }

  popup.style.left = leftPos + 'px';
  popup.style.top = (thRect.bottom - viewerRect.top) + 'px';
  popup.style.minWidth = minWidth + 'px';

  viewer.appendChild(popup);

  // Re-adjust horizontal position after append to use actual width if it overflows
  const popupWidth = popup.offsetWidth;
  if (leftPos + popupWidth > viewerRect.width) {
    leftPos = (thRect.right - viewerRect.left) - popupWidth;
    if (leftPos < 0) leftPos = 0;
    popup.style.left = leftPos + 'px';
  }

  const itemsEl = popup.querySelector('.db-popup-items');
  const statusEl = popup.querySelector('.db-popup-status');
  const searchInput = popup.querySelector('.db-popup-search');

  // Save popup ref
  app.dbColPopup = { el: popup, table: tableName, column: colName, values, excluded, total };

  /**
   * Render the checklist from current popup state.
   */
  function renderItems(filterText) {
    if (!app.dbColPopup) return;
    const { values: allValues, excluded } = app.dbColPopup;
    const ft = (filterText || '').toLowerCase();
    const isClearAll = excluded.has('__ALL__');

    const filtered = ft
      ? allValues.filter(v => v !== null && String(v).toLowerCase().includes(ft))
      : allValues;

    const selectedCount = isClearAll ? 0 : allValues.length - excluded.size;

    let html = '';
    for (const v of filtered) {
      const valStr = v === null ? '__NULL__' : String(v);
      const checked = isClearAll ? false : !excluded.has(valStr);
      const display = v === null ? '<i>NULL</i>' : String(v).replace(/</g, '&lt;').replace(/>/g, '&gt;');
      html += `<label class="db-popup-item ${checked ? '' : 'unchecked'}">
        <input type="checkbox" class="db-popup-check" data-val="${valStr.replace(/"/g, '&quot;')}" ${checked ? 'checked' : ''} />
        <span class="db-popup-val">${display}</span>
      </label>`;
    }

    const shown = filtered.length;
    const totalNote = isClearAll ? `${allValues.length} values` : `${allValues.length} values total`;
    const filteredNote = ft && ft.length > 0
      ? `${shown} of ${allValues.length} matched`
      : totalNote;

    itemsEl.innerHTML = html || '<div class="db-popup-empty">No matching values</div>';
    statusEl.innerHTML = `${filteredNote} &nbsp;|&nbsp; ${selectedCount} selected` +
      (excluded.size > 0 && !isClearAll ? ` &nbsp;<span style="color:var(--warning);">(${excluded.size} excluded)</span>` : '');

    // Wire checkbox changes
    itemsEl.querySelectorAll('.db-popup-check').forEach(cb => {
      cb.addEventListener('change', () => {
        const val = cb.dataset.val;
        if (!app.dbColPopup) return;
        const excl = app.dbColPopup.excluded;
        // Remove __ALL__ sentinel if present — we're now using individual exclusions
        excl.delete('__ALL__');
        if (cb.checked) {
          excl.delete(val);
        } else {
          excl.add(val);
        }
        // If every value is now excluded, switch to __ALL__ sentinel
        if (excl.size >= allValues.length) {
          excl.clear();
          excl.add('__ALL__');
        }
        // Persist exclusions to localStorage but don't re-query yet
        if (!app.dbExclusions[tableName]) app.dbExclusions[tableName] = {};
        app.dbExclusions[tableName][colName] = Array.from(excl);
        saveExclusions();
        // Re-render the checkbox list only
        renderItems(searchInput.value);
      });
    });
  }

  renderItems('');

  // Search input
  searchInput.addEventListener('input', () => {
    renderItems(searchInput.value);
  });

  // Select all
  popup.querySelector('.db-popup-select-all').addEventListener('click', (e) => {
    e.preventDefault();
    if (!app.dbColPopup) return;
    app.dbColPopup.excluded.clear();
    if (!app.dbExclusions[tableName]) app.dbExclusions[tableName] = {};
    app.dbExclusions[tableName][colName] = [];
    saveExclusions();
    renderItems(searchInput.value);
  });

  // Clear (exclude all)
  popup.querySelector('.db-popup-clear').addEventListener('click', (e) => {
    e.preventDefault();
    if (!app.dbColPopup) return;
    app.dbColPopup.excluded = new Set(['__ALL__']);
    if (!app.dbExclusions[tableName]) app.dbExclusions[tableName] = {};
    app.dbExclusions[tableName][colName] = ['__ALL__'];
    saveExclusions();
    renderItems(searchInput.value);
  });

  // Sort buttons
  popup.querySelectorAll('.db-popup-sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const dir = btn.dataset.sort;
      if (!app.dbSortState[tableName]) app.dbSortState[tableName] = {};
      app.dbSortState[tableName] = { col: colName, dir };
      saveSortState();
      closeColPopup();
      queryTable(tableName);
    });
  });

  // Unsort button — reset to default (created_at DESC)
  popup.querySelector('.db-popup-unsort-btn').addEventListener('click', () => {
    delete app.dbSortState[tableName];
    saveSortState();
    closeColPopup();
    queryTable(tableName);
  });

  // Apply button — re-query with accumulated exclusions
  popup.querySelector('.db-popup-apply-btn').addEventListener('click', () => {
    closeColPopup();
    queryTable(tableName);
  });

  // Hide column button
  popup.querySelector('.db-popup-hide-btn').addEventListener('click', () => {
    if (!app.dbHiddenCols[tableName]) app.dbHiddenCols[tableName] = [];
    const idx = app.dbHiddenCols[tableName].indexOf(colName);
    if (idx === -1) app.dbHiddenCols[tableName].push(colName);
    saveHiddenCols();
    closeColPopup();
    queryTable(tableName);
  });

  // Prevent clicks inside popup from bubbling to document
  popup.addEventListener('click', (e) => e.stopPropagation());

  // Focus search
  setTimeout(() => searchInput.focus(), 50);
}

// Global click-outside handler — registered once
let _popupOutsideHandler = null;
function ensurePopupOutsideHandler() {
  if (_popupOutsideHandler) return;
  _popupOutsideHandler = (e) => {
    // If the click is on a th-text, let the th-text handler handle closing/toggling
    // so we don't naturally close it here and then have the th-text handler re-open it
    if (e.target.closest('.th-text')) {
      return; 
    }
    
    if (app.dbColPopup && app.dbColPopup.el) {
      if (!app.dbColPopup.el.contains(e.target)) {
        const tableName = app.dbColPopup.table;
        const wasOpen = !!app.dbColPopup;
        closeColPopup();
        if (wasOpen) queryTable(tableName);
      }
    }
  };
  document.addEventListener('click', _popupOutsideHandler);
}

function cancelEditing() {
  if (app.editingCell) {
    app.editingCell = null;
    if (app.dbCurrentResult) {
      renderTableData(app.dbCurrentResult);
    }
  }
}

/**
 * Build multi-column exclusion filters as JSON for the query URL.
 */
function getExclusionParams(tableName) {
  if (!app.dbExclusions[tableName]) return '';
  const specs = [];
  for (const [col, vals] of Object.entries(app.dbExclusions[tableName])) {
    if (!vals || vals.length === 0) continue;
    // Remove __NULL__ sentinel from the exclusion list — it needs special SQL handling
    const realVals = vals.filter(v => v !== '__NULL__');
    const includeNull = !vals.includes('__NULL__');
    const spec = { col, op: 'not_in', include_null: includeNull };
    if (realVals.length > 0) {
      spec.val = realVals.join(',');
    }
    specs.push(spec);
  }
  if (specs.length === 0) return '';
  return 'filters_json=' + encodeURIComponent(JSON.stringify(specs));
}

function rowPkKey(row, pkCols) {
  if (!pkCols || !pkCols.length) return null;
  return pkCols
    .map((c) => (row[c] === null || row[c] === undefined ? '' : String(row[c])))
    .join('␟');
}

// Cells over this raw-string length skip the inline JSON tree render. The
// modal viewer (↗ button) still parses and renders the full tree on demand.
const INLINE_JSON_MAX_CHARS = 8000;

// LRU-ish cache: value string → { html, isJson }. Insertion-order eviction
// keeps the cache from growing unbounded when the user scrolls through many
// pages of large content. fmtCell output is pure for the same input value,
// so the cache never needs explicit invalidation.
const CELL_HTML_CACHE_MAX = 1000;
const cellHtmlCache = new Map();
function cacheGet(key) {
  if (!cellHtmlCache.has(key)) return undefined;
  const v = cellHtmlCache.get(key);
  cellHtmlCache.delete(key);
  cellHtmlCache.set(key, v);
  return v;
}
function cacheSet(key, value) {
  if (cellHtmlCache.size >= CELL_HTML_CACHE_MAX) {
    const firstKey = cellHtmlCache.keys().next().value;
    if (firstKey !== undefined) cellHtmlCache.delete(firstKey);
  }
  cellHtmlCache.set(key, value);
}

// One shared hover overlay holds the edit (✎) and expand (↗) buttons. We
// move it into the currently-hovered cell instead of stamping two buttons
// into every cell on render. The buttons keep their original class names so
// the existing click delegation in edit.js continues to find them via
// closest('.db-cell').
//
// Touch devices without :hover get the per-cell buttons rendered inline, so
// edit/expand are reachable on first tap.
const HAS_HOVER = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(hover: hover) and (pointer: fine)').matches
  : true;

let _cellOverlay = null;
let _cellOverlayAdmin = null;
function getCellOverlay() {
  // Rebuild when the cell was wiped (edit mode replaces innerHTML) or when
  // admin status flipped since the last build (so the edit button picks up
  // the restricted state).
  const adminNow = isAdmin();
  if (_cellOverlay && _cellOverlay.isConnected && _cellOverlayAdmin === adminNow) {
    return _cellOverlay;
  }
  const span = document.createElement('span');
  span.className = 'db-cell-overlay';
  span.innerHTML =
    editBtnHtml() +
    '<button class="db-cell-expand" title="Open full viewer">↗</button>';
  _cellOverlay = span;
  _cellOverlayAdmin = adminNow;
  return span;
}

let _cellOverlayDelegated = false;
function ensureCellOverlayDelegation(container) {
  if (_cellOverlayDelegated || !container || !HAS_HOVER) return;
  _cellOverlayDelegated = true;
  container.addEventListener('mouseover', (e) => {
    const cell = e.target.closest('.db-cell');
    if (!cell || !container.contains(cell)) return;
    const overlay = getCellOverlay();
    if (overlay.parentElement !== cell) cell.appendChild(overlay);
  });
}

// Lazy chunked rendering: render dbRenderLimit rows initially, then grow
// the limit by dbRenderStep when the user scrolls near the bottom of the
// table viewport. Browser preserves scrollTop across the re-render since
// we only append new rows below the existing ones.
let _scrollGrowDelegated = false;
function ensureScrollGrowDelegation(container) {
  if (_scrollGrowDelegated || !container) return;
  _scrollGrowDelegated = true;
  let growPending = false;
  container.addEventListener('scroll', () => {
    if (growPending) return;
    const result = app.dbCurrentResult;
    if (!result || !result.rows) return;
    if (app.dbRenderLimit >= result.rows.length) return;
    const nearBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 300;
    if (!nearBottom) return;
    growPending = true;
    requestAnimationFrame(() => {
      app.dbRenderLimit = Math.min(
        result.rows.length,
        (app.dbRenderLimit || 0) + (app.dbRenderStep || 30),
      );
      renderTableData(result, true);
      growPending = false;
    });
  });
}

// Slice the fetched rows down to the current render window.
function visibleRows(result) {
  const limit = app.dbRenderLimit || result.rows.length;
  return result.rows.length <= limit ? result.rows : result.rows.slice(0, limit);
}

// Single delegated mousedown listener for row-resize handles. Attached once
// per table container so newly inserted rows work without per-render rebinding.
let _rowResizeDelegated = false;
function ensureRowResizeDelegation(container) {
  if (_rowResizeDelegated || !container) return;
  _rowResizeDelegated = true;

  let resizeHandle = null;
  let resizeStartY = 0;
  let resizeStartHeight = 0;
  let resizeRow = null;

  function onMouseMove(e) {
    if (!resizeHandle) return;
    const delta = e.clientY - resizeStartY;
    const newHeight = Math.max(24, resizeStartHeight + delta);
    resizeRow.querySelectorAll('td').forEach((td) => {
      td.style.height = newHeight + 'px';
      td.style.maxHeight = 'none';
    });
    resizeRow.querySelectorAll('.db-cell-pre').forEach((pre) => {
      pre.style.maxHeight = 'none';
    });
  }

  function onMouseUp() {
    if (resizeHandle) {
      resizeHandle.classList.remove('resizing');
      resizeHandle = null;
      resizeRow = null;
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
  }

  container.addEventListener('mousedown', (e) => {
    const handle = e.target.closest('.db-row-resize-handle');
    if (!handle || !container.contains(handle)) return;
    e.preventDefault();
    const ri = parseInt(handle.dataset.ri, 10);
    const row = container.querySelector(`.db-row[data-ri="${ri}"]`);
    if (!row) return;
    resizeHandle = handle;
    resizeRow = row;
    resizeStartY = e.clientY;
    resizeStartHeight = row.getBoundingClientRect().height;
    handle.classList.add('resizing');
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  });
}

function renderTableData(result, silent) {
  const data = document.getElementById('db-table-data');
  const pkCols = getPKColumns(result.table);
  const displayCols = getDisplayColumns(result.columns, result.table);
  const tableName = result.table;
  const sort = getSortForTable(tableName);

  // Backend appends "\n\n[Tool calls: ...]" suffix to assistant content
  // for history reconstruction. Hide it in the display.
  function stripToolCallsSuffix(s) {
    if (typeof s !== 'string') return s;
    const idx = s.indexOf('\n\n[Tool calls: ');
    return idx >= 0 ? s.slice(0, idx) : s;
  }

  function fmtCell(val) {
    if (val === null) return { html: 'NULL', isJson: false };
    // Cache key uses the *raw* value (pre-strip) since the output depends on
    // the strip + JSON-detect path it falls through.
    const cacheKey = typeof val === 'string' ? val : null;
    if (cacheKey !== null) {
      const cached = cacheGet(cacheKey);
      if (cached) return cached;
    }

    let result;
    if (typeof val === 'string') {
      const stripped = stripToolCallsSuffix(val);
      // Skip inline JSON tree for huge strings — the modal renders it on demand.
      if (stripped.length > 1 && stripped.length <= INLINE_JSON_MAX_CHARS) {
        const trimmed = stripped.trim();
        if (trimmed && (trimmed[0] === '{' || trimmed[0] === '[')) {
          const jsonHtml = formatJsonAsHtml(trimmed);
          if (jsonHtml) {
            result = { html: jsonHtml, isJson: true };
          }
        }
      }
      if (!result) {
        result = {
          html: String(stripped).replace(/</g, '&lt;').replace(/>/g, '&gt;'),
          isJson: false,
        };
      }
    } else {
      result = {
        html: String(val).replace(/</g, '&lt;').replace(/>/g, '&gt;'),
        isJson: false,
      };
    }

    if (cacheKey !== null) cacheSet(cacheKey, result);
    return result;
  }

  function cellInnerHtml(val) {
    const { html: display, isJson } = fmtCell(val);
    const inner = isJson
      ? `<div class="db-cell-json">${display}</div>`
      : `<pre class="db-cell-pre">${display}</pre>`;
    if (HAS_HOVER) return inner;
    // Touch fallback: inline buttons so first-tap access still works.
    return `${editBtnHtml()}${inner}<button class="db-cell-expand" title="Open full viewer">↗</button>`;
  }

  // Column width strategy
  function getColWidth(table, col) {
    if (app.COL_WIDTHS[col]) return app.COL_WIDTHS[col];
    const contentTables = ['interactions', 'context_defaults', 'context_templates', 'context'];
    if (table === 'interactions' && col === 'input') return '300px';
    if (contentTables.includes(table) && col === 'content') return '300px';
    const px = Math.max(40, col.length * 7.5 + 20);
    return Math.round(px) + 'px';
  }

  function buildRowPairHtml(row, ri, pk) {
    const rowClass = ri % 2 === 0 ? 'db-row' : 'db-row db-row-even';
    const isInteractions = result.table === 'interactions';
    const trStyle = isInteractions ? ' style="height:100px"' : '';
    const pkAttr = pk !== null && pk !== undefined ? ` data-pk="${String(pk).replace(/"/g, '&quot;')}"` : '';
    let h = `<tr class="${rowClass}" data-ri="${ri}"${pkAttr}${trStyle}>`;
    h += `<td class="db-row-delete-td">${deleteBtnHtml(ri)}</td>`;
    for (const col of displayCols) {
      const val = row[col];
      const w = getColWidth(result.table, col);
      const style = w ? ` style="width:${w};min-width:${w};max-width:${w}"` : '';
      const cls = val === null ? 'col-null' : '';
      const { html: display, isJson } = fmtCell(val);
      const safeVal = String(val).replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const inner = isJson ? `<div class="db-cell-json">${display}</div>` : `<pre class="db-cell-pre">${display}</pre>`;
      const touchButtons = HAS_HOVER ? '' : editBtnHtml();
      const touchExpand = HAS_HOVER ? '' : '<button class="db-cell-expand" title="Open full viewer">↗</button>';
      h += `<td class="db-cell ${cls}"${style} data-row="${ri}" data-col="${col}" data-val="${safeVal}">${touchButtons}${inner}${touchExpand}</td>`;
    }
    h += '</tr>';
    h += `<tr class="db-row-resize-row" data-ri="${ri}"${pkAttr}><td colspan="${displayCols.length + 1}" class="db-row-resize-td"><div class="db-row-resize-handle" data-ri="${ri}"></div></td></tr>`;
    return h;
  }

  function patchExistingRow(tr, row, ri) {
    // Update positional attrs + even/odd styling
    tr.dataset.ri = String(ri);
    const baseClass = ri % 2 === 0 ? 'db-row' : 'db-row db-row-even';
    if (tr.className !== baseClass) tr.className = baseClass;
    const delBtn = tr.querySelector('.db-row-delete');
    if (delBtn) delBtn.dataset.ri = String(ri);
    const cells = tr.querySelectorAll('td.db-cell');
    for (let ci = 0; ci < displayCols.length && ci < cells.length; ci++) {
      const col = displayCols[ci];
      const val = row[col];
      const cell = cells[ci];
      const newKey = val === null ? 'null' : String(val);
      if (cell.dataset.val !== newKey) {
        cell.innerHTML = cellInnerHtml(val);
        cell.dataset.val = newKey;
        cell.className = 'db-cell' + (val === null ? ' col-null' : '');
      }
      if (cell.dataset.row !== String(ri)) cell.dataset.row = String(ri);
    }
  }

  // Rows actually rendered into the DOM (chunked window over result.rows).
  const renderedRows = visibleRows(result);

  if (silent) {
    const tbody = data.querySelector('table.db-table tbody');
    if (tbody && pkCols && pkCols.length) {
      // PK-keyed reconciliation: reuse existing TRs across reorderings and
      // row count changes, only building new TRs for genuinely new PKs.
      const existingPairs = new Map(); // pk → { dataTr, resizeTr }
      let cursor = tbody.firstElementChild;
      while (cursor) {
        if (cursor.classList.contains('db-row')) {
          const pk = cursor.dataset.pk;
          const next = cursor.nextElementSibling;
          if (pk && next && next.classList.contains('db-row-resize-row')) {
            existingPairs.set(pk, { dataTr: cursor, resizeTr: next });
            cursor = next.nextElementSibling;
            continue;
          }
        }
        cursor = cursor.nextElementSibling;
      }

      const newRowsHtml = [];
      const reused = []; // [{ pair, row, ri }]
      const targetOrder = [];   // sequence of either { kind: 'reuse', pk } or { kind: 'new', idx }
      let pendingNewIdx = 0;

      for (let ri = 0; ri < renderedRows.length; ri++) {
        const row = renderedRows[ri];
        const pk = rowPkKey(row, pkCols);
        const existing = pk !== null ? existingPairs.get(pk) : null;
        if (existing) {
          existingPairs.delete(pk);
          reused.push({ pair: existing, row, ri });
          targetOrder.push({ kind: 'reuse', pk });
        } else {
          newRowsHtml.push(buildRowPairHtml(row, ri, pk));
          targetOrder.push({ kind: 'new', idx: pendingNewIdx++ });
        }
      }

      // Materialise new rows in a single innerHTML parse, then collect their TR pairs.
      let newPairs = [];
      if (newRowsHtml.length) {
        const tmp = document.createElement('tbody');
        tmp.innerHTML = newRowsHtml.join('');
        const newDataTrs = tmp.querySelectorAll('tr.db-row');
        const newResizeTrs = tmp.querySelectorAll('tr.db-row-resize-row');
        for (let i = 0; i < newDataTrs.length; i++) {
          newPairs.push({ dataTr: newDataTrs[i], resizeTr: newResizeTrs[i] });
        }
      }

      // Patch cells on reused rows before reordering (cheaper while detached? no — they're still attached, that's fine).
      for (const { pair, row, ri } of reused) {
        patchExistingRow(pair.dataTr, row, ri);
        // Keep resize TR's positional attrs in sync
        pair.resizeTr.dataset.ri = String(ri);
        const handle = pair.resizeTr.querySelector('.db-row-resize-handle');
        if (handle) handle.dataset.ri = String(ri);
      }

      // Assemble target order into a fragment, then swap into tbody in one shot.
      const frag = document.createDocumentFragment();
      const reusedByPk = new Map();
      for (const r of reused) reusedByPk.set(rowPkKey(r.row, pkCols), r.pair);
      for (const entry of targetOrder) {
        if (entry.kind === 'reuse') {
          const pair = reusedByPk.get(entry.pk);
          if (pair) {
            frag.appendChild(pair.dataTr);
            frag.appendChild(pair.resizeTr);
          }
        } else {
          const pair = newPairs[entry.idx];
          if (pair) {
            frag.appendChild(pair.dataTr);
            frag.appendChild(pair.resizeTr);
          }
        }
      }

      // Old TRs not consumed (existingPairs still has entries) drop on the floor
      // when we replace tbody contents. Row-resize handles use event delegation
      // (ensureRowResizeDelegation), so new TRs work without rebinding.
      tbody.replaceChildren(frag);

      updateHeaderSortArrows(tableName);
      updateFilterIndicators(tableName);
      return;
    }

    // No PK columns (or no tbody yet): fall back to the legacy position-based
    // diff. If row count differs, do a full rebuild.
    if (tbody) {
      const existingRows = tbody.querySelectorAll('tr.db-row');
      for (let ri = 0; ri < renderedRows.length && ri < existingRows.length; ri++) {
        const row = renderedRows[ri];
        const cells = existingRows[ri].querySelectorAll('td.db-cell');
        for (let ci = 0; ci < displayCols.length && ci < cells.length; ci++) {
          const col = displayCols[ci];
          const val = row[col];
          const cell = cells[ci];
          if (cell.dataset.val !== String(val)) {
            cell.innerHTML = cellInnerHtml(val);
            cell.dataset.val = val === null ? 'null' : String(val);
            cell.className = 'db-cell' + (val === null ? ' col-null' : '');
          }
        }
      }
      if (renderedRows.length !== existingRows.length) {
        return renderTableData(result, false);
      }
      updateHeaderSortArrows(tableName);
      updateFilterIndicators(tableName);
      return;
    }
  }

  // ── Full render ──

  let html = '<table class="db-table"><thead>';

  // Header row — column names clickable to open filter popup
  html += '<tr>';
  // Leading column for per-row delete buttons
  html += '<th class="db-th db-row-delete-th"></th>';
  for (const col of displayCols) {
    const w = getColWidth(result.table, col);
    const style = w ? ` style="width:${w};min-width:${w};max-width:${w}"` : '';
    const isSortCol = col === sort.col;
    const sortArrow = isSortCol ? (sort.dir === 'ASC' ? ' ▲' : ' ▼') : '';
    const hasFilter = app.dbExclusions[tableName] && app.dbExclusions[tableName][col] && app.dbExclusions[tableName][col].length > 0;
    const filterClass = hasFilter ? ' filtered' : '';
    
    // Add eye/eye-off toggle only if setting is checked
    let hideBtnHtml = '';
    if (app.dbShowHidden) {
      const isHiddenNow = app.dbHiddenCols[tableName] && app.dbHiddenCols[tableName].includes(col);
      const icon = isHiddenNow ? '👁️‍🗨️' : '👁️'; 
      const btnClass = isHiddenNow ? 'th-hide-btn hidden-col' : 'th-hide-btn';
      hideBtnHtml = `<button class="${btnClass}" data-table="${tableName}" data-col="${col}" title="${isHiddenNow ? 'Show' : 'Hide'} column">${icon}</button>`;
    }

    const safeColTitle = String(col).replace(/"/g, '&quot;');
    html += `<th class="db-th${filterClass}" data-col="${col}"${style} draggable="true" title="${safeColTitle}">
      ${hideBtnHtml}
      <span class="th-text" data-col="${col}" title="${safeColTitle}">
        <span class="th-name">${col}</span>
        <span class="th-sort">${sortArrow}</span>
        <span class="th-funnel">${hasFilter ? ' 🔽' : ''}</span>
      </span>
      <span class="th-resize"></span>
    </th>`;
  }
  html += '</tr>';

  html += '</thead><tbody>';

  for (let ri = 0; ri < renderedRows.length; ri++) {
    const row = renderedRows[ri];
    const pk = rowPkKey(row, pkCols);
    html += buildRowPairHtml(row, ri, pk);
  }
  html += '</tbody></table>';
  data.innerHTML = html;

  // ── Column name click → open popup ──
  data.querySelectorAll('.th-text').forEach(span => {
    span.addEventListener('click', (e) => {
      e.stopPropagation();
      const th = span.closest('.db-th');
      const col = th.dataset.col;
      
      if (app.dbColPopup && app.dbColPopup.table === tableName && app.dbColPopup.column === col) {
        closeColPopup();
        return;
      }
      
      openColPopup(th, tableName, col);
    });
  });

  // ── Column hide bindings ──
  data.querySelectorAll('.th-hide-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const table = btn.dataset.table;
      const col = btn.dataset.col;
      
      if (!app.dbHiddenCols[table]) app.dbHiddenCols[table] = [];
      const idx = app.dbHiddenCols[table].indexOf(col);
      
      if (idx !== -1) {
        // Unhide
        app.dbHiddenCols[table].splice(idx, 1);
      } else {
        // Hide
        app.dbHiddenCols[table].push(col);
      }
      
      saveHiddenCols();
      
      queryTable(tableName); // Re-render table taking hidden into account
    });
  });

  // ── Drag-to-reorder columns ──
  let dragCol = null;
  let dragTh = null;
  let dragPlaceholder = null;

  data.querySelectorAll('.db-th').forEach(th => {
    th.addEventListener('dragstart', (e) => {
      dragCol = th.dataset.col;
      dragTh = th;
      e.dataTransfer.effectAllowed = 'move';
      th.style.opacity = '0.4';
    });
    th.addEventListener('dragend', () => {
      if (dragTh) dragTh.style.opacity = '';
      if (dragPlaceholder) dragPlaceholder.remove();
      dragCol = null; dragTh = null; dragPlaceholder = null;
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
    });
    th.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
      th.classList.add('drop-target');
    });
    th.addEventListener('dragleave', () => {
      th.classList.remove('drop-target');
    });
    th.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!dragCol || dragCol === th.dataset.col) return;

      const currentOrder = getDisplayColumns(result.columns, result.table);
      const fromIdx = currentOrder.indexOf(dragCol);
      const toIdx = currentOrder.indexOf(th.dataset.col);
      if (fromIdx === -1 || toIdx === -1) return;

      currentOrder.splice(fromIdx, 1);
      currentOrder.splice(toIdx, 0, dragCol);
      saveColumnOrder(result.table, currentOrder);

      if (dragTh) dragTh.style.opacity = '';
      dragCol = null; dragTh = null;
      data.querySelectorAll('.db-th').forEach(t => t.classList.remove('drop-target'));
      queryTable(app.dbSelectedTable);
    });
  });

  // Row-resize handles: one delegated listener on the container (idempotent).
  ensureRowResizeDelegation(data);
  // Hover overlay for edit/expand buttons: one delegated mouseover listener.
  ensureCellOverlayDelegation(data);
  // Lazy-render: extend the rendered window when the user scrolls down.
  ensureScrollGrowDelegation(data);

  initColumnResize();
  ensurePopupOutsideHandler();
}

/**
 * Update sort arrows on existing header without full re-render.
 */
function updateHeaderSortArrows(tableName) {
  const sort = getSortForTable(tableName);
  document.querySelectorAll('.db-th .th-sort').forEach(span => {
    const col = span.closest('.th-text').dataset.col;
    if (col === sort.col) {
      span.textContent = sort.dir === 'ASC' ? ' ▲' : ' ▼';
    } else {
      span.textContent = '';
    }
  });
}

/**
 * Update filter funnel indicators on existing headers.
 */
function updateFilterIndicators(tableName) {
  document.querySelectorAll('.db-th').forEach(th => {
    const col = th.dataset.col;
    const hasFilter = app.dbExclusions[tableName] && app.dbExclusions[tableName][col] && app.dbExclusions[tableName][col].length > 0;
    const funnel = th.querySelector('.th-funnel');
    if (hasFilter) {
      th.classList.add('filtered');
      if (funnel) funnel.textContent = ' 🔽';
    } else {
      th.classList.remove('filtered');
      if (funnel) funnel.textContent = '';
    }
  });
}

function getActiveDbsLocal() {
  const f = typeof window.getCheckedDbs === 'function' ? window.getCheckedDbs() : null;
  if (f && f.length) return f;
  const sel = document.getElementById('db-select');
  return sel && sel.value ? [sel.value] : [];
}

function buildQueryUrl(dbName, tableName, limit, offset, sortCol, sortDir, exclParams, legacyFilter, withCount) {
  let url = apiPath(`/api/v1/db/query?db=${encodeURIComponent(dbName)}&table=${encodeURIComponent(tableName)}&limit=${limit}&offset=${offset}`);
  url += `&order_by=${encodeURIComponent(sortCol)}&order_dir=${sortDir}`;
  if (exclParams) url += '&' + exclParams;
  if (legacyFilter) {
    const [fCol, fVal] = legacyFilter;
    url += `&filter_col=${encodeURIComponent(fCol)}&filter_op=contains&filter_val=${encodeURIComponent(fVal)}`;
  }
  if (withCount === false) url += '&with_count=false';
  return url;
}

async function queryTable(tableName, opts) {
  const dbs = getActiveDbsLocal();
  const sort = getSortForTable(tableName);
  const sortCol = sort.col;
  const sortDir = sort.dir;
  const data = document.getElementById('db-table-data');
  const silent = opts?.silent;
  const multi = dbs.length > 1;
  app.dbMultiMode = multi;

  if (!silent) {
    data.innerHTML = '';
    cancelEditing();
    closeColPopup();
    if (opts?.keepOffset) {
      // keep current offset
    } else {
      app.dbPageOffset = 0;
    }
    // Any non-silent reload (table change, sort, filter, prev/next) starts
    // with a fresh chunked render window at the top of the table.
    app.dbRenderLimit = app.dbRenderStep || 30;
  }

  const exclParams = getExclusionParams(tableName);
  const filterEntries = Object.entries(app.dbFilters);
  const legacyFilter = filterEntries.length > 0 ? filterEntries[0] : null;

  try {
    if (!multi) {
      const dbName = dbs[0] || document.getElementById('db-select').value;
      // Silent polls keep the cached total; the backend skips COUNT(*).
      const url = buildQueryUrl(dbName, tableName, app.dbPageLimit, app.dbPageOffset, sortCol, sortDir, exclParams, legacyFilter, !silent);
      const res = await fetch(authUrl(url));
      if (res.status === 401) {
        localStorage.removeItem('auth_token');
        window.location.reload();
        return;
      }
      const result = await res.json();
      // Server returns total = -1 when COUNT(*) was skipped on a silent poll.
      // In that case, keep the cached dbTotalRows and patch the result object
      // so downstream code (updatePageInfo) sees a real number.
      if (typeof result.total === 'number' && result.total >= 0) {
        app.dbTotalRows = result.total;
      } else {
        result.total = app.dbTotalRows;
      }
      app.dbCurrentResult = result;
      if (!result.columns || !result.columns.length) {
        if (!silent) data.innerHTML = '<div class="db-hint">Empty or invalid table</div>';
        updatePageInfo();
        return;
      }
      renderTableData(result, silent);
      updatePageInfo();
      return;
    }

    // Multi-mode: fetch each DB (no offset; bigger cap), merge, sort, slice.
    const PER_DB_CAP = 1000;
    const perDbResults = await Promise.all(dbs.map(async (db) => {
      const url = buildQueryUrl(db, tableName, PER_DB_CAP, 0, sortCol, sortDir, exclParams, legacyFilter, !silent);
      try {
        const r = await fetch(authUrl(url));
        if (!r.ok) return { db, columns: [], rows: [], total: 0 };
        const j = await r.json();
        const t = typeof j.total === 'number' && j.total >= 0 ? j.total : 0;
        return { db, columns: j.columns || [], rows: j.rows || [], total: t };
      } catch (e) {
        return { db, columns: [], rows: [], total: 0 };
      }
    }));

    // Skip DBs that don't have this table (empty columns) — they still report total=0 anyway
    const usable = perDbResults.filter((r) => r.columns.length);
    if (!usable.length) {
      app.dbTotalRows = 0;
      app.dbCurrentResult = { table: tableName, columns: [], rows: [], total: 0, multi: true };
      if (!silent) data.innerHTML = '<div class="db-hint">No matching tables in selected DBs</div>';
      updatePageInfo();
      return;
    }

    // Union of columns (preserve order from first usable), prepend `_db`.
    const colSet = new Set();
    const colOrder = ['_db'];
    usable.forEach((r) => r.columns.forEach((c) => {
      if (!colSet.has(c)) { colSet.add(c); colOrder.push(c); }
    }));

    // Tag rows with _db
    const allRows = [];
    usable.forEach((r) => {
      r.rows.forEach((row) => {
        const tagged = Object.assign({ _db: r.db }, row);
        allRows.push(tagged);
      });
    });

    // Client-side sort on sortCol (works for strings, numbers, dates as strings)
    if (sortCol) {
      const dir = sortDir === 'DESC' ? -1 : 1;
      allRows.sort((a, b) => {
        const av = a[sortCol];
        const bv = b[sortCol];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        if (av < bv) return -1 * dir;
        if (av > bv) return 1 * dir;
        return 0;
      });
    }

    const total = perDbResults.reduce((s, r) => s + (r.total || 0), 0);
    const start = Math.min(app.dbPageOffset, Math.max(0, allRows.length - 1));
    const pageRows = allRows.slice(start, start + app.dbPageLimit);

    const result = {
      table: tableName,
      columns: colOrder,
      rows: pageRows,
      total: total,
      limit: app.dbPageLimit,
      offset: app.dbPageOffset,
      multi: true,
      // Hint to consumers: data is already paginated client-side
      _allRows: allRows,
    };
    app.dbTotalRows = allRows.length; // for pagination math against what we actually have
    app.dbCurrentResult = result;
    renderTableData(result, silent);
    updatePageInfo();
  } catch (e) {
    if (!silent) {
      data.innerHTML = `<div class="db-hint">Error: ${e.message}</div>`;
    }
  }
}

function updatePageInfo() {
  const info = document.getElementById('db-page-info');
  const prevBtn = document.getElementById('db-page-prev');
  const nextBtn = document.getElementById('db-page-next');
  if (!app.dbSelectedTable || app.dbTotalRows <= app.dbPageLimit) {
    info.textContent = '';
    prevBtn.style.display = 'none';
    nextBtn.style.display = 'none';
    return;
  }
  const start = app.dbPageOffset + 1;
  const end = Math.min(app.dbPageOffset + app.dbPageLimit, app.dbTotalRows);
  const totalPages = Math.ceil(app.dbTotalRows / app.dbPageLimit);
  const curPage = Math.floor(app.dbPageOffset / app.dbPageLimit) + 1;
  info.textContent = `${start}–${end} / ${app.dbTotalRows} (pg ${curPage}/${totalPages})`;
  prevBtn.style.display = '';
  nextBtn.style.display = '';
  prevBtn.disabled = app.dbPageOffset <= 0;
  nextBtn.disabled = app.dbPageOffset + app.dbPageLimit >= app.dbTotalRows;
}

export { cancelEditing, renderTableData, queryTable };
