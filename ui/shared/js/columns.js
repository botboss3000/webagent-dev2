'use strict';

// DB column model — PK / display column helpers and the shared WHERE-clause builder
// (buildRowWhere), deduplicated from former copies in delete.js / edit.js.

import { app } from './state.js';

export function getPKColumns(tableName) {
  const t = app.dbTables.find((x) => x.name === tableName);
  if (!t) return [];
  return t.columns.filter((c) => c.pk).map((c) => c.name);
}

/**
 * Build a WHERE clause object from a row, using PK columns first, then
 * falling back to all non-`_db` columns. Deduplicated from legacy copies
 * in delete.js and edit.js (clone-group dup:5c438efb).
 */
export function buildRowWhere(row, tableName, columns) {
  const pkCols = getPKColumns(tableName);
  const where = {};
  for (const pk of pkCols) {
    where[pk] = row[pk];
  }
  if (!Object.keys(where).length) {
    for (const col of columns) {
      if (col === '_db') continue;
      where[col] = row[col];
    }
  }
  return where;
}

export function saveColumnOrder(tableName, cols) {
  app.dbColumnOrder[tableName] = cols;
  localStorage.setItem('dbColumnOrder', JSON.stringify(app.dbColumnOrder));
}

/** Single implementation (deduped from legacy duplicate). */
export function getDisplayColumns(columns, tableName) {
  let ordered = [];
  
  if (app.dbColumnOrder[tableName]) {
    for (const c of app.dbColumnOrder[tableName]) {
      if (columns.includes(c)) ordered.push(c);
    }
    for (const c of columns) {
      if (!app.dbColumnOrder[tableName].includes(c)) ordered.push(c);
    }
  } else {
    const TABLE_ORDERS = {
      interactions: [
        'role',
        'input',
        'content',
        'tool_name',
        'metadata',
        'session_id',
        'id',
        'parent_id',
        'tool_call_id',
        'created_at',
      ],
    };

    const order = TABLE_ORDERS[tableName];
    if (order) {
      for (const c of order) {
        if (columns.includes(c)) ordered.push(c);
      }
      for (const c of columns) {
        if (!order.includes(c)) ordered.push(c);
      }
    } else {
      const priority = ['created_at', 'role', 'content', 'session_id', 'id'];
      for (const p of priority) {
        if (columns.includes(p)) ordered.push(p);
      }
      for (const c of columns) {
        if (!priority.includes(c)) ordered.push(c);
      }
    }
  }

  // Filter out hidden columns if dbShowHidden is disabled 
  // (if dbShowHidden is true, we want to SEE the columns, so we don't filter them out)
  if (!app.dbShowHidden && app.dbHiddenCols[tableName]) {
    const hiddenSet = new Set(app.dbHiddenCols[tableName]);
    ordered = ordered.filter(col => !hiddenSet.has(col));
  }

  return ordered.length > 0 ? ordered : columns;
}
