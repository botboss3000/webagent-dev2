'use strict';

/**
 * ═══════════════════════════════════════════════════════════════════════════════
 * 🤖 AI CODING AGENT — READ THIS FIRST
 * ═══════════════════════════════════════════════════════════════════════════════
 * THIS FILE is the single source of truth for the sessions page viewer
 * configuration. The inline config objects (SESSIONS_CONFIG, RECYCLE_BIN_CONFIG,
 * BOTH_CONFIG) drive every column, every responsive breakpoint, the gutter,
 * sort keys, data-source endpoints, and row-type scoping — the rendering code
 * in sessions-page.js is a GENERIC ENGINE that reads these configs and builds
 * the table.
 *
 * The page is one combined dataset filtered by a chip switcher
 * (Sessions | Recycling bin | Both). Each data source carries an `origin`
 * ('active' | 'bin') so rows from the two catalogs can be told apart in the
 * merged 'both' view.
 *
 * TO CHANGE ANYTHING about the table's structure:
 *   → Add/remove/reorder a column        — edit the `columns` array
 *   → Change a column's width            — edit `width` on that column
 *   → Make a column sticky               — set `sticky: true` + `sticky_offset`
 *   → Scope a column to a row type       — set `for: ["session"]` / `["automation"]` / both
 *   → Add a new view (chip)              — add a new config block + its id to VIEW_IDS
 *   → Change responsive breakpoints      — edit `responsive.breakpoints`
 *   → Change the data-source endpoint    — edit `data_sources[].endpoint`
 *   → Change the gutter behaviour        — edit `gutter` block
 *
 * DO NOT edit sessions-page.js to change column layout — it reads EVERYTHING
 * from these config objects via getColumns(), getGutterConfig(), etc.
 *
 * AUTOMATIONS_CONFIG is retained for reference only — Automations is no longer
 * offered by the sessions switcher; it is its own dashboard embedded in the
 * Agents page (ui/main-panel/agents/automations, rendered by js/automations.js).
 *
 * The JSON files in data/config/data.*.json are REFERENCE COPIES — they are
 * NOT the runtime source. The runtime is this file.
 * ═══════════════════════════════════════════════════════════════════════════════
 *
 * Column Config — inline definitions for the sessions data viewer.
 * Each view (sessions, recycle-bin, both) declares:
 *   - id, label, icon        → chip identity
 *   - data_sources[]         → { row_type, endpoint, origin } per source
 *   - gutter                 → { width, default_open, auto_collapse_at }
 *   - columns[]              → { id, width, sticky, sticky_offset, sort_key, align, for[] }
 *   - responsive.breakpoints → { max_width, hide[] }
 *
 * Configs live here (not fetched from data/config/) because there is no
 * /data/config static mount on the server — only /ui, /user_data, /screenshots,
 * and /plugins/engines are mounted. The data/config/ tree is filesystem-only.
 */

// ── Inline configs (mirrors data/config/data.*.json) ────────────────

// Session columns + responsive rules are shared by all three views — the
// merged 'both' view shows exactly the same columns as the others.
const SESSION_COLUMNS = [
  { id: "check",      width: 32,  sticky: true,  sticky_offset: 0, for: ["session"] },
  { id: "title",      width: 260, sort_key: "title",                    for: ["session"] },
  { id: "links",      width: 70,                                         for: ["session"] },
  { id: "msgs",       width: 70,  sort_key: "message_count",       align: "right", for: ["session"] },
  { id: "tokens_in",  width: 70,  sort_key: "total_input_tokens",  align: "right", for: ["session"] },
  { id: "tokens_out", width: 70,  sort_key: "total_output_tokens", align: "right", for: ["session"] },
  { id: "cost",       width: 70,  sort_key: "total_cost",          align: "right", for: ["session"] },
  { id: "duration",   width: 80,  sort_key: "total_duration_ms",   align: "right", for: ["session"] }
];

const SESSION_RESPONSIVE = {
  breakpoints: [
    { max_width: 600,  hide: ["tokens_in", "tokens_out", "cost", "duration"] },
    { max_width: 900,  hide: ["tokens_in", "tokens_out", "cost", "duration"] },
    { max_width: 1100, hide: ["duration"] }
  ]
};

const SESSIONS_CONFIG = {
  id: "sessions",
  label: "Sessions",
  icon: "message-square",
  data_sources: [
    { row_type: "session", endpoint: "/api/v1/db/session-stats?status=active", origin: "active" }
  ],
  gutter: { width: 6, default_open: true, auto_collapse_at: 800 },
  columns: SESSION_COLUMNS,
  responsive: SESSION_RESPONSIVE
};

