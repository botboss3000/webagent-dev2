'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

// DB viewer pagination + toolbar — page controls, initial table load, and the
// live auto-refresh poll. Part of the Database viewer (see ui/shared/js/index.js).

import { app } from './state.js';
import { queryTable, cancelEditing, loadPersistedDbState } from './query-render.js';
import { fetchTables, renderTableList, updateTableCounts } from './tables.js';
import { apiPath } from './config.js';
import { authUrl, isAdmin } from './left-login.js';
import { randomUUID } from './uuid.js';

const RESTRICTED_RESET_TIP = 'Restricted: only admins can reset the database';

const QUERY_REFRESH_MS = 3000;
const TABLES_REFRESH_MS = 20000;

export function stopAutoRefresh() {
  if (app.autoRefreshInterval) {
    clearInterval(app.autoRefreshInterval);
    app.autoRefreshInterval = null;
  }
  if (app.autoRefreshTablesInterval) {
    clearInterval(app.autoRefreshTablesInterval);
    app.autoRefreshTablesInterval = null;
  }
  const status = document.getElementById('db-auto-status');
  if (status) status.textContent = '';
}

export function startAutoRefresh() {
  stopAutoRefresh();
  if (!app.dbSelectedTable) return;
  const status = document.getElementById('db-auto-status');
  if (status) status.textContent = '⟳ auto 3s';

  app.autoRefreshInterval = setInterval(() => {
    if (document.hidden) return;
    if (app.dbSelectedTable && !app.editingCell) {
      queryTable(app.dbSelectedTable, { silent: true });
    }
  }, QUERY_REFRESH_MS);

  app.autoRefreshTablesInterval = setInterval(() => {
    if (document.hidden) return;
    updateTableCounts();
  }, TABLES_REFRESH_MS);
}

