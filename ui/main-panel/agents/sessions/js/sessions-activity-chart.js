'use strict';

// Lane order is not this chart's own opinion: it is the chat session picker's
// order, applied through the SAME shared comparator the picker and the sessions
// table use, so a lane's position and a table row's position cannot drift apart.
import { compareSessionsByPinnedOrder } from '../../../../shared/js/session-ordering.js';
// Same spring-back overscroll the chat transcript uses, so the strip's edge feel
// matches the surface the user already knows.
import { applyRubberBand } from '../../../../shared/js/rubber-band.js';

/**
 * Sessions activity chart — one vertical lane per session, drawn above the
 * sessions table.
 *
 * X is session order, and that order is not this chart's own opinion: it is the
 * shared SESSION LIST ORDER the sessions page's table and the chat session picker
 * both use (ui/shared/js/session-ordering.js) — pinned sessions first in their
 * user-defined order, then everything else newest-first. Applying one shared
 * comparator means a lane's position and a table row's position cannot drift apart.
 * The strip lays that order out with the NEWEST on the RIGHT, so a lane's index is
 * mirrored into its position: the list's first row is the far-right lane and
 * scrolling left walks back in time. The strip therefore opens already scrolled to
 * the newest end rather than to the oldest.
 *
 * Y is a SHARED time range read on a LOG scale, and the bars are CENTRE-aligned. Every
 * lane is measured against the longest session, and a lane's bar straddles the median
 * line — half its height above, half below. Height is logarithmic, so equal steps of
 * height are equal RATIOS of duration rather than equal amounts of it: a session ten
 * times as long sits one step higher, not ten times as tall (see durationShare). That
 * is what makes the strip readable, because real spans run from seconds to days — on a
 * linear range every session under a couple of hours collapses onto the MIN_LANE_H floor
 * and the few multi-day ones own the whole plot, so the lanes a reader could actually
 * compare are the ones drawn identically. Every lane still clears MIN_LANE_H and the
 * longest still spans the full plot, so heights order the lanes exactly as their
 * durations do, and the exact figure rides on the tooltip. The median line is the plot's
 * only reference, so there is no Y axis and no gutter — which is also why the scale is
 * stated here rather than on an axis label.
 *
 * Token use is carried by the bar's WIDTH. Each lane's own span is cut into equal
 * sections: an IDLE section is a fixed IDLE_W block in the idle tone, and a RUN section
 * — one that burned tokens — is that block PLUS its token share, drawn in the run tone.
 * The BLOCK is the unit that reads, not the section, and the two kinds are drawn
 * differently on purpose. Consecutive IDLE sections are joined into one solid quiet block
 * (see JOINT_LAP), because a quiet stretch is ONE thing: holding its sections apart carved it
 * into a machine-made ladder of dashes and so asserted structure the data never had.
 * Consecutive RUN sections are the opposite — each run block is its own element, held RUN_GAP
 * clear of the run beside it — because the run profile is what the strip is read for. A run's
 * WIDTH carries its token share (IDLE_W plus that share, so a run always reads wider than the
 * quiet block around it and the busiest swells to the lane's full width), so keeping the run
 * blocks apart is what shows how the session's token use was DISTRIBUTED across its runs,
 * where one merged run blob would show only the extent they span together. A change of kind is
 * therefore not the only visible boundary, and the quiet stretches between runs stay single
 * solid blocks. A session whose work happened in one stretch reads as a stack of run-coloured
 * blocks; a longer session of alternating runs and idle reads as those stacks sitting between
 * quiet idle-coloured blocks, so both WHERE the work happened and HOW LONG the quiet stretches
 * were are visible at a glance. The section count follows the bar's own height, so a long
 * session naturally holds more run/idle blocks than a short one. The data comes from the
 * per-call usage ledger rather than from daily totals so the profile reflects WHEN the calls
 * actually happened.
 *
 * Colour carries PINNED vs the rest, not the lane's identity: a pinned session draws
 * its own tone pair (PINNED_RUN_COLOR / PINNED_IDLE_COLOR, the model palette's second
 * colour and its muted relative) while every other lane draws the first colour's pair.
 * Within a pair the two tones are the whole vocabulary — quiet vs active — so lanes
 * are compared on the SHAPE of their runs, and the pinned sessions the list leads with
 * are identifiable at a glance without a legend. All four tones come from the
 * model-analytics chart's own theme (see RUN_COLOR / PINNED_RUN_COLOR below), so this
 * still reads as the same chart family as the Abilities and Model pages.
 *
 * The shell deliberately reuses the model-analytics chart classes
 * (`.ac-model-analytics*`, see
 * ui/main-panel/app/settings/agent-settings/model-analytics.js) so this reads as
 * the same chart family as the Abilities and Model pages. Only the plot itself is
 * new.
 *
 * The panel carries NO header: the plot is the whole surface. The one-line
 * summary ("N sessions · X tokens · longest session …") sits behind the chart
 * head's icon button as a popover instead of taking a row.
 *
 * A STATS PANEL is parked in the plot's top-left corner, mirroring the Model
 * analytics chart's pinned tip (model-analytics.js, TIP-PINNED-PANEL, on the
 * shared .ac-model-analytics-tip shell). It is pointer-transparent, so the lanes
 * beneath it stay clickable, and it carries two states: with no lane picked it
 * SUMMARISES the whole strip — session count, tokens, and the active/idle split
 * the two tones already encode — and picking a lane swaps it to that lane's own
 * figures (date, total time, tokens, calls) plus a single action, an "Open session"
 * link. Picking a lane is chart-LOCAL apart from that link: the pick itself opens
 * nothing, it only aims this panel at the lane and grows that lane by half so the
 * lane the figures describe is unmistakable (the [aria-pressed] rule in
 * sessions.css), and picking the same lane again returns the panel to the summary,
 * so it always has a way home. The link is the one deliberate way out of the chart:
 * it hands the pick to the page, which opens that session in the chat panel.
 *
 * The strip draws one session per PITCH (25) CSS px, so how many lanes are on
 * screen follows the panel's available width rather than a fixed count. The
 * drawing is as wide as the lanes need, so reaching the older lanes means panning
 * the strip sideways (see below).
 *
 * A lane is picked with a CLICK — mouse and touch share that one code path, so a tap and a
 * click cannot diverge. Picking a lane tints it, grows it, and aims the stats panel at it;
 * picking the SAME lane again returns the panel to the summary. There is no drag-to-scrub
 * gesture: a drag across the chart picks nothing. `touch-action: pan-y` on the strip
 * (sessions.css) still refuses the browser's horizontal pan for the WHOLE chart, so a
 * touch drag scrolls the page vertically rather than the lanes out from under the finger.
 *
 * The strip hides its native scrollbar and rubber-bands at both ends instead
 * (same shared applyRubberBand spring the chat transcript uses). Panning it sideways is
 * the WHEEL's job rather than a drag's, so a wheel scroll through the middle of the range
 * stays native and only a scroll past an edge is translated and sprung back. The strip
 * also owns `overscroll-behavior-x: none` (see sessions.css) so a horizontal overscroll
 * can never chain out to the browser and trigger back/forward history nav.
 *
 * Data: GET /admin/settings/session-activity (app/admin/settings.py), asked for
 * FETCH_LANES sessions.
 */

