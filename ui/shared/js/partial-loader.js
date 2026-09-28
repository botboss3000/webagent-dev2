// Fetches HTML partials at boot and injects them into placeholder slots in
// index.html before the main JS modules run. Two-phase load:
//   Phase 1: top-level tab pages (Gen UI / Agents / Admin Tools) — each
//            partial's body replaces the inner HTML of its mount.
//   Phase 2: admin sub-pages — each partial uses <template data-slot="...">
//            blocks whose content is appended into the matching selector
//            in the parent document (the slots live inside the admin-tools
//            shell loaded in phase 1).
//
// Resolves the exported `partialsReady` promise once all partials are in
// the DOM, after which the bootstrap in index.html dynamically imports the
// main JS modules so every getElementById call still resolves.

// The top-level page partials are derived from the page catalog at boot (drop-in
// ui/<page>/page.json descriptors) rather than hardcoded — adding a page folder
// makes its partial load with no edit here. The chat side-panel is the one fixed
// (non-page) partial. Pages without an `html` (e.g. the iframe-only Terminal)
// contribute no partial; their content mount is built by __buildHeader.
const CHAT_PARTIAL = { url: './ui/chat/chat-side-panel.html', mount: '#chat-panel' };
const PRIVATE_ACCESS_PARTIAL = {
  url: './ui/private-access/private-access-page/private-access-page.html',
  mount: '#app-restricted-overlay',
};

function topLevelFromCatalog(mainPages) {
  const list = [];
  for (const p of (mainPages || [])) {
    if (p.html && p.mount && p.dir) {
      list.push({ url: `./ui/${p.dir}/${p.html}`, mount: p.mount, pageId: p.id });
    }
    // A main page may ALSO ship extra <template data-slot> partials (its page.json
    // `partials` list, e.g. Instances' App Config sections). These are injected
    // with the same slot-appending used for admin sub-page partials, after the
    // page's own html has been mounted (see injectSlotsFromHtml in phase 1).
    for (const extra of (p.partials || [])) {
      if (extra) list.push({ url: `./ui/${p.dir}/${extra}`, slot: true, pageId: p.id });
    }
  }
  list.push({ ...CHAT_PARTIAL, pageId: '__chat__' });
  return list;
}

async function loadTopLevelPartials(entries) {
  const html = await Promise.all(entries.map((p) => fetchHtml(p.url)));
  entries.forEach((p, i) => {
    if (p.slot) {
      if (document.querySelector(`[data-page-partial="${p.url}"]`)) return;
      injectSlotsFromHtml(html[i], p.url);
      const marker = document.createElement('span');
      marker.hidden = true;
      marker.dataset.pagePartial = p.url;
      document.body.appendChild(marker);
    } else {
      injectIntoMount(html[i], p.mount);
      const mount = document.querySelector(p.mount);
      if (mount) mount.dataset.pagePartial = p.url;
    }
  });
}

const _pageHydration = new Map();

function catalogPage(pageId, catalog) {
  const pages = catalog && Array.isArray(catalog.main) ? catalog.main : [];
  return pages.find((page) => page && page.id === pageId) || null;
}

function pagePartialEntries(page) {
  if (!page) return [];
  return topLevelFromCatalog([page]).filter((entry) => entry.pageId !== '__chat__');
}

function ensurePagePartial(pageId, catalog = window.__pagesCatalog) {
  const page = catalogPage(pageId, catalog);
  if (!page) return Promise.resolve();
  const existing = _pageHydration.get(pageId);
  if (existing) return existing;
  const pending = loadTopLevelPartials(pagePartialEntries(page))
    .then(async () => {
      if (pageId !== 'admin-tools') return;
      const adminUrls = ADMIN_SUB_PAGES.concat(dropInAdminPartials(catalog.admin));
      const subHtml = await Promise.all(adminUrls.map(fetchHtml));
      adminUrls.forEach((url, index) => injectSlotsFromHtml(subHtml[index], url));
    })
    .catch((error) => {
      _pageHydration.delete(pageId);
      throw error;
    });
  _pageHydration.set(pageId, pending);
  return pending;
}

// tabs.js calls this before starting a dynamically imported page module. This
// keeps inactive page HTML off the startup critical path without allowing a
// page's initializer to race its own DOM.
window.__ensurePagePartial = (pageId) => ensurePagePartial(pageId);