function restartAutoRefresh() {
  if (app.dbSelectedTable) {
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
}

export function initDbPaginationAndToolbar() {
  app.stopAutoRefresh = stopAutoRefresh;

  // One-shot cleanup: the inner-sidebar collapse toggle was removed in favor
  // of each panel header's own files-panel-collapse-btn. Drop any stale state.
  try { localStorage.removeItem('dbSidebarCollapsed'); } catch(_) {}

  const prevBtn = document.getElementById('db-page-prev');
  const nextBtn = document.getElementById('db-page-next');
  if (prevBtn) {
    prevBtn.addEventListener('click', () => {
      if (app.dbPageOffset <= 0) return;
      app.dbPageOffset = Math.max(0, app.dbPageOffset - app.dbPageLimit);
      if (app.dbSelectedTable) queryTable(app.dbSelectedTable, { keepOffset: true });
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener('click', () => {
      if (app.dbPageOffset + app.dbPageLimit >= app.dbTotalRows) return;
      app.dbPageOffset += app.dbPageLimit;
      if (app.dbSelectedTable) queryTable(app.dbSelectedTable, { keepOffset: true });
    });
  }

  // Reset DB Dropdown
  const resetBtn = document.getElementById('db-reset');
  const resetDropdown = document.getElementById('db-reset-dropdown');
  const resetOptions = document.getElementById('db-reset-options');
  const resetApply = document.getElementById('db-reset-apply');

  // Reflect the admin-only restriction on the toolbar reset button so it
  // gets a hover tooltip and a disabled look for non-admins. We refresh
  // this whenever admin status is loaded — the page renders before the
  // profile fetch completes.
  function syncResetBtnState() {
    if (!resetBtn) return;
    if (isAdmin()) {
      resetBtn.classList.remove('restricted');
      resetBtn.disabled = false;
      resetBtn.title = '';
    } else {
      resetBtn.classList.add('restricted');
      resetBtn.disabled = true;
      resetBtn.title = RESTRICTED_RESET_TIP;
    }
  }
  syncResetBtnState();
  window.addEventListener('admin-status-loaded', syncResetBtnState);

  resetBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!isAdmin()) return;  // hover tooltip on the button explains why
    const isVisible = resetDropdown.style.display === 'block';
    
    if (!isVisible) {
      // Build options
      resetOptions.innerHTML = '';
      
      // Load saved preferences
      let savedResetPrefs = null;
      try {
        const saved = localStorage.getItem('dbResetExcludePrefs');
        if (saved) savedResetPrefs = JSON.parse(saved);
      } catch (err) {}
      
      app.dbTables.forEach(t => {
        const div = document.createElement('div');
        div.style.display = 'flex';
        div.style.alignItems = 'center';
        div.style.gap = '6px';
        
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'db-reset-cb';
        cb.value = t.name;
        
        // Use saved prefs if available, otherwise default logic (check everything except templates)
        if (savedResetPrefs !== null) {
          // If the table name is NOT in the excluded array, it should be checked (included for reset)
          cb.checked = !savedResetPrefs.includes(t.name);
        } else {
          // Default check logic - everything except templates
          if (t.name !== 'context_templates' && t.name !== 'agent_templates') {
            cb.checked = true;
          }
        }
        
        const label = document.createElement('label');
        label.textContent = t.name;
        label.style.fontSize = '11px';
        label.style.color = 'var(--fg-1)';
        label.style.cursor = 'pointer';
        
        // click label toggles checkbox
        label.addEventListener('click', () => { cb.checked = !cb.checked; });

        div.appendChild(cb);
        div.appendChild(label);
        resetOptions.appendChild(div);
      });
      
      resetDropdown.style.display = 'block';
    } else {
      resetDropdown.style.display = 'none';
    }
  });

  // Close dropdown when clicking outside
  document.addEventListener('click', (e) => {
    if (!resetDropdown.contains(e.target) && e.target !== resetBtn) {
      resetDropdown.style.display = 'none';
    }
  });

  resetApply.addEventListener('click', async () => {
    resetApply.textContent = 'Resetting...';
    resetApply.style.background = 'var(--danger)';
    resetApply.disabled = true;

    try {
      const dbName = document.getElementById('db-select').value;
      
      // Compute excluded tables (what is NOT checked)
      const excludeTables = [];
      document.querySelectorAll('.db-reset-cb').forEach(cb => {
        if (!cb.checked) {
          excludeTables.push(cb.value);
        }
      });
      
      // Save preferences to localStorage
      localStorage.setItem('dbResetExcludePrefs', JSON.stringify(excludeTables));
      
      const params = new URLSearchParams();
      params.append('db', dbName);
      
      // If we unchecked everything, excludeTables is empty.
      // If we don't pass 'exclude' in the URL, FastAPI will use the default value ["context_templates", "agent_templates"]
      // To bypass the default and force an empty exclusion list (i.e. truncate ALL), we pass an empty exclude parameter.
      if (excludeTables.length > 0) {
        excludeTables.forEach(t => params.append('exclude', t));
      } else {
        params.append('exclude', '');
      }

      const res = await fetch(authUrl(apiPath('/api/v1/db/reset?' + params.toString())), { method: 'DELETE' });
      const result = await res.json();
      
      if (result.success) {
        // Reload tables and refresh view
        const { fetchTables } = await import('./tables.js');
        resetApply.textContent = '✓ Done';
        resetApply.style.background = 'var(--success)';

        setTimeout(() => {
          resetDropdown.style.display = 'none';
          resetApply.textContent = 'Confirm Reset';
          resetApply.style.background = 'var(--danger)';
          resetApply.disabled = false;
        }, 1500);

        app.dbSelectedTable = null;
        app.dbCurrentResult = null;
        cancelEditing();
        stopAutoRefresh();

        // Clear the webchat too
        const chatEl = document.getElementById('chat-messages-inner');
        if (chatEl) chatEl.innerHTML = '';
        // Start a fresh session so next chat doesn't load stale history
        app.currentSessionId = randomUUID();
        localStorage.setItem('terminalSessionId', app.currentSessionId);
        if (typeof app.populateSessionSelect === 'function') {
          app.populateSessionSelect(app.currentUserId);
        }

        await fetchTables(dbName);
        
        // Default to interactions after reset
        const tableToLoad = app.dbTables.some((t) => t.name === 'interactions')
          ? 'interactions'
          : null;
        if (tableToLoad) {
          app.dbSelectedTable = tableToLoad;
          renderTableList();
          localStorage.setItem('lastDbTable', tableToLoad);
          queryTable(tableToLoad).then(() => startAutoRefresh());
        } else {
          document.getElementById('db-table-data').innerHTML =
            '<div class="db-hint">Database reset</div>';
        }
      } else {
        alert('Reset failed');
        resetApply.textContent = 'Confirm Reset';
        resetApply.style.background = 'var(--danger)';
        resetApply.disabled = false;
      }
    } catch (err) {
      alert('Error: ' + err.message);
      resetApply.textContent = 'Confirm Reset';
      resetApply.style.background = 'var(--danger)';
      resetApply.disabled = false;
    }
  });

  document.getElementById('db-refresh').addEventListener('click', () => {
    const dbName = document.getElementById('db-select').value;
    app.dbSelectedTable = null;
    app.dbCurrentResult = null;
    cancelEditing();
    stopAutoRefresh();
    fetchTables(dbName).then(() => {
      // Default to interactions or saved table
      const tableToLoad = _resolveInitialTable();
      if (tableToLoad) {
        app.dbSelectedTable = tableToLoad;
        renderTableList();
        queryTable(tableToLoad).then(() => startAutoRefresh());
      } else {
        document.getElementById('db-table-data').innerHTML =
          '<div class="db-hint">Select a table to view its contents</div>';
      }
    });
  });
  // Soft refresh on db-select changes (avoids hard reset that drops selected table)
  let __dbChangeTimer = null;
  const softRefreshActiveDbs = () => {
    if (__dbChangeTimer) clearTimeout(__dbChangeTimer);
    __dbChangeTimer = setTimeout(async () => {
      const dbName = document.getElementById('db-select').value;
      await fetchTables(dbName);
      if (app.dbSelectedTable && app.dbTables.some((t) => t.name === app.dbSelectedTable)) {
        queryTable(app.dbSelectedTable, { silent: true, keepOffset: true });
      } else if (app.dbTables.some((t) => t.name === 'interactions')) {
        app.dbSelectedTable = 'interactions';
        renderTableList();
        queryTable('interactions', { silent: true });
      }
    }, 80);
  };
  document.getElementById('db-select').addEventListener('change', softRefreshActiveDbs);
  window.addEventListener('db-active-changed', softRefreshActiveDbs);

  // Show Hidden toggle
  const showHiddenBtn = document.getElementById('db-show-hidden-btn');
  function updateShowHiddenBtn() {
    const isShowing = localStorage.getItem('dbShowHidden') === 'true';
    showHiddenBtn.textContent = isShowing ? '👁 Hide hidden' : '👁‍🗨 Show hidden';
    showHiddenBtn.style.color = isShowing ? 'var(--brand)' : 'var(--fg-3)';
  }
  if (showHiddenBtn) {
    updateShowHiddenBtn();
    showHiddenBtn.addEventListener('click', () => {
      app.dbShowHidden = !app.dbShowHidden;
      localStorage.setItem('dbShowHidden', app.dbShowHidden);
      updateShowHiddenBtn();
      if (app.dbSelectedTable) queryTable(app.dbSelectedTable);
    });
  }
}

/** Resolve which table to load first: saved preference, interactions, or null. */
function _resolveInitialTable() {
  const savedTable = localStorage.getItem('lastDbTable');
  return savedTable && app.dbTables.some((t) => t.name === savedTable)
    ? savedTable
    : app.dbTables.some((t) => t.name === 'interactions')
      ? 'interactions'
      : null;
}

function _loadInitialTable() {
  const tableToLoad = _resolveInitialTable();
  if (tableToLoad) {
    app.dbSelectedTable = tableToLoad;
    renderTableList();
    queryTable(tableToLoad);
  }
}

export function runInitialDbTableLoad(dbName) {
  loadPersistedDbState();
  dbName = dbName || document.getElementById('db-select')?.value || 'local.db';
  fetchTables(dbName).then(() => {
    _loadInitialTable();
  });
}