// The two tone PAIRS. RUN_COLOR is the model-analytics chart's PRIMARY series
// colour (COLORS[0] in
// ui/main-panel/app/settings/agent-settings/model-analytics.js) and
// PINNED_RUN_COLOR is its second (COLORS[1]), so a pinned lane's blocks are literally
// two colours from the same palette the Model and Abilities pages draw with. Each
// idle tone is its run tone desaturated and held at mid lightness (see mutedColor),
// so idle reads as the same family at clearly lower intensity rather than as an
// unrelated grey — and, because the lightness is mid-range rather than a wash, all
// four tones stay legible on the light and the dark theme.
const RUN_COLOR = '#2f80ed';
const PINNED_RUN_COLOR = '#9b51e0';
const IDLE_COLOR = mutedColor(RUN_COLOR);
const PINNED_IDLE_COLOR = mutedColor(PINNED_RUN_COLOR);

// Lane pitch: exactly one session per 25 CSS px. viewBox units are 1:1 with CSS
// px here (the SVG's CSS width is set to its own viewBox width, see render()), so
// how many sessions are on screen falls out of the panel's AVAILABLE WIDTH rather
// than from a fixed lane count. The strip is drawn as wide as the lanes need, so
// anything past the right edge is reached by scrolling it.
const PITCH = 25;

// Sessions asked of the server and drawn into the strip. This bounds the payload
// on an account with thousands of sessions; the strip scrolls through them.
const FETCH_LANES = 120;

// Every section is drawn, in one of two widths. An IDLE section (no tokens in that
// slice of the lane's span) is a fixed IDLE_W block in the lane's idle tone. A RUN
// section is IDLE_W PLUS its token share, in the lane's run tone — so the quietest
// run still reads wider than the idle track around it and the busiest swells to
// IDLE_W + LANE_MAX_W. Each lane's own span is cut into equal sections (SLICE_PX tall
// in viewBox units, capped at MAX_SLICES), so a lane reads as a profile rather than as
// one block per call.
const IDLE_W = 5;        // fixed width of a section with no token use
const BASELINE_W = 2;    // the "normal" run width before any token share is added
const LANE_MAX_W = 16;   // widest a run swells to; IDLE_W + this still clears PITCH
const SLICE_PX = 5;
const MAX_SLICES = 120;

// The VISIBLE separation between two blocks of DIFFERENT kind — one quiet stretch and one
// active one — in viewBox units. Half the gap comes off each side of the boundary, so both
// blocks stay centred in their own slots AND the bar's OUTER extent is untouched: the bottom
// block keeps its exact bottom edge and the top block its exact top edge, which is what keeps
// laneH, the centre alignment on the median line and the height ordering meaning what they
// say. See the section loop in render().
const SEAM_GAP = 2;

// The VISIBLE separation between two consecutive RUN sections, in viewBox units. Unlike a
// quiet stretch, the run profile is not one thing: each run block's WIDTH is its own token
// share, so a run is drawn as its own element held clear of the run beside it, and the stack of
// blocks then shows how the session's token use was distributed. It is deliberately TIGHTER
// than SEAM_GAP, which marks the louder quiet↔active change. Taken half from each side like
// SEAM_GAP, so a run block still sits centred in its own slot and a run span's OUTER edges —
// the lane's own top and bottom when the session never went quiet — stay exact.
const RUN_GAP = 1;

// The INVISIBLE overlap between two consecutive IDLE sections, in viewBox units. Quiet sections
// form ONE block, and joined blocks still need an overlap: when two rects share an edge that
// lands on a fractional pixel that row is only partly covered by each neighbour, so the dark
// plot bleeds through it as a hairline — the exact seam this chart has already had to fix once.
// Reaching past the shared edge puts the row fully inside both rects, and because both sides
// wear the same tone the overlap itself cannot be seen. It costs no meaning: the pair's OUTER
// edges still sit on their true slot edges, so a joined block spans its own stretch exactly and
// only the interior step moves by half a pixel.
const JOINT_LAP = 1;

// Floor for a lane's bar height, and the bottom of the log scale. A lane's height is
// MIN_LANE_H plus its share of the range (see durationShare), so a session with no
// measurable span — or one lasting seconds — still reads as one small block on the
// median line instead of an invisible hairline, while the longest session still reaches
// the full plot height.
const MIN_LANE_H = 6;

// One minute: the duration the log scale is anchored at, and the reason it exists.
// Session spans run from a few seconds to several days, so on a linear range everything
// under a couple of hours rounded onto the MIN_LANE_H floor while the multi-day sessions
// owned the whole plot — the lanes a reader could actually compare were the ones drawn
// identically. Anchoring on a FIXED floor rather than on the shortest span actually
// present keeps the scale stable account to account: a lone 2-second session must not
// restretch every other lane's strip.
const LOG_FLOOR_MS = 60_000;