// Load the Admin Tools SUB-VIEW partials (Explorer / Git / Terminal / Database /
// Settings / Interaction / Runtime Loop / …) into their slots inside
// admin-tools.html (#admin-tools main areas + #files-sidebar panels).
// ensurePagePartial('admin-tools') gates this on admin-tools being a main-catalog
// page; Admin Tools is now an Instances TAB, so it is no longer in the main
// catalog — this helper lets the nested hub load its sub-view panels directly.
window.__ensureAdminSubPages = async function () {
  // Idempotent: a mounted hub already has its sub-view <main>s injected. The
  // nested Admin Tools tab re-runs this on every tile re-render, so guard
  // against appending duplicate panels.
  if (document.querySelector('#admin-tools .files-main[data-view]')) return;
  let catalog = window.__pagesCatalog;
  if ((!catalog || !catalog.admin) && typeof window.__loadPagesCatalog === 'function') {
    catalog = await window.__loadPagesCatalog();
  }
  const adminUrls = dropInAdminPartials(catalog && catalog.admin);
  const subHtml = await Promise.all(adminUrls.map(fetchHtml));
  adminUrls.forEach((url, index) => injectSlotsFromHtml(subHtml[index], url));
};

const ADMIN_SUB_PAGES = [
  // Empty: EVERY admin view now ships its partial inside its own
  // ui/admin-tools/<id>/ folder and loads from the catalog via
  // dropInAdminPartials(). Explorer + Terminal were the last fixed-path
  // built-ins; their HTML now lives in explorer/ and terminal/ like the rest.
  // (Their JS lifecycle is still the shared inline "Files" engine in files.js —
  //  that engine split is a separate, later phase — but nothing loads from a
  //  fixed root path any more.) A new admin view = drop a folder; no edit here.
];

// Every admin view is a DROP-IN: it carries its own partial(s) inside its
// ui/admin-tools/<id>/ folder and loads from its descriptor — so a new folder
// needs no edit here. Its main partial (`html`) must expose <template
// data-slot="#admin-tools"> (a <main class="files-main" id="files-<id>-main"
// data-view="<id>">) and optionally a <template data-slot="#files-sidebar">
// panel. A multi-section view (Settings) additionally lists section partials in
// its descriptor `partials` array; those load right after its `html`.
//
// This set is the escape hatch for any view whose partial should NOT be loaded
// from its folder here (it ships elsewhere). It is empty now that Explorer +
// Terminal moved their HTML into explorer/ and terminal/ — those two still
// share one inline JS engine (files.js), but their partials load like any other.
const BUILTIN_ADMIN_IDS = new Set([]);

function dropInAdminPartials(adminPages) {
  const urls = [];
  for (const p of (adminPages || [])) {
    if (BUILTIN_ADMIN_IDS.has(p.id)) continue;
    if (!p.dir) continue;
    // The page's own partial first (it creates any slot targets its section
    // partials append into), then each extra partial in declared order.
    if (p.html) urls.push(`./ui/${p.dir}/${p.html}`);
    for (const extra of (p.partials || [])) {
      if (extra) urls.push(`./ui/${p.dir}/${extra}`);
    }
  }
  return urls;
}

// Import + start each `kind:"splash"` plugin from the catalog. Mirrors the page
// lifecycle dispatch in ui/shared/js/tabs.js: a dynamic import of the descriptor's
// `entry` module, then a call to its `start` export. The welcome screen is now a
// server-rendered landing page at / (app/main.py), so `start` no longer mounts an
// overlay — it just exposes window.WA_SPLASH for the account "Show welcome" toggle.
// Resolves the entry path against document.baseURI (entries are repo-root-relative,
// e.g. "ui/splash/splash-page/js/splash-page.js"). Per-plugin failures are isolated.
function bootSplashPlugins(splashPages) {
  for (const p of (splashPages || [])) {
    if (!p || !p.entry || !p.start) continue;
    try {
      import(new URL(p.entry, document.baseURI).href)
        .then((m) => { const fn = m && m[p.start]; if (typeof fn === 'function') fn(p); })
        .catch((e) => console.error('splash start ' + p.id + ' failed', e));
    } catch (e) {
      console.error('splash import ' + p.id + ' failed', e);
    }
  }
}

let _serverDownWarned = false;

