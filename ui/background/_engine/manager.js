/* ============================================================
   Background manager  —  the engine (core infrastructure)
   ------------------------------------------------------------
   Owns the single full-screen <genui> and decides WHICH background
   plugin runs, separately for dark and light mode. Backgrounds are
   drop-in folders under ui/background/<id>/ (see registry.js); this
   file knows none of them by name — it just loads the id the admin
   chose for the active theme.

   Flow on load:
     1. Read the cached choice from localStorage so a repeat visitor
        sees the right background with no flash.
     2. Start the background for the current theme; pre-load the other
        theme's background so a dark<->light flip is instant.
     3. Fetch the admin's saved choice (public endpoint) and reconcile
        — swap only if it actually changed.
     4. Watch for theme flips (body.light-mode) and swap accordingly.

   Selectable values are background ids ("stargaze", "bullet-grid",
   …) or the built-in "none" (plain themed background, no genui
   animation — the cursor glow / card spotlights live in
   ui/shared/js/cursor-effects.js and stay on regardless).
   ============================================================ */
(function () {
  var DEFAULTS = { dark: 'stargaze', light: 'bullet-grid' };
  // The loading curtain gets a responsive grid even when the saved choice is
  // "none". It receives the boot-orbit input already emitted by index.html;
  // after the curtain is gone, desiredId() immediately resumes the saved choice.
  var BOOT_ID = 'bullet-grid';
  var CACHE_KEY = 'wa_bg_choice';
  var CONFIG_URL = '/api/v1/auth/ui-config';
  var BASE = 'ui/background/';

  var genuiHost = document.getElementById('stargaze-bg');
  if (!genuiHost) return;  // no background surface on this page

  // The public mount is the GenUI custom element. Renderers need a 2D drawing
  // surface, so keep its implementation canvas private inside that host rather
  // than replacing the GenUI element with the legacy <canvas> tag.
  var genui = genuiHost.querySelector(':scope > canvas[data-wa-bg-surface]');
  if (!genui) {
    genui = document.createElement('canvas');
    genui.dataset.waBgSurface = 'true';
    genui.setAttribute('aria-hidden', 'true');
    genui.style.cssText = 'position:absolute;inset:0;display:block;pointer-events:none;';
    genuiHost.appendChild(genui);
  }

  var choice = readCache();      // { dark, light }
  var activeId = null;           // id currently drawing ('none' included)
  var activeApi = null;          // its { start, stop }
  var injected = {};             // id -> true once its script tag is added

  function readCache() {
    var c = { dark: DEFAULTS.dark, light: DEFAULTS.light };
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        var p = JSON.parse(raw);
        if (p && p.dark) c.dark = p.dark;
        if (p && p.light) c.light = p.light;
      }
    } catch (e) { /* defaults */ }
    return c;
  }

  function writeCache() {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(choice)); } catch (e) {}
  }

  function isLight() {
    return !!(document.body && document.body.classList.contains('light-mode'));
  }

  // Keep the grid visible for every frame of the loading curtain, not merely
  // until the shell removes is-booting. This mirrors index.html's boot-orbit
  // lifetime exactly, including its short reveal transition.
  function bootActive() {
    return !!document.body && (
      document.body.classList.contains('is-booting') ||
      !!document.getElementById('boot-curtain')
    );
  }

  function desiredId() {
    if (bootActive()) return BOOT_ID;
    return isLight() ? choice.light : choice.dark;
  }

  // Add a plugin's <script> once. Its <id>.js calls WA_BG.register on load.
  function inject(id) {
    if (!id || id === 'none' || injected[id]) return;
    injected[id] = true;
    var s = document.createElement('script');
    s.src = BASE + id + '/' + id + '.js';
    s.async = true;
    s.onerror = function () {
      // Folder was deleted / renamed. Fall back so the page is never blank.
      injected[id] = false;
      console.warn('background: failed to load "' + id + '", falling back');
      if (desiredId() === id) {
        choice[isLight() ? 'light' : 'dark'] = DEFAULTS.dark;
        reconcile();
      }
    };
    document.head.appendChild(s);
  }

  // Make `id` the live background. Async-safe: a theme flip mid-load is
  // re-checked before we actually start a renderer.
  function activate(id) {
    if (id === activeId) return;
    if (activeApi && activeApi.stop) {
      try { activeApi.stop(); } catch (e) {}
    }
    activeApi = null;
    clearGenui();

    if (!id || id === 'none') { activeId = 'none'; return; }
    activeId = id;
    inject(id);
    window.WA_BG.whenReady(id, function (api) {
      // Only start if this id is still the one we want (guards races).
      if (desiredId() !== id) return;
      activeApi = api;
      activeId = id;
      try { api.start(genui); } catch (e) { console.warn('background start failed', e); }
    });
  }

  function clearGenui() {
    try {
      var ctx = genui.getContext('2d');
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, genui.width, genui.height);
    } catch (e) {}
  }

  // Show the desired background for the current theme; warm up the other.
  function reconcile() {
    activate(desiredId());
    inject(isLight() ? choice.dark : choice.light);  // preload the other theme's bg
  }

  // ── React to theme flips (body.light-mode toggled by the theme menu) ──
  var themeObserver = new MutationObserver(function () { reconcile(); });
  if (document.body) {
    themeObserver.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  }
  // index.html emits this only after the boot curtain is gone. Observe it in
  // addition to the class mutation so the temporary grid cannot outlive boot.
  window.addEventListener('wa-boot-pointerend', reconcile);

  // ── React to live palette/border/font edits (Appearance panel) ──
  // appearance.js fires this whenever it re-applies the CSS-variable overrides.
  // The plugin keeps the SAME id (no swap) — it just needs to re-read its colour
  // tokens off the body, which it does via an optional refresh() hook. Plugins
  // without one simply ignore the change (their look isn't palette-derived).
  window.addEventListener('wa-appearance-changed', function () {
    if (activeApi && typeof activeApi.refresh === 'function') {
      try { activeApi.refresh(); } catch (e) {}
    }
  });

  // ── Public: let the admin Appearance selector apply a change live ──
  window.WA_BG.setChoice = function (next) {
    if (!next) return;
    if (next.dark) choice.dark = next.dark;
    if (next.light) choice.light = next.light;
    writeCache();
    reconcile();
  };
  window.WA_BG.getChoice = function () { return { dark: choice.dark, light: choice.light }; };
  // The id currently drawing ('none' when no animation). Handy for debugging.
  window.WA_BG.active = function () { return activeId; };

  // ── Boot: start from cache immediately, then reconcile with the server ──
  reconcile();

  window.__fetchUiConfig({ headers: authHeader() })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (cfg) {
      if (!cfg) return;
      var changed = false;
      if (cfg.background_dark && cfg.background_dark !== choice.dark) { choice.dark = cfg.background_dark; changed = true; }
      if (cfg.background_light && cfg.background_light !== choice.light) { choice.light = cfg.background_light; changed = true; }
      if (changed) { writeCache(); reconcile(); }
    })
    .catch(function () { /* keep cached/default choice */ });

  // The ui-config endpoint is public, but send the token when present so it
  // works identically on authenticated pages.
  function authHeader() {
    try {
      var t = localStorage.getItem('auth_token');
      if (t) return { Authorization: 'Bearer ' + t };
    } catch (e) {}
    return {};
  }
})();
