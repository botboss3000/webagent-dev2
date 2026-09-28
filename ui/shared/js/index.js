'use strict';

// Database viewer orchestrator — initDbViewer() wires the table list, query/render,
// column resize, inline edit, row delete, pagination/toolbar and the cell modal
// into the Admin Tools → Database view. Sibling db-viewer modules live in this folder.

import { app } from './state.js';
import { queryTable } from './query-render.js';
import { initJsonToggle } from './json-tree.js';
import { setTableDeps, renderTableList, fetchTables } from './tables.js';
import { initGlobalColumnResizeListeners } from './columnResize.js';
import { initDbCellEditors } from './edit.js';
import { initDbRowDelete } from './delete.js';
import {
  initDbPaginationAndToolbar,
  runInitialDbTableLoad,
  startAutoRefresh,
} from './pagination.js';
import { initCellModal } from './modal.js';

export function initDbViewer() {
  initJsonToggle();
  setTableDeps({ queryTable, startAutoRefresh });
  initGlobalColumnResizeListeners();
  initDbCellEditors();
  initDbRowDelete();
  initDbPaginationAndToolbar();
  initCellModal();
  // Data fetching deferred to startDbViewer() — runs only when the
  // Database sidebar view is activated.
}

/** Fetch tables and render the DB viewer. Called when the Database sidebar
 *  view becomes active (from applySidebarView in files.js). */
export function startDbViewer() {
  runInitialDbTableLoad();
  // Admin status arrives after init. When it lands, re-fetch table counts
  // (server-side filtering now depends on admin status) and re-render the
  // current view so button restricted-state matches the loaded admin flag.
  window.addEventListener('admin-status-loaded', () => {
    const dbName = document.getElementById('db-select')?.value || 'local.db';
    fetchTables(dbName).then(() => {
      renderTableList();
      if (app.dbSelectedTable) queryTable(app.dbSelectedTable);
    });
  });
}
