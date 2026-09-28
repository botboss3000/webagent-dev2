'use strict';

// DB inline cell editing — save edited cell values back to the table via the
// shared WHERE builder (columns.js). Part of the Database viewer.

import { app } from './state.js';
import { getPKColumns, buildRowWhere } from './columns.js';
import { cancelEditing, renderTableData } from './query-render.js';
import { openCellPopup } from './modal.js';
import { apiPath } from './config.js';
import { authHeaders, isAuthenticated, isAdmin } from './left-login.js';

async function saveEdit(cell, newValue) {
  if (!app.editingCell || !app.dbCurrentResult) return;

  const row = app.dbCurrentResult.rows[app.editingCell.rowIndex];
  const where = buildRowWhere(row, app.dbCurrentResult.table, app.dbCurrentResult.columns);

  const values = {};
  values[app.editingCell.colName] = newValue;

  try {
    // In multi-mode rows carry _db; route update to source DB.
    const dbName = row && row._db ? row._db : document.getElementById('db-select').value;
    const res = await fetch(apiPath('/api/v1/db/update'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        db: dbName,
        table: app.dbCurrentResult.table,
        where: where,
        values: values,
      }),
    });
    const result = await res.json();
    if (!result.success) {
      alert('Save failed');
      return;
    }
    row[app.editingCell.colName] = newValue;
    app.editingCell = null;
    renderTableData(app.dbCurrentResult);
  } catch (e) {
    alert('Error saving: ' + e.message);
  }
}

export function initDbCellEditors() {
  document.getElementById('db-table-data').addEventListener('click', (e) => {
    const editBtn = e.target.closest('.db-cell-edit');
    if (!editBtn) return;
    
    e.stopPropagation();
    
    const cell = editBtn.closest('.db-cell');
    if (!cell) return;

    const ri = parseInt(cell.dataset.row, 10);
    const col = cell.dataset.col;
    const pkCols = app.dbCurrentResult ? getPKColumns(app.dbCurrentResult.table) : [];
    if (pkCols.includes(col)) return;

    // Admin-only editing: require authentication
    if (!isAuthenticated()) return;
    if (!isAdmin()) return;  // hover tooltip on the button explains why
    if (
      app.editingCell &&
      app.editingCell.rowIndex === ri &&
      app.editingCell.colName === col
    ) {
      return;
    }
    cancelEditing();

    const originalValue = cell.dataset.val === 'null' ? '' : cell.dataset.val;

    cell.innerHTML = `<textarea class="db-edit-input" rows="4">${originalValue.replace(/"/g, '&quot;')}</textarea>`;
    const textarea = cell.querySelector('textarea');
    textarea.focus();
    textarea.select();

    app.editingCell = { rowIndex: ri, colName: col, originalValue, textarea };

    textarea.addEventListener('keydown', (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
        ev.preventDefault();
        saveEdit(cell, textarea.value);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        cancelEditing();
      }
    });

    textarea.addEventListener('blur', () => {
      setTimeout(() => {
        if (app.editingCell && app.editingCell.textarea === textarea) {
          saveEdit(cell, textarea.value);
        }
      }, 100);
    });
  });

  document.getElementById('db-table-data').addEventListener('click', (e) => {
    const btn = e.target.closest('.db-cell-expand');
    if (!btn) return;
    e.stopPropagation();

    // Cancel any active inline editing before opening modal
    if (app.editingCell) {
      cancelEditing();
    }

    const cell = btn.closest('.db-cell');
    if (!cell) return;

    const ri = parseInt(cell.dataset.row, 10);
    const col = cell.dataset.col;
    const originalValue = cell.dataset.val === 'null' ? '' : cell.dataset.val;

    openCellPopup(cell, ri, col, originalValue, saveEdit);
  });
}