async function fetchHtml(url) {
  let resp;
  try {
    resp = await fetch(url, { cache: 'no-store' });
  } catch (err) {
    // Connection refused / network down — every partial will fail with the same
    // cause, so emit a single clear message instead of one per partial.
    if (!_serverDownWarned) {
      _serverDownWarned = true;
      const host = location.host || 'localhost:8080';
      console.error(
        `[WebAgent] Cannot reach the backend at ${host}.`,
        '\n  The server appears to be down — start it (e.g. `python -m app.main`) then refresh the page.'
      );
    }
    // A previously visited partial may be present even when an older service
    // worker did not intercept this request. Read CacheStorage directly so an
    // upgrade to offline-reader support does not require two online reloads.
    try {
      const cached = await caches.match(new URL(url, document.baseURI).href);
      if (cached) return cached.text();
    } catch (_) { /* CacheStorage unavailable — preserve the original error */ }
    throw err;
  }
  if (!resp.ok) {
    try {
      const cached = await caches.match(new URL(url, document.baseURI).href);
      if (cached) return cached.text();
    } catch (_) { /* fall through */ }
    throw new Error(`Failed to fetch ${url}: ${resp.status}`);
  }
  return resp.text();
}

function injectIntoMount(html, mountSelector) {
  const mount = document.querySelector(mountSelector);
  if (!mount) throw new Error(`Mount not found: ${mountSelector}`);
  mount.innerHTML = html;
}

function injectSlotsFromHtml(html, sourceUrl) {
  // Parse as a document fragment to access top-level <template data-slot>.
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  const templates = wrapper.querySelectorAll('template[data-slot]');
  if (!templates.length) {
    throw new Error(`No <template data-slot> blocks found in ${sourceUrl}`);
  }
  for (const tpl of templates) {
    const sel = tpl.getAttribute('data-slot');
    const target = document.querySelector(sel);
    if (!target) {
      throw new Error(`Slot target "${sel}" not found (from ${sourceUrl})`);
    }
    target.appendChild(tpl.content.cloneNode(true));
  }
}

async function hydrateLateMainPages(catalog) {
  if (!catalog || !Array.isArray(catalog.main)) return;
  if (typeof window.__ensurePageStyles === 'function') window.__ensurePageStyles(catalog);
  if (typeof window.__buildHeader === 'function') window.__buildHeader(catalog.main);
  // Hydrate each page independently and preserve descriptor order within it:
  // its main HTML creates the targets used by its extra slot partials. One slow
  // or broken optional page must never prevent Agents/Instances from mounting.
  // Reconcile only pages already requested by this document. Hydrating every
  // catalog page here made the late authority response trigger another startup
  // request burst just as the user began interacting with the active page.
  const requestedId = new URLSearchParams(location.search).get('tab')
    || document.querySelector('.tab-content.active[id]')?.id?.replace(/^tab-/, '')
    || (catalog.main.some((page) => page && page.id === 'agents') ? 'agents' : catalog.main[0]?.id);
  const requestedPages = catalog.main.filter((page) => {
    if (!page) return false;
    const mount = page.mount && document.querySelector(page.mount);
    return page.id === requestedId || !!(mount && mount.dataset.pagePartial);
  });
  const results = await Promise.allSettled(requestedPages.map(async (page) => {
    if (!page || !page.dir) return;
    if (page.html && page.mount) {
      const url = `./ui/${page.dir}/${page.html}`;
      const mount = document.querySelector(page.mount);
      if (mount && mount.dataset.pagePartial !== url) {
        injectIntoMount(await fetchHtml(url), page.mount);
        mount.dataset.pagePartial = url;
        delete mount.dataset.minimumShell;
      }
    }
    for (const extra of (page.partials || [])) {
      if (!extra) continue;
      const url = `./ui/${page.dir}/${extra}`;
      if (document.querySelector(`[data-page-partial="${url}"]`)) continue;
      injectSlotsFromHtml(await fetchHtml(url), url);
      const marker = document.createElement('span');
      marker.hidden = true;
      marker.dataset.pagePartial = url;
      document.body.appendChild(marker);
    }
  }));
  results.forEach((result, index) => {
    if (result.status === 'rejected') {
      const page = requestedPages[index];
      console.error('late page hydration failed: ' + ((page && page.id) || index), result.reason);
    }
  });
}

