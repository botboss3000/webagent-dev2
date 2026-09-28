'use strict';

// DB row delete — wires the per-row trash button to a DELETE against the table,
// using the shared WHERE builder (columns.js). Part of the Database viewer.

import { app } from './state.js';
import { buildRowWhere } from './columns.js';
import { queryTable } from './query-render.js';
import { apiPath } from './config.js';
import { authHeaders, isAuthenticated, isAdmin } from './left-login.js';

export function initDbRowDelete() {
  document.getElementById('db-table-data').addEventListener('click', async (e) => {
    const btn = e.target.closest('.db-row-delete');
    if (!btn) return;
    e.stopPropagation();

    if (!isAuthenticated()) {
      alert('You must be logged in to delete rows.');
      return;
    }
    if (!isAdmin()) return;  // hover tooltip on the button explains why
    if (!app.dbCurrentResult) return;

    const ri = parseInt(btn.dataset.ri, 10);
    const row = app.dbCurrentResult.rows[ri];
    if (!row) return;

    const where = buildRowWhere(row, app.dbCurrentResult.table, app.dbCurrentResult.columns);

    const summary = Object.entries(where)
      .map(([k, v]) => `${k}=${v === null ? 'NULL' : v}`)
      .join(', ');
    if (!confirm(`Delete row from "${app.dbCurrentResult.table}" where ${summary}?`)) {
      return;
    }

    const dbName = row && row._db ? row._db : document.getElementById('db-select').value;
    try {
      const res = await fetch(apiPath('/api/v1/db/row'), {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          db: dbName,
          table: app.dbCurrentResult.table,
          where: where,
        }),
      });
      const result = await res.json();
      if (!res.ok || !result.success) {
        alert('Delete failed: ' + (result.detail || res.statusText));
        return;
      }
      await queryTable(app.dbCurrentResult.table);
    } catch (e) {
      alert('Error deleting: ' + e.message);
    }
  });
}
