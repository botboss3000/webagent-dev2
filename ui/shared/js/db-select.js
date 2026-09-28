// ── Custom db-select dropdown ──
// Extracted from index.html inline script for maintainability.
document.addEventListener('DOMContentLoaded', function() {

  // Multi-DB checked-set (persisted). When > 1, viewer enters multi-mode.
  function getCheckedDbs() {
    try {
      var raw = localStorage.getItem('dbCheckedDbs');
      var arr = raw ? JSON.parse(raw) : [];
      if (Array.isArray(arr) && arr.length) return arr;
    } catch(e) {}
    var sel = document.getElementById('db-select');
    return sel && sel.value ? [sel.value] : [];
  }
  function setCheckedDbs(arr) {
    localStorage.setItem('dbCheckedDbs', JSON.stringify(arr));
    window.dispatchEvent(new CustomEvent('db-checked-changed', { detail: arr }));
  }
  window.getCheckedDbs = getCheckedDbs;
  window.setCheckedDbs = setCheckedDbs;

  function setDbSelectValue(name) {
    var sel = document.getElementById('db-select');
    var label = document.getElementById('db-select-label');
    if (!sel) return;
    var prev = sel.value;
    sel.value = name;
    if (label) label.textContent = name;
    // Sole-select also resets checked set to just this name
    setCheckedDbs([name]);
    if (prev !== name) {
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      // Same value but checked set changed — trigger refresh
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  function closeDbMenu() {
    var menu = document.getElementById('db-select-menu');
    if (menu) menu.hidden = true;
    var trig = document.getElementById('db-select-trigger');
    if (trig) trig.setAttribute('aria-expanded', 'false');
    closeDbRowActions();
  }

  function closeDbRowActions() {
    var open = document.querySelector('.db-pick-actions');
    if (open) open.remove();
  }

  async function deleteDb(name) {
    if (name === 'local.db') return;
    if (!confirm('Delete database file "' + name + '"? This cannot be undone.')) return;
    var token = localStorage.getItem('auth_token');
    var sel = document.getElementById('db-select');
    try {
      var res = await fetch(
        '/api/v1/db/file?db=' + encodeURIComponent(name) +
        '&token=' + encodeURIComponent(token || ''),
        { method: 'DELETE' }
      );
      if (!res.ok) {
        var err = await res.json().catch(function() { return { detail: res.statusText }; });
        alert('Delete failed: ' + (err.detail || res.statusText));
        return;
      }
      if (sel && sel.value === name) {
        setDbSelectValue('local.db');
      }
      await populateDbList({ force: true });
    } catch(e) {
      alert('Delete failed: ' + e.message);
    }
  }

  function openDbRowActions(name, kebabEl) {
    closeDbRowActions();
    var popup = document.createElement('div');
    popup.className = 'db-pick-actions';
    popup.dataset.dbName = name;
    popup.innerHTML =
      '<button class="db-pick-action danger" data-action="delete" type="button">' +
      '<i data-lucide="trash-2" style="width:14px;height:14px;"></i> Delete</button>';
    document.body.appendChild(popup);
    var kb = kebabEl.getBoundingClientRect();
    var pw = popup.offsetWidth, ph = popup.offsetHeight;
    var left = kb.right - pw;
    var top = kb.bottom + 4;
    if (left < 4) left = 4;
    if (left + pw > window.innerWidth - 4) left = window.innerWidth - pw - 4;
    if (top + ph > window.innerHeight - 4) top = kb.top - ph - 4;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
    popup.addEventListener('click', function(e) {
      e.stopPropagation();
      var btn = e.target.closest('.db-pick-action');
      if (!btn) return;
      var action = btn.dataset.action;
      closeDbRowActions();
      if (action === 'delete') deleteDb(name);
    });
    if (window.lucide && window.lucide.createIcons) window.lucide.createIcons();
  }

  function getShowAllDbs() {
    return localStorage.getItem('dbShowAllDbs') === 'true';
  }
  function setShowAllDbs(v) {
    localStorage.setItem('dbShowAllDbs', v ? 'true' : 'false');
  }

  function renderDbMenu(databases) {
    var menu = document.getElementById('db-select-menu');
    var sel = document.getElementById('db-select');
    if (!menu || !sel) return;
    menu.innerHTML = '';

    // ── Header row: "All databases" master toggle + delete-all ──
    var header = document.createElement('li');
    header.className = 'db-pick-header';
    var allCb = document.createElement('input');
    allCb.type = 'checkbox';
    allCb.className = 'db-pick-cb';
    allCb.checked = getShowAllDbs();
    allCb.title = 'Auto-select all databases (regular polling)';
    allCb.addEventListener('click', function(ev) { ev.stopPropagation(); });
    allCb.addEventListener('change', function() {
      setShowAllDbs(allCb.checked);
      if (allCb.checked) {
        setCheckedDbs(databases.slice());
        startDbAutoPoll();
      } else {
        stopDbAutoPoll();
        setCheckedDbs([]);
      }
      refreshDbSelectLabel();
      window.dispatchEvent(new CustomEvent('db-active-changed'));
      renderDbMenu(databases);
    });
    var headerLabel = document.createElement('span');
    headerLabel.className = 'db-pick-header-label';
    headerLabel.textContent = 'All databases';
    headerLabel.addEventListener('click', function() {
      allCb.checked = !allCb.checked;
      allCb.dispatchEvent(new Event('change'));
    });
    header.appendChild(allCb);
    header.appendChild(headerLabel);

    // Delete-all button (skips local.db)
    var delAll = document.createElement('button');
    delAll.type = 'button';
    delAll.className = 'db-pick-header-trash';
    delAll.title = 'Delete all databases (keeps local.db)';
    delAll.innerHTML = '<i data-lucide="trash-2" style="width:13px;height:13px;"></i>';
    delAll.addEventListener('click', async function(ev) {
      ev.stopPropagation();
      var victims = databases.filter(function(n) { return n !== 'local.db'; });
      if (!victims.length) {
        alert('No databases to delete (only local.db present).');
        return;
      }
      if (!confirm('Delete ' + victims.length + ' database file(s)? local.db will be kept. This cannot be undone.')) return;
      var token = localStorage.getItem('auth_token');
      var failed = [];
      await Promise.all(victims.map(async function(name) {
        try {
          var res = await fetch(
            '/api/v1/db/file?db=' + encodeURIComponent(name) +
            '&token=' + encodeURIComponent(token || ''),
            { method: 'DELETE' }
          );
          if (!res.ok) {
            var err = await res.json().catch(function() { return { detail: res.statusText }; });
            failed.push(name + ': ' + (err.detail || res.statusText));
          }
        } catch(e) {
          failed.push(name + ': ' + e.message);
        }
      }));
      if (failed.length) alert('Some deletes failed:\n' + failed.join('\n'));
      // Reset selection back to local.db, refresh list
      var s = document.getElementById('db-select');
      if (s) s.value = 'local.db';
      setCheckedDbs(['local.db']);
      refreshDbSelectLabel();
      await populateDbList({ force: true });
      window.dispatchEvent(new CustomEvent('db-active-changed'));
    });
    header.appendChild(delAll);

    menu.appendChild(header);

    var checked = new Set(getCheckedDbs());
    databases.forEach(function(name) {
      var li = document.createElement('li');
      li.className = 'db-pick-row' + (name === sel.value ? ' selected' : '');
      li.setAttribute('role', 'option');
      li.dataset.dbName = name;

      // Checkbox — multi-select for merged view
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'db-pick-cb';
      cb.checked = checked.has(name);
      cb.title = 'Include in merged view';
      cb.addEventListener('click', function(ev) { ev.stopPropagation(); });
      cb.addEventListener('change', function() {
        var current = new Set(getCheckedDbs());
        if (cb.checked) current.add(name); else current.delete(name);
        var arr = Array.from(current);
        setCheckedDbs(arr);
        // If active sel is now unchecked and others remain, switch sel to first checked
        if (!cb.checked && sel.value === name && arr.length > 0) {
          sel.value = arr[0];
        }
        // Update .selected class on rows without rebuilding the menu
        Array.from(menu.querySelectorAll('.db-pick-row')).forEach(function(rowEl) {
          rowEl.classList.toggle('selected', rowEl.dataset.dbName === sel.value);
        });
        refreshDbSelectLabel();
        // Soft notify — pagination.js debounces and does silent re-query.
        window.dispatchEvent(new CustomEvent('db-active-changed'));
      });
      li.appendChild(cb);

      var label = document.createElement('span');
      label.className = 'db-pick-title';
      label.textContent = name;
      label.title = name;
      label.addEventListener('click', function() {
        setDbSelectValue(name);
        closeDbMenu();
      });
      li.appendChild(label);

      // Kebab → Delete (hidden for local.db)
      if (name !== 'local.db') {
        var kebab = document.createElement('button');
        kebab.type = 'button';
        kebab.className = 'db-pick-kebab';
        kebab.title = 'More actions';
        kebab.dataset.dbName = name;
        kebab.innerHTML = '<i data-lucide="more-vertical" style="width:13px;height:13px;"></i>';
        kebab.addEventListener('click', function(ev) {
          ev.stopPropagation();
          openDbRowActions(name, kebab);
        });
        li.appendChild(kebab);
      }

      menu.appendChild(li);
    });

    if (window.lucide && window.lucide.createIcons) window.lucide.createIcons();
  }

  var __lastDbList = [];

  function arraysEqualUnordered(a, b) {
    if (a.length !== b.length) return false;
    var sa = a.slice().sort();
    var sb = b.slice().sort();
    for (var i = 0; i < sa.length; i++) if (sa[i] !== sb[i]) return false;
    return true;
  }

  async function populateDbList(opts) {
    var sel = document.getElementById('db-select');
    if (!sel) return;
    var token = localStorage.getItem('auth_token');
    if (!token) return;
    try {
      var res = await fetch('/api/v1/db/list?token=' + encodeURIComponent(token));
      if (!res.ok) return;
      var data = await res.json();
      var prevValue = sel.value;
      sel.innerHTML = '';
      data.databases.forEach(function(name) {
        var opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        sel.appendChild(opt);
      });
      // Restore previous selection if still present, else local.db, else first
      var nextValue = data.databases.includes(prevValue) ? prevValue
        : data.databases.includes('local.db') ? 'local.db'
        : (data.databases[0] || '');
      if (nextValue) {
        sel.value = nextValue;
      }

      // If "All databases" mode is on, sync checked set to full list.
      if (getShowAllDbs()) {
        var prevChecked = getCheckedDbs();
        if (!arraysEqualUnordered(prevChecked, data.databases)) {
          setCheckedDbs(data.databases.slice());
          // Trigger viewer refresh only on actual change
          sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }

      refreshDbSelectLabel();
      // Re-render only on list change or when menu open, to avoid clobbering hover state.
      var listChanged = !arraysEqualUnordered(__lastDbList, data.databases);
      var menu = document.getElementById('db-select-menu');
      var menuOpen = menu && !menu.hidden;
      if (listChanged || menuOpen || (opts && opts.force)) {
        renderDbMenu(data.databases);
      }
      __lastDbList = data.databases.slice();
    } catch(e) {
      console.warn('Failed to load db list:', e);
    }
  }

  // ── Regular polling (active when "All databases" toggle is on) ──
  var __dbPollTimer = null;
  function startDbAutoPoll() {
    if (__dbPollTimer) return;
    __dbPollTimer = setInterval(function() {
      populateDbList();
    }, 3000);
  }
  function stopDbAutoPoll() {
    if (__dbPollTimer) {
      clearInterval(__dbPollTimer);
      __dbPollTimer = null;
    }
  }

  // Keep label + menu in sync if any other code sets sel.value directly
  function refreshDbSelectLabel() {
    var sel = document.getElementById('db-select');
    var label = document.getElementById('db-select-label');
    if (!sel || !label) return;
    var checked = getCheckedDbs();
    if (checked.length > 1) {
      label.textContent = sel.value + ' +' + (checked.length - 1);
    } else {
      label.textContent = sel.value;
    }
  }
  var selEl = document.getElementById('db-select');
  if (selEl) {
    selEl.addEventListener('change', function() {
      refreshDbSelectLabel();
      // Only rebuild menu DOM when menu is actually open (avoids flicker during background updates)
      var menu = document.getElementById('db-select-menu');
      if (menu && !menu.hidden) {
        var names = Array.from(selEl.options).map(function(o) { return o.value; });
        renderDbMenu(names);
      }
    });
  }
  window.addEventListener('db-checked-changed', refreshDbSelectLabel);

  // Trigger toggles menu (fixed-positioned at trigger rect so no ancestor clips it).
  function positionDbMenu() {
    var trig = document.getElementById('db-select-trigger');
    var menu = document.getElementById('db-select-menu');
    if (!trig || !menu) return;
    var r = trig.getBoundingClientRect();
    var w = Math.max(r.width, 240);
    var left = r.left;
    if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
    if (left < 8) left = 8;
    menu.style.top = (r.bottom + 4) + 'px';
    menu.style.left = left + 'px';
    menu.style.minWidth = w + 'px';
  }
  var trigger = document.getElementById('db-select-trigger');
  if (trigger) {
    trigger.addEventListener('click', function(ev) {
      ev.stopPropagation();
      var menu = document.getElementById('db-select-menu');
      if (!menu) return;
      if (!menu.hidden) {
        closeDbMenu();
      } else {
        // Refetch list every time menu opens so newly-created/deleted DBs appear
        populateDbList({ force: true }).then(function() {
          positionDbMenu();
          menu.hidden = false;
          trigger.setAttribute('aria-expanded', 'true');
        });
      }
    });
  }
  window.addEventListener('resize', positionDbMenu);
  window.addEventListener('scroll', positionDbMenu, true);
  // Click outside closes menu (kebab popup is positioned outside #db-select-wrap)
  document.addEventListener('click', function(ev) {
    var wrap = document.getElementById('db-select-wrap');
    if (wrap && !wrap.contains(ev.target) && !ev.target.closest('.db-pick-actions')) {
      closeDbMenu();
    }
  });
  document.addEventListener('keydown', function(ev) {
    if (ev.key === 'Escape') {
      closeDbMenu();
    }
  });

  // ── Download button ──
  var downloadBtn = document.getElementById('db-download-btn');
  if (downloadBtn) {
    downloadBtn.addEventListener('click', function() {
      var sel = document.getElementById('db-select');
      var dbName = sel ? sel.value : 'local.db';
      var token = localStorage.getItem('auth_token');
      var url = '/api/v1/db/download?db=' + encodeURIComponent(dbName);
      if (token) url += '&token=' + encodeURIComponent(token);
      window.open(url, '_blank');
    });
  }

  // ── Populate on first db tab appearance ──
  populateDbList().then(function() {
    // Resume auto-poll if "All databases" was previously enabled
    if (getShowAllDbs()) startDbAutoPoll();
  });
});