function retryCatalogAfterCoreBoot() {
  // A cold process can serve the cached/read-only shell before its API routes
  // exist. Retry the authoritative catalog in the background so the page
  // upgrades itself without asking the user to refresh after the server arrives.
  const deadline = Date.now() + 120000;
  const retry = async () => {
    if (Date.now() > deadline || typeof window.__loadPagesCatalog !== 'function') return;
    try {
      const health = await fetch('/health', { cache: 'no-store' });
      const status = health.ok ? await health.json().catch(() => ({})) : {};
      if (status.initialization === 'starting') throw new Error('core still starting');
      const authoritativeCatalog = await window.__loadPagesCatalog();
      if (!authoritativeCatalog || !Array.isArray(authoritativeCatalog.main)) throw new Error('invalid catalog');
      if (!window.__pagesCatalogAuthoritative) throw new Error('catalog authority unavailable');
      await hydrateLateMainPages(authoritativeCatalog);
      window.dispatchEvent(new CustomEvent('pages-catalog-ready', {
        detail: { catalog: authoritativeCatalog },
      }));
    } catch (_) {
      setTimeout(retry, 1500);
    }
  };
  setTimeout(retry, 1000);
}

// A server can return long after the cold-start retry window. The health poll
// owns the cached-reader transition, so use that same transition to replace the
// durable snapshot with fresh authority without requiring a page reload.
window.__reconcilePagesCatalog = async function reconcilePagesCatalog() {
  if (window.__pagesCatalogAuthoritative) return window.__pagesCatalog;
  if (typeof window.__loadPagesCatalog !== 'function') {
    throw new Error('Page catalog loader unavailable');
  }
  const catalog = await window.__loadPagesCatalog();
  if (!window.__pagesCatalogAuthoritative) throw new Error('Catalog authority unavailable');
  await hydrateLateMainPages(catalog);
  window.dispatchEvent(new CustomEvent('pages-catalog-ready', {
    detail: { catalog },
  }));
  return catalog;
};

window.addEventListener('webagent-offline-readonly-changed', (event) => {
  if (event.detail?.active) {
    // The snapshot was authoritative when fetched, but it is only a durable
    // cached view for the duration of an outage. Mark it for mandatory
    // revalidation so reconnect cannot mistake pre-outage permissions for the
    // server's current catalog.
    window.__pagesCatalogAuthoritative = false;
    return;
  }
  window.__reconcilePagesCatalog().catch(() => {});
});

