

WebagentGenui.register(function (root, api) {
  const $ = (id) => root.getElementById(id);

  const ICONS = {
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>',
    agents: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="18" height="13" rx="3"/><circle cx="8.5" cy="13.5" r="1.2" fill="currentColor"/><circle cx="15.5" cy="13.5" r="1.2" fill="currentColor"/><path d="M9 17h6"/><path d="M12 3v4"/></svg>',
    sessions: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
    browser: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
    genui: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/><path d="M7 14l2.5-2.5L12 14l3-3 2 2"/></svg>',
    widget: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    coins: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18"/><path d="M7 6h1v4"/><path d="M16.71 13.88l.7.71-2.82 2.82"/></svg>',
    database: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
    arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>',
    server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7M21 4v4h-4"/></svg>',
    dollar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>',
    layers: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
    activity: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    zap: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>'
  };

  const ST = {
    todo:     { label: 'Not started', cls: 'st-todo' },
    progress: { label: 'In progress', cls: 'st-progress' },
    done:     { label: 'Resolved',    cls: 'st-done' },
    blocked:  { label: 'Blocked',     cls: 'st-blocked' }
  };
  const ORDER = ['todo', 'progress', 'done', 'blocked'];
  const TAG = { critical: 'tag-crit', high: 'tag-high', medium: 'tag-med', low: 'tag-low' };
  const LS_KEY = 'prodread-status-v1';
  const LS_COST = 'prodread-cost-v1';

  let data = {};
  try { data = api.getData() || {}; } catch (_) {}
  const sections = Array.isArray(data.sections) ? data.sections : [];
  const security = Array.isArray(data.security) ? data.security : [];
  const database = (data.database && Array.isArray(data.database.items)) ? data.database : { items: [] };
  const dbItems = Array.isArray(database.items) ? database.items : [];

  const allItems = [];
  sections.forEach(s => (s.items || []).forEach(it => { it._section = s.id; allItems.push(it); }));
  security.forEach(it => { it._section = 'security'; allItems.push(it); });
  dbItems.forEach(it => { it._section = 'database'; allItems.push(it); });

  let overlay = {};
  try { overlay = JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch (_) {}
  const storeOverlay = () => { try { localStorage.setItem(LS_KEY, JSON.stringify(overlay)); } catch (_) {} };

  const statusOf = (it) => overlay[it.id] || it.status || 'todo';
  const setStatus = (it, st) => { overlay[it.id] = st; storeOverlay(); };

  let filter = 'all';
  let selectedSurfaces = new Set();  // empty = nothing shown; click cards to reveal their checklists
  const visible = (it) => filter === 'all' || (filter === 'done' ? statusOf(it) === 'done' : statusOf(it) !== 'done');

  // ── Cost state ──
  const C = (data.cost && data.cost.free) || {};
  const SEED = {
    active_users: num(C.active_users, 1000),
    sessions_per_user_month: num(C.sessions_per_user_month, 15),
    avg_input_tokens: num(C.avg_input_tokens, 4000),
    avg_output_tokens: num(C.avg_output_tokens, 2000),
    input_price: num(C.input_price_per_mtok, 1.00),
    output_price: num(C.output_price_per_mtok, 5.00),
    fixed_infra: num(C.fixed_infra_month, 50),
    storage_mb_per_user: num(C.storage_mb_per_user, 20),
    storage_gb_price: num(C.storage_gb_price, 0.10)
  };
  let costOverrides = {};
  try { costOverrides = JSON.parse(localStorage.getItem(LS_COST) || '{}'); } catch (_) {}
  const costState = Object.assign({}, SEED, costOverrides);
  const saveCost = () => { try { localStorage.setItem(LS_COST, JSON.stringify(costOverrides)); } catch (_) {} };
  const sessionTypes = (data.cost && Array.isArray(data.cost.session_types)) ? data.cost.session_types : [];
  const tierDefs = (data.cost && data.cost.pricing && Array.isArray(data.cost.pricing.tiers)) ? data.cost.pricing.tiers : [];

  // ── Helpers ──
  function num(v, d) { const n = parseFloat(v); return isFinite(n) ? n : d; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function fmtUSD(n) {
    n = num(n, 0);
    if (n >= 1000) return '$' + (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
    if (n >= 1) return '$' + n.toFixed(2);
    if (n >= 0.01) return '$' + n.toFixed(3);
    return '$' + n.toFixed(4);
  }
  function marginClass(m) { return m >= 50 ? 'tm-good' : m >= 25 ? 'tm-mid' : m >= 0 ? 'tm-thin' : 'tm-bad'; }

  function statusChip(it) {
    const st = statusOf(it);
    const stc = ST[st] || ST.todo;
    return '<button class="st ' + stc.cls + '" data-cycle="' + esc(it.id) + '" title="Click to change status">' +
      '<span class="dot"></span><span class="st-label">' + stc.label + '</span></button>';
  }
  function itemRow(it) {
    return '<div class="item" data-id="' + esc(it.id) + '">' + statusChip(it) +
      '<div class="i-body">' +
        '<div class="i-top"><span class="i-title">' + esc(it.title) + '</span>' +
          (it.priority && TAG[it.priority] ? '<span class="tag ' + TAG[it.priority] + '">' + esc(it.priority) + '</span>' : '') +
        '</div>' +
        (it.desc ? '<div class="i-desc">' + esc(it.desc) + '</div>' : '') +
        (it.loc ? '<div class="i-meta"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="3"/></svg>' + esc(it.loc) + '</div>' : '') +
      '</div></div>';
  }
  function wireStatus(container) {
    container.querySelectorAll('.st[data-cycle]').forEach(b => b.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = b.dataset.cycle;
      const it = allItems.find(x => x.id === id);
      if (!it) return;
      const next = ORDER[(ORDER.indexOf(statusOf(it)) + 1) % ORDER.length];
      setStatus(it, next);
      render();
    }));
  }
  function barChart(el, rows) {
    if (!el) return;
    const max = Math.max.apply(null, rows.map(r => r.value).concat([0.0001]));
    el.innerHTML = rows.map(r =>
      '<div class="bar-row">' +
        '<div class="bar-label">' + esc(r.label) + '</div>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + (r.value / max * 100).toFixed(1) + '%;background:' + r.color + '"></div></div>' +
        '<div class="bar-value">' + fmtUSD(r.value) + '</div>' +
      '</div>'
    ).join('');
  }

  // ── Render ──
  function render() {
    renderHeader();
    renderOverview();
    if (selectedSurfaces.size === 0) {
      // nothing selected — show placeholder, hide all content + area headings
      $('sections').innerHTML = '<div class="empty-note" style="padding:40px 20px;text-align:center"><div style="font-size:15px;font-weight:620;color:var(--fg-2,#b0b0b0);margin-bottom:6px">Select cards above to see their checklists</div><div style="font-size:12px;color:var(--fg-3,#8a8a8a)">Click one or more surfaces to reveal their readiness tasks, security controls, cost model, or database architecture.</div></div>';
      ['security','cost','database'].forEach(id => { const el = $('surface-' + id); if (el) el.style.display = 'none'; });
      if ($('secGrid')) $('secGrid').innerHTML = '';
      if ($('dbWrap')) $('dbWrap').innerHTML = '';
      if ($('costWrap')) $('costWrap').innerHTML = '';
      return;
    }
    // Show only the sections whose cards are selected
    const showSurfaces = [];
    sections.forEach(s => { if (selectedSurfaces.has(s.id)) showSurfaces.push(s); });
    if (showSurfaces.length > 0) {
      renderSelectedSurfaces(showSurfaces);
    } else {
      $('sections').innerHTML = '';
    }
    // Analysis-area headings are static HTML — hide the whole wrapper unless toggled
    ['security','cost','database'].forEach(id => {
      const el = $('surface-' + id);
      if (el) el.style.display = selectedSurfaces.has(id) ? '' : 'none';
    });
    if (selectedSurfaces.has('security')) renderSecurity(); else if ($('secGrid')) $('secGrid').innerHTML = '';
    if (selectedSurfaces.has('database')) renderDatabase(); else if ($('dbWrap')) $('dbWrap').innerHTML = '';
    if (selectedSurfaces.has('cost')) renderCost(); else if ($('costWrap')) $('costWrap').innerHTML = '';
  }

  function renderHeader() {
    const total = allItems.length;
    let done = 0, prog = 0, left = 0;
    allItems.forEach(it => {
      const s = statusOf(it);
      if (s === 'done') done++; else if (s === 'progress') prog++; else left++;
    });
    // KPI cards were removed from the header; the status chip is the only
    // remaining header surface. Guard every lookup so the toolbar works with
    // or without the old KPI markup.
    const setTxt = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    setTxt('kTotal', total);
    setTxt('kDone', done);
    setTxt('kProg', prog);
    setTxt('kLeft', left);
    setTxt('statusText', total ? (done === total ? 'Ready · all resolved' : 'In progress · ' + prog) : 'No data');
    const badge = $('statusBadge');
    if (badge) {
      const ready = done === total && total;
      badge.style.background = ready ? 'var(--success-soft, rgba(76,175,125,.13))' : 'var(--warning-soft, rgba(255,180,50,.13))';
      badge.style.color = ready ? 'var(--success, #4caf7d)' : 'var(--warning, #ffb432)';
      badge.style.borderColor = ready ? 'var(--success-mid, rgba(76,175,125,.3))' : 'var(--warning-mid, rgba(255,180,50,.28))';
    }
    setTxt('updatedAt', data.updatedAt ? 'Data seeded ' + data.updatedAt : '');
  }

  function renderOverview() {
    const wrap = $('overview');
    wrap.innerHTML = '';
    const allIds = sections.map(s => s.id).concat(['security','cost','database']);
    // Build DOM directly so active classes come from the Set (no escaping in strings)
    sections.forEach(s => {
      const items = s.items || [];
      const done = items.filter(it => statusOf(it) === 'done').length;
      const pct = items.length ? Math.round(done / items.length * 100) : 0;
      const card = document.createElement('div');
      card.className = 'ov-card' + (selectedSurfaces.has(s.id) ? ' active' : '');
      card.dataset.go = s.id;
      card.innerHTML = '<div class="ov-top"><div class="ov-ic">' + (ICONS[s.icon] || ICONS.chat) + '</div><div class="ov-name">' + esc(s.name) + '</div></div>' +
        '<div class="ov-bar"><div class="ov-fill' + (pct === 100 && items.length ? ' full' : '') + '" style="width:' + pct + '%"></div></div>' +
        '<div class="ov-meta"><span>' + done + '/' + items.length + ' done</span><span>' + pct + '%</span></div>';
      wrap.appendChild(card);
    });
    // Security card
    (function(){
      const done = security.filter(it => statusOf(it) === 'done').length;
      const card = document.createElement('div');
      card.className = 'ov-card area' + (selectedSurfaces.has('security') ? ' active' : '');
      card.dataset.go = 'security';
      card.innerHTML = '<div class="ov-top"><div class="ov-ic">' + ICONS.shield + '</div><div class="ov-name">Security &amp; Compliance</div></div><div class="ov-meta"><span>' + done + '/' + security.length + ' done</span><span>controls</span></div>';
      wrap.appendChild(card);
    })();
    // Cost card
    (function(){
      const card = document.createElement('div');
      card.className = 'ov-card area' + (selectedSurfaces.has('cost') ? ' active' : '');
      card.dataset.go = 'cost';
      card.innerHTML = '<div class="ov-top"><div class="ov-ic">' + ICONS.coins + '</div><div class="ov-name">Cost Analysis</div></div><div class="ov-meta"><span>free-tier model</span><span>pricing</span></div>';
      wrap.appendChild(card);
    })();
    // Database card
    (function(){
      const done = dbItems.filter(it => statusOf(it) === 'done').length;
      const card = document.createElement('div');
      card.className = 'ov-card area' + (selectedSurfaces.has('database') ? ' active' : '');
      card.dataset.go = 'database';
      card.innerHTML = '<div class="ov-top"><div class="ov-ic">' + ICONS.database + '</div><div class="ov-name">Database Readiness</div></div><div class="ov-meta"><span>' + done + '/' + dbItems.length + ' done</span><span>architecture</span></div>';
      wrap.appendChild(card);
    })();
    // Card clicks are delegated once on the rail in wireOverviewCarousel(),
    // so drag-to-scroll can suppress a trailing click without re-wiring.
  }

  // ── Overview carousel ──
  // One nowrap horizontal row with edge fades + chevrons (mirrors the app's
  // Instances carousel). Chevrons page by one card; drag-to-scroll; a drag
  // never toggles a card (its trailing click is suppressed). Wired once —
  // renderOverview() only rebuilds the cards inside #overview, so this
  // survives every re-render.
  function wireOverviewCarousel() {
    const wrap = $('ovWrap');
    const rail = $('overview');
    const prev = $('ovPrev');
    const next = $('ovNext');
    if (!wrap || !rail) return;

    // Card toggle, delegated on the rail so clicks work even after a drag
    // (the click target can be the rail itself, not the card).
    rail.addEventListener('click', (e) => {
      const card = e.target.closest('.ov-card');
      if (!card) return;
      e.stopPropagation();
      e.preventDefault();
      const id = card.dataset.go;
      if (selectedSurfaces.has(id)) { selectedSurfaces.delete(id); }
      else { selectedSurfaces.add(id); }
      render();
    });

    let dragged = false, startX = 0, startLeft = 0, raf = 0;

    const updateOv = () => {
      const max = rail.scrollWidth - rail.clientWidth;
      wrap.classList.toggle('can-scroll-left', rail.scrollLeft > 4);
      wrap.classList.toggle('can-scroll-right', rail.scrollLeft < max - 4);
    };
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; updateOv(); }); };

    const page = (dir) => {
      const card = rail.querySelector('.ov-card');
      const step = (card ? card.offsetWidth + 12 : 220) * dir;
      rail.scrollBy({ left: step, behavior: 'smooth' });
    };
    if (prev) prev.addEventListener('click', (e) => { e.preventDefault(); page(-1); });
    if (next) next.addEventListener('click', (e) => { e.preventDefault(); page(1); });

    // Drag-to-scroll. Move/up are tracked on the shadow root (no pointer
    // capture), so a clean tap still lands on the card and toggles it, while
    // a swipe scrolls and suppresses the trailing click.
    const onMove = (e) => {
      if (e.buttons === 0) { dragged = false; return; }  // release happened outside the genui
      if (!dragged && Math.abs(e.clientX - startX) > 5) dragged = true;
      if (dragged) rail.scrollLeft = startLeft - (e.clientX - startX);
    };
    const onUp = (e) => {
      root.removeEventListener('pointermove', onMove);
      root.removeEventListener('pointerup', onUp);
      root.removeEventListener('pointercancel', onUp);
      if (dragged) {
        const suppress = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
        rail.addEventListener('click', suppress, { capture: true, once: true });
        // If the release happened outside the genui the trailing click never
        // reaches the rail — drop the suppressor so it can't eat the next tap.
        setTimeout(() => rail.removeEventListener('click', suppress, { capture: true }), 600);
        dragged = false;
      }
    };
    rail.addEventListener('pointerdown', (e) => {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      dragged = false; startX = e.clientX; startLeft = rail.scrollLeft;
      root.addEventListener('pointermove', onMove, { passive: true });
      root.addEventListener('pointerup', onUp);
      root.addEventListener('pointercancel', onUp);
    });

    rail.addEventListener('scroll', onScroll, { passive: true });
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(updateOv);
      ro.observe(rail);
    }
    updateOv();
  }

  function renderSelectedSurfaces(selectedList) {
    const wrap = $('sections');
    wrap.innerHTML = selectedList.map(s => {
      const items = (s.items || []).filter(visible);
      const all = s.items || [];
      const done = all.filter(it => statusOf(it) === 'done').length;
      const pct = all.length ? Math.round(done / all.length * 100) : 0;
      const list = items.length ? items.map(itemRow).join('') : '<div class="empty-note">No items match this filter.</div>';
      return '<div class="surface" id="surface-' + esc(s.id) + '">' +
        '<div class="surface-head" data-toggle="' + esc(s.id) + '">' +
          '<div class="s-icon">' + (ICONS[s.icon] || ICONS.chat) + '</div>' +
          '<div class="s-title"><div class="s-name">' + esc(s.name) +
            (s.loc ? '<span class="s-loc">' + esc(s.loc) + '</span>' : '') +
          '</div></div>' +
          '<div class="s-prog">' +
            '<div class="s-count"><b>' + done + '</b>/' + all.length + ' resolved</div>' +
            '<div class="s-bar"><div class="s-fill' + (pct === 100 && all.length ? ' full' : '') + '" style="width:' + pct + '%"></div></div>' +
          '</div>' +
          '<div class="s-chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></div>' +
        '</div>' +
        '<div class="items">' + list + '</div>' +
      '</div>';
    }).join('');
    wireSections(wrap);
  }

  function renderSections() {
    const wrap = $('sections');
    wrap.innerHTML = sections.map(s => {
      const items = (s.items || []).filter(visible);
      const all = s.items || [];
      const done = all.filter(it => statusOf(it) === 'done').length;
      const pct = all.length ? Math.round(done / all.length * 100) : 0;
      const list = items.length ? items.map(itemRow).join('') : '<div class="empty-note">No items match this filter.</div>';
      return '<div class="surface" id="surface-' + esc(s.id) + '">' +
        '<div class="surface-head" data-toggle="' + esc(s.id) + '">' +
          '<div class="s-icon">' + (ICONS[s.icon] || ICONS.chat) + '</div>' +
          '<div class="s-title"><div class="s-name">' + esc(s.name) +
            (s.loc ? '<span class="s-loc">' + esc(s.loc) + '</span>' : '') +
          '</div></div>' +
          '<div class="s-prog">' +
            '<div class="s-count"><b>' + done + '</b>/' + all.length + ' resolved</div>' +
            '<div class="s-bar"><div class="s-fill' + (pct === 100 && all.length ? ' full' : '') + '" style="width:' + pct + '%"></div></div>' +
          '</div>' +
          '<div class="s-chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></div>' +
        '</div>' +
        '<div class="items">' + list + '</div>' +
      '</div>';
    }).join('');
    wireSections(wrap);
  }

  function wireSections(wrap) {
    wrap.querySelectorAll('.surface-head').forEach(h => h.addEventListener('click', () => {
      h.closest('.surface').classList.toggle('collapsed');
    }));
    wireStatus(wrap);
  }

  function renderSecurity() {
    const wrap = $('secGrid');
    if (!wrap) return;
    const items = security.filter(visible);
    const done = security.filter(it => statusOf(it) === 'done').length;
    $('secCount').textContent = done + '/' + security.length + ' resolved';
    wrap.innerHTML = items.length ? items.map(it =>
      '<div class="sec-card" data-id="' + esc(it.id) + '">' +
        '<div class="sec-top"><span class="sec-group">' + esc(it.group || 'Control') + '</span>' + statusChip(it) + '</div>' +
        '<div class="sec-title">' + esc(it.title) + '</div>' +
        (it.desc ? '<div class="sec-desc">' + esc(it.desc) + '</div>' : '') +
        (it.loc ? '<div class="sec-loc">' + esc(it.loc) + '</div>' : '') +
      '</div>'
    ).join('') : '<div class="empty-note" style="grid-column:1/-1">No items match this filter.</div>';
    wireStatus(wrap);
  }

  function renderDatabase() {
    const wrap = $('dbWrap');
    if (!wrap) return;
    const stores = Array.isArray(database.stores) ? database.stores : [];
    const items = dbItems.filter(visible);
    const done = dbItems.filter(it => statusOf(it) === 'done').length;
    $('dbCount').textContent = done + '/' + dbItems.length + ' resolved';

    const cache = database.cache || {};
    const authority = database.authority || {};
    const flow = [
      { role: 'Server cache', name: cache.name || 'SQLite (server-local)', desc: cache.desc || 'Fast local reads + writes for the main path', loc: cache.loc || 'app/db/local.py', icon: 'server' },
      { role: 'Sync', name: 'Sync worker', desc: 'Idempotent upserts, batch flush, retry + backoff', loc: 'app/db/sync', icon: 'refresh' },
      { role: 'Authority', name: authority.name || 'PostgreSQL (remote)', desc: authority.desc || 'Source of truth — instance data mirrored remotely, survives server loss', loc: authority.loc || 'app/db/postgres_backend.py', icon: 'database', cls: 'fc-auth' }
    ];

    wrap.innerHTML =
      '<div class="db-flow">' + flow.map((f, i) =>
        (i > 0 ? '<div class="db-arrow">' + ICONS.arrow + '</div>' : '') +
        '<div class="db-node ' + (f.cls || '') + '">' + (ICONS[f.icon] || ICONS.database) +
          '<div class="db-role">' + f.role + '</div>' +
          '<div class="db-name">' + esc(f.name) + '</div>' +
          '<div class="db-desc">' + esc(f.desc) + '</div>' +
          '<div class="db-loc">' + esc(f.loc) + '</div>' +
        '</div>'
      ).join('') + '</div>' +
      '<div class="cost-title">' + ICONS.layers + ' Database segmentation — separate stores per domain' +
        '<span class="cost-sub">main path: SQLite on server · authority: Postgres remote</span></div>' +
      '<div class="stores-grid">' + (stores.length ? stores.map(s =>
        '<div class="store"><div class="st-name">' + esc(s.name) + '<span class="sot sot-' + esc(s.sot || 'local') + '">' + esc(s.sotLabel || s.sot || 'local') + '</span></div>' +
        '<div class="st-scope">' + esc(s.scope) + '</div>' +
        '<div class="st-file">' + esc(s.file) + '</div></div>'
      ).join('') : '<div class="empty-note" style="grid-column:1/-1">No stores defined.</div>') + '</div>' +
      '<div class="cost-title">' + ICONS.activity + ' Operational readiness</div>' +
      '<div class="db-checklist">' + (items.length ? items.map(itemRow).join('') : '<div class="empty-note">No items match this filter.</div>') + '</div>';
    wireStatus(wrap);
  }

  // ── Cost analysis ──
  // Assumption field definitions come from the data bag so an agent can add/remove fields
  // without touching this script. Shape: { k, label, step } per field.
  const ASSUME_FIELDS = (data.cost && Array.isArray(data.cost.assumption_fields) && data.cost.assumption_fields.length)
    ? data.cost.assumption_fields
    : [
    { k: 'active_users', label: 'Active free users', step: 100 },
    { k: 'sessions_per_user_month', label: 'Sessions / user / mo', step: 1 },
    { k: 'avg_input_tokens', label: 'Avg input tokens / session', step: 100 },
    { k: 'avg_output_tokens', label: 'Avg output tokens / session', step: 100 },
    { k: 'input_price', label: 'Input $ / 1M tokens', step: 0.05 },
    { k: 'output_price', label: 'Output $ / 1M tokens', step: 0.05 },
    { k: 'fixed_infra', label: 'Fixed infra $ / month', step: 5 },
    { k: 'storage_mb_per_user', label: 'Storage MB / user', step: 5 },
    { k: 'storage_gb_price', label: 'Storage $ / GB / mo', step: 0.01 }
  ];

  function kpiCard(label, value, sub) {
    return '<div class="cost-kpi"><div class="ck-label">' + label + '</div><div class="ck-value">' + value + '</div><div class="ck-sub">' + esc(sub) + '</div></div>';
  }

  function buildAssumptions() {
    const grid = $('assumeGrid');
    if (!grid) return;
    grid.innerHTML = ASSUME_FIELDS.map(f =>
      '<div class="assume"><label>' + f.label + '</label>' +
      '<input class="num-input" type="number" data-k="' + f.k + '" step="' + f.step + '" value="' + costState[f.k] + '"></div>'
    ).join('');
    grid.querySelectorAll('.num-input').forEach(inp => inp.addEventListener('input', () => {
      const v = parseFloat(inp.value);
      if (!isFinite(v)) return;
      costState[inp.dataset.k] = v;
      costOverrides[inp.dataset.k] = v;
      saveCost();
      recomputeCost();
    }));
  }

  function recomputeCost() {
    const c = costState;
    const perSession = (c.avg_input_tokens / 1e6) * c.input_price + (c.avg_output_tokens / 1e6) * c.output_price;
    const perUserMonth = perSession * c.sessions_per_user_month;
    const modelCost = perUserMonth * c.active_users;
    const storageGB = (c.active_users * c.storage_mb_per_user) / 1024;
    const storageCost = storageGB * c.storage_gb_price;
    const total = modelCost + storageCost + c.fixed_infra;
    const perUser = c.active_users ? total / c.active_users : 0;

    const kpis = $('costKpis');
    if (kpis) kpis.innerHTML =
      kpiCard('Cost / user / mo', fmtUSD(perUser), 'total incl. infra') +
      kpiCard('Cost / session', fmtUSD(perSession), 'avg across free usage') +
      kpiCard('Model cost / mo', fmtUSD(modelCost), c.active_users + ' users x ' + c.sessions_per_user_month + ' sessions') +
      kpiCard('Total / month', fmtUSD(total), fmtUSD(storageCost) + ' storage · ' + fmtUSD(c.fixed_infra) + ' infra');

    barChart($('chartBreakdown'), [
      { label: 'Model tokens', value: modelCost, color: 'var(--accent)' },
      { label: 'Storage', value: storageCost, color: 'var(--purple)' },
      { label: 'Fixed infra', value: c.fixed_infra, color: 'var(--warning)' }
    ]);

    const colors = ['var(--success)', 'var(--accent)', 'var(--warning)', 'var(--danger)', 'var(--purple)'];
    barChart($('chartSessions'), sessionTypes.map((t, i) => ({
      label: t.name,
      value: (t.input_tokens / 1e6) * c.input_price + (t.output_tokens / 1e6) * c.output_price,
      color: colors[i % colors.length]
    })));

    const tiers = $('tierGrid');
    if (tiers) tiers.innerHTML = tierDefs.map(t => {
      const cost = t.sessions * ((t.input_tokens / 1e6) * c.input_price + (t.output_tokens / 1e6) * c.output_price);
      const custom = !!t.custom;
      const margin = custom || t.price <= 0 ? null : (t.price - cost) / t.price * 100;
      const priceStr = custom ? 'Custom' : (t.price <= 0 ? 'Free' : '$' + t.price + '/mo');
      const marginStr = margin == null ? (custom ? '—' : 'subsidized') : Math.round(margin) + '%';
      const mcls = margin == null ? 'tm-mid' : marginClass(margin);
      return '<div class="tier">' +
        '<div class="t-name">' + esc(t.name) + '</div>' +
        '<div class="t-price">' + priceStr + '</div>' +
        '<div class="t-note">' + esc(t.note || '') + '</div>' +
        '<div class="t-row"><span>Cost to serve</span><span>' + fmtUSD(cost) + '/mo</span></div>' +
        '<div class="t-row"><span>Margin</span><span class="t-margin ' + mcls + '">' + marginStr + '</span></div>' +
        '<div class="t-row"><span>Included</span><span>' + t.sessions + ' sessions/mo</span></div>' +
      '</div>';
    }).join('');
  }

  function renderCost() {
    const wrap = $('costWrap');
    if (!wrap) return;
    wrap.innerHTML =
      '<div class="cost-card wide"><div class="cost-title">' + ICONS.dollar + ' Free tier economics — cost to serve</div>' +
        '<div class="cost-kpis" id="costKpis"></div></div>' +
      '<div class="cost-card"><div class="cost-title">' + ICONS.layers + ' Assumptions <button class="mini-btn" id="costReset">Reset to defaults</button></div>' +
        '<div class="assume-grid" id="assumeGrid"></div></div>' +
      '<div class="cost-card"><div class="cost-title">' + ICONS.activity + ' Monthly cost breakdown <span class="cost-sub">free tier</span></div>' +
        '<div id="chartBreakdown"></div></div>' +
      '<div class="cost-card"><div class="cost-title">' + ICONS.zap + ' Cost per session by complexity <span class="cost-sub">' + esc(((data.cost && data.cost.model && data.cost.model.name) || 'blended model')) + '</span></div>' +
        '<div id="chartSessions"></div></div>' +
      '<div class="cost-card wide"><div class="cost-title">' + ICONS.coins + ' Pricing strategy — cost vs price by tier</div>' +
        '<div class="tier-grid" id="tierGrid"></div></div>';
    const reset = $('costReset');
    if (reset) reset.addEventListener('click', () => {
      costOverrides = {};
      saveCost();
      Object.keys(SEED).forEach(k => costState[k] = SEED[k]);
      buildAssumptions();
      recomputeCost();
    });
    buildAssumptions();
    recomputeCost();
  }

  // ── Filters + reset + boot ──
  $('filters').querySelectorAll('.filter-btn').forEach(b => b.addEventListener('click', () => {
    $('filters').querySelectorAll('.filter-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    filter = b.dataset.f;
    render();
  }));

  $('resetBtn').addEventListener('click', () => {
    overlay = {};
    storeOverlay();
    render();
  });

  // ── Header chat pill ──
  // Compact inline one-shot: user types a task, hits send (or Enter), the agent
  // processes it, and the genui re-renders with results.  Configured via
  // chatConfig.pill in the data bag (agentId, prompt).
  const hPill = $('hPill');
  const hPillInput = $('hPillInput');
  const hPillSend = $('hPillSend');
  if (hPill && hPillInput && hPillSend) {
    const updateSend = () => { hPillSend.disabled = !hPillInput.value.trim(); };
    hPillInput.addEventListener('input', updateSend);
    hPillInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!hPillSend.disabled) hPillSend.click();
      }
    });
    let _sending = false;
    hPillSend.addEventListener('click', () => {
      const msg = hPillInput.value.trim();
      if (!msg || _sending) return;
      _sending = true;
      hPill.classList.add('h-pill-sending');
      hPillInput.disabled = true;
      hPillSend.disabled = true;
      hPillInput.value = '';
      hPillInput.placeholder = 'Working…';
      // The genui re-renders on completion so we don't need an explicit reset
      api.chat(msg);
    });
  }

  // ── Widget toggle ──
  // Delegates entirely to api.createChatButton(), which reads chatConfig from
  // the data bag.  When chatConfig.enabled is false the button returns null and
  // our header icon hides.  An agent can change the chat target, title, or icon
  // by editing the data bag — no JS changes needed.
  const widgetToggle = $('widgetToggle');
  if (widgetToggle && api.createChatButton) {
    const chatBtn = api.createChatButton();
    if (!chatBtn) {
      widgetToggle.style.display = 'none';
    } else {
      chatBtn.style.display = 'none';
      (root.host || root).appendChild(chatBtn);
      let _open = false;
      widgetToggle.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        chatBtn.click();
        _open = !_open;
        widgetToggle.classList.toggle('off', _open);
      });
      const mo = new MutationObserver(() => {
        const anyOpen = !!document.querySelector('.chat-widget:not([hidden])');
        if (!anyOpen) { _open = false; widgetToggle.classList.remove('off'); }
        else if (!_open) { _open = true; widgetToggle.classList.add('off'); }
      });
      mo.observe(document.body, { childList: true, subtree: true });
    }
  }

  render();
  wireOverviewCarousel();
});

