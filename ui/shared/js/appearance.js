/* ============================================================
   Appearance applier  —  full theme palette + border + fonts (boot-time)
   ------------------------------------------------------------
   Mirrors the background manager (ui/background/_engine/manager.js):
   the admin's choices live server-side in data/config/app-settings.json
   and are served to every visitor (anonymous + signed-in) through the
   public /api/v1/auth/ui-config endpoint. This file pulls those values
   and applies them app-wide by injecting a single <style> block of
   CSS-variable overrides ON TOP of design-system.css — so the design
   tokens read from the JSON, not from the hard-coded CSS defaults.

   What it controls — the "one place" theme knobs:
     • The full per-theme PALETTE: accent (--brand), secondary (--purple),
       the status hues (--success / --warning / --danger), the neutral
       surfaces (--bg-0 / --bg-elev / --bg-tint), text (--fg-1 / --fg-3),
       the border colour (--border) and the ambient page-glow
       (--ambient-2-rgb). Each is chosen separately for dark and light,
       mirroring the two palette blocks in design-system.css.
     • --border-width  shared line weight; "0" turns EVERY neutral border
                       off (accent / focus / status borders are NOT wired
                       here, so they survive borders-off).
     • --font-sans / --font-mono  the UI body + monospace font stacks.

   HOW IT WINS THE CASCADE (and why the dark/light split matters):
     design-system.css declares the dark palette on :root and RE-declares
     the light palette on `body.light-mode`. A naive override on :root is
     BEATEN by `body.light-mode` in light mode (higher specificity) — that
     was the old "borders off doesn't work in light mode" bug. So we emit
     TWO theme blocks, each more specific than the design-system rule it
     overrides:
       • dark  → `body:not(.light-mode){…}`  (beats :root, inert in light)
       • light → `body.light-mode{…}`        (same specificity, later source)
     Custom properties inherit, and `body` is an ancestor of every rendered
     element (incl. the animated background, which reads tokens off body),
     so setting them on body cascades everywhere. Fonts (theme-independent)
     go on :root.

   For any palette hue that feeds rgba() tints, we ALSO write its `-rgb`
   triple, derived from the chosen hex — so changing one swatch moves the
   solid colour AND every translucent tint built from it.

   Loaded render-blocking in <head> right after design-system.css so the
   override <style> wins the cascade and is in place before the app mounts.
   A blank value means "use the design-system.css default" — that token is
   simply left un-overridden.
   ============================================================ */
