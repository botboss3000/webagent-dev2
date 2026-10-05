'use strict';

import { advanceDeleteBtn, resetDeleteBtn } from './delete-control.js';

/**
 * Model catalog analytics — a standalone panel mounted before the shared model
 * table. The chart is deliberately independent of model-table.js because the
 * admin catalog needs a cross-model comparison rather than one row's history.
 */

const COLORS = ['#2f80ed', '#9b51e0', '#00b894', '#f2c94c', '#ec4899', '#06b6d4', '#84cc16', '#6366f1'];
const DRAG_THRESHOLD = 6;
// Plural / singular nouns per grouping, shared by the legend's accessible name
// and the empty-state sentence so the two can never drift apart.
const GROUP_NOUNS = { models: 'Models', sessions: 'Sessions', agents: 'Agents' };
const GROUP_NOUN = { models: 'model', sessions: 'session', agents: 'agent' };

function formatTokens(value) {
  const n = Number(value || 0);
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(n >= 10_000_000_000 ? 0 : 1).replace(/\.0$/, '')}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(Math.round(n));
}

function formatCost(value) {
  const n = Number(value || 0);
  return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

function niceCeiling(value) {
  if (!(value > 0)) return 1;
  const exponent = Math.floor(Math.log10(value));
  const unit = 10 ** exponent;
  const fraction = value / unit;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return nice * unit;
}

function esc(value) {
  const node = document.createElement('span');
  node.textContent = String(value ?? '');
  return node.innerHTML;
}

let analyticsInstanceCount = 0;

export function initModelAnalytics(host, { fetchFn, apiPath, scope = 'global', agentId = '', description = '', nestedAgentStacks = false }) {
  if (!host || host.dataset.modelAnalyticsMounted === '1') return null;
  host.dataset.modelAnalyticsMounted = '1';
  const instanceId = ++analyticsInstanceCount;
  const scaleInputId = `ac-model-analytics-y-max-${instanceId}`;
  let payload = null;
  let metric = 'tokens';
  let interval = 'day';
  let loadVersion = 0;
  // 'stacked' (the default) renders one bar per day whose segments are the
  // series, so the bar height is the day's total across series. 'grouped' keeps
  // the previous side-by-side bars, one per series.
  let layout = 'stacked';
  // models | sessions | agents — what the daily totals are broken down by. The
  // chart menu exposes this as the "Group by" choice.
  let groupBy = 'models';
  // 0 is the explicit "all available history" sentinel. Fixed values remain
  // user-selected trailing calendar windows.
  let dateRange = 0;
  let visibleModels = null;
  let selectedBuckets = null;
  let geometry = null;
  // Active x-axis band drag: {x0, width} in band-local pixels, or null.
  let rangeDrag = null;
  // Live pointers over the plot, keyed by pointerId, plus the state of an
  // in-progress two-finger pinch.
  const activePointers = new Map();
  let pinch = null;
  let suppressPopoverUntil = 0;
  // MODEL-LEGEND-ALIAS: model id → display name, supplied by the sibling model
  // table after each roster load. Empty until that table reports in, and empty for
  // any model the roster does not cover; both fall back to the raw id below.
  let modelLabels = new Map();

  host.innerHTML = `
    <section class="ac-model-analytics" aria-label="Model usage chart">
      <header class="ac-model-analytics-head">
        <div class="ac-model-analytics-controls" aria-label="Chart controls"><button type="button" class="ac-model-analytics-reset-range" data-model-analytics-reset-range aria-label="Reset selected range" title="Reset selected range" hidden><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M13.5 7.25A5.7 5.7 0 1 1 11.9 3.2"></path><path d="M12 1.75v3.1H8.9"></path></svg></button><button type="button" class="ac-model-analytics-more" data-chart-popover="more" aria-label="More chart options" aria-haspopup="true" title="More chart options"><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="8" cy="3.2" r="1.45"></circle><circle cx="8" cy="8" r="1.45"></circle><circle cx="8" cy="12.8" r="1.45"></circle></svg></button></div>
      </header>

      <div class="ac-model-analytics-popover" id="${scaleInputId}-range" popover="auto"><label>Date range <select class="ac-input ac-input-sm" data-model-analytics-range><option value="0" selected>All available history</option><option value="7">Last 7 days</option><option value="14">Last 14 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option></select></label></div>
      <div class="ac-model-analytics-popover" id="${scaleInputId}-models" popover="auto"><div data-model-analytics-models></div></div>
      <div class="ac-model-analytics-popover" id="${scaleInputId}-more" popover="auto">
        <div class="ac-model-analytics-menu-group">
          <label>Time interval <select class="ac-input ac-input-sm" data-model-analytics-interval aria-label="Time interval"><option value="minute">Per minute</option><option value="hour">Per hour</option><option value="day" selected>Per day · history</option></select></label>
          <span class="ac-model-analytics-menu-label">Times shown in UTC</span>
        </div>
        <hr class="ac-model-analytics-sep">
        <div class="ac-model-analytics-menu-group">
          <span class="ac-model-analytics-menu-label">Metric</span>
          <div class="ac-model-analytics-toggle" role="radiogroup" aria-label="Metric"><button type="button" class="ac-btn ac-btn-sm active" role="radio" aria-checked="true" data-model-analytics-metric="tokens">Tokens</button><button type="button" class="ac-btn ac-btn-sm" role="radio" aria-checked="false" data-model-analytics-metric="cost">Cost</button></div>
        </div>
        <hr class="ac-model-analytics-sep">
        <div class="ac-model-analytics-menu-group">
          <label class="ac-model-analytics-check"><input type="checkbox" data-model-analytics-stacked checked><span>Stacked</span></label>
        </div>
        <hr class="ac-model-analytics-sep">
        <div class="ac-model-analytics-menu-group">
          <span class="ac-model-analytics-menu-label">Group by</span>
          <div class="ac-model-analytics-toggle" role="radiogroup" aria-label="Group by"><button type="button" class="ac-btn ac-btn-sm active" role="radio" aria-checked="true" data-model-analytics-group="models">Models</button><button type="button" class="ac-btn ac-btn-sm" role="radio" aria-checked="false" data-model-analytics-group="sessions">Sessions</button><button type="button" class="ac-btn ac-btn-sm" role="radio" aria-checked="false" data-model-analytics-group="agents">Agents</button></div>
        </div>
      </div>
      <div class="ac-model-analytics-chart" aria-live="polite"></div>
    </section>`;

  const chart = host.querySelector('.ac-model-analytics-chart');
  const metricButtons = [...host.querySelectorAll('[data-model-analytics-metric]')];
  const groupButtons = [...host.querySelectorAll('[data-model-analytics-group]')];
  const stackedCheckbox = host.querySelector('[data-model-analytics-stacked]');
  // The reset-range control lives in the header beside the chart menu; only its
  // visibility changes between renders so it never shifts position.
  const resetButton = host.querySelector('[data-model-analytics-reset-range]');

  function circleXIcon() {
    return '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="6.1"></circle><path d="m5.7 5.7 4.6 4.6m0-4.6-4.6 4.6"></path></svg>';
  }

  function resetHistoryClearBtn(button, model) {
    if (!button) return;
    clearTimeout(button._modelHistoryTimer);
    resetDeleteBtn(button, {
      size: '14px',
      title: `Clear ${model} history`,
      label: `Clear ${model} usage history`,
    });
    button.innerHTML = circleXIcon();
  }

  function resetHistoryClearButtons(except = null) {
    host.querySelectorAll('[data-model-history-clear]').forEach(button => {
      if (button !== except) resetHistoryClearBtn(button, button.dataset.modelHistoryClear || 'model');
    });
  }

  // The eight-colour palette is the original look and is kept whenever the series
  // list is small enough not to repeat. Past that it would hand eight series the
  // same colour, so sessions and large agent lists get a generated golden-angle
  // hue ramp instead — every series ends up with its own colour.
  function seriesColor(index, total) {
    if (total <= COLORS.length) return COLORS[index % COLORS.length];
    return `hsl(${Math.round((index * 137.508) % 360)}, 68%, ${index % 2 ? 44 : 56}%)`;
  }

  // The API supplies display labels for the opaque session/agent ids. Titles are
  // not always resolvable — a subagent or background run keeps its session row in
  // another store — and a raw 36-character uuid makes the tooltip unreadable, so
  // an unresolved id is shortened to a recognisable prefix instead.
  function seriesLabel(series) {
    // MODEL-LEGEND-ALIAS: under the Models grouping a series is a model id, and the
    // model table mounted alongside this chart already knows the name to show for
    // that row — the roster's display name. That name wins. A model whose roster row
    // carries no display name, or one that is on no roster at all (retired rows,
    // background-task models), keeps the raw id exactly as before.
    if (groupBy === 'models') {
      const alias = modelLabels.get(series);
      if (alias) return String(alias);
    }
    const labels = payload?.series_labels;
    const label = labels && labels[series];
    if (label) return String(label);
    const raw = String(series);
    return raw.length > 16 ? `${raw.slice(0, 8)}…` : raw;
  }

  function nestedMeta(series) {
    return payload?.series_meta?.[series] || {};
  }

  function activeNestedAgents(series, days, key) {
    const groups = new Map();
    series.forEach(item => {
      const meta = nestedMeta(item);
      const agentId = String(meta.agent_id || '');
      if (!agentId) return;
      const used = days.some(day => Number(day?.models?.[item]?.[key]) > 0);
      if (!used) return;
      if (!groups.has(agentId)) groups.set(agentId, { id: agentId, label: String(meta.agent_label || seriesLabel(item)), series: [] });
      groups.get(agentId).series.push(item);
    });
    return [...groups.values()];
  }

  // Catmull-Rom through the ACTUAL bar-top values, emitted as cubic Beziers. This
  // interpolates rather than approximates, so the curve passes exactly through the
  // top of every bar while still bending between them instead of drawing straight
  // segments. Each tangent is set by the neighbouring points, which is what gives
  // the line its curvature; a run of equal values stays gently straight, as it
  // should, since there is nothing there to curve around.
  function smoothPath(points) {
    if (points.length < 2) return '';
    let path = `M ${points[0][0].toFixed(1)} ${points[0][1].toFixed(1)}`;
    for (let i = 0; i < points.length - 1; i += 1) {
      const p0 = points[i - 1] || points[i];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[i + 2] || p2;
      const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
      path += ` C${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
    }
    return path;
  }

  // The same Catmull-Rom tangents smoothPath emits, kept as numbers so the curve can
  // be evaluated at any x rather than only at the knots sitting under each bar.
  function curveSegments(points) {
    const segments = [];
    for (let i = 0; i < points.length - 1; i += 1) {
      const p0 = points[i - 1] || points[i];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[i + 2] || p2;
      segments.push({
        x0: p1[0], y0: p1[1],
        c1x: p1[0] + (p2[0] - p0[0]) / 6, c1y: p1[1] + (p2[1] - p0[1]) / 6,
        c2x: p2[0] - (p3[0] - p1[0]) / 6, c2y: p2[1] - (p3[1] - p1[1]) / 6,
        x1: p2[0], y1: p2[1],
      });
    }
    return segments;
  }

  function cubicAt(a, b, c, d, t) {
    const u = 1 - t;
    return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * d;
  }

  // x climbs monotonically along a segment, so bisecting t finds the point whose x
  // matches, and the y there is the curve's height at that x.
  function segmentYAt(segment, x) {
    let lo = 0;
    let hi = 1;
    for (let i = 0; i < 24; i += 1) {
      const mid = (lo + hi) / 2;
      if (cubicAt(segment.x0, segment.c1x, segment.c2x, segment.x1, mid) < x) lo = mid; else hi = mid;
    }
    const t = (lo + hi) / 2;
    return cubicAt(segment.y0, segment.c1y, segment.c2y, segment.y1, t);
  }

  // Where a vertical line at x meets the uppermost curve, in viewBox units, plus which
  // line it landed on — the node and its charge take that line's own colour. x is
  // clamped into each curve's own span so the node waits at the end of the line
  // instead of vanishing once the pointer passes the first or last bar.
  function curveTopAt(lines, x) {
    let top = null;
    lines.forEach((line, lineIndex) => {
      const segments = line.segments;
      if (!segments?.length) return;
      const clamped = Math.max(segments[0].x0, Math.min(segments[segments.length - 1].x1, x));
      const segment = segments.find(item => clamped <= item.x1) || segments[segments.length - 1];
      const y = segmentYAt(segment, clamped);
      if (top == null || y < top.y) top = { y, lineIndex };
    });
    return top;
  }

  // How far along the DRAWN path a given x sits. The charge streak is placed by arc
  // length, and arc length only matches the knot distances at the knots themselves.
  function lengthAtX(path, x) {
    const total = path.getTotalLength();
    let lo = 0;
    let hi = total;
    for (let i = 0; i < 22; i += 1) {
      const mid = (lo + hi) / 2;
      if (path.getPointAtLength(mid).x < x) lo = mid; else hi = mid;
    }
    return { at: (lo + hi) / 2, total };
  }

  function activeDays() {
    const available = Array.isArray(payload?.days) ? payload.days : [];
    const base = interval !== 'day' || dateRange === 0 ? available : available.slice(-dateRange);
    if (!selectedBuckets) return { base, days: base };
    const [from, to] = selectedBuckets;
    return { base, days: base.slice(from, to + 1) };
  }

  // Sessions regularly number in the hundreds, so listing every one would bury the
  // panel. That view shows the day's five busiest and says so; the Total row still
  // covers every session so it keeps matching the drawn bar height.
  const TOP_SESSIONS = 5;

  // TIP-PINNED-PANEL: the info panel no longer trails the pointer. It is pinned to a
  // corner of the plot and shows TWO states. With nothing focused it summarises the
  // whole visible range: every visible series ranked by its total over the range, plus
  // a grand total. The instant the pointer focuses a day the same panel swaps to that
  // day's breakdown. Anchoring it to a corner is what keeps it clear of the bars and
  // the dashed trend entirely — a panel this tall can never reliably dodge a curve
  // that fills most of the plot, so it simply stops trying to and parks instead.
  function summaryHtml(series, days, key, formatter, layout) {
    const totals = series.map((item, index) => ({
      item,
      index,
      value: days.reduce((sum, day) => sum + (Number(day?.models?.[item]?.[key]) || 0), 0),
    }));
    const ranked = groupBy === 'sessions' ? [...totals].sort((a, b) => b.value - a.value) : totals;
    const shown = groupBy === 'sessions' ? ranked.slice(0, TOP_SESSIONS) : ranked;
    const rows = shown.map(({ item, index, value }) =>
      `<div class="ac-model-analytics-tip-row"><i style="background:${seriesColor(index, series.length)}"></i><span>${esc(seriesLabel(item))}</span><b>${esc(formatter(value))}</b></div>`
    ).join('');
    const note = groupBy === 'sessions' && ranked.length > TOP_SESSIONS
      ? `<div class="ac-model-analytics-tip-note">Top ${TOP_SESSIONS} of ${ranked.length} sessions</div>`
      : '';
    const grand = totals.reduce((sum, entry) => sum + entry.value, 0);
    const total = layout === 'stacked'
      ? `<div class="ac-model-analytics-tip-row ac-model-analytics-tip-total"><span>Total</span><b>${esc(formatter(grand))}</b></div>`
      : '';
    // The unfocused summary deliberately omits a date range so the pinned panel
    // stays compact. A focused marker continues to show its individual date.
    return `${rows}${note}${total}`;
  }

  // The focused state: one day's breakdown, which is what the pointer is asking about.
  function tooltipHtml(day, series, key, formatter) {
    const ranked = groupBy === 'sessions'
      ? [...series].sort((a, b) => (Number(day?.models?.[b]?.[key]) || 0) - (Number(day?.models?.[a]?.[key]) || 0))
      : series;
    const shown = groupBy === 'sessions' ? ranked.slice(0, TOP_SESSIONS) : ranked;
    // Colours stay keyed to the series' position in the full list so a row's dot
    // still matches its segment in the bar.
    const rows = shown.map(item => {
      const value = Number(day?.models?.[item]?.[key]) || 0;
      const index = series.indexOf(item);
      return `<div class="ac-model-analytics-tip-row"><i style="background:${seriesColor(index, series.length)}"></i><span>${esc(seriesLabel(item))}</span><b>${esc(formatter(value))}</b></div>`;
    }).join('');
    const note = groupBy === 'sessions' && ranked.length > TOP_SESSIONS
      ? `<div class="ac-model-analytics-tip-note">Top ${TOP_SESSIONS} of ${ranked.length} sessions</div>`
      : '';
    // A stacked bar's height is the day's total, so name that total explicitly.
    const total = layout === 'stacked'
      ? `<div class="ac-model-analytics-tip-row ac-model-analytics-tip-total"><span>Total</span><b>${esc(formatter(series.reduce((sum, item) => sum + (Number(day?.models?.[item]?.[key]) || 0), 0)))}</b></div>`
      : '';
    return `<div class="ac-model-analytics-tip-date">${esc(String(day?.date || '').replace('T', ' ') + (interval === 'day' ? '' : ' UTC'))}</div>${rows}${note}${total}`;
  }

  function render() {
    const { base, days } = activeDays();
    // payload.models is the API's GENERIC series container: model names under the
    // Models grouping, session ids under Sessions, agent ids under Agents.
    const allSeries = Array.isArray(payload?.models) ? payload.models : [];
    const series = allSeries.filter(item => !visibleModels || visibleModels.has(item));
    const key = metric === 'cost' ? 'cost_usd' : 'usage_tokens';
    const formatter = metric === 'cost' ? formatCost : formatTokens;
    // NESTED-AGENT-STACKS: My Agents keeps the familiar model stack, but gives
    // each active agent its own sibling stack in a day's band. A roster-only
    // agent never enters nestedAgents, so it cannot reserve an empty slot.
    const nested = nestedAgentStacks && payload?.group_by === 'agent_models' && groupBy === 'models' && layout === 'stacked';
    const nestedAgents = nested ? activeNestedAgents(series, days, key) : [];
    // In stacked mode a bar's height is that day's TOTAL across series, so the
    // automatic ceiling must be the largest daily total — sizing it from the
    // largest single series makes every stack taller than the plot and clips the
    // topmost segment away entirely. Nested stacks are independently scaled bars.
    const observed = nested
      ? Math.max(0, ...days.flatMap(day => nestedAgents.map(agent => agent.series.reduce((sum, item) => sum + (Number(day?.models?.[item]?.[key]) || 0), 0))))
      : layout === 'stacked'
        ? Math.max(0, ...days.map(day => series.reduce((sum, item) => sum + (Number(day?.models?.[item]?.[key]) || 0), 0)))
        : Math.max(0, ...days.flatMap(day => series.map(item => Number(day?.models?.[item]?.[key]) || 0)));
    // The y axis is gone, so there is no manual maximum any more: the ceiling is
    // always the rounded automatic fit for whatever window is on screen.
    const ceiling = niceCeiling(observed);
    if (!series.length || !days.length || observed <= 0) {
      geometry = null;
      if (resetButton) resetButton.hidden = !selectedBuckets;
      chart.innerHTML = `<p class="ac-model-analytics-empty">No recorded ${GROUP_NOUN[groupBy]} usage in the selected range.</p>`;
      return;
    }

    // No y axis at all: no scale labels and no horizontal gridlines. The left
    // margin collapses to a small gutter now that nothing is labelled there.
    const W = 880, H = 310, L = 14, R = 20, T = 18, B = 48;
    const plotW = W - L - R, plotH = H - T - B;
    const xStep = plotW / days.length;
    const groupW = Math.min(xStep * 0.78, 32);
    const stacked = layout === 'stacked';
    const gap = Math.min(2, groupW / Math.max(series.length * 4, 1));
    const barW = Math.max(1, (groupW - gap * Math.max(series.length - 1, 0)) / series.length);
    const bars = nested ? days.map((day, dayIndex) => {
      const dayW = xStep * 0.82;
      const agentGap = Math.min(3, dayW / Math.max(nestedAgents.length * 5, 1));
      const agentW = Math.max(1, (dayW - agentGap * Math.max(nestedAgents.length - 1, 0)) / nestedAgents.length);
      const dayX = L + dayIndex * xStep + (xStep - dayW) / 2;
      return nestedAgents.map((agent, agentIndex) => {
        let used = 0;
        const x = dayX + agentIndex * (agentW + agentGap);
        const segments = agent.series.map(item => {
          const value = Number(day?.models?.[item]?.[key]) || 0;
          const height = value > 0 ? Math.min(value / ceiling * plotH, plotH - used) : 0;
          const y = T + plotH - used - height;
          used += height;
          const seriesIndex = series.indexOf(item);
          return height > 0 ? `<rect class="ac-model-analytics-bar" data-model-series="${esc(item)}" fill="${seriesColor(seriesIndex, series.length)}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${agentW.toFixed(2)}" height="${height.toFixed(2)}" rx="1.5"><title>${esc(agent.label)} · ${esc(nestedMeta(item).model || seriesLabel(item))} · ${esc(String(day.date || ''))}: ${esc(formatter(value))}</title></rect>` : '';
        }).join('');
        return `<g class="ac-model-analytics-agent-bar-group" data-agent-id="${esc(agent.id)}" data-agent-label="${esc(agent.label)}" data-date="${esc(String(day.date || ''))}">${segments}</g>`;
      }).join('');
    }).join('') : days.map((day, dayIndex) => {
      const groupX = L + dayIndex * xStep + (xStep - groupW) / 2;
      const segments = [];
      if (stacked) {
        let used = 0;
        series.forEach((item, seriesIndex) => {
          const value = Number(day?.models?.[item]?.[key]) || 0;
          const height = value > 0 ? Math.min(value / ceiling * plotH, plotH - used) : 0;
          if (height > 0) segments.push({ item, seriesIndex, value, height, x: groupX, width: groupW, y: T + plotH - used - height });
          used += height;
        });
      } else {
        series.forEach((item, seriesIndex) => {
          const value = Number(day?.models?.[item]?.[key]) || 0;
          const height = value > 0 ? Math.max(1, Math.min(plotH, value / ceiling * plotH)) : 0;
          if (height > 0) segments.push({ item, seriesIndex, value, height, x: groupX + seriesIndex * (barW + gap), width: barW, y: T + plotH - height });
        });
      }
      return segments.map(({ item, seriesIndex, value, height, x, width, y }) =>
        `<rect class="ac-model-analytics-bar" fill="${seriesColor(seriesIndex, series.length)}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${width.toFixed(2)}" height="${height.toFixed(2)}" rx="1.5"><title>${esc(seriesLabel(item))} · ${esc(String(day.date || ''))}: ${esc(formatter(value))}</title></rect>`
      ).join('');
    }).join('');
    // The curve is threaded through the TOP of every bar: one curve over the daily
    // totals when the bars are stacked, or one curve per series when they sit side
    // by side, since then every bar has its own top to pass through.
    const trendValues = nested
      ? nestedAgents.map(agent => days.map(day => agent.series.reduce((sum, item) => sum + (Number(day?.models?.[item]?.[key]) || 0), 0)))
      : stacked
        ? [days.map(day => series.reduce((sum, item) => sum + (Number(day?.models?.[item]?.[key]) || 0), 0))]
        : series.map(item => days.map(day => Number(day?.models?.[item]?.[key]) || 0));
    const trendStrokes = nested
      ? nestedAgents.map((agent, index) => seriesColor(index, nestedAgents.length))
      : stacked ? ['var(--accent)'] : series.map((item, index) => seriesColor(index, series.length));
    // The knots behind each drawn curve are kept as numbers as well, so the glowing
    // node can be placed on the curve at ANY x, not only at the bucket centres. Each
    // line also keeps its own stroke, so the node and its charge can take that colour
    // instead of a fixed accent.
    const trendLines = [];
    const trendPaths = [];
    if (days.length >= 3) {
      trendValues.forEach((values, lineIndex) => {
        const points = values.map((value, index) => {
          const centre = nested
            ? (() => { const dayW = xStep * 0.82; const agentGap = Math.min(3, dayW / Math.max(nestedAgents.length * 5, 1)); const agentW = Math.max(1, (dayW - agentGap * Math.max(nestedAgents.length - 1, 0)) / nestedAgents.length); return L + index * xStep + (xStep - dayW) / 2 + lineIndex * (agentW + agentGap) + agentW / 2; })()
            : stacked
              ? L + index * xStep + xStep / 2
              : L + index * xStep + (xStep - groupW) / 2 + lineIndex * (barW + gap) + barW / 2;
          return [centre, T + plotH - Math.max(0, Math.min(plotH, value / ceiling * plotH))];
        });
        const stroke = trendStrokes[lineIndex] || 'var(--accent)';
        trendLines.push({ segments: curveSegments(points), stroke });
        trendPaths.push(`<path class="ac-model-analytics-trend" clip-path="url(#${scaleInputId}-plot-clip)" d="${smoothPath(points)}" stroke="${stroke}"></path>`);
      });
    }
    // The charge is TWO shared strokes rather than a lit copy of every line: it only
    // ever rides the one line the node is on, so its geometry is copied in from that
    // line instead. Both sit after the curves so the streak draws over them.
    const charge = trendPaths.length
      ? `<path class="ac-model-analytics-charge-glow" clip-path="url(#${scaleInputId}-plot-clip)"></path><path class="ac-model-analytics-charge" clip-path="url(#${scaleInputId}-plot-clip)"></path>`
      : '';
    const trend = `${trendPaths.join('')}${charge}`;
    // The date labels are no longer their own click target: the axis band below
    // the plot owns both the range popover (click) and range selection (drag).
    const labels = days.map((day, index) => (index % Math.max(1, Math.ceil(days.length / 8)) === 0 || index === days.length - 1)
      ? `<text class="ac-model-analytics-axis" x="${(L + index * xStep + xStep / 2).toFixed(1)}" y="${H - 15}" text-anchor="middle">${esc(interval === 'day' ? String(day.date || '').slice(5).replace('-', '/') : String(day.date || '').slice(11, 16))}</text>` : '').join('');
    // Hundreds of sessions cannot be read as legend entries, so Sessions mode
    // states how many series it is charting instead. Models and agents keep the
    // legend, which is also the anchor for the per-series visibility picker.
    const legend = groupBy === 'sessions'
      ? `<div class="ac-model-analytics-legend ac-model-analytics-legend-count" aria-label="Sessions in chart">${series.length} Session${series.length === 1 ? '' : 's'}</div>`
      : `<div class="ac-model-analytics-legend" aria-label="${esc(GROUP_NOUNS[groupBy])} in chart">${series.map((item, index) => `<button type="button" class="ac-model-analytics-legend-item" data-chart-popover="models"><i style="background:${seriesColor(index, series.length)}"></i>${esc(seriesLabel(item))}</button>`).join('')}</div>`;
    const title = metric === 'cost' ? 'Cost (USD)' : 'Tokens';
    // The reset control is a persistent header button, so render only flips its
    // visibility instead of rebuilding it inside the plot.
    if (resetButton) resetButton.hidden = !selectedBuckets;
    chart.innerHTML = `<div class="ac-model-analytics-plot"><svg class="ac-model-analytics-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)} per ${interval} ${stacked ? `stacked by ${groupBy}` : `grouped by ${groupBy}`} over ${days.length} ${interval} buckets (UTC), up to ${esc(formatter(ceiling))}"><defs><clipPath id="${scaleInputId}-plot-clip"><rect x="${L}" y="${T}" width="${plotW}" height="${plotH}"></rect></clipPath></defs>${bars}${trend}${labels}</svg><div class="ac-model-analytics-tip"></div><div class="ac-model-analytics-hover-layer" aria-hidden="true" style="left:${(L / W * 100).toFixed(3)}%;right:${(R / W * 100).toFixed(3)}%;top:${(T / H * 100).toFixed(3)}%;bottom:${(B / H * 100).toFixed(3)}%"><div class="ac-model-analytics-cross" hidden></div><div class="ac-model-analytics-node" hidden></div><div class="ac-model-analytics-tip" hidden></div><div class="ac-model-analytics-zoombox" hidden></div></div><div class="ac-model-analytics-axis-band" data-chart-popover="range" role="presentation" title="Drag across the dates to zoom; click for range options; pinch the graph to zoom both axes" style="left:${(L / W * 100).toFixed(3)}%;right:${(R / W * 100).toFixed(3)}%;top:${((T + plotH) / H * 100).toFixed(3)}%;bottom:0"></div></div>${legend}`;
    geometry = { L, R, T, B, W, H, plotW, plotH, n: days.length, days, base, series, key, formatter, trendLines };
    // TIP-PINNED-PANEL: the panel starts on the RANGE summary and swaps to a day's
    // breakdown only while the pointer is focused on one. Re-rendering (zoom, metric,
    // grouping, range) therefore always lands back on the summary.
    showSummary();
  }

  // Fill the pinned panel with the range-level summary — every visible series ranked
  // by its total across the days currently on screen.
  function showSummary() {
    const tip = chart.querySelector('.ac-model-analytics-tip');
    if (!tip || !geometry) return;
    tip.innerHTML = summaryHtml(geometry.series, geometry.days, geometry.key, geometry.formatter, layout);
  }

  // MODEL-LEGEND-ALIAS: rebuilt on every payload load AND whenever the roster's
  // display names arrive, so a roster that resolves after the chart still re-labels
  // the picker in place instead of leaving the raw ids behind.
  function renderModelPicker() {
    const picker = host.querySelector('[data-model-analytics-models]');
    if (!picker) return;
    // A picker row per session would be unusable, so Sessions mode relies on the
    // "N Sessions" summary instead. Clearing one series' history is a MODEL
    // action (the DELETE route takes a model name), so it stays models-only.
    picker.innerHTML = groupBy === 'sessions' ? '' : (payload?.models || []).map(item => `<div class="ac-model-analytics-model-row"><label><input type="checkbox" value="${esc(item)}" ${!visibleModels || visibleModels.has(item) ? 'checked' : ''}><span>${esc(seriesLabel(item))}</span></label>${groupBy === 'models' ? `<button type="button" class="ac-model-analytics-history-clear" data-model-history-clear="${esc(item)}"></button>` : ''}</div>`).join('');
    picker.querySelectorAll('[data-model-history-clear]').forEach(button => resetHistoryClearBtn(button, button.dataset.modelHistoryClear || 'model'));
  }

  async function reload() {
    const version = ++loadVersion;
    chart.innerHTML = '<p class="ac-model-analytics-empty">Loading usage…</p>';
    try {
      const analyticsGroupBy = nestedAgentStacks && groupBy === 'models' ? 'agent_models' : groupBy;
      const params = new URLSearchParams({ days: String(dateRange), scope, interval, group_by: analyticsGroupBy });
      if (agentId) params.set('agent_id', agentId);
      const response = await fetchFn(apiPath(`/admin/settings/model-usage-analytics?${params.toString()}`));
      const data = response.ok ? await response.json() : null;
      if (!data || data.error) throw new Error(data?.error || `HTTP ${response.status}`);
      if (version !== loadVersion) return;
      payload = data;
      selectedBuckets = null;
      renderModelPicker();
      render();
    } catch (_) {
      if (version !== loadVersion) return;
      payload = null;
      geometry = null;
      chart.innerHTML = '<p class="ac-model-analytics-empty">Usage data is unavailable right now.</p>';
    }
  }

  function hideHover() {
    chart.querySelector('.ac-model-analytics-cross')?.setAttribute('hidden', '');
    chart.querySelector('.ac-model-analytics-node')?.setAttribute('hidden', '');
    // TIP-PINNED-PANEL: the panel is permanent, so losing the pointer only returns it
    // to the range summary — it is never hidden.
    showSummary();
    chargeOff();
  }

  // Park the charge by collapsing it to a zero-length dash, which strokes nothing.
  function chargeOff() {
    chart.querySelectorAll('.ac-model-analytics-charge-glow, .ac-model-analytics-charge').forEach(el => {
      el.setAttribute('stroke-dasharray', '0 100000');
    });
  }

  // The stats panel belongs to the gesture; the locator does not. Releasing the
  // touch drops the day readout but leaves the vertical line where it was put, and
  // the panel falls back to the range summary (TIP-PINNED-PANEL).
  function hideTip() {
    showSummary();
  }

  function dismissHoverOnOutsidePointer(event) {
    const plot = chart.querySelector('.ac-model-analytics-plot');
    if (!plot?.contains(event.target)) hideHover();
    if (!event.target.closest('[data-model-history-clear]')) resetHistoryClearButtons();
  }

  document.addEventListener('pointerdown', dismissHoverOnOutsidePointer, true);

  function chartPoint(event) {
    const layer = event.target.closest('.ac-model-analytics-hover-layer');
    if (!layer || !geometry) return null;
    const rect = layer.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    return { layer, rect, x, y, slot: rect.width / geometry.n };
  }

  // Range selection is deliberately confined to the x-axis band beneath the plot.
  // Inside the plot a press-drag simply tracks the momentary locator, so a swipe
  // across the graph can never reshape the axis by accident.
  chart.addEventListener('pointerleave', event => {
    if (event.target.closest('.ac-model-analytics-hover-layer')) {
      // Only the readout goes: the locator stays put once a touch lifts.
      hideTip();
      const zbox = chart.querySelector('.ac-model-analytics-zoombox');
      if (zbox) zbox.hidden = true;
    }
    // A pointer that leaves without a pointerup must not linger in the pinch set,
    // or the chart would keep thinking a second finger is still down.
    activePointers.delete(event.pointerId);
    if (pinch && activePointers.size < 2) pinch = null;
  });

  // ── Pinch to zoom ──────────────────────────────────────────────────────────
  // A pinch rewrites the VISIBLE WINDOW instead of scaling the drawing. A CSS or
  // SVG transform would only magnify the same picture; narrowing the window
  // genuinely shrinks the x range, and because the y ceiling is recomputed from
  // the days actually on screen, the y range re-fits at the same moment. That is
  // what makes a pinch zoom into real detail on BOTH axes.
  const MIN_PINCH_BUCKETS = 2;

  function pinchMetrics() {
    const [a, b] = [...activePointers.values()];
    return {
      distance: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)),
      midX: (a.x + b.x) / 2,
    };
  }

  // Apply an x-window of `span` base buckets, anchored so the bucket under the
  // gesture midpoint stays put, then drop any manual maximum so y auto-fits.
  function applyZoom(span, anchorIndex, fraction) {
    const total = geometry?.base?.length || 0;
    if (total < MIN_PINCH_BUCKETS) return false;
    const nextSpan = Math.max(MIN_PINCH_BUCKETS, Math.min(total, Math.round(span)));
    const from = Math.max(0, Math.min(total - nextSpan, Math.round(anchorIndex - fraction * nextSpan)));
    const next = nextSpan >= total ? null : [from, from + nextSpan - 1];
    if (selectedBuckets && next && next[0] === selectedBuckets[0] && next[1] === selectedBuckets[1]) return false;
    selectedBuckets = next;
    // The ceiling is re-derived from the visible days on every render, so the y
    // range re-fits the zoomed window with no extra work needed here.
    render();
    return true;
  }

  function beginPinch() {
    // A pinch supersedes any single-pointer gesture already running.
    rangeDrag = null;
    pinch = null;
    hideTip();
    const zbox = chart.querySelector('.ac-model-analytics-zoombox');
    if (zbox) zbox.hidden = true;
    const layer = chart.querySelector('.ac-model-analytics-hover-layer');
    if (!layer || !geometry || geometry.base.length < MIN_PINCH_BUCKETS) return;
    const rect = layer.getBoundingClientRect();
    const { distance, midX } = pinchMetrics();
    const [from, to] = selectedBuckets || [0, geometry.base.length - 1];
    const fraction = Math.max(0, Math.min(1, (midX - rect.left) / Math.max(1, rect.width)));
    pinch = {
      distance,
      fraction,
      span: to - from + 1,
      anchorIndex: from + fraction * (to - from + 1),
    };
  }

  function updatePinch() {
    if (!pinch || !geometry) return;
    // Fingers moving apart means zooming in, so the window span shrinks.
    applyZoom(pinch.span / (pinchMetrics().distance / pinch.distance), pinch.anchorIndex, pinch.fraction);
  }

  function endPointer(event) {
    activePointers.delete(event.pointerId);
    // Lifting one finger ends the pinch; the window it produced stays.
    if (pinch && activePointers.size < 2) pinch = null;
  }

  chart.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    const inPlot = event.target.closest('.ac-model-analytics-hover-layer, .ac-model-analytics-axis-band');
    if (inPlot && geometry) {
      activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      // The second finger turns the gesture into a pinch and cancels whatever the
      // first one had begun.
      if (activePointers.size === 2) { beginPinch(); return; }
    }
    const band = event.target.closest('.ac-model-analytics-axis-band');
    if (band && geometry) {
      const rect = band.getBoundingClientRect();
      rangeDrag = { x0: event.clientX - rect.left, width: rect.width };
      try { band.setPointerCapture(event.pointerId); } catch (_) {}
      return;
    }
    const point = chartPoint(event);
    if (!point) return;
    try { point.layer.setPointerCapture(event.pointerId); } catch (_) {}
  });
  chart.addEventListener('pointermove', event => {
    if (activePointers.has(event.pointerId)) activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pinch) { updatePinch(); return; }
    if (rangeDrag) {
      // The band and the hover layer share the same horizontal extent, so the
      // zoombox takes band-local coordinates directly.
      const band = event.target.closest('.ac-model-analytics-axis-band');
      const zbox = chart.querySelector('.ac-model-analytics-zoombox');
      if (band && zbox) {
        const rect = band.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const x0 = Math.max(0, Math.min(rangeDrag.x0, x));
        const x1 = Math.min(rect.width, Math.max(rangeDrag.x0, x));
        zbox.style.left = `${x0}px`;
        zbox.style.width = `${x1 - x0}px`;
        zbox.hidden = false;
      }
      return;
    }
    const point = chartPoint(event);
    if (!point || !geometry) return;
    const cross = chart.querySelector('.ac-model-analytics-cross');
    const tip = chart.querySelector('.ac-model-analytics-tip');
    const bucket = Math.max(0, Math.min(geometry.n - 1, Math.floor(point.x / point.slot)));
    // The locator follows the pointer continuously. Snapping it to the bar centres made
    // the whole overlay advance in visible steps, and anything riding it stepped too.
    const nx = Math.max(0, Math.min(point.rect.width, point.x));
    if (cross) { cross.style.left = `${nx}px`; cross.style.height = `${point.rect.height}px`; cross.hidden = false; }
    // The node sits where that same x meets the curve itself, so it glides between the
    // bars instead of hopping bucket to bucket. ViewBox geometry is converted into the
    // layer's own pixel box, which covers exactly the plot area.
    const node = chart.querySelector('.ac-model-analytics-node');
    const glowEl = chart.querySelector('.ac-model-analytics-charge-glow');
    const coreEl = chart.querySelector('.ac-model-analytics-charge');
    const vbX = geometry.L + nx / point.rect.width * geometry.plotW;
    const picked = geometry.trendLines?.length ? curveTopAt(geometry.trendLines, vbX) : null;
    if (node && picked) {
      const line = geometry.trendLines[picked.lineIndex];
      node.style.left = `${nx.toFixed(2)}px`;
      node.style.top = `${((picked.y - geometry.T) / geometry.plotH * point.rect.height).toFixed(2)}px`;
      // The node wears the colour of the line it is riding, so the two read as one thing.
      node.style.setProperty('--node-color', line.stroke);
      node.hidden = false;
      // The charge is that same line lit up, parked at the node's own distance along it.
      const drawn = chart.querySelectorAll('.ac-model-analytics-trend')[picked.lineIndex];
      if (drawn && glowEl && coreEl) {
        const streak = 80;
        const { at, total } = lengthAtX(drawn, vbX);
        const dash = `${streak} ${(total + streak * 2).toFixed(1)}`;
        const offset = (streak / 2 - at).toFixed(2);
        [glowEl, coreEl].forEach(el => {
          el.setAttribute('d', drawn.getAttribute('d') || '');
          el.setAttribute('stroke-dasharray', dash);
          el.setAttribute('stroke-dashoffset', offset);
          el.style.color = line.stroke;
        });
      }
    } else {
      if (node) node.hidden = true;
      chargeOff();
    }
    if (tip) {
      // TIP-PINNED-PANEL: the panel is parked in the plot's corner by CSS, so the only
      // thing a focus does is swap its CONTENT to this day's breakdown. Nothing here
      // touches left/top, which is what makes the panel perfectly stable: it never
      // moves, never covers the curve as it chases the pointer, and needs no geometry
      // to decide where to sit.
      tip.innerHTML = tooltipHtml(geometry.days[bucket], geometry.series, geometry.key, geometry.formatter);
    }
  });
  // Desktop pinch has two routes: Chrome, Edge and Firefox report a trackpad pinch
  // as ctrl+wheel, while Safari reports its own gesture events. Both resolve to the
  // same step — scale the visible window about the point under the fingers. Plain
  // wheel is deliberately left alone so the page still scrolls normally.
  let pendingSpan = null;
  let pendingSpanAt = 0;

  function zoomAt(factor, clientX, layer) {
    const total = geometry?.base?.length || 0;
    if (!layer || total < MIN_PINCH_BUCKETS) return;
    const rect = layer.getBoundingClientRect();
    const [from, to] = selectedBuckets || [0, total - 1];
    const span = to - from + 1;
    const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
    // A trackpad emits a long run of very small deltas. Deriving each one from the
    // already-rounded window let a half-percent nudge round straight back to the same
    // bucket count, so the pinch appeared to do nothing at all. The pending span is
    // therefore carried as a float across the gesture and only rounded when applied.
    const now = performance.now();
    if (pendingSpan == null || now - pendingSpanAt > 250) pendingSpan = span;
    pendingSpanAt = now;
    pendingSpan = Math.max(MIN_PINCH_BUCKETS, Math.min(total, pendingSpan * factor));
    applyZoom(pendingSpan, from + fraction * span, fraction);
  }

  chart.addEventListener('wheel', event => {
    if (!event.ctrlKey || !geometry) return;
    const layer = event.target.closest('.ac-model-analytics-hover-layer');
    if (!layer) return;
    event.preventDefault();
    // deltaMode 1 reports lines and 2 reports pages; unscaled, the same gesture would
    // crawl on those platforms.
    const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaMode === 2 ? event.deltaY * 100 : event.deltaY;
    zoomAt(Math.exp(Math.max(-.4, Math.min(.4, delta * .0025))), event.clientX, layer);
  }, { passive: false });

  // Safari's trackpad pinch never reaches the page as a wheel event.
  let gestureScale = 1;
  chart.addEventListener('gesturestart', event => {
    if (!geometry) return;
    event.preventDefault();
    gestureScale = 1;
    pendingSpan = null;
  });
  chart.addEventListener('gesturechange', event => {
    if (!geometry) return;
    event.preventDefault();
    const layer = chart.querySelector('.ac-model-analytics-hover-layer');
    const scale = Number(event.scale) || 1;
    // Fingers apart means zooming in, which is a smaller window.
    zoomAt(gestureScale / scale, event.clientX, layer);
    gestureScale = scale;
  });
  chart.addEventListener('pointerup', event => {
    endPointer(event);
    // Lifting the finger ends the readout but leaves the locator in place.
    hideTip();
    const drag = rangeDrag;
    rangeDrag = null;
    const zbox = chart.querySelector('.ac-model-analytics-zoombox');
    if (zbox) zbox.hidden = true;
    if (!drag || !geometry) return;
    const band = event.target.closest('.ac-model-analytics-axis-band');
    if (!band) return;
    const rect = band.getBoundingClientRect();
    const x = event.clientX - rect.left;
    // Short of the threshold this was a click, not a swipe: leave the range alone
    // so the band's own click handler can open the date-range popover.
    if (Math.abs(x - drag.x0) < DRAG_THRESHOLD) return;
    const slot = rect.width / geometry.n;
    const from = Math.max(0, Math.min(geometry.n - 1, Math.floor(Math.min(drag.x0, x) / slot)));
    const to = Math.max(0, Math.min(geometry.n - 1, Math.floor(Math.max(drag.x0, x) / slot)));
    if (to <= from) return;
    const baseStart = geometry.base.findIndex(day => day === geometry.days[0]);
    selectedBuckets = [baseStart + from, baseStart + to];
    // A drag-set window must not be picked up mid-gesture by a following pinch.
    pendingSpan = null;
    suppressPopoverUntil = performance.now() + 300;
    render();
  });
  chart.addEventListener('pointercancel', event => { endPointer(event); hideTip(); });

  stackedCheckbox?.addEventListener('change', () => {
    layout = stackedCheckbox.checked ? 'stacked' : 'grouped';
    render();
  });

  host.querySelector('[data-model-analytics-interval]').addEventListener('change', event => {
    interval = event.target.value;
    selectedBuckets = null;
    reload();
  });

  groupButtons.forEach(button => button.addEventListener('click', () => {
    const next = button.dataset.modelAnalyticsGroup;
    if (next === groupBy || !GROUP_NOUNS[next]) return;
    groupBy = next;
    groupButtons.forEach(item => {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-checked', String(selected));
    });
    // Series keys are not comparable across groupings (a model name is not a
    // session id), so the previous visibility selection and zoom must not leak.
    visibleModels = null;
    selectedBuckets = null;
    reload();
  }));

  metricButtons.forEach(button => button.addEventListener('click', () => {
    metric = button.dataset.modelAnalyticsMetric;
    metricButtons.forEach(item => {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-checked', String(selected));
    });
    render();
  }));

  function openAnchoredPopover(panel, trigger, kind) {
    if (!panel?.showPopover) return;
    host.querySelectorAll('.ac-model-analytics-popover:popover-open').forEach(openPanel => openPanel.hidePopover());
    const anchor = trigger.getBoundingClientRect();
    panel.style.visibility = 'hidden';
    panel.showPopover();
    const rect = panel.getBoundingClientRect();
    const gap = 8;
    const maxLeft = Math.max(gap, window.innerWidth - rect.width - gap);
    const maxTop = Math.max(gap, window.innerHeight - rect.height - gap);
    let left = anchor.right + gap;
    let top = anchor.top - 10;
    if (kind === 'range') { left = anchor.left + anchor.width / 2 - rect.width / 2; top = anchor.top - rect.height - gap; }
    else if (kind === 'models') { left = anchor.left; top = anchor.bottom + gap; }
    // The kebab sits at the panel's top-right, so its menu right-aligns under the
    // button instead of opening off the right edge of the viewport.
    else if (kind === 'more') { left = anchor.right - rect.width; top = anchor.bottom + gap; }
    panel.style.left = `${Math.min(Math.max(gap, left), maxLeft)}px`;
    panel.style.top = `${Math.min(Math.max(gap, top), maxTop)}px`;
    panel.style.visibility = '';
  }

  async function clearModelHistory(model) {
    const params = new URLSearchParams({ model, scope });
    if (agentId) params.set('agent_id', agentId);
    const response = await fetchFn(apiPath(`/admin/settings/model-usage-analytics?${params.toString()}`), { method: 'DELETE' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data?.error) throw new Error(data?.error || `HTTP ${response.status}`);
    await reload();
  }

  // A native popover light-dismisses on pointerdown, so by the time a click lands
  // the open panel already reads as closed and the click handler below would just
  // re-open it — which is why pressing the kebab felt like nothing happened. Catch
  // the toggle-off during pointerdown, close explicitly, and suppress the reopen.
  host.addEventListener('pointerdown', event => {
    const trigger = event.target.closest('[data-chart-popover]');
    if (!trigger) return;
    const panel = host.querySelector(`#${scaleInputId}-${trigger.dataset.chartPopover}`);
    if (!panel?.matches(':popover-open')) return;
    suppressPopoverUntil = performance.now() + 500;
    panel.hidePopover();
  }, true);

  host.addEventListener('click', event => {
    const historyClear = event.target.closest('[data-model-history-clear]');
    if (historyClear) {
      event.preventDefault();
      event.stopPropagation();
      const model = historyClear.dataset.modelHistoryClear || '';
      resetHistoryClearButtons(historyClear);
      advanceDeleteBtn(historyClear, {
        size: '14px',
        armTitle: `Click again to clear ${model} history`,
        armLabel: `Click again to clear ${model} usage history`,
        busyTitle: `Clearing ${model} history…`,
        busyLabel: `Clearing ${model} usage history`,
        onArm: button => {
          clearTimeout(button._modelHistoryTimer);
          button._modelHistoryTimer = setTimeout(() => resetHistoryClearBtn(button, model), 4000);
        },
        onConfirm: async button => {
          try {
            await clearModelHistory(model);
          } catch (_) {
            resetHistoryClearBtn(button, model);
          }
        },
      });
      return;
    }
    const reset = event.target.closest('[data-model-analytics-reset-range]');
    if (reset) { selectedBuckets = null; render(); return; }
    const trigger = event.target.closest('[data-chart-popover]');
    if (!trigger || performance.now() < suppressPopoverUntil) return;
    const kind = trigger.dataset.chartPopover;
    openAnchoredPopover(host.querySelector(`#${scaleInputId}-${kind}`), trigger, kind);
  });
  host.querySelector('[data-model-analytics-range]').addEventListener('change', event => {
    const nextRange = Number(event.target.value);
    dateRange = Number.isFinite(nextRange) && nextRange >= 0 ? nextRange : 0;
    selectedBuckets = null;
    render();
  });
  host.querySelector('[data-model-analytics-models]').addEventListener('change', () => {
    visibleModels = new Set([...host.querySelectorAll('[data-model-analytics-models] input:checked')].map(input => input.value));
    render();
  });
  // MODEL-LEGEND-ALIAS: the sibling model table owns the roster, so it publishes the
  // display names here after every load. An empty map — no roster yet, or a model the
  // roster does not cover — leaves every series labelled by its raw id.
  function setModelLabels(providers) {
    const next = new Map();
    (Array.isArray(providers) ? providers : []).forEach(row => {
      const model = String(row?.model || '');
      const name = String(row?.display_name || '').trim();
      // First row wins for a model that appears twice, so the label stays stable
      // across reorders. A row with no display name never contributes a label.
      if (model && name && !next.has(model)) next.set(model, name);
    });
    modelLabels = next;
    if (!payload) return;
    renderModelPicker();
    render();
  }

  // MODEL-FOCUS-NOTE: the transient "no usage yet" note raised when the row the user
  // opened names a model the chart cannot plot. It lives in the card HEADER rather
  // than the chart body, because every render() replaces that body's contents — and
  // the header's left side is empty (its controls sit right), so the note never
  // reflows the chart. It clears itself after 2s, and a click dismisses it early.
  let focusNoteTimer = 0;
  function flashFocusNote(text) {
    const head = host.querySelector('.ac-model-analytics-head') || host;
    let note = head.querySelector('.ac-model-analytics-note');
    if (!note) {
      note = document.createElement('p');
      note.className = 'ac-model-analytics-note';
      // role=status makes this an implicit polite live region, so the note is
      // announced even though no visible control takes focus.
      note.setAttribute('role', 'status');
      note.addEventListener('click', hideFocusNote);
      head.appendChild(note);
    }
    if (focusNoteTimer) clearTimeout(focusNoteTimer);
    note.textContent = text;
    note.classList.add('on');
    focusNoteTimer = setTimeout(hideFocusNote, 2000);
  }

  function hideFocusNote() {
    if (focusNoteTimer) { clearTimeout(focusNoteTimer); focusNoteTimer = 0; }
    const note = host.querySelector('.ac-model-analytics-note');
    if (note) note.classList.remove('on');
  }

  // MODEL-ROW-FOCUS: the sibling model table points the chart at one model while its
  // row is open, and calls this with '' to hand the whole group back. It drives the
  // same filter the "Model" picker does, so the picker's checkboxes stay in step.
  // Sessions/agents groupings key their series by session/agent id rather than by
  // model, so a model focus cannot apply there and is ignored while one is selected.
  // A model with no recorded usage is the one case where narrowing would leave the
  // chart with nothing to draw: there the baseline whole-group view is kept and the
  // short note above explains why, rather than blanking the plot.
  function setModelFocus(model) {
    if (model && groupBy !== 'models') return;
    const key = String(model || '').trim();
    // A series missing from the payload has two possible causes. While the payload is
    // still null the chart has simply not loaded yet, so the focus is remembered and
    // applied once the data lands. Once it HAS loaded, an absent series means this
    // model has no usage to show.
    if (key && payload && !(Array.isArray(payload.models) ? payload.models : []).includes(key)) {
      if (visibleModels) { visibleModels = null; renderModelPicker(); render(); }
      flashFocusNote('Selected model has no usage yet');
      return;
    }
    visibleModels = key ? new Set([key]) : null;
    if (!payload) return;
    renderModelPicker();
    render();
  }

  reload();
  return {
    reload,
    setModelFocus,
    setModelLabels,
    destroy() {
      document.removeEventListener('pointerdown', dismissHoverOnOutsidePointer, true);
      host.replaceChildren();
      delete host.dataset.modelAnalyticsMounted;
    },
  };
}