const AUTOMATIONS_CONFIG = {
  id: "automations",
  label: "Automations",
  icon: "settings-2",
  data_sources: [
    { row_type: "automation", endpoint: "/api/v1/automations/dashboard", result_shape: "nested" }
  ],
  gutter: { width: 6, default_open: true, auto_collapse_at: 800 },
  columns: [
    { id: "check",   width: 32,  sticky: true,  sticky_offset: 0,   for: ["automation"] },
    { id: "agent",   width: 140,                                    for: ["automation"] },
    { id: "name",    width: 220, sort_key: "label",                 for: ["automation"] },
    { id: "status",  width: 100, sort_key: "status",                for: ["automation"] },
    { id: "type",    width: 90,                                     for: ["automation"] },
    { id: "trigger", width: 140,                                    for: ["automation"] },
    { id: "output",  width: 120,                                    for: ["automation"] },
    { id: "last",    width: 100,                                    for: ["automation"] },
    { id: "next",    width: 100,                                    for: ["automation"] },
    { id: "count",   width: 60,  align: "right",                   for: ["automation"] },
    { id: "device",  width: 80,                                     for: ["automation"] },
    { id: "enabled", width: 50,  align: "center",                  for: ["automation"] }
  ],
  responsive: {
    breakpoints: [
      { max_width: 600,  hide: ["type", "trigger", "output", "last", "next", "count", "device"] },
      { max_width: 900,  hide: ["count", "device"] }
    ]
  }
};

const RECYCLE_BIN_CONFIG = {
  id: "recycle-bin",
  label: "Recycling bin",
  icon: "trash-2",
  data_sources: [
    { row_type: "session", endpoint: "/api/v1/db/session-stats?status=recycled", origin: "bin" }
  ],
  gutter: { width: 6, default_open: true, auto_collapse_at: 800 },
  columns: SESSION_COLUMNS,
  responsive: SESSION_RESPONSIVE
};

// The 'both' chip — merges the active and recycled catalogs into ONE table.
// Rows keep their `_origin` ('active' | 'bin') so the renderer can badge binned
// rows and keep them non-loadable. Columns are the shared session set.
const BOTH_CONFIG = {
  id: "both",
  label: "All sessions",
  icon: "layers",
  data_sources: [
    { row_type: "session", endpoint: "/api/v1/db/session-stats?status=active",  origin: "active" },
    { row_type: "session", endpoint: "/api/v1/db/session-stats?status=recycled", origin: "bin" }
  ],
  gutter: { width: 6, default_open: true, auto_collapse_at: 800 },
  columns: SESSION_COLUMNS,
  responsive: SESSION_RESPONSIVE
};

// Lookup table — viewId → config object
const _BUILTIN = {
  'sessions':     SESSIONS_CONFIG,
  'recycle-bin':  RECYCLE_BIN_CONFIG,
  'both':         BOTH_CONFIG,
  // AUTOMATIONS_CONFIG retained for reference only — the Automations tab is its
  // own main-panel page and is no longer offered by the sessions switcher.
  'automations':  AUTOMATIONS_CONFIG
};

// Canonical list of view ids (order drives the chip switcher)
const VIEW_IDS = ['sessions', 'recycle-bin', 'both'];

/**
 * Return a viewer config synchronously. No fetch — configs are inline.
 * Async for backward compat with callers that `await` it.
 */
async function loadViewerConfig(viewId) {
  var cfg = _BUILTIN[viewId];
  if (!cfg) throw new Error('Unknown view: ' + viewId);
  return cfg;
}

/**
 * Return the list of view ids in dropdown order.
 */
function getViewIds() {
  return VIEW_IDS;
}

/**
 * Return the column array from a loaded config.
 */
function getColumns(cfg) {
  return cfg.columns || [];
}

/**
 * Return the gutter settings from a loaded config.
 * Defaults if missing.
 */
function getGutterConfig(cfg) {
  var g = cfg.gutter || {};
  return {
    width: g.width || 6,
    default_open: g.default_open !== false,
    auto_collapse_at: g.auto_collapse_at || 800,
  };
}

/**
 * Return the data sources array from a loaded config.
 */
function getDataSources(cfg) {
  return cfg.data_sources || [];
}

/**
 * Return the responsive breakpoint rules from a loaded config.
 */
function getBreakpointRules(cfg) {
  return (cfg.responsive && cfg.responsive.breakpoints) ? cfg.responsive.breakpoints : [];
}

/**
 * Given the active config and current viewport width, return a Set of
 * column ids that should be HIDDEN at this width. Columns NOT in this
 * set are visible.
 */
function getHiddenColumns(cfg, viewportWidth) {
  var rules = getBreakpointRules(cfg);
  var hidden = new Set();
  for (var i = 0; i < rules.length; i++) {
    var rule = rules[i];
    if (viewportWidth <= rule.max_width) {
      var hide = rule.hide || [];
      for (var j = 0; j < hide.length; j++) hidden.add(hide[j]);
    }
  }
  return hidden;
}

/**
 * Compute the total sticky-column width (sum of widths for columns with
 * sticky:true in the config). Used by the gutter drag handler to know
 * how far left the columns can slide.
 */
function getStickyTotalWidth(cfg) {
  var total = 0;
  var cols = cfg.columns || [];
  for (var i = 0; i < cols.length; i++) {
    if (cols[i].sticky) total += cols[i].width || 0;
  }
  return total;
}

/**
 * Build a responsive-hide CSS class name for a column id + breakpoint width.
 * e.g. "col-resp-hide-600"
 */
function respHideClass(maxWidth) {
  return 'col-resp-hide-' + maxWidth;
}

export {
  loadViewerConfig,
  getViewIds,
  getColumns,
  getGutterConfig,
  getDataSources,
  getBreakpointRules,
  getHiddenColumns,
  getStickyTotalWidth,
  respHideClass,
};