export const partialsReady = (async () => {
  // Phase 0: fetch the page catalog and (authoritatively) build the header tab
  // strip + content mounts from it before loading any partials. __buildHeader /
  // __loadPagesCatalog come from the classic header-build.js loaded earlier; if
  // it is somehow absent we fail closed until authority becomes reachable.
  let catalog = { main: [], admin: [] };
  let catalogPromise = null;
  try {
    catalogPromise = (typeof window.__loadPagesCatalog === 'function')
      ? window.__loadPagesCatalog()
      : (window.__pagesUnavailable ? window.__pagesUnavailable() : catalog);
    // Header pre-paint may already have established an identity-scoped cache.
    // Use it immediately instead of imposing a two-second
    // authority timeout on every cold backend start. The in-flight memoized
    // request still reconciles the authoritative catalog below.
    const cachedCatalog = window.__readPagesCache && window.__readPagesCache();
    catalog = cachedCatalog || (window.__pagesUnavailable
      ? window.__pagesUnavailable()
      : { main: [], admin: [], splash: [], _unavailable: true });
    window.__pagesCatalog = catalog;
    window.__catalogBootedFromCache = Boolean(cachedCatalog);
  } catch (_) {
    catalog = window.__pagesUnavailable
      ? window.__pagesUnavailable()
      : { main: [], admin: [], splash: [], _unavailable: true };
    window.__pagesCatalog = catalog;
    window.__catalogBootedFromCache = false;
    retryCatalogAfterCoreBoot();
  }
  // Inject each page's own stylesheet(s) from the (authoritative) catalog before
  // any partial HTML is mounted — so dropped-in pages style correctly with no
  // <link> edit to index.html. Idempotent with the pre-paint cache injection.
  if (typeof window.__ensurePageStyles === 'function') {
    try { window.__ensurePageStyles(catalog); } catch (e) { console.error('ensurePageStyles failed', e); }
  }
  if (typeof window.__buildHeader === 'function') {
    try { window.__buildHeader(catalog.main); } catch (e) { console.error('buildHeader failed', e); }
  }

  // Splash plugin (drop-in `kind:"splash"` — the welcome screen). It is NOT a tab
  // and NOT an admin view: its `entry` module is dynamically imported and its
  // `start` export is called. The welcome screen itself is now a server-rendered
  // landing page at / (app/main.py); `start` only exposes window.WA_SPLASH so the
  // account "Show welcome screen" toggle works. When no splash folder exists
  // `catalog.splash` is empty and this is a no-op — so the whole feature comes and
  // goes with its `ui/splash/<id>/` folder.
  bootSplashPlugins(catalog.splash);

  const TOP_LEVEL = topLevelFromCatalog(catalog.main);

  // Phase 1: load the requested page and chat first. A page's own partial replaces the
  // inner HTML of its mount; any extra <template data-slot> partials (page.json
  // `partials`, flagged `slot: true` by topLevelFromCatalog) are appended into
  // their slot targets — the same slot-injection admin sub-pages use — so a main
  // page can ship multi-section markup (e.g. Instances' App Config) with no edit
  // to index.html. Order is preserved: html first, then its slot partials.
  // `?tab=` is authoritative for direct links; the visible select is the shell
  // catalog-order control, not a reliable default (its first option can be the
  // locked Admin Tools page). A bare URL always begins with the lightweight
  // Agents landing while role checks decide whether to promote an admin to
  // Instances. Loading every Instances/Admin/Settings partial before importing
  // main.js made an Agents visit wait on dozens of unrelated files.
  const requestedPageId = new URLSearchParams(location.search).get('tab')
    || 'agents';
  await Promise.all([
    ensurePagePartial(requestedPageId, catalog),
    loadTopLevelPartials([
      ...TOP_LEVEL.filter((p) => p.pageId === '__chat__'), PRIVATE_ACCESS_PARTIAL,
    ]),
  ]);

  // Render lucide icons for the freshly injected DOM.
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }

  // The first boot is intentionally tenant-cache-first. When the slow
  // authority result arrives, make it available to tabs.js and any page that
  // wants to reconcile its state without blocking first paint.  Do not rebuild
  // the DOM here: the active page owns its lifecycle after main.js starts.
  if (catalogPromise && typeof catalogPromise.then === 'function') {
    Promise.resolve(catalogPromise).then((authoritativeCatalog) => {
      if (!authoritativeCatalog || !Array.isArray(authoritativeCatalog.main)) return;
      if (!window.__pagesCatalogAuthoritative) {
        retryCatalogAfterCoreBoot();
        return;
      }
      hydrateLateMainPages(authoritativeCatalog)
        .then(() => window.dispatchEvent(new CustomEvent('pages-catalog-ready', {
          detail: { catalog: authoritativeCatalog },
        })))
        .catch((error) => console.error('late catalog hydration failed', error));
    }).catch(() => {});
  }
})();

// Settings keeps its shell eager and its expensive section fragments on demand.
const _settingsPartialHydration = new Map();
function ensureSettingsPagePartial(pageId, settingsPageId, catalog = window.__pagesCatalog) {
  const page = catalogPage(pageId, catalog);
  const entry = (page?.settings_partials || []).find((item) => item && item.id === settingsPageId);
  if (!entry?.path || !page?.dir) return Promise.reject(new Error(`Unknown Settings page: ${settingsPageId}`));
  const url = `./ui/${page.dir}/${entry.path}`;
  if (document.querySelector(`[data-page-partial="${url}"]`)) return Promise.resolve();
  const existing = _settingsPartialHydration.get(url);
  if (existing) return existing;
  const pending = fetchHtml(url).then((html) => {
    if (!document.querySelector(`[data-page-partial="${url}"]`)) {
      injectSlotsFromHtml(html, url);
      const marker = document.createElement('span');
      marker.hidden = true;
      marker.dataset.pagePartial = url;
      document.body.appendChild(marker);
    }
  }).catch((error) => {
    _settingsPartialHydration.delete(url);
    throw error;
  });
  _settingsPartialHydration.set(url, pending);
  return pending;
}
window.__ensureSettingsPagePartial = (settingsPageId) => ensureSettingsPagePartial('app', settingsPageId);