(function () {
  // ── Token map: appearance key (base) → CSS variable(s) it drives ─────────
  // Each entry stores the per-theme keys `<base>_dark` / `<base>_light`. For a
  // hue with translucent tints we also write its `-rgb` triple (derived from
  // the hex) and, where the palette has a `-strong` hover variant, mirror the
  // base value into it. `var:null` = the token only contributes its rgb triple
  // (the ambient glow is referenced only as rgba(var(--ambient-2-rgb), a)).
  var TOKENS = [
    { base: 'accent',        cssVar: '--brand',   rgbVar: '--brand-rgb',   strongVar: '--brand-strong' },
    { base: 'secondary',     cssVar: '--purple',  rgbVar: '--purple-rgb',  strongVar: '--purple-strong' },
    { base: 'success',       cssVar: '--success', rgbVar: '--success-rgb' },
    { base: 'warning',       cssVar: '--warning', rgbVar: '--warning-rgb' },
    { base: 'danger',        cssVar: '--danger',  rgbVar: '--danger-rgb' },
    { base: 'surface_bg',    cssVar: '--bg-0',     rgbVar: '--bg-0-rgb' },
    { base: 'surface_panel', cssVar: '--bg-elev' },
    { base: 'surface_tint',  cssVar: '--bg-tint' },
    { base: 'text',          cssVar: '--fg-1' },
    { base: 'text_muted',    cssVar: '--fg-3' },
    { base: 'ambient',       cssVar: null,        rgbVar: '--ambient-2-rgb' },
    { base: 'border_color',  cssVar: '--border' },
    // Chat bubbles + the chat composer pill — optional overrides. Left BLANK by
    // default so they follow the theme-derived defaults (resolved at the element
    // in index.css; all three share the same panel-tint default so they match out
    // of the box); set a value to pin a colour. Alpha-capable, so a value of 0
    // opacity gives a transparent surface (text/pill with no background).
    { base: 'user_bubble',        cssVar: '--user-bubble-bg' },
    { base: 'user_bubble_border', cssVar: '--user-bubble-border' },
    { base: 'agent_bubble',       cssVar: '--agent-bubble-bg' },
    { base: 'agent_bubble_border',cssVar: '--agent-bubble-border' },
    { base: 'chat_pill_bg',       cssVar: '--chat-pill-bg' },
    { base: 'chat_pill_border',   cssVar: '--chat-pill-border' },
  ];

  // Defaults mirror the design-system.css palette + typography. Kept in sync
  // with app.admin.settings._DEFAULT_APPEARANCE on the server.
  var DEFAULTS = {
    border_width: '1px',
    font_sans: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    font_mono: "'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace",
    // Border colour keeps its historical key names.
    border_color_dark: '#333333',  border_color_light: '#cccccc',
    accent_dark: '#ffffff',        accent_light: '#000000',
    secondary_dark: '#d9d9d9',     secondary_light: '#262626',
    success_dark: '#22c55e',       success_light: '#16a34a',
    warning_dark: '#eab308',       warning_light: '#ca8a04',
    danger_dark: '#ef4444',        danger_light: '#dc2626',
    surface_bg_dark: '#000000',    surface_bg_light: '#ffffff',
    surface_panel_dark: '#0d0d0d', surface_panel_light: '#f5f5f5',
    surface_tint_dark: '#1a1a1a',  surface_tint_light: '#e8e8e8',
    text_dark: '#ffffff',          text_light: '#000000',
    text_muted_dark: '#a3a3a3',    text_muted_light: '#666666',
    ambient_dark: '#404040',       ambient_light: '#d9d9d9',
    // Bubbles default BLANK → follow the design-system.css derived defaults
    // (set a colour in the Appearance panel to override). Stored as #rrggbb or
    // #rrggbbaa (the trailing aa = opacity).
    user_bubble_dark: '',          user_bubble_light: '',
    user_bubble_border_dark: '',   user_bubble_border_light: '',
    agent_bubble_dark: '',         agent_bubble_light: '',
    agent_bubble_border_dark: '',  agent_bubble_border_light: '',
    chat_pill_bg_dark: '',         chat_pill_bg_light: '',
    chat_pill_border_dark: '',     chat_pill_border_light: '',
    // ── Theme-independent style knobs (JSON-only; no UI). Each default is
    //    "no change" so they're inert until edited in app-settings.json. ──
    radius_scale: '1',     // corner roundness   (0 = square … 2 = extra round)
    shadow_strength: '1',  // shadow depth        (0 = flat … 2 = heavy)
    ui_scale: '1',         // overall UI size     (zoom; 1 = normal)
    reduce_motion: 'off',  // 'on' → near-instant animations/transitions
    cursor_glow: 'on',     // 'off' → hide the pointer glow
    // ── RAM speed layer (ui/shared/js/media-cache.js) — app-wide client knob ──
    //   media_cache_enabled   — 'on' (default) | 'off' to disable the in-memory
    //                           attachment/thumbnail cache entirely.
    //   media_cache_budget_mb — memory ceiling for cached bytes/thumbnails (MB);
    //                           LRU-evicts above it, so it can never exceed this.
    media_cache_enabled: 'on',
    media_cache_budget_mb: '128',
    // ── Chat panel layout (app-wide defaults) ──
    //   chat_position        — desktop split side: 'right' (default) | 'left'
    //   chat_default_visible — first-visit desktop default: 'visible' | 'hidden'.
    //     Read at boot by index.html (__chatVisibilityDefault) off the cached
    //     value; once a visitor toggles the chat their own choice wins.
    //   chat_panel_width     — positive desktop panel width in px or %. A saved
    //     manual drag is an inline pixel width and therefore wins in the cascade.
    chat_position: 'right',
    chat_default_visible: 'visible',
    chat_panel_width: '380px',
    // ── TALL chat-composer (#chat-input-row) geometry + button knobs. Each is a
    //    CSS length injected as the matching --chat-pill-* variable (see app1.css
    //    CHAT-PILL-VARS); defaults mirror the CSS fallbacks. The compact 1-line
    //    bars keep their own fixed sizing. ──
    chat_pill_max_width: '500px',          // composer width cap
    chat_pill_radius: '41px',              // corner roundness
    chat_pill_padding: '4px 4px 0px 4px', // outer padding (CSS shorthand)
    chat_pill_min_height: '',          // resting text-area height — CSS file is the single source of truth
    chat_pill_max_height: '160px',         // grow cap before scrolling
    chat_pill_font_size: '14px',           // input + placeholder text size
    chat_pill_attach_size: '38px',         // attach (+) button box
    chat_pill_attach_icon: '24px',         // attach (+) glyph
    chat_pill_button_size: '62px',         // mic / send button box
    chat_pill_button_icon: '44px'          // mic / send glyph
  };

  // The chat-pill knobs above, mapped to the CSS variable each one drives. Every
  // value is a plain CSS length passed through clean(); table-driven so adding a
  // pill knob is a one-line edit here + a var() in app1.css.
  var CHAT_PILL_KNOBS = [
    { key: 'chat_pill_max_width',   cssVar: '--chat-pill-max-width' },
    { key: 'chat_pill_radius',      cssVar: '--chat-pill-radius' },
    { key: 'chat_pill_padding',     cssVar: '--chat-pill-padding' },
    { key: 'chat_pill_min_height',  cssVar: '--chat-pill-min-height' },
    { key: 'chat_pill_max_height',  cssVar: '--chat-pill-max-height' },
    { key: 'chat_pill_font_size',   cssVar: '--chat-pill-font-size' },
    { key: 'chat_pill_attach_size', cssVar: '--chat-pill-attach-size' },
    { key: 'chat_pill_attach_icon', cssVar: '--chat-pill-attach-icon' },
    { key: 'chat_pill_button_size', cssVar: '--chat-pill-button-size' },
    { key: 'chat_pill_button_icon', cssVar: '--chat-pill-button-icon' }
  ];
  var CACHE_KEY = 'wa_appearance';
  var CONFIG_URL = '/api/v1/auth/ui-config';
  var STYLE_ID = 'wa-appearance-overrides';

  var values = readCache();   // { <every key in DEFAULTS> }

  function readCache() {
    var v = {};
    for (var k in DEFAULTS) { if (DEFAULTS.hasOwnProperty(k)) v[k] = DEFAULTS[k]; }
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        var p = JSON.parse(raw);
        if (p && typeof p === 'object') {
          for (var key in DEFAULTS) {
            if (DEFAULTS.hasOwnProperty(key) && typeof p[key] === 'string' && p[key]) {
              v[key] = p[key];
            }
          }
        }
      }
    } catch (e) { /* defaults */ }
    return v;
  }

  function writeCache() {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(values)); } catch (e) {}
  }

  // Defensive: these values are admin-controlled and dropped into CSS text.
  // Strip anything that could break out of the declaration block.
  function clean(s) {
    return String(s == null ? '' : s).replace(/[{}<>;]/g, '').trim();
  }

  // Parse "#rrggbb" OR "#rrggbbaa" (the trailing aa = opacity, 00–ff). Returns
  // { hex6:'#rrggbb', rgb:'r, g, b', a:0..1 } or null if not a hex colour, so a
  // non-hex value is skipped rather than corrupting the declaration.
  function parseColor(v) {
    var m = /^#?([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(String(v == null ? '' : v).trim());
    if (!m) return null;
    var n = parseInt(m[1], 16);
    var a = m[2] != null ? parseInt(m[2], 16) / 255 : 1;
    return {
      hex6: '#' + m[1],
      rgb: ((n >> 16) & 255) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255),
      a: Math.round(a * 1000) / 1000,
    };
  }

  // A plain non-negative number (allows decimals), or '' if not — so a junk
  // value is ignored and the CSS default (1) holds rather than breaking calc().
  function num(v) {
    v = String(v == null ? '' : v).trim();
    return /^[0-9]+(\.[0-9]+)?$/.test(v) ? v : '';
  }
  // Matches the server-side chat-panel-width contract. The value becomes CSS, so
  // cache/config corruption falls back to app1.css rather than being emitted.
  function chatPanelWidth(v) {
    v = clean(v);
    var match = /^(?:\d+(?:\.\d+)?|\.\d+)(?:px|%)$/.exec(v);
    return match && parseFloat(v) > 0 ? v : '';
  }
  function isTrue(v) {
    return ['1', 'true', 'on', 'yes'].indexOf(String(v == null ? '' : v).trim().toLowerCase()) >= 0;
  }
  function isFalse(v) {
    return ['0', 'false', 'off', 'no', 'hide'].indexOf(String(v == null ? '' : v).trim().toLowerCase()) >= 0;
  }

  // Push a token's declarations (the solid var, its rgb triple, its -strong
  // mirror) into a per-theme declaration array, for the given theme suffix.
  // Alpha-aware: a value carrying opacity (#rrggbbaa with aa < ff) is emitted as
  // rgba(r, g, b, a) so surfaces/bubbles can be made translucent; the -rgb triple
  // stays alpha-free so existing rgba(var(--x-rgb), a) tints are unaffected.
  function pushToken(decls, tok, suffix) {
    var raw = clean(values[tok.base + suffix]);
    if (!raw) return;
    var c = parseColor(raw);
    var colorVal = c ? (c.a < 1 ? 'rgba(' + c.rgb + ', ' + c.a + ')' : c.hex6) : raw;
    if (tok.cssVar)    decls.push(tok.cssVar + ': ' + colorVal + ';');
    if (tok.strongVar) decls.push(tok.strongVar + ': ' + colorVal + ';');
    if (tok.rgbVar && c) decls.push(tok.rgbVar + ': ' + c.rgb + ';');
  }

  // Build + (re)inject the override stylesheet. Three blocks:
  //   :root  (theme-independent)  → fonts + numeric knobs
  //   :root  (dark palette)       → dark is the ROOT/default theme, so its
  //          overrides MUST land at :root — design-system.css declares the dark
  //          DERIVED tokens (--accent, --bg-1, --border-soft, the brand-glow
  //          shadows, …) at :root too, computing them from the base swatches at
  //          the SAME scope. If the dark override sat on body:not(.light-mode)
  //          instead, those :root-scoped derived tokens would keep computing
  //          from the un-overridden base defaults — i.e. the whole derived ramp
  //          would stay frozen on the stock cyan/blue palette in dark mode.
  //   body.light-mode             → light palette + shared border width
  // Safe to put the dark block at :root: design-system.css always re-declares
  // the FULL light palette at body.light-mode (higher specificity than :root),
  // so a dark-only override can never leak into light mode.
  // border-width is written into BOTH theme blocks so it beats design-system's
  // per-theme re-declaration in either mode (the old light-mode "off" bug).
  function apply() {
    var rootDecls = [];
    var sans = clean(values.font_sans);
    var mono = clean(values.font_mono);
    if (sans) rootDecls.push('--font-sans: ' + sans + ';');
    if (mono) rootDecls.push('--font-mono: ' + mono + ';');

    // Theme-independent numeric style knobs (roundness / depth / size). Each is
    // a multiplier the design-system tokens read via calc()/zoom; only written
    // when it's a valid number, so a blank/junk value leaves the CSS default.
    var radius = num(values.radius_scale);
    var depth = num(values.shadow_strength);
    var uiScale = num(values.ui_scale);
    if (radius)  rootDecls.push('--radius-scale: ' + radius + ';');
    if (depth)   rootDecls.push('--shadow-strength: ' + depth + ';');
    if (uiScale) rootDecls.push('--ui-scale: ' + uiScale + ';');

    // Desktop chat-panel width. The mobile stylesheet has an explicit 100% width,
    // while a user drag writes an inline pixel width that outranks this default.
    var panelWidth = chatPanelWidth(values.chat_panel_width);
    if (panelWidth) rootDecls.push('--chat-panel-width: ' + panelWidth + ';');

    // Chat-composer geometry + button knobs — all CSS lengths, theme-independent.
    // clean() strips anything that could break out of the declaration; a blank
    // value leaves the app1.css fallback for that knob in force.
    CHAT_PILL_KNOBS.forEach(function (knob) {
      var v = clean(values[knob.key]);
      if (v) rootDecls.push(knob.cssVar + ': ' + v + ';');
    });

    var width = clean(values.border_width);

    var darkDecls = [];
    var lightDecls = [];
    TOKENS.forEach(function (tok) {
      pushToken(darkDecls, tok, '_dark');
      pushToken(lightDecls, tok, '_light');
    });
    if (width) {
      darkDecls.push('--border-width: ' + width + ';');
      lightDecls.push('--border-width: ' + width + ';');
    }

    var css = '';
    if (rootDecls.length)  css += ':root{' + rootDecls.join('') + '}';
    // Dark = the default theme → its overrides go on :root so the design-system
    // dark DERIVED tokens (also at :root) recompute from the overridden bases.
    if (darkDecls.length)  css += ':root{' + darkDecls.join('') + '}';
    if (lightDecls.length) css += 'body.light-mode{' + lightDecls.join('') + '}';

    // Behaviour knobs that aren't a single token — emitted as whole rules only
    // when switched away from their default. reduce_motion collapses animation +
    // transition durations app-wide (accessibility-friendly); cursor_glow 'off'
    // hides the pointer glow element (ui/shared/js/cursor-effects.js).
    if (isTrue(values.reduce_motion)) {
      css += '*,*::before,*::after{animation-duration:.001ms!important;' +
             'animation-iteration-count:1!important;transition-duration:.001ms!important;' +
             'scroll-behavior:auto!important}';
    }
    if (isFalse(values.cursor_glow)) {
      css += '#cursor-glow{display:none!important}';
    }

    // RAM speed layer: push the app-wide On/Off + memory budget to media-cache.js.
    // Stashed on a global too, so the cache can self-configure even if its module
    // finishes loading AFTER this first apply() runs (either load order is safe).
    var mcBudget = num(values.media_cache_budget_mb);
    var mcCfg = {
      enabled: !isFalse(values.media_cache_enabled),
      budgetBytes: (mcBudget ? mcBudget : 128) * 1024 * 1024
    };
    try { window.__waMediaCacheCfg = mcCfg; } catch (e) {}
    try { if (window.WA_MEDIA_CACHE) window.WA_MEDIA_CACHE.configure(mcCfg); } catch (e) {}

    // Chat panel side (desktop split only). 'left' reverses the #stage flex row
    // so the chat sits left of the main panel; the resize handle detects the side
    // on its own (chatResize.js). Mobile is a top/bottom stack — untouched.
    if (clean(values.chat_position).toLowerCase() === 'left') {
      css += '@media (min-width: 801px){#stage{flex-direction:row-reverse}}';
    }

    var el = document.getElementById(STYLE_ID);
    if (!el) {
      el = document.createElement('style');
      el.id = STYLE_ID;
      (document.head || document.documentElement).appendChild(el);
    }
    el.textContent = css;

    // Tell JS-driven surfaces a palette/border/font change just landed. CSS-var
    // consumers re-style automatically; genui that read the palette INTO JS
    // (the animated backgrounds) cache it and would otherwise stay stale until a
    // resize or dark<->light flip — so they listen for this and re-read live.
    try { window.dispatchEvent(new CustomEvent('wa-appearance-changed')); } catch (e) {}
  }

  // ── Public: let the Appearance settings panel apply a change live ──────────
  window.WA_APPEARANCE = {
    setOverrides: function (next) {
      if (!next || typeof next !== 'object') return;
      for (var k in DEFAULTS) {
        if (DEFAULTS.hasOwnProperty(k) && typeof next[k] === 'string') values[k] = next[k];
      }
      writeCache();
      apply();
    },
    getOverrides: function () {
      var out = {};
      for (var k in values) { if (values.hasOwnProperty(k)) out[k] = values[k]; }
      return out;
    },
    // The token map, so the panel can render one swatch per editable token
    // without re-declaring the list (single source of truth for the mapping).
    tokens: TOKENS.map(function (t) { return t.base; })
  };

  // ── Boot: apply from cache immediately, then reconcile with the server ──
  apply();

  window.__fetchUiConfig({ headers: authHeader() })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (cfg) {
      if (!cfg) return;
      // Reconcile the server-rendered title with the saved app-wide name for
      // cached shells and runtime config refreshes. Preserve public-agent and
      // route-error titles, which are more specific than the site name.
      if (!window.__agentId && !window.__routeError && typeof cfg.site_name === 'string' && cfg.site_name.trim()) {
        document.title = cfg.site_name.trim();
      }
      var changed = false;
      for (var k in DEFAULTS) {
        if (DEFAULTS.hasOwnProperty(k) && typeof cfg[k] === 'string' && cfg[k] && cfg[k] !== values[k]) {
          values[k] = cfg[k];
          changed = true;
        }
      }
      if (changed) { writeCache(); apply(); }
    })
    .catch(function () { /* keep cached/default values */ });

  // The ui-config endpoint is public, but send the token when present so it
  // behaves identically on authenticated pages.
  function authHeader() {
    try {
      var t = localStorage.getItem('auth_token');
      if (t) return { Authorization: 'Bearer ' + t };
    } catch (e) {}
    return {};
  }
})();
