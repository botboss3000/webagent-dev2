// V2 dropdown presentation/lifecycle, with independent Rust data authority.
export function createSessionList(root, {guard, refresh, select, client, recycled = () => {}}) {
  let items = [], selected = '', config = {}, query = '', status = 'active';
  let hits = [], continuation = null, searchInfo = '', searching = false, epoch = 0, timer = 0, destroyed = false;
  let editing = null;
  const menu = root.querySelector('.session-dropdown-menu');
  const abort = new AbortController(), timers = new Set(), receipts = new Map();
  const icon = name => `<i data-lucide="${name}" style="width:14px;height:14px"></i>`;
  const button = (title, glyph, fn, cls = '') => {
    const b = document.createElement('button'); b.type = 'button'; b.className = cls;
    b.title = title; b.setAttribute('aria-label', title); b.innerHTML = icon(glyph);
    if (fn) b.onclick = e => { e.stopPropagation(); guard(fn)(e); };
    return b;
  };
  function delay(fn, ms) { const t = setTimeout(() => { timers.delete(t); if (!destroyed) fn(); }, ms); timers.add(t); return t; }
  function elapsed(ts) {
    if (!ts) return '';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    for (const [unit, size] of [['s',60], ['m',3600], ['h',86400], ['d',604800], ['w',2419200]]) {
      if (seconds < size) return Math.floor(seconds / ({s:1,m:60,h:3600,d:86400,w:604800}[unit])) + unit;
    }
    return new Date(ts * 1000).toLocaleDateString(undefined, {month:'short', day:'numeric'});
  }
  function statusNode(item) {
    const node = document.createElement('span');
    node.className = 'session-row-status ' + (item.is_unread ? 'session-status-unread' : 'session-status-idle');
    node.dataset.ts = item.last_active_at || '';
    node.textContent = elapsed(item.last_active_at);
    node.title = (item.is_unread ? `${item.unread_count} unread assistant messages · ` : '') +
      (item.last_active_at ? 'Last active ' + new Date(item.last_active_at * 1000).toLocaleString() : 'Activity unknown');
    return node;
  }
  function label() {
    const item = items.find(s => s.id === selected);
    const label = root.querySelector('#session-dropdown-label');
    if (label) label.textContent = item?.title || 'New Session';
    root.dataset.loaded = 'true';
    for (const id of ['icon','status','kebab','delete']) {
      const node = root.querySelector('#session-dropdown-' + id);
      node.hidden = config.trigger?.[{icon:'agent_icon',kebab:'row_actions'}[id] || id] === false || (id === 'delete' && config.trigger?.delete !== true);
      node.style.display = node.hidden ? 'none' : '';
    }
    root.querySelector('#session-dropdown-icon').classList.toggle('session-row-agent-warning', !!item?.pinned);
    const statusEl = root.querySelector('#session-dropdown-status');
    statusEl.replaceChildren(); if (item) statusEl.append(statusNode(item));
    root.querySelector('#session-dropdown-delete').disabled = !item || item.status === 'recycled';
  }
  async function mutate(item, action, changes = {}) {
    const body = {expected_revision:item.metadata_revision, ...changes};
    const receiptKey = JSON.stringify([item.id, action, body]);
    if (!receipts.has(receiptKey)) receipts.set(receiptKey, crypto.randomUUID());
    body.client_request_id = receipts.get(receiptKey);
    try {
      const result = await client[action](item.id, body);
      receipts.delete(receiptKey);
      if (action === 'recycle' && selected === item.id) recycled(item.id);
      if (result.session) items = items.map(s => s.id === item.id ? result.session : s);
      root._sessionDropdownController.setPopup(null);
      await refresh(); await loadScope();
    } catch (error) {
      // Keep the exact receipt/body available for an uncertain-response retry.
      searchInfo = error.message || 'Session update failed'; render(); throw error;
    }
  }
  function confirmButton(b, text, fn) {
    let armed = false, busy = false;
    const old = b.innerHTML;
    b.onclick = e => {
      e.stopPropagation(); if (busy) return;
      if (!armed) {
        armed = true; b.textContent = text; b.setAttribute('aria-label', text);
        delay(() => { armed = false; b.innerHTML = old; }, 3000); return;
      }
      busy = true; b.disabled = true;
      guard(async () => { try { await fn(); } finally { busy = false; b.disabled = false; armed = false; b.innerHTML = old; } })(e);
    };
  }
  function rename(item, element) {
    if (!element || editing) return;
    const input = document.createElement('input'); input.className = 'session-row-title-input'; input.value = item.title;
    editing = input; element.replaceWith(input); input.focus(); input.select(); let done = false;
    const finish = commit => {
      if (done) return; done = true; editing = null; input.replaceWith(element);
      const title = input.value.trim(); label(); render();
      if (commit && title && title !== item.title) guard(() => mutate(item, 'patch', {title}))();
    };
    input.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); finish(true); } if (e.key === 'Escape') { e.preventDefault(); finish(false); } });
    input.addEventListener('blur', () => finish(true));
  }
  function actions(item, anchor) {
    const popup = document.createElement('div'); popup.className = 'session-row-actions'; popup.dataset.source = 'session-list'; popup.dataset.id = item.id;
    for (const [key, title, glyph, fn] of [
      ['pinning',item.pinned ? 'Unpin session' : 'Pin session',item.pinned ? 'pin-off' : 'pin',() => mutate(item,'patch',{pinned:!item.pinned})],
      ['auto_rename','Auto rename','wand-2',null],
      ['rename','Rename session','pencil',() => { root._sessionDropdownController.setPopup(null); rename(item, anchor.closest('.session-row')?.querySelector('.session-row-title') || root.querySelector('#session-dropdown-label')); }],
      ['open_in_widget','Open in widget','messages-square',null],
      ['refresh','Refresh session','refresh-cw',async () => { await refresh(); await loadScope(); }],
      ['start_scratchpad','Start a scratchpad','notepad-text',null],
      ['delete','Recycle session','trash-2',() => mutate(item,'recycle')]
    ]) {
      if (config.menu?.[key] === false) continue;
      const b = button(title,glyph,fn,'session-row-action' + (key === 'delete' ? ' danger' : ''));
      b.append(document.createTextNode(' ' + title)); b.disabled = !fn;
      if (!fn) b.title = title + ' — unavailable in v3';
      if (key === 'delete') confirmButton(b, 'Recycle?', fn);
      popup.append(b);
    }
    for (const [key,title,value] of [['agent_id','Agent',item.agent_id],['session_id','Session',item.id]]) {
      if (config.menu?.[key] === false) continue;
      const b = button(title,'copy',() => navigator.clipboard.writeText(value),'session-row-action session-row-id');
      b.append(document.createTextNode(` ${title}: ${value}`)); popup.append(b);
    }
    root._sessionDropdownController.setPopup(popup); document.body.append(popup);
    const r = anchor.getBoundingClientRect(); popup.style.position = 'fixed';
    popup.style.left = Math.max(4,Math.min(r.right-popup.offsetWidth,innerWidth-popup.offsetWidth-4)) + 'px';
    popup.style.top = Math.max(4,Math.min(r.bottom+4,innerHeight-popup.offsetHeight-4)) + 'px'; window.lucide?.createIcons();
  }
  async function search(more = false) {
    const turn = ++epoch, q = query, scope = status, token = more ? continuation : null;
    searching = true; if (!more) { hits = []; continuation = null; } searchInfo = ''; render();
    try {
      const page = await client.search({q,status:scope,continuation:token});
      if (destroyed || turn !== epoch) return;
      hits = more ? [...hits,...page.results] : page.results; continuation = page.continuation;
      searchInfo = page.incomplete_due_to_size ? `${page.skipped_oversized_total} oversized records skipped; search is incomplete.` : (page.scan_complete ? 'Search complete' : 'More records remain; continue search.');
    } catch (error) { if (turn === epoch && !destroyed) searchInfo = 'Search failed: ' + (error.message || 'Unable to search private sessions'); }
    finally { if (turn === epoch && !destroyed) { searching = false; render(); } }
  }
  async function loadScope() {
    const turn = ++epoch, scope = status;
    const data = await client.sessions(scope);
    if (destroyed || turn !== epoch) return;
    if (scope === 'active') items = data.sessions;
    else binItems = data.sessions;
    if (query.trim()) await search(); else render();
  }
  let binItems = [];
  function render() {
    if (menu.hidden || editing || destroyed) return;
    const focused = menu.querySelector('.session-manage-search-input');
    const focus = focused === document.activeElement, position = focused?.selectionStart;
    menu.replaceChildren();
    const source = status === 'active' ? items : binItems;
    const rows = query.trim() ? hits.map(hit => ({item:source.find(s => s.id === hit.session_id),hit})).filter(r => r.item) : source.map(item => ({item}));
    for (const {item,hit} of rows) {
      const row = document.createElement('div'); row.className = 'session-row' + (item.id === selected ? ' selected' : '') + (item.pinned ? ' pinned' : '') + (status === 'recycled' ? ' recycled' : '');
      row.dataset.id = item.id; row.dataset.agent = item.agent_id; row.tabIndex = 0;
      const title = document.createElement('span'); title.className = 'session-row-title';
      if (config.menu?.agent_icons !== false) { const avatar = document.createElement('span'); avatar.className = 'session-row-agent-icon' + (item.pinned ? ' session-row-agent-warning' : ''); avatar.innerHTML = icon('bot'); title.append(avatar); }
      const text = document.createElement('span'); text.className = 'session-row-title-label'; text.textContent = item.title || 'New Session'; title.append(text); row.append(title);
      if (hit) { const snippet = document.createElement('span'); snippet.className = 'session-row-msg-hit'; snippet.textContent = hit.snippet; snippet.title = hit.snippet; row.append(snippet); }
      if (config.menu?.status !== false) row.append(statusNode(item));
      if (status === 'recycled') {
        const b = button('Restore session','archive-restore',null,'session-row-bin'); b.textContent = 'bin';
        confirmButton(b,'Restore?',() => mutate(item,'restore')); row.append(b);
      } else if (config.menu?.row_actions !== false) row.append(button('More…','more-vertical',() => actions(item,row.querySelector('.session-row-kebab')),'session-row-kebab'));
      menu.append(row);
    }
    if (!rows.length) { const empty = document.createElement('div'); empty.className = 'session-dropdown-empty'; empty.textContent = searching ? 'Searching…' : searchInfo.startsWith('Search failed:') ? 'Search unavailable; no results verified' : query ? 'No matches in this page' : status === 'recycled' ? 'Recycle bin is empty' : 'No sessions yet'; menu.append(empty); }
    if (searchInfo) { const info = document.createElement('div'); info.className = 'session-dropdown-sync'; info.setAttribute('role','status'); info.textContent = searchInfo; menu.append(info); }
    if (continuation) { const more = button('Continue private search','search',() => search(true),'session-row-action'); more.textContent = 'Continue search'; more.disabled = searching; menu.append(more); }
    const bar = document.createElement('div'); bar.className = 'session-manage-row';
    if (config.menu?.search !== false) {
      const wrap = document.createElement('span'); wrap.className = 'session-manage-search'; wrap.innerHTML = icon('search');
      const input = document.createElement('input'); input.className = 'session-manage-search-input'; input.placeholder = 'Search private titles and content…'; input.setAttribute('aria-label','Search private session titles and content'); input.value = query;
      input.oninput = () => { query = input.value; ++epoch; searching = false; hits = []; continuation = null; clearTimeout(timer); if (query.trim()) timer = delay(() => guard(() => search())(),250); else render(); };
      wrap.append(input); bar.append(wrap);
    }
    if (config.menu?.recycle_bin !== false) { const b = button(status === 'active' ? 'Recycle bin' : 'Back to active sessions','recycle',async () => { status = status === 'active' ? 'recycled' : 'active'; hits = []; continuation = null; await loadScope(); }); b.setAttribute('aria-pressed',String(status === 'recycled')); bar.append(b); }
    for (const [key,glyph,name] of [['hidden_sessions','eye-off','Hidden sessions'],['batch_delete','square','Batch recycle']]) { if (config.menu?.[key] === false) continue; const b = button(name,glyph,null); b.disabled = true; b.title = name + ' — unavailable in v3'; bar.append(b); }
    menu.append(bar); menu.style.setProperty('--session-menu-open-height',Math.min(menu._availableHeight || 480,menu.scrollHeight) + 'px');
    window.lucide?.createIcons();
    if (focus) { const input = menu.querySelector('input'); input?.focus(); if (position != null) input?.setSelectionRange(position,position); }
  }
  root.querySelector('#session-dropdown-kebab').onclick = e => { e.stopPropagation(); const item = items.find(s => s.id === selected); if (item) actions(item,e.currentTarget); };
  confirmButton(root.querySelector('#session-dropdown-delete'),'Recycle?',() => { const item = items.find(s => s.id === selected); return item ? mutate(item,'recycle') : undefined; });
  const tick = setInterval(() => { for (const node of [...menu.querySelectorAll('[data-ts]'),...root.querySelectorAll('[data-ts]')]) node.textContent = elapsed(Number(node.dataset.ts)); },1000);
  return {
    update(rows,id) { items = rows; selected = id; label(); render(); },
    configure(cfg) { config = cfg || {}; menu.style.maxHeight = config.menu?.max_height || ''; label(); render(); },
    reset() { ++epoch; clearTimeout(timer); query = ''; hits = []; continuation = null; searching = false; searchInfo = ''; status = 'active'; },
    destroy() { destroyed = true; ++epoch; clearTimeout(timer); clearInterval(tick); for (const t of timers) clearTimeout(t); abort.abort(); editing?.remove(); root.querySelector('#session-dropdown-kebab').onclick = null; root.querySelector('#session-dropdown-delete').onclick = null; }
  };
}
