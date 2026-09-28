'use strict';

// DB table column resize — drag handles on table headers; persists widths.
// Part of the Database viewer (see ui/shared/js/index.js initDbViewer).

import { app } from './state.js';

export function initColumnResize() {
  document.querySelectorAll('.th-resize').forEach((handle) => {
    const newHandle = handle.cloneNode(true);
    handle.parentNode.replaceChild(newHandle, handle);

    newHandle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const th = newHandle.closest('.db-th');
      const table = th.closest('.db-table');
      const colIndex = Array.from(th.parentNode.children).indexOf(th);
      const startX = e.clientX;
      const startWidth = th.getBoundingClientRect().width;

      app.resizeData = { th, table, colIndex, startX, startWidth };
      newHandle.classList.add('resizing');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const line = document.createElement('div');
      line.className = 'resize-line';
      line.style.left = e.clientX + 'px';
      document.body.appendChild(line);
      app.resizeData.line = line;
    });
  });
}

export function initGlobalColumnResizeListeners() {
  document.addEventListener('mousemove', (e) => {
    if (!app.resizeData) return;
    const diff = e.clientX - app.resizeData.startX;
    const newWidth = Math.max(30, app.resizeData.startWidth + diff);
    app.resizeData.line.style.left = e.clientX + 'px';
  });

  document.addEventListener('mouseup', (e) => {
    if (!app.resizeData) return;

    const diff = e.clientX - app.resizeData.startX;
    const newWidth = Math.max(30, app.resizeData.startWidth + diff);

    const colName = app.resizeData.th.dataset.col;
    const table = app.resizeData.table;
    const colIndex = app.resizeData.colIndex;

    app.resizeData.th.style.width = newWidth + 'px';
    app.resizeData.th.style.minWidth = newWidth + 'px';
    app.resizeData.th.style.maxWidth = newWidth + 'px';

    table.querySelectorAll('tbody tr').forEach((row) => {
      const cell = row.children[colIndex];
      if (cell) {
        cell.style.width = newWidth + 'px';
        cell.style.minWidth = newWidth + 'px';
        cell.style.maxWidth = newWidth + 'px';
      }
    });

    app.COL_WIDTHS[colName] = newWidth + 'px';

    localStorage.setItem('dbColWidths', JSON.stringify(app.COL_WIDTHS));

    app.resizeData.th.querySelector('.th-resize')?.classList.remove('resizing');
    if (app.resizeData.line) app.resizeData.line.remove();
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    app.resizeData = null;
  });
}
