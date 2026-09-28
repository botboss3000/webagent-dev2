'use strict';

// DB cell modal — view/edit popup for a single database cell (JSON-aware viewer +
// editor). Part of the Database viewer (see ui/shared/js/index.js initDbViewer).

import { app } from './state.js';
import { formatJsonAsHtml } from './json-tree.js';

let isEditing = false;

function openCellPopup(cell, ri, col, originalValue, onSave) {
  app.cellPopupData = { cell, col, originalValue, onSave };
  isEditing = false;

  const titleEl = document.getElementById('cell-modal-title');
  const viewer = document.getElementById('cell-modal-viewer');
  const editor = document.getElementById('cell-modal-editor');
  const toggleBtn = document.getElementById('cell-modal-edit-toggle');

  titleEl.textContent = 'Viewing: ' + col;
  
  // Strip the backend "\n\n[Tool calls: ...]" suffix for display only.
  // Editor keeps the original so save round-trip preserves the marker.
  let viewerValue = originalValue;
  if (typeof viewerValue === 'string') {
    const idx = viewerValue.indexOf('\n\n[Tool calls: ');
    if (idx >= 0) viewerValue = viewerValue.slice(0, idx);
  }

  let formattedHtml = null;
  let displayValue = originalValue;

  if (typeof viewerValue === 'string' && viewerValue.length > 0) {
    const trimmed = viewerValue.trim();
    if (trimmed && (trimmed[0] === '{' || trimmed[0] === '[')) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed === 'object') {
          formattedHtml = formatJsonAsHtml(trimmed);
          displayValue = JSON.stringify(parsed, null, 2);
        }
      } catch (e) { /* not JSON, fall through to plain editor */ }
    }
  }

  if (formattedHtml) {
    viewer.innerHTML = formattedHtml;
    viewer.style.display = 'block';
    editor.style.display = 'none';
    toggleBtn.textContent = '✎ Edit';
    toggleBtn.style.display = 'block';
  } else if (viewerValue !== originalValue && viewerValue.length > 0) {
    // Plain (non-JSON) content with a stripped tool-calls suffix: show it
    // in the viewer as text so the user isn't dropped straight into edit mode.
    viewer.textContent = viewerValue;
    viewer.style.display = 'block';
    editor.style.display = 'none';
    toggleBtn.textContent = '✎ Edit';
    toggleBtn.style.display = 'block';
  } else {
    viewer.style.display = 'none';
    editor.style.display = 'block';
    editor.value = originalValue;
    toggleBtn.style.display = 'none'; // Only show toggle if we have a nice viewer
    titleEl.textContent = 'Editing: ' + col;
    isEditing = true;
  }

  document.getElementById('cell-modal').classList.add('open');

  if (isEditing) {
    editor.focus();
    editor.scrollTop = 0;
  } else {
    viewer.scrollTop = 0;
  }
}

function toggleEdit() {
  const viewer = document.getElementById('cell-modal-viewer');
  const editor = document.getElementById('cell-modal-editor');
  const toggleBtn = document.getElementById('cell-modal-edit-toggle');
  const titleEl = document.getElementById('cell-modal-title');
  const { col, originalValue } = app.cellPopupData;

  isEditing = !isEditing;
  if (isEditing) {
    viewer.style.display = 'none';
    editor.style.display = 'block';
    
    let displayValue = originalValue;
    try {
      const parsed = JSON.parse(originalValue);
      displayValue = JSON.stringify(parsed, null, 2);
    } catch(e) {}
    
    editor.value = displayValue;
    toggleBtn.textContent = '👁 View';
    titleEl.textContent = 'Editing: ' + col;
    editor.focus();
  } else {
    viewer.style.display = 'block';
    editor.style.display = 'none';
    toggleBtn.textContent = '✎ Edit';
    titleEl.textContent = 'Viewing: ' + col;
  }
}

function closeCellPopup(discard) {
  document.getElementById('cell-modal').classList.remove('open');
  if (discard) {
    app.cellPopupData = null;
    return;
  }
  if (app.cellPopupData) {
    const { cell, col, originalValue, onSave } = app.cellPopupData;
    app.cellPopupData = null;
    
    // Only save if we were in editing mode AND the value actually changed
    if (isEditing) {
      const newValue = document.getElementById('cell-modal-editor').value;
      if (newValue !== originalValue) {
        const ri = parseInt(cell.dataset.row, 10);
        app.editingCell = { rowIndex: ri, colName: col, originalValue };
        if (typeof onSave === 'function') onSave(cell, newValue);
      }
    }
  }
}

export function initCellModal() {
  document.getElementById('cell-modal-edit-toggle').addEventListener('click', toggleEdit);
  document.getElementById('cell-modal-close').addEventListener('click', () =>
    closeCellPopup(true),
  );
  document.getElementById('cell-modal-backdrop').addEventListener('click', () =>
    closeCellPopup(false),
  );
  document.getElementById('cell-modal-editor').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      closeCellPopup(false);
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeCellPopup(true);
    }
  });
  document.getElementById('cell-modal-editor').addEventListener('blur', () => {
    setTimeout(() => {
      if (app.cellPopupData) {
        closeCellPopup(false);
      }
    }, 150);
  });
}

export { openCellPopup };