// Each mounted chart owns one details popover, so the panel id must be unique
// per host (a page can mount more than one chart).
let popoverSeq = 0;

function esc(value) {
  const node = document.createElement('span');
  node.textContent = String(value ?? '');
  return node.innerHTML;
}

/** Escape for use inside a double-quoted attribute (esc() leaves quotes alone). */
function attr(value) {
  return esc(value).replace(/"/g, '&quot;');
}

function formatTokens(value) {
  const n = Number(value || 0);
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(n >= 10_000_000_000 ? 0 : 1).replace(/\.0$/, '')}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(Math.round(n));
}

function formatDuration(ms) {
  const total = Math.max(0, Math.round(Number(ms) || 0));
  const secs = Math.round(total / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ${mins % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

/**
 * A lane's share of the shared Y range, on a LOG scale.
 *
 * `duration` is the lane's own span in ms and `span` the longest session in the strip.
 * The result is 0..1 — 0 for a session with no measurable span, 1 for the longest one —
 * so render() can lay the range over [MIN_LANE_H, plotH] and keep both ends exact.
 *
 * log1p against LOG_FLOOR_MS rather than a plain log, because session spans legitimately
 * reach ZERO (a session whose calls all landed in the same instant) and a plain log would
 * send those to -Infinity. The floor, not the smallest span actually present, fixes the
 * scale, so one account's 3-second outlier cannot restretch every other account's strip.
 * Two sessions an equal RATIO apart therefore differ by the same height anywhere on the
 * scale, which is the property that keeps a 40-second session distinguishable from a
 * 4-minute one while a 68-hour one still fits the same plot.
 */
function durationShare(duration, span) {
  const value = Math.max(0, Number(duration) || 0);
  const top = Math.max(0, Number(span) || 0);
  const ceiling = Math.log1p(top / LOG_FLOOR_MS);
  if (!(ceiling > 0)) return 0;
  return Math.min(1, Math.max(0, Math.log1p(value / LOG_FLOOR_MS) / ceiling));
}

/** HSL triple for a palette hex or an `hsl(h, s%, l%)` string, else null. */
function colorToHsl(color) {
  const text = String(color || '').trim();
  const hex = /^#([0-9a-f]{6})$/i.exec(text);
  if (hex) {
    const n = parseInt(hex[1], 16);
    const r = ((n >> 16) & 255) / 255;
    const g = ((n >> 8) & 255) / 255;
    const b = (n & 255) / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const d = max - min;
    let h = 0;
    if (d) {
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h = (h * 60 + 360) % 360;
    }
    const l = (max + min) / 2;
    const s = d ? d / (1 - Math.abs(2 * l - 1)) : 0;
    return { h, s: s * 100, l: l * 100 };
  }
  const hsl = /^hsl\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%\s*\)$/i.exec(text);
  if (hsl) return { h: Number(hsl[1]), s: Number(hsl[2]), l: Number(hsl[3]) };
  return null;
}

/** The idle tone: RUN_COLOR with its saturation pulled down and its lightness held at
 *  mid range. Idle therefore reads as clearly a different colour from the run blocks
 *  while still belonging to the same theme — and, because the lightness is mid-range
 *  rather than a wash, it stays legible on both the light and the dark theme instead
 *  of vanishing into one of them. */
function mutedColor(color) {
  const hsl = colorToHsl(color);
  if (!hsl) return color;
  return `hsl(${Math.round(hsl.h)}, 24%, 50%)`;
}

/**
 * Order lanes exactly as the chat session picker orders sessions.
 *
 * The shared comparator keys on `id`, `pinned` / `sort_order` / `recycled` and
 * `activity_at`, so each lane is re-offered under those names. The server
 * supplies a real `activity_at` on every lane (it folds in a terminal run's
 * completion time, exactly as the picker's own query does), so this is a true
 * ranking rather than one that silently collapses onto a tie-break. Should a
 * lane arrive without it, the lane's own last call stands in — never an absent
 * field, which would flatten the whole chart onto the id tie-break.
 */
function orderLanes(sessions) {
  const keyed = sessions.map((session) => ({
    ...session,
    id: session.session_id,
    activity_at: session.activity_at || session.ended_at || session.started_at || '',
  }));
  return keyed.sort(compareSessionsByPinnedOrder);
}

/**
 * Mount the chart into `host`. Returns `{ reload, destroy }`.
 *
 * Options:
 *   fetchFn     — fetch wrapper (defaults to window.fetch)
 *   apiPath     — path mapper (defaults to identity)
 *   headers     — () => headers object for the request
 *   onOpen      — (sessionId, agentId, title) => void, run by the stats panel's
 *                 "Open session" link. Optional: with no callback the panel is
 *                 read-only and the link is simply inert.
 *
 * Activating a lane is chart-local: it points the pinned stats panel at that lane
 * rather than reporting the pick out to the caller. The panel's "Open session" link
 * is the single exception, and that goes through `onOpen`.
 */
export function initSessionsActivityChart(host, options = {}) {
  if (!host || host.dataset.sessionsActivityMounted === '1') return null;
  host.dataset.sessionsActivityMounted = '1';

  const fetchFn = typeof options.fetchFn === 'function' ? options.fetchFn : ((url, opts) => fetch(url, opts));
  const pathFn = typeof options.apiPath === 'function' ? options.apiPath : ((path) => path);
  const headerFn = typeof options.headers === 'function' ? options.headers : (() => ({}));
  let payload = null;
  let loading = false;
  // The lane whose figures the pinned panel is showing, or '' while it is summing
  // the whole strip. Chart-local — it never opens a session.
  let selectedId = '';
  // The lanes from the last render, kept so the panel can total them (and find the
  // picked lane) without re-deriving anything.
  let lastLanes = [];
  // True once the plot has been drawn at least once — the FIRST paint opens the strip
  // at the newest end, and later re-renders leave the reader where they were.
  let painted = false;
  // Signature of the last request (scope + range) so the 30s table refresh does
  // not re-fetch an unchanged chart, and a scope change always does.
  let lastSignature = '';
  let lastScope = { agentId: '' };

  const popoverId = `sessions-activity-details-${++popoverSeq}`;

  // No header text: the head holds only the details button, exactly like the
  // model chart's controls-only head. The summary the old status line carried
  // now lives inside the popover below.
  host.innerHTML = `
    <section class="ac-model-analytics sessions-activity" aria-label="Session activity chart">
      <header class="ac-model-analytics-head">
        <div class="ac-model-analytics-controls" aria-label="Chart details">
          <button type="button" class="ac-model-analytics-more sessions-activity-details" data-activity-details aria-haspopup="true" aria-label="Session activity details" title="Session activity details" hidden><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="8" cy="3.2" r="1.45"></circle><circle cx="8" cy="8" r="1.45"></circle><circle cx="8" cy="12.8" r="1.45"></circle></svg></button>
        </div>
      </header>
      <div class="ac-model-analytics-popover" id="${popoverId}" popover="auto">
        <p class="sessions-activity-summary" data-activity-summary></p>
      </div>
      <div class="sessions-activity-plot">
        <div class="ac-model-analytics-chart sessions-activity-chart" data-activity-chart aria-live="polite"></div>
        <div class="ac-model-analytics-tip sessions-activity-tip" data-activity-tip hidden></div>
      </div>
    </section>`;

  const chartEl = host.querySelector('[data-activity-chart]');
  const tipEl = host.querySelector('[data-activity-tip]');
  const detailsBtn = host.querySelector('[data-activity-details]');
  const detailsPanel = host.querySelector('.ac-model-analytics-popover');
  const summaryEl = host.querySelector('[data-activity-summary]');

  // The native scrollbar is hidden (sessions.css) and the edges spring back
  // instead. applyRubberBand only intercepts a gesture that is ALREADY at an
  // edge, so normal scrolling through the middle of the range stays native — it
  // never hijacks a scroll that still has somewhere to go. Feel is deliberately
  // the transcript's short, responsive pull rather than the shared default:
  // resistance 0.5 / 220ms spring, or the strip reads as sticky near the cap.
  applyRubberBand(chartEl, {
    axis: 'x',
    pullThreshold: 50,
    maxPull: 50,
    resistance: 0.5,
    snapDuration: 220,
    snapEasing: 'cubic-bezier(0.2, 0.85, 0.3, 1)',
    // Arm the right-edge pull within 2px of the true max scroll. A tighter
    // tolerance strands the gesture: trackpad momentum rarely settles on the
    // exact pixel, so the band would simply never engage at the newest end —
    // which is where the strip opens, so that is the edge reached first.
    bottomEdgeTol: 2,
  });

  // ── Plot ────────────────────────────────────────────────────────────────

  function render() {
    const sessions = Array.isArray(payload?.sessions) ? payload.sessions : [];
    if (!sessions.length) {
      chartEl.innerHTML = '<p class="ac-model-analytics-empty">No token activity yet. Lanes appear here as your agents work.</p>';
      lastLanes = [];
      if (tipEl) { tipEl.hidden = true; tipEl.innerHTML = ''; }
      setStatus('');
      return;
    }

    // H is the view box height, and therefore the SVG's own rendered height too: the
    // CSS width is set to the view box width, so one view box unit is exactly one CSS
    // px. T and B are kept EQUAL on purpose — a picked lane scales up HALF about
    // the median line (the [aria-pressed] rule in sessions.css), so the plot must
    // reserve that much room on BOTH sides of the line or the pop would be cut off by
    // the view box (and it would be cut off for real: .sessions-activity-chart carries
    // overflow-y: hidden, so no overflow trick can rescue it). With T = B = 60 the plot
    // is 240 tall and its centre is the SVG's centre (y=180), so a lane at full plot
    // height, scaled 1.5, spans exactly y=0..360: the whole box, never a clipped edge.
    // plotH is unchanged from the old T=B=30 layout, so every bar height and the log
    // scale stay identical — only the plot's origin shifts down 30 units.
    const H = 360, T = 60, B = 60, R = 16;
    const plotH = H - T - B;
    // The median line: the shared zero every lane's bar is centred on.
    const midY = T + plotH / 2;
    // The strip is exactly as wide as its lanes need at one per PITCH, so the
    // drawing does not stretch to fill a wide panel or cram into a narrow one —
    // the available width decides how many lanes you SEE, and the rest are a
    // scroll away. R is breathing room after the last lane.
    const stripW = Math.max(1, sessions.length) * PITCH + R;

    // SHARED time range on a LOG scale, CENTRE-aligned. `sharedSpan` is the longest
    // session in the strip and therefore the top of the range; every lane's height is its
    // durationShare() of that, so equal steps of height are equal RATIOS of duration
    // rather than equal amounts of it (a session ten times as long is one step taller, not
    // ten times as tall). Each bar still straddles the median line instead of sitting on
    // the floor — half its height above, half below — so the shortest sessions stay
    // readable around the same reference as the longest ones instead of flattening every
    // start along the bottom.
    let longest = 0;
    for (const session of sessions) {
      longest = Math.max(longest, Math.max(0, Number(session.duration_ms) || 0));
    }
    const sharedSpan = longest > 0 ? longest : 1;

    const step = PITCH;

    // Colour carries pinned vs the rest, never a lane's identity: a pinned session
    // draws the PINNED pair and every other lane the primary pair. Within a pair the
    // two tones are the whole vocabulary — quiet vs active — so lanes are compared on
    // the SHAPE of their runs and no legend is needed.

    // Pre-bucket each lane's calls into sections, because the bar's WIDTH is what
    // carries token use. This has to run before drawing: the widest section — the
    // shared token scale every lane is measured against — is only known once all
    // lanes have been bucketed.
    const lanes = sessions.map((session) => {
      const duration = Math.max(0, Number(session.duration_ms) || 0);
      // A session with a single call has no span to normalise against, so its one
      // call lands in the first section — a single block, which is the truth.
      const span = duration > 0 ? duration : 1;
      // The bar is the session's share of the shared range — read on the log scale, so
      // that share says how many RATIOS up from LOG_FLOOR_MS the session sits rather than
      // what fraction of the longest one it is — laid over the plot height ABOVE
      // MIN_LANE_H. A session with no measurable span lands exactly on MIN_LANE_H and the
      // longest fills the plot, so neither end of the range needs a special case.
      const laneH = MIN_LANE_H + durationShare(duration, sharedSpan) * (plotH - MIN_LANE_H);
      // Section resolution is ~SLICE_PX of each lane's OWN height, so a long session
      // naturally holds more run/idle blocks than a short one.
      const sliceCount = Math.max(1, Math.min(MAX_SLICES, Math.round(laneH / SLICE_PX)));
      const buckets = new Array(sliceCount).fill(0);
      for (const point of (Array.isArray(session.points) ? session.points : [])) {
        const offset = Math.min(span, Math.max(0, Number(point[0]) || 0));
        const index = Math.min(sliceCount - 1, Math.floor((offset / span) * sliceCount));
        buckets[index] += Number(point[1]) || 0;
      }
      // Each section covers `span / sliceCount` of the lane's wall-clock span, so the
      // count of token-carrying sections splits that span into the ACTIVE time the run
      // tone stands for and the IDLE time the quiet tone stands for. That is the split
      // the pinned panel totals up across lanes — the same two things the chart draws.
      let runSections = 0;
      for (const tokens of buckets) if (tokens) runSections += 1;
      const slabMs = span / sliceCount;
      const runMs = runSections * slabMs;
      return { session, duration, span, laneH, sliceCount, buckets, runMs, idleMs: Math.max(0, span - runMs) };
    });
    lastLanes = lanes;
    let maxBucket = 0;
    for (const lane of lanes) {
      for (const tokens of lane.buckets) maxBucket = Math.max(maxBucket, tokens);
    }
    maxBucket = Math.max(1, maxBucket);

    const parts = [];

    lanes.forEach((lane, laneIndex) => {
      const { session, duration, span, laneH, sliceCount, buckets } = lane;
      // NEWEST on the RIGHT: the list's first row is the rightmost lane, so a lane's
      // index is mirrored into the strip and scrolling left walks back in time.
      const center = (lanes.length - 1 - laneIndex) * step + step / 2;
      // The bar is CENTRE-aligned on the median line — half the span above it, half
      // below — so a short session is a short bar around the same line rather than a
      // stub on the floor.
      const laneTop = T + (plotH - laneH) / 2;
      const laneBottom = laneTop + laneH;
      const slabH = laneH / sliceCount;
      const pinned = !!session.pinned;
      // Pinned sessions wear the second palette pair so the rows the list leads with
      // are identifiable at a glance; every other lane wears the primary pair.
      const runColor = pinned ? PINNED_RUN_COLOR : RUN_COLOR;
      const idleColor = pinned ? PINNED_IDLE_COLOR : IDLE_COLOR;
      const title = String(session.title || session.session_id || 'Session');
      const startedAt = String(session.started_at || '');
      const baseHalf = BASELINE_W / 2;
      const maxHalf = Math.max(baseHalf, LANE_MAX_W / 2);

      parts.push(`<g class="sessions-activity-lane" data-activity-session="${attr(session.session_id)}" data-activity-agent="${attr(session.agent_id || '')}" data-activity-pinned="${pinned ? '1' : '0'}" role="button" tabindex="0" aria-pressed="${session.session_id === selectedId ? 'true' : 'false'}" aria-label="${attr(`${title}: ${formatTokens(session.total_tokens)} tokens over ${formatDuration(duration)}${pinned ? ' (pinned)' : ''}`)}">`);
      // Hover affordance only: it covers the lane's own bar extent (one PITCH wide,
      // matching the hit target's column) so the tint appears under the lane you are
      // pointing at, and it stays invisible until then. The lane's visible marks are
      // the sections themselves, so a permanently tinted slab would just be a second,
      // meaningless bar sitting behind the real one.
      parts.push(`<rect class="sessions-activity-lane-bg" x="${(center - PITCH / 2).toFixed(2)}" y="${laneTop.toFixed(2)}" width="${PITCH}" height="${laneH.toFixed(2)}" rx="2" fill="${RUN_COLOR}"></rect>`);
      // The hit target spans the full plot height even though the bar does not, so a
      // lane stays hoverable and clickable across the empty space above and below it.
      // It lands BEFORE the sections so each one keeps its own hover title while the
      // lane's quiet stretches stay hoverable.
      parts.push(`<rect class="sessions-activity-hit" x="${(center - PITCH / 2).toFixed(2)}" y="${T}" width="${PITCH}" height="${plotH}" fill="transparent"><title>${esc(`${title} · ${formatTokens(session.total_tokens)} tokens · ${formatDuration(duration)} · ${session.call_count || 0} calls${startedAt ? ` · started ${startedAt.replace('T', ' ').slice(0, 16)}` : ''}${pinned ? ' · pinned' : ''}`)}</title></rect>`);
      // There is no separate full-height rule behind the sections: a lane's extent IS
      // its idle track, which is drawn section by section in the idle tone. A single
      // baseline-width column used to run the session's whole wall-clock span behind
      // everything, and that read as an inner spline threading every quiet stretch
      // together. The idle sections replace it, so quiet stretches are visible as
      // their own quiet-coloured blocks instead of as something showing through.

      // One section per time bucket, and EVERY one is drawn:
      //   idle → IDLE_W wide in the lane's idle tone. No title of its own, and it is
      //          pointer-transparent (see sessions.css), so the lane's hit rect keeps
      //          owning the hover text across the quiet stretches.
      //   run  → IDLE_W + `baseline + token share` in the lane's run tone, so a run
      //          always reads wider than the idle track around it and the busiest run
      //          swells to IDLE_W + LANE_MAX_W. Nothing else varies per run: the pair's
      //          two tones are the whole vocabulary, so lanes are compared on the SHAPE
      //          of their runs, not on their colour.
      // Bucket 0 sits at the lane's BOTTOM because offsets are measured from the
      // session's first call, so a run grows upward with time within the bar. Sections
      // are square-cornered on purpose: rounding each one turned the run profile into a
      // string of beads instead of the stack of solid blocks it should read as.
      //
      // A section's edges follow what sits on that side rather than a fixed inset:
      //   different kind → the column breaks here. The edge pulls back SEAM_GAP/2, so the two
      //          blocks read as separate elements with clear plot between them.
      //   same kind, run → the edge pulls back RUN_GAP/2, so each run block stands as its own
      //          element and the token profile its width carries stays countable.
      //   same kind, idle → the two are ONE quiet block, so they are joined: the edge reaches
      //          JOINT_LAP/2 PAST the shared boundary instead of pulling back from it, which
      //          keeps that row covered by two same-tone rects and so kills the
      //          fractional-pixel hairline the plot would otherwise show through.
      // Either way the LANE's own outer edges stay exact — the bottom section keeps the
      // lane's true bottom edge and the top section its true top edge — so laneH, the centre
      // alignment on the median line and the height ordering all still mean what they say.
      const gapHalf = SEAM_GAP / 2;
      const runGapHalf = RUN_GAP / 2;
      const lapHalf = JOINT_LAP / 2;
      const runAt = (index) => index >= 0 && index < sliceCount && !!buckets[index];
      // Only a quiet stretch is one whole block; every run block stands on its own.
      const edgeInset = (neighbourIsRun, selfIsRun) => {
        if (neighbourIsRun !== selfIsRun) return gapHalf;
        return selfIsRun ? runGapHalf : -lapHalf;
      };
      buckets.forEach((tokens, bucket) => {
        const selfIsRun = !!tokens;
        const insetTop = bucket >= sliceCount - 1 ? 0 : edgeInset(runAt(bucket + 1), selfIsRun);
        const insetBottom = bucket <= 0 ? 0 : edgeInset(runAt(bucket - 1), selfIsRun);
        // Bucket 0 is the lane's BOTTOM, so a slot's own top edge is
        // laneBottom - (bucket + 1) * slabH.
        const y = laneBottom - (bucket + 1) * slabH + insetTop;
        const height = Math.max(1, slabH - insetTop - insetBottom).toFixed(2);
        if (!tokens) {
          parts.push(`<rect class="sessions-activity-idle" x="${(center - IDLE_W / 2).toFixed(2)}" y="${y.toFixed(2)}" width="${IDLE_W}" height="${height}" fill="${idleColor}"></rect>`);
          return;
        }
        const share = Math.min(1, tokens / maxBucket);
        const half = IDLE_W / 2 + baseHalf + share * (maxHalf - baseHalf);
        parts.push(`<rect class="sessions-activity-bar" x="${(center - half).toFixed(2)}" y="${y.toFixed(2)}" width="${(half * 2).toFixed(2)}" height="${height}" fill="${runColor}"><title>${esc(`${title} · +${formatDuration((bucket / sliceCount) * span)} in · ${formatTokens(tokens)} tokens`)}</title></rect>`);
      });
      parts.push('</g>');
    });

    // ── Median line + x axis caption ────────────────────────────────────────
    // The median line is the plot's only reference: every bar is centred on it, so
    // it replaces the floor baseline a bottom-aligned chart would draw.
    parts.push(`<line class="sessions-activity-median" x1="0" y1="${midY.toFixed(1)}" x2="${(stripW - R).toFixed(1)}" y2="${midY.toFixed(1)}"></line>`);
    if (sessions.length > 1) {
      // Lanes run list-order with the NEWEST on the right, so the freshest session
      // sits where the list's first row is and "oldest" is the far-left end the strip
      // scrolls back to. The labels follow the data, not a calendar.
      // Anchored to the PLOT, not to H: T/B exist to reserve the pop's headroom, so
      // pinning the caption to H would drop it far below the bars as that headroom grows.
      const axisY = T + plotH + 12;
      parts.push(`<text class="ac-model-analytics-axis" x="4" y="${axisY.toFixed(1)}">oldest</text>`);
      parts.push(`<text class="ac-model-analytics-axis" x="${(stripW - R).toFixed(1)}" y="${axisY.toFixed(1)}" text-anchor="end">newest</text>`);
    }

    const totalTokens = sessions.reduce((sum, s) => sum + (Number(s.total_tokens) || 0), 0);
    const pinnedCount = sessions.filter((s) => !!s.pinned).length;
    // Newest lives at the RIGHT end, so the strip opens there rather than at the
    // oldest lane. Capture the current position first: replacing the SVG resets
    // scrollLeft, and a re-render should otherwise leave the reader where they were.
    const wasAtNewest = chartEl.scrollLeft + chartEl.clientWidth >= chartEl.scrollWidth - 2;
    const keepLeft = chartEl.scrollLeft;
    // The SVG's CSS width is set to its own viewBox width, so one viewBox unit is
    // exactly one CSS px and PITCH is a literal 25px. Anything wider than the panel
    // is reached by scrolling the strip.
    chartEl.innerHTML = `<svg class="ac-model-analytics-svg sessions-activity-svg" style="width:${stripW}px" viewBox="0 0 ${stripW} ${H}" role="img" aria-label="Token activity across ${sessions.length} sessions">${parts.join('')}</svg>`;
    if (!painted || wasAtNewest) chartEl.scrollLeft = chartEl.scrollWidth;
    else chartEl.scrollLeft = keepLeft;
    painted = true;
    setStatus(`${sessions.length} session${sessions.length === 1 ? '' : 's'}`
      + ` · ${formatTokens(totalTokens)} tokens`
      + ` · longest session ${formatDuration(sessionLongest(sessions))}`
      + (pinnedCount ? ` · ${pinnedCount} pinned` : ''));
    // Paint the corner panel last, so it reflects the lanes just drawn (and the
    // pick carried over from before the re-render).
    paintTip();
  }

  function sessionLongest(sessions) {
    return sessions.reduce((max, s) => Math.max(max, Number(s.duration_ms) || 0), 0);
  }

  // ── Pinned stats panel ──────────────────────────────────────────────────
  // Mirrors the Model chart's TIP-PINNED-PANEL: a readout parked in the plot's
  // top-left corner rather than a pointer follower, on the SAME shell
  // (.ac-model-analytics-tip) so the two read as one family. It is
  // pointer-transparent, so the lanes beneath it stay clickable. With no lane
  // picked it summarises the whole strip; picking a lane swaps it to that lane's
  // own figures.

  function tipRow(color, label, value) {
    return `<div class="ac-model-analytics-tip-row"><i style="background:${color}"></i><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
  }

  /** The unfocused state: the whole strip in four or five lines. */
  function summaryTipHtml(lanes) {
    let tokens = 0, active = 0, idle = 0, pinned = 0;
    for (const lane of lanes) {
      tokens += Number(lane.session.total_tokens) || 0;
      active += lane.runMs;
      idle += lane.idleMs;
      if (lane.session.pinned) pinned += 1;
    }
    return `<div class="ac-model-analytics-tip-date">All sessions</div>`
      + tipRow(RUN_COLOR, 'Sessions', String(lanes.length))
      + tipRow(RUN_COLOR, 'Tokens', formatTokens(tokens))
      + tipRow(RUN_COLOR, 'Active time', formatDuration(active))
      + tipRow(IDLE_COLOR, 'Idle time', formatDuration(idle))
      + (pinned ? tipRow(PINNED_RUN_COLOR, 'Pinned', String(pinned)) : '')
      + `<div class="ac-model-analytics-tip-row ac-model-analytics-tip-total"><span>Total time</span><b>${esc(formatDuration(active + idle))}</b></div>`;
  }

  /**
   * The focused state: one lane's own figures, headed by its title, and the panel's one
   * action — a link that opens the picked session in the chat panel.
   *
   * Picking a lane is chart-local (it only aims this panel), so this link is the single
   * way a lane leads OUT of the chart. It carries the lane's agent id as well as its
   * session id, because a session belongs to an agent and the chat has to switch to that
   * agent to show it. The control is a <button>, since it acts rather than navigates,
   * but it is styled as a link (see sessions.css) — the panel is a readout, and this is
   * the one place it leads anywhere.
   */
  function laneTipHtml(lane) {
    const { session, duration } = lane;
    const pinned = !!session.pinned;
    const tone = pinned ? PINNED_RUN_COLOR : RUN_COLOR;
    const title = String(session.title || session.session_id || 'Session');
    const startedAt = String(session.started_at || '');
    return `<div class="ac-model-analytics-tip-date">${esc(title)}${pinned ? ' · pinned' : ''}</div>`
      + tipRow(tone, 'Date', startedAt ? startedAt.replace('T', ' ').slice(0, 16) : '—')
      + tipRow(tone, 'Total time', formatDuration(duration))
      + tipRow(tone, 'Tokens', formatTokens(session.total_tokens))
      + tipRow(IDLE_COLOR, 'Calls', String(session.call_count || 0))
      + `<div class="sessions-activity-tip-foot"><button type="button" class="sessions-activity-open" data-activity-open="${attr(session.session_id)}" data-activity-open-agent="${attr(session.agent_id || '')}" title="Open this session in the chat panel">Open session</button></div>`;
  }

  function paintTip() {
    if (!tipEl) return;
    if (!lastLanes.length) { tipEl.hidden = true; tipEl.innerHTML = ''; return; }
    const lane = selectedId ? lastLanes.find((entry) => entry.session.session_id === selectedId) : null;
    tipEl.hidden = false;
    tipEl.innerHTML = lane ? laneTipHtml(lane) : summaryTipHtml(lastLanes);
  }

  /**
   * The panel's one action: hand a lane to the page, which opens that session in the
   * chat panel. Picking a lane is otherwise chart-local, so this is the only route out
   * of the chart and it exists only when the caller wired `onOpen` (see
   * _wireActivityChart in sessions-page.js). With no callback the panel stays a pure
   * readout and the link does nothing.
   */
  function requestOpen(sessionId, agentId) {
    if (!sessionId || typeof options.onOpen !== 'function') return;
    const lane = lastLanes.find((entry) => entry.session.session_id === sessionId);
    options.onOpen(sessionId, agentId || '', lane ? String(lane.session.title || '') : '');
  }

  // The link is the ONE part of the panel that accepts the pointer: the panel shell is
  // pointer-transparent so the lanes beneath it stay clickable (see
  // .ac-model-analytics-tip), and only the link opts back in. The listener therefore
  // rides the panel — which is not the link's ancestor in the lanes' tree — and acts
  // only on the link, so every other press still reaches the lane underneath.
  if (tipEl) {
    tipEl.addEventListener('click', (event) => {
      const link = event.target && event.target.closest ? event.target.closest('[data-activity-open]') : null;
      if (!link) return;
      requestOpen(link.dataset.activityOpen, link.dataset.activityOpenAgent);
    });
  }

  // ── Details popover ─────────────────────────────────────────────────────
  // The one-line summary lives behind the chart head's icon button instead of
  // taking a row of its own, so the plot is the whole panel. This mirrors the
  // model chart's popover exactly (see model-analytics.js openAnchoredPopover):
  // a native `popover="auto"` panel — so it light-dismisses and escapes every
  // overflow box — positioned under its trigger by hand, because
  // .ac-model-analytics-popover is fixed with `inset: auto`.
  let suppressPopoverUntil = 0;

  /** Show the icon button when there is a summary to report, and mirror that
   *  summary into the panel. An empty summary hides the button outright. */
  function setStatus(text) {
    const value = String(text || '');
    if (summaryEl) summaryEl.textContent = value;
    if (detailsBtn) {
      detailsBtn.hidden = !value;
      detailsBtn.setAttribute('aria-label', value ? `Session activity details: ${value}` : 'Session activity details');
    }
    if (!value && detailsPanel && detailsPanel.matches(':popover-open')) detailsPanel.hidePopover();
  }

  function openDetails() {
    const panel = detailsPanel;
    if (!detailsBtn || !panel || !panel.showPopover) return;
    if (!summaryEl || !summaryEl.textContent) return;
    if (panel.matches(':popover-open')) return;
    const anchor = detailsBtn.getBoundingClientRect();
    panel.style.visibility = 'hidden';           // measurable before placing
    panel.showPopover();
    const rect = panel.getBoundingClientRect();
    const gap = 8;
    const maxLeft = Math.max(gap, window.innerWidth - rect.width - gap);
    const maxTop = Math.max(gap, window.innerHeight - rect.height - gap);
    // The button sits at the panel's top-right, so the popover right-aligns under
    // it rather than opening off the right edge of the viewport.
    const left = anchor.right - rect.width;
    const top = anchor.bottom + gap;
    panel.style.left = `${Math.min(Math.max(gap, left), maxLeft)}px`;
    panel.style.top = `${Math.min(Math.max(gap, top), maxTop)}px`;
    panel.style.visibility = '';
  }

  // A native popover light-dismisses on pointerdown, so by the time the click
  // lands the open panel already reads as closed and the handler below would
  // just re-open it. Catch the toggle-off during pointerdown and suppress the
  // reopen — the same fix the model chart uses.
  host.addEventListener('pointerdown', (event) => {
    const trigger = event.target.closest && event.target.closest('[data-activity-details]');
    if (!trigger || !detailsPanel || !detailsPanel.matches(':popover-open')) return;
    suppressPopoverUntil = performance.now() + 500;
    detailsPanel.hidePopover();
  }, true);

  host.addEventListener('click', (event) => {
    const trigger = event.target.closest && event.target.closest('[data-activity-details]');
    if (!trigger || performance.now() < suppressPopoverUntil) return;
    openDetails();
  });

  // ── Data ────────────────────────────────────────────────────────────────

  async function reload(scope = {}, opts = {}) {
    const agentId = scope && scope.agentId ? String(scope.agentId) : '';
    // A native Codex catalog has its own authority and no usage_events rows, so
    // the chart has nothing honest to draw for it.
    if (scope && scope.native) {
      host.hidden = true;
      if (detailsPanel && detailsPanel.matches(':popover-open')) detailsPanel.hidePopover();
      return;
    }
    host.hidden = false;
    lastScope = { agentId };
    const signature = agentId;
    if (!opts.force && signature === lastSignature && payload) return;
    lastSignature = signature;
    loading = true;
    if (!payload) setStatus('Loading session activity…');
    try {
      const qs = new URLSearchParams({ days: '0', limit: String(FETCH_LANES) });
      if (agentId) qs.set('agent_id', agentId);
      let token = '';
      try { token = localStorage.getItem('auth_token') || ''; } catch (_) { token = ''; }
      if (token) qs.set('token', token);
      const response = await fetchFn(pathFn(`/admin/settings/session-activity?${qs.toString()}`), { headers: headerFn() });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (signature !== lastSignature) return;   // a newer request superseded this one
      if (data && data.error) throw new Error(String(data.error));
      payload = data || { sessions: [] };
      if (Array.isArray(payload.sessions)) payload.sessions = orderLanes(payload.sessions);
      render();
    } catch (error) {
      if (signature !== lastSignature) return;
      payload = { sessions: [] };
      chartEl.innerHTML = '<p class="ac-model-analytics-empty">Session activity is unavailable right now.</p>';
      lastLanes = [];
      if (tipEl) { tipEl.hidden = true; tipEl.innerHTML = ''; }
      setStatus('');
      console.warn('[Sessions] Activity chart load failed', error);
    } finally {
      loading = false;
    }
  }

  // ── Interaction ─────────────────────────────────────────────────────────

  function laneTarget(event) {
    const node = event.target && event.target.closest ? event.target.closest('[data-activity-session]') : null;
    return node && chartEl.contains(node) ? node : null;
  }

  // Pointing at a lane is chart-LOCAL: it aims the pinned panel at that lane instead of
  // opening anything. `setPick` is the ABSOLUTE form, `selectLane` is the TOGGLE that a
  // click, tap or keypress uses, so activating the picked lane again drops back to the
  // whole-strip summary and the panel always has a way home. Mouse and touch share this
  // one path — the click listener below — so they cannot diverge.
  function setPick(sessionId) {
    selectedId = String(sessionId || '');
    chartEl.querySelectorAll('[data-activity-session]').forEach((node) => {
      node.setAttribute('aria-pressed', node.dataset.activitySession === selectedId ? 'true' : 'false');
    });
    paintTip();
  }

  function selectLane(sessionId) {
    setPick(sessionId === selectedId ? '' : sessionId);
  }
  chartEl.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const lane = laneTarget(event);
    if (!lane) return;
    event.preventDefault();
    selectLane(lane.dataset.activitySession);
  });

  // ── Pick: one click path for mouse and touch ───────────────────────────
  //
  // `click` fires on pointerup, so a press that turns into a drag or a page scroll never
  // reaches this handler and picks nothing — there is no gesture to arm and no distance to
  // travel. The same handler serves a mouse click and a finger tap: they cannot diverge,
  // which is exactly what "just like mouse clicking" asks for. `selectLane` is the toggle,
  // so re-tapping an already-picked lane drops the panel back to the strip summary.
  chartEl.addEventListener('click', (event) => {
    const lane = laneTarget(event);
    if (!lane) return;
    selectLane(lane.dataset.activitySession);
  });

  return {
    reload,
    destroy() {
      if (detailsPanel && detailsPanel.matches(':popover-open')) detailsPanel.hidePopover();
      host.dataset.sessionsActivityMounted = '';
      host.innerHTML = '';
    },
  };
}
