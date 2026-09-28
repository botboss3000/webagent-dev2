'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agents — Abilities/Connections tab.
 * Renders the agent ability table.
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import {
  _isMockAgent,
  _ensureAbilityCatalog,
  _memoryStateFromAgent,
  _memoryUpdatesFor,
  _userIsAdmin,
} from './state.js';
import { _esc, _putAgentField } from './utils.js';
import { _mockAbilityConnections, _saveMockAbilityConnection } from './mock-agent.js';
import { _iconHtml, _chevronSvg, loadAbilityCatalog } from '../../../shared/js/dom-utils.js';
import { build as buildAgentAbilityTable } from '../../../shared/js/agent-ability-table.js';
import { buildLevelSelector, copyPolicy, isBaseLevel, loadMemberLevels, narrowedList, policyAllows, saveMemberLevel } from './member-level-policy.js';

function _buildAbilitiesOverview(agent, { loadConnections, loadAgentTools, canViewContext, levelSelector = null, levelCount = 1 }) {
  const overview = document.createElement('section');
  overview.className = 'agent-abilities-overview';
  // COMPOSITION-ONLY-SUMMARY: the chart's centre total and its breakdown legend are
  // the single readout for base context. The old Base Context token/pricing row and
  // the Agent Abilities inventory panel are gone — the legend carries their counts —
  // so TIER is the only group left in the summary grid.
  overview.innerHTML = `
    <section class="agent-context-composition" aria-label="Base context composition" aria-live="polite">
      <aside class="agent-context-composition-details" aria-label="Agent context breakdown">
        <h4>AGENT CONTEXT</h4>
        <div class="agent-context-composition-legend" data-context-composition-legend>
          <span class="is-loading">Loading composition…</span>
        </div>
      </aside>
      <div class="agent-context-composition-primary">
        <div class="agent-context-composition-chart is-loading sk-shimmer" data-context-composition-chart role="group" tabindex="0" aria-label="Base context composition loading" aria-describedby="agent-context-composition-tip">
          <svg class="agent-context-composition-bars" data-context-composition-bars viewBox="0 0 200 200" aria-hidden="true"></svg>
          <span data-context-composition-total>—</span>
          <i class="agent-context-composition-node" data-context-composition-node hidden aria-hidden="true"></i>
          <div class="agent-context-composition-tip" data-context-composition-tip id="agent-context-composition-tip" role="tooltip" hidden></div>
        </div>
      </div>
    </section>
    <div class="agent-abilities-summary" aria-label="Agent capability summary" aria-live="polite">
      <section class="agent-abilities-summary-group agent-abilities-tier-group">
        <h3>TIER <span data-stat="tier-count">(${levelCount})</span></h3>
        <div class="agent-abilities-tier-control" data-tier-selector>Base agent - admin access</div>
      </section>
    </div>
    <div class="agent-abilities-sync" aria-label="Template synchronization"></div>`;

  if (levelSelector) {
    levelSelector.querySelector('label')?.remove();
    levelSelector.classList.add('agent-abilities-tier-select');
    overview.querySelector('[data-tier-selector]')?.replaceChildren(levelSelector);
  }

  const request = async (url) => {
    const response = await fetch(url, { headers: authHeaders() });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };

  const compositionChart = overview.querySelector('[data-context-composition-chart]');
  const compositionLegend = overview.querySelector('[data-context-composition-legend]');
  const compositionTotal = overview.querySelector('[data-context-composition-total]');
  const compositionBars = overview.querySelector('[data-context-composition-bars]');
  const compositionNode = overview.querySelector('[data-context-composition-node]');
  const compositionTip = overview.querySelector('[data-context-composition-tip]');
  let compositionEntries = [];
  let compositionSources = [];
  // RADIAL-RAIL-DYNAMIC: the rail the ball rides is rebuilt for every focus from the
  // category's *current* bar tips, rather than baked once at render time. A knot that
  // belongs to the popped category is offset by that group's own live translate, so the
  // ball rides the lifted bars. When the rail was baked once, it kept following the
  // resting tips and the ball visibly detached from a set that had popped out.
  let compositionPoppedKey = null;
  let compositionFocusState = null;
  let compositionRailFrame = null;
  // Bottom of the source-bar band. It is both the bars' own inner radius and the
  // locator needle's inner anchor, so the needle starts exactly where the bars start
  // instead of at the disc's centre.
  const COMPOSITION_BAR_INNER = 68;
  // CONTEXT-GROUP-POPOUT: how far a hovered category's whole bar set slides outward
  // along its own bisector, in the SVG's 0 0 200 200 units. The ring's outer edge sits
  // at 97 and the chart panel carries 24px of side padding, so this stays inside the
  // container even at the widest point of the pop.
  const COMPOSITION_GROUP_POP = 9;
  // CONTEXT-BAR-HOTPOP: how much further the single source bar under the pointer travels
  // than the rest of its group, radially along its own mid-bearing. It is added to the
  // group's own slide (which moves the set), so the highlighted bar reads as standing
  // proud of its neighbours rather than merely moving with them.
  const COMPOSITION_BAR_POP = 8;
  // CONTEXT-SECTION-MAGNIFY: in addition to the slide, the hovered category's bars grow
  // radially by this factor, and the bar under the pointer grows by this second factor
  // again. The growth is radial with the inner edge pinned, so every bar keeps its
  // angular width: the section enlarges outward and can never widen into its neighbours.
  // That is also why this cannot be a CSS transform — a scale about the disc centre would
  // preserve angles but drag the inner edge off the ring, and a scale about the bar's own
  // inner point would widen it by the same factor (a 25% widening would exceed the
  // 86%-of-slot bar width and overlap the set).
  const COMPOSITION_GROUP_GROW = 1.25;
  const COMPOSITION_BAR_GROW = 1.5;
  // The eased growth the animation is currently showing, per category, plus the same for
  // the highlighted bar. Eased rather than switched so a set enlarges in step with its
  // CSS slide instead of snapping a frame ahead of it.
  const compositionGrow = new Map();
  let compositionHotGrow = 1;
  // This render's sources by key, so the animation can rebuild one bar's path without
  // re-running the whole chart build. The ring helpers below are hoisted for the same
  // reason: the growth is geometry, so it has to be re-laid-out as it animates.
  let compositionSourceByKey = new Map();
  const compositionPolar = (fraction, radius) => {
    const angle = fraction * Math.PI * 2;
    return `${(100 + Math.sin(angle) * radius).toFixed(2)} ${(100 - Math.cos(angle) * radius).toFixed(2)}`;
  };
  const compositionArcPath = (start, end, inner, outer) => {
    const sweep = Math.max(0.001, (end - start) * Math.PI * 2);
    const large = sweep > Math.PI ? 1 : 0;
    return `M ${compositionPolar(start, inner)} L ${compositionPolar(start, outer)} A ${outer} ${outer} 0 ${large} 1 ${compositionPolar(end, outer)} L ${compositionPolar(end, inner)} A ${inner} ${inner} 0 ${large} 0 ${compositionPolar(start, inner)} Z`;
  };
  // The category whose set is currently lifted, and the single bar lifted with it. Both
  // are keys, not elements, so the rail can be rebuilt from state when either changes.
  let compositionHotKey = null;

  const hideCompositionFocus = () => {
    compositionNode?.setAttribute('hidden', '');
    compositionTip?.setAttribute('hidden', '');
    compositionBars?.querySelector('.agent-context-composition-cross')?.setAttribute('hidden', '');
    compositionFocusState = null;
    if (compositionRailFrame) { cancelAnimationFrame(compositionRailFrame); compositionRailFrame = null; }
    popCompositionGroup(null);
  };
  // RADIAL-RAIL-DYNAMIC: an element's *live* translate, read from its own computed
  // transform. Reading it rather than mirroring the intended vector is what lets the ball
  // ride the bars through a pop instead of jumping straight to the settled tips;
  // mid-transition the computed matrix is the interpolated one, so the ball and the bars
  // move together. It is the element's OWN transform only — an ancestor's translate is not
  // included — so the rail sums the group's slide and the hot bar's lift explicitly.
  const compositionLiveOffset = (element) => {
    if (!element) return { dx: 0, dy: 0 };
    const transform = getComputedStyle(element).transform;
    const match = typeof transform === 'string' ? transform.match(/matrix\(([^)]+)\)/) : null;
    if (!match) return { dx: 0, dy: 0 };
    const values = match[1].split(',').map((value) => Number(value.trim()));
    return { dx: values[4] || 0, dy: values[5] || 0 };
  };
  const compositionGroupOffset = (key) => {
    if (!key || !compositionBars) return { dx: 0, dy: 0 };
    return compositionLiveOffset([...compositionBars.querySelectorAll('.agent-context-composition-group')]
      .find((item) => item.dataset.contextGroup === key));
  };
  // CONTEXT-BAR-HOTPOP: the highlighted bar's own lift, keyed on the same source key the
  // markup writes into data-context-source.
  const compositionHotOffset = (key) => {
    if (!key || !compositionBars) return { dx: 0, dy: 0 };
    return compositionLiveOffset([...compositionBars.querySelectorAll('.agent-context-composition-bar')]
      .find((item) => item.dataset.contextSource === key));
  };
  // CONTEXT-SECTION-MAGNIFY: the factor a bar is drawn at right now — its category's own
  // growth while that category is hovered, times the extra growth when it is the bar the
  // pointer is on. Read from the eased state rather than from the DOM, because the
  // enlargement is baked into the path geometry rather than applied as a transform.
  const compositionSourceGrow = (source) =>
    (compositionGrow.get(source.categoryKey) ?? 1) * (source.key === compositionHotKey ? compositionHotGrow : 1);
  // The bar's visible tip radius. The inner edge is pinned, so the bar extends outward by
  // the growth factor times its resting length.
  const compositionSourceOuter = (source) =>
    COMPOSITION_BAR_INNER + (source.outer - COMPOSITION_BAR_INNER) * compositionSourceGrow(source);
  // Rewrite every bar's `d` from the current eased growth. Bars are paths, so the
  // enlargement is geometry and has to be re-laid-out as it animates.
  const renderCompositionBarGeometry = () => {
    compositionBars?.querySelectorAll('.agent-context-composition-bar').forEach((bar) => {
      const source = compositionSourceByKey.get(bar.dataset.contextSource);
      if (!source) return;
      bar.setAttribute('d', compositionArcPath(source.start, source.end, COMPOSITION_BAR_INNER, compositionSourceOuter(source)));
    });
  };
  // Ease every category — and the highlighted bar — one frame toward its target, and
  // report whether anything is still moving so the caller knows when to stop the loop.
  const stepCompositionGrow = () => {
    let moving = false;
    compositionEntries.forEach((entry) => {
      const current = compositionGrow.get(entry.key) ?? 1;
      const target = entry.key === compositionPoppedKey ? COMPOSITION_GROUP_GROW : 1;
      if (current === target) return;
      compositionGrow.set(entry.key, Math.abs(target - current) < 0.004 ? target : current + (target - current) * 0.24);
      moving = true;
    });
    const hotTarget = compositionHotKey ? COMPOSITION_BAR_GROW : 1;
    if (compositionHotGrow !== hotTarget) {
      compositionHotGrow = Math.abs(hotTarget - compositionHotGrow) < 0.004 ? hotTarget : compositionHotGrow + (hotTarget - compositionHotGrow) * 0.24;
      moving = true;
    }
    if (moving) renderCompositionBarGeometry();
    return moving;
  };
  // RADIAL-RAIL-DYNAMIC: build the closed curve through the source-bar tips in circular
  // source order, lifting the popped category's knots by that group's live offset and the
  // highlighted bar's knot by its own extra lift — so the curve reaches out to the bars
  // that have actually moved, including the single bar standing proud of its set. The knot
  // radius is the bar's *grown* tip, so the enlarged tips are still what the ball rides.
  const compositionRail = (poppedKey, hotKey) => {
    if (!compositionSources.length) return null;
    const group = compositionGroupOffset(poppedKey);
    const hot = compositionHotOffset(hotKey);
    const knots = compositionSources.map((source) => {
      const fraction = (source.start + source.end) / 2;
      const angle = fraction * Math.PI * 2;
      const lifted = Boolean(poppedKey) && source.categoryKey === poppedKey;
      const proud = Boolean(hotKey) && source.key === hotKey;
      const tip = compositionSourceOuter(source);
      return {
        key: source.key,
        fraction,
        x: 100 + Math.sin(angle) * tip + (lifted ? group.dx : 0) + (proud ? hot.dx : 0),
        y: 100 - Math.cos(angle) * tip + (lifted ? group.dy : 0) + (proud ? hot.dy : 0),
      };
    });
    if (knots.length < 2) return null;
    return { points: knots };
  };
  const trendPointAt = (fraction, poppedKey, hotKey) => {
    const trend = compositionRail(poppedKey, hotKey);
    if (!trend?.points?.length) return null;
    // CONTEXT-BAR-MARKER: the ball takes the radius of the bar the pointer is *on* — the
    // knot the rail already carries for that bar — instead of interpolating between the
    // two bars that bracket the pointer's bearing.
    //
    // The interpolation was the last place the marker could leave the bars: crossing the
    // drawn gap between two neighbours it produced a radius that belonged to neither, so
    // the ball came to rest floating in the dead space between them (measured before this
    // change: 718 of 720 bearings around the ring placed the ball off the highlighted
    // bar). The signal regions tile edge to edge (CONTEXT-BAR-EDGE-HIT), so the owning bar
    // is always defined and the ball is always on a real bar.
    //
    // Reading the radius from the owning knot rather than from the space between knots
    // keeps the needle and the ball one straight instrument at every bearing: the ball
    // stays on the pointer's ray and only its distance changes, and the radius still comes
    // from the live pops, so the ball climbs to the popped group's tips — including the
    // one bar standing proud of its set — then steps to the next bar's tip.
    const source = compositionSourceAtFraction(fraction);
    const knot = source && trend.points.find((item) => item.key === source.key);
    if (!knot || ![knot.x, knot.y].every(Number.isFinite)) return null;
    const angle = fraction * Math.PI * 2;
    const radius = Math.hypot(knot.x - 100, knot.y - 100);
    if (!Number.isFinite(radius)) return null;
    return {
      x: 100 + Math.sin(angle) * radius,
      y: 100 - Math.cos(angle) * radius,
    };
  };
  // RADIAL-LOCATOR-REACH: the outward end of the needle. The chart sits inside a
  // container panel, and the locator should read across that whole region rather than
  // stop at the ring, so the tip is wherever the pointer's ray leaves the container
  // box — clamped to at least the ring's outer edge so it always clears the bars. The
  // reach is measured in screen pixels and converted into the SVG's 0 0 200 200 space
  // through the chart's own width, so it stays correct across the responsive
  // breakpoints that resize the chart.
  const compositionLocatorReach = (dx, dy) => {
    const chartRect = compositionChart?.getBoundingClientRect();
    const container = compositionChart?.parentElement;
    const chartWidth = chartRect?.width || 0;
    if (!chartWidth || !container) return 100;
    const units = 200 / chartWidth;
    const cx = chartRect.left + chartWidth / 2;
    const cy = chartRect.top + chartRect.height / 2;
    const box = container.getBoundingClientRect();
    // A small inset keeps the tip inside the container's rounded corner.
    const inset = 6;
    const limits = [];
    if (dx > 1e-6) limits.push((box.right - inset - cx) / dx);
    if (dx < -1e-6) limits.push((box.left + inset - cx) / dx);
    if (dy > 1e-6) limits.push((box.bottom - inset - cy) / dy);
    if (dy < -1e-6) limits.push((box.top + inset - cy) / dy);
    const reach = Math.min(...limits.filter((value) => Number.isFinite(value) && value > 0));
    if (!Number.isFinite(reach)) return 100;
    // Never shorter than the ring, never long enough to run away with the layout.
    return Math.min(Math.max(reach * units, 100), 400);
  };
  // RADIAL-LOCATOR-START: the inner end of the needle. Outside the bar band it stays
  // pinned to the bottom of the bars (COMPOSITION_BAR_INNER); inside the centre circle
  // it follows the pointer inward instead, so the cursor is always a point ON the line.
  // That is what lets the locator reach the middle of the chart — the region a
  // ring-anchored needle could only cross, never touch — and it keeps the two ends and
  // the contact region describing one and the same span.
  const compositionLocatorStart = (pointerRadius) => {
    if (!Number.isFinite(pointerRadius)) return COMPOSITION_BAR_INNER;
    return Math.max(0, Math.min(COMPOSITION_BAR_INNER, pointerRadius));
  };
  // CONTEXT-GROUP-POPOUT: the whole category the pointer is over — its translucent track
  // and every bar in the set — slides out as one piece lifted off the ring rather than as
  // individual bars. Only one group pops at a time, and it always travels straight out
  // along its own bisector: the vector is baked per group into the SVG markup, so the
  // slide is purely the `is-popped` class and the pop cannot lean toward the pointer.
  // Keyboard navigation routes through the same call, so arrow-stepping around the ring
  // pops the focused category too instead of leaving the pointer as the only trigger.
  // RADIAL-RAIL-DYNAMIC: while a group is sliding out, the rail has to keep up with it.
  // A stationary pointer emits no pointermove, so without this the ball would freeze on
  // the rail the moment the pop began and only catch up on the next mouse move. Re-paint
  // the ball and needle for the length of the transition (with slack for the easing tail),
  // then stop, so the loop is bounded and only runs on an actual change of category.
  // Land exactly on target. The easing approaches its target asymptotically and the loop
  // is also bounded by a deadline, so without this the bars stop a fraction of a unit
  // short of the size they are meant to be showing (measured: ~0.2 units on a 1.25x
  // section, ~0.8 on the 1.875x bar, because the loop's deadline arrives first).
  const settleCompositionGrow = () => {
    compositionEntries.forEach((entry) => {
      compositionGrow.set(entry.key, entry.key === compositionPoppedKey ? COMPOSITION_GROUP_GROW : 1);
    });
    compositionHotGrow = compositionHotKey ? COMPOSITION_BAR_GROW : 1;
    renderCompositionBarGeometry();
    if (compositionFocusState) paintCompositionGeometry();
  };
  const startCompositionRailSettle = () => {
    if (compositionRailFrame) cancelAnimationFrame(compositionRailFrame);
    const until = performance.now() + 420;
    const step = () => {
      compositionRailFrame = null;
      // The growth is geometry, so it is eased here frame by frame; the slide is a CSS
      // transition and repainting the geometry each frame is what keeps the ball on the
      // moving tips while that runs. The loop is bounded by BOTH: it stops once the growth
      // has settled and either the focus has gone or the transition window has elapsed.
      const moving = stepCompositionGrow();
      if (compositionFocusState) paintCompositionGeometry();
      if ((moving || compositionFocusState) && performance.now() < until) {
        compositionRailFrame = requestAnimationFrame(step);
      } else if (moving) {
        settleCompositionGrow();
      }
    };
    compositionRailFrame = requestAnimationFrame(step);
  };
  // CONTEXT-GROUP-POPOUT: set the lifted category and the
  // single highlighted bar in one step. Both are keyed to the current selection, so exactly one
  // group and at most one bar are ever lifted; clearing passes nulls and strands nothing
  // off the ring. The rail is only re-run when the selection actually changes, so a
  // pointer moving inside one bar neither restarts the loop nor re-runs the pop.
  const popCompositionGroup = (key, hotKey = null) => {
    const popped = key || null;
    const hot = hotKey || null;
    const changed = compositionPoppedKey !== popped || compositionHotKey !== hot;
    compositionPoppedKey = popped;
    compositionHotKey = hot;
    compositionBars?.querySelectorAll('.agent-context-composition-group').forEach((group) => {
      group.classList.toggle('is-popped', Boolean(popped) && group.dataset.contextGroup === popped);
    });
    compositionBars?.querySelectorAll('.agent-context-composition-bar').forEach((bar) => {
      bar.classList.toggle('is-hot', Boolean(hot) && bar.dataset.contextSource === hot);
    });
    if (changed) startCompositionRailSettle();
  };
  // RADIAL-RAIL-DYNAMIC: the ball and needle are painted from the stored focus state, so
  // the pop animation can re-run just this part each frame without rebuilding the tooltip
  // or re-reading the pointer. The ball is solved against the rail for the category that
  // is currently popped and the bar that is currently highlighted, so it rides the moved
  // bars — including the one bar standing proud of its set — rather than the ring.
  const paintCompositionGeometry = () => {
    const state = compositionFocusState;
    if (!state || !compositionNode) return;
    const { fraction, pointerRadius } = state;
    const point = trendPointAt(fraction, compositionPoppedKey, compositionHotKey);
    const angle = fraction * Math.PI * 2;
    // Without a rail (for example an incomplete payload), retain the ring-centred
    // locator. The normal path uses the exact top of the interpolated radial bar.
    compositionNode.style.setProperty('--context-node-x', `${(point ? point.x / 2 : 50 + Math.sin(angle) * 41).toFixed(2)}%`);
    compositionNode.style.setProperty('--context-node-y', `${(point ? point.y / 2 : 50 - Math.cos(angle) * 41).toFixed(2)}%`);
    compositionNode.removeAttribute('hidden');
    // RADIAL-LOCATOR: the model chart's vertical cross turned radially. The needle
    // holds the pointer's angle — the same angle solved into the ball above — so it
    // passes exactly through the glowing node, and it tracks the whole contact region
    // rather than only the top trend line. Both ends now live inside the graph
    // container: the inner end is released from the bar ring so it can follow the
    // pointer into the centre circle (RADIAL-LOCATOR-START), and the outer end runs out
    // to the container's edge (RADIAL-LOCATOR-REACH).
    const crossLine = compositionBars?.querySelector('.agent-context-composition-cross');
    if (crossLine) {
      if (point) {
        const dx = Math.sin(angle);
        const dy = -Math.cos(angle);
        const reach = compositionLocatorReach(dx, dy);
        const start = compositionLocatorStart(pointerRadius);
        crossLine.setAttribute('x1', (100 + dx * start).toFixed(2));
        crossLine.setAttribute('y1', (100 + dy * start).toFixed(2));
        crossLine.setAttribute('x2', (100 + dx * reach).toFixed(2));
        crossLine.setAttribute('y2', (100 + dy * reach).toFixed(2));
        crossLine.removeAttribute('hidden');
      } else {
        crossLine.setAttribute('hidden', '');
      }
    }
  };
  const showCompositionFocus = (source, fraction, clientX, clientY, pointerRadius) => {
    if (!compositionChart || !compositionNode || !compositionTip || !source) return;
    compositionFocusState = { source, fraction, pointerRadius };
    // The ball is painted with the neutral text token, so this variable no longer
    // colours it — it is the active-source key the arrow-key handler matches on, and
    // dropping it would break keyboard navigation around the ring.
    compositionNode.style.setProperty('--context-node-color', source.color);
    // CONTEXT-BAR-HOTPOP: the category is lifted and, with it, the single bar the pointer
    // is over. The bar's key rides on the source so the rail can pick that one knot out and
    // offset it further than the rest of the set. Both are applied before the geometry is
    // painted, so the rail reads each pop as it starts rather than a frame late — and being
    // keyed on a change of selection, a repeat hover over the same bar neither restarts the
    // loop nor re-runs the pop.
    popCompositionGroup(source.categoryKey, source.key);
    paintCompositionGeometry();
    const percent = Math.round((source.tokens / Math.max(1, compositionEntries.total || 1)) * 100);
    compositionTip.innerHTML = `<strong>${_esc(source.label)}</strong><span>${_esc(source.categoryLabel)} · ${percent}% of base context</span><b>${source.tokens.toLocaleString()} tokens</b>`;
    compositionTip.removeAttribute('hidden');
    const tipRect = compositionTip.getBoundingClientRect();
    const gap = 14;
    const placeLeft = clientX < window.innerWidth / 2 ? clientX + gap : clientX - tipRect.width - gap;
    const placeTop = clientY + gap;
    compositionTip.style.left = `${Math.max(gap, Math.min(placeLeft, window.innerWidth - tipRect.width - gap)).toFixed(0)}px`;
    compositionTip.style.top = `${Math.max(gap, Math.min(placeTop, window.innerHeight - tipRect.height - gap)).toFixed(0)}px`;
  };
  // CONTEXT-BAR-EDGE-HIT: resolve a pointer bearing to the bar whose *signal* region owns
  // it. Regions tile each group edge to edge, so inside a group this is exact; the
  // angular-nearest fallback covers only the floating-point seam at fraction 0/1, and
  // measures distance across the wrap so the answer is never an arbitrary end bar.
  const compositionSourceAtFraction = (fraction) => {
    if (!compositionSources.length) return null;
    const wrapped = ((fraction % 1) + 1) % 1;
    const hit = compositionSources.find((item) => wrapped >= item.hitStart && wrapped < item.hitEnd);
    if (hit) return hit;
    let best = compositionSources[0];
    let bestDistance = Infinity;
    for (const item of compositionSources) {
      const mid = (item.hitStart + item.hitEnd) / 2;
      const raw = Math.abs(wrapped - mid);
      const distance = Math.min(raw, 1 - raw);
      if (distance < bestDistance) { bestDistance = distance; best = item; }
    }
    return best;
  };
  const focusCompositionAt = (clientX, clientY) => {
    if (!compositionChart || !compositionEntries.length) return;
    const rect = compositionChart.getBoundingClientRect();
    const x = clientX - rect.left - rect.width / 2;
    const y = clientY - rect.top - rect.height / 2;
    // The locator reads the pointer's bearing from the disc centre anywhere in the
    // contact region — RADIAL-LOCATOR-HIT: the chart's containing panel, which is the
    // same box the needle's outward end is measured against, so the detected span and
    // the drawn span are one and the same region. That covers the centre circle and the
    // panel around the disc, not merely the bars, the way the model cross reads its
    // whole plot.
    const fraction = ((Math.atan2(x, -y) / (Math.PI * 2)) + 1) % 1;
    // Pointer distance from the centre in the SVG's 0 0 200 200 units, so the needle's
    // inner end can follow the pointer inside the centre circle.
    const pointerRadius = rect.width > 0 ? (Math.hypot(x, y) * 200) / rect.width : NaN;
    // CONTEXT-BAR-EDGE-HIT: the bar is chosen by its signal region, never by a fallback to
    // the group's last bar. That fallback was what highlighted the end bar of a set
    // whenever the pointer sat in the drawn gap between two of its bars.
    const source = compositionSourceAtFraction(fraction);
    showCompositionFocus(source, fraction, clientX, clientY, pointerRadius);
  };
  const focusCompositionIndex = (index) => {
    if (!compositionChart || !compositionSources.length) return;
    const source = compositionSources[(index + compositionSources.length) % compositionSources.length];
    const fraction = (source.start + source.end) / 2;
    const rect = compositionChart.getBoundingClientRect();
    const angle = fraction * Math.PI * 2;
    const clientX = rect.left + rect.width * (0.5 + Math.sin(angle) * 0.41);
    const clientY = rect.top + rect.height * (0.5 - Math.cos(angle) * 0.41);
    showCompositionFocus(source, fraction, clientX, clientY);
  };
  // RADIAL-LOCATOR-HIT: the listeners live on the chart's containing panel, not on the
  // 164px disc, so the needle and ball appear for the pointer anywhere in the graph
  // container. Binding to the disc alone left the surrounding panel — every corner the
  // needle's tip already reached — silently dead.
  const compositionHitRegion = compositionChart?.parentElement || compositionChart;
  compositionHitRegion?.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    try { compositionHitRegion.setPointerCapture(event.pointerId); } catch (_) {}
    focusCompositionAt(event.clientX, event.clientY);
  });
  compositionHitRegion?.addEventListener('pointermove', (event) => focusCompositionAt(event.clientX, event.clientY));
  compositionHitRegion?.addEventListener('pointerleave', (event) => {
    if (event.pointerType === 'mouse') hideCompositionFocus();
  });
  compositionHitRegion?.addEventListener('pointercancel', hideCompositionFocus);
  compositionChart?.addEventListener('keydown', (event) => {
    if (!compositionSources.length) return;
    const current = compositionSources.findIndex((source) => !compositionNode?.hidden && source.color === compositionNode.style.getPropertyValue('--context-node-color'));
    const selected = current < 0 ? 0 : current;
    if (event.key === 'Home') { event.preventDefault(); focusCompositionIndex(0); }
    else if (event.key === 'End') { event.preventDefault(); focusCompositionIndex(-1); }
    else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') { event.preventDefault(); focusCompositionIndex(selected + 1); }
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') { event.preventDefault(); focusCompositionIndex(selected - 1); }
    else if (event.key === 'Escape') { hideCompositionFocus(); }
  });
  // CONTEXT-OUTSIDE-CLICK: a click that lands anywhere outside the chart's contact region
  // drops the focus entirely — the marker leaves the ring, the tooltip panel closes and the
  // lifted set slides back. The listener is on the document rather than on the container
  // because the whole point is the click that lands *away* from the chart; containment
  // against the contact region is what keeps a click on the chart with the chart, whose own
  // pointerdown already handles it. The chart can hold DOM focus (it is tabindex="0"), so
  // the outside click releases that too — an unfocused chart should not keep a focus ring.
  // It unbinds itself once the chart leaves the DOM, so re-rendering the tab cannot stack
  // document listeners.
  const onCompositionOutsidePointerDown = (event) => {
    if (!compositionHitRegion?.isConnected) {
      document.removeEventListener('pointerdown', onCompositionOutsidePointerDown);
      return;
    }
    if (compositionHitRegion.contains(event.target)) return;
    if (document.activeElement === compositionChart) compositionChart.blur();
    if (compositionFocusState || compositionPoppedKey) hideCompositionFocus();
  };
  document.addEventListener('pointerdown', onCompositionOutsidePointerDown);
  const setComposition = (parts, total, breakdown = {}, counts = {}) => {
    if (!compositionChart || !compositionLegend || !compositionTotal || !compositionBars) return;
    // Short names — the legend is a compact one-line-per-group list now, so the
    // long labels ("System prompt", "Tool documentation") became Prompt/Tools/Gates.
    const entries = [
      { key: 'system_prompt', label: 'Prompt', color: 'var(--chart-1)' },
      { key: 'skills', label: 'Skills', color: 'var(--chart-2)' },
      { key: 'tools', label: 'Tools', color: 'var(--chart-3)' },
    ].map((entry) => ({
      ...entry,
      tokens: Math.max(0, Number(parts?.[entry.key]?.tokens ?? parts?.[entry.key]) || 0),
    }));
    const resolvedTotal = Math.max(0, Number(total) || entries.reduce((sum, entry) => sum + entry.tokens, 0));
    // CONTEXT-SECTION-MAGNIFY: polar() and arcPath() used to live here. They are hoisted
    // to module scope now, because the magnify animation rebuilds individual bar paths on
    // later frames and has to reach the same builder without re-running this whole build.
    const sourcesFor = (entry) => {
      const supplied = Array.isArray(breakdown?.[entry.key]) ? breakdown[entry.key] : [];
      const sourceTotal = supplied.reduce((sum, source) => sum + Math.max(0, Number(source?.tokens) || 0), 0);
      // A group with no tokens (for example a gate set that is switched off) must not
      // emit a bar: a zero-token source divides by a zero "largest" bar, so its outer
      // radius — and the trend knot riding it — would be NaN and poison the chart. The
      // category track still marks the group's place in the ring.
      if (!sourceTotal) return entry.tokens > 0 ? [{ label: entry.label, tokens: entry.tokens, misc: false }] : [];
      let remaining = entry.tokens;
      const sources = supplied.map((source) => {
        const tokens = Math.floor((Math.max(0, Number(source.tokens) || 0) / sourceTotal) * entry.tokens);
        remaining -= tokens;
        return { label: String(source.label || 'Untitled source'), tokens, misc: Boolean(source.misc) };
      });
      for (const source of [...sources].sort((a, b) => b.tokens - a.tokens)) {
        if (!remaining) break;
        source.tokens += 1;
        remaining -= 1;
      }
      return sources.filter((source) => source.tokens > 0);
    };
    hideCompositionFocus();
    compositionEntries = [];
    compositionSources = [];
    compositionEntries.total = resolvedTotal;
    if (!resolvedTotal) {
      compositionChart.className = 'agent-context-composition-chart is-empty';
      compositionChart.style.background = '';
      compositionBars.replaceChildren();
      compositionChart.setAttribute('aria-label', 'Base context composition is unavailable');
      compositionTotal.textContent = 'Unavailable';
      compositionLegend.innerHTML = '<span class="is-loading">Composition unavailable</span>';
      return;
    }
    let position = 0;
    entries.forEach((entry) => {
      entry.start = position;
      position += entry.tokens / resolvedTotal;
      entry.end = position;
      // The category's own bearing — the one and only direction its hover pop-out travels
      // (CONTEXT-GROUP-POPOUT). No pointer bearing is folded in, so the set can never lean
      // toward the cursor or the touch.
      entry.bisector = (entry.start + entry.end) / 2;
      entry.sources = sourcesFor(entry);
      const slot = (entry.end - entry.start) / entry.sources.length;
      const gap = Math.min(0.004, slot * 0.14);
      const largest = Math.max(...entry.sources.map((source) => source.tokens));
      entry.sources.forEach((source, index) => {
        source.categoryLabel = entry.label;
        source.categoryKey = entry.key;
        source.color = entry.color;
        // CONTEXT-BAR-HOTPOP: a stable per-source key, so the highlighted bar can be found
        // again from the focus state (both for the class toggle and for reading its live
        // lift while it animates). Labels are not unique, so the slot index is used.
        source.key = `${entry.key}:${index}`;
        source.start = entry.start + slot * index + gap / 2;
        source.end = entry.start + slot * (index + 1) - gap / 2;
        // CONTEXT-BAR-EDGE-HIT: the *signal* region, as opposed to the drawn shape. The bar
        // is painted inset by half a gap on each side, so its visual span leaves a dead
        // sliver between neighbours; hit-testing on that span flagged no bar at all there
        // and fell through to the "last bar of the group" fallback. The signal region runs
        // slot-edge to slot-edge instead, so the group's whole angular span is covered with
        // no dead space and no overlap — the marker can only ever land on a real bar.
        source.hitStart = entry.start + slot * index;
        source.hitEnd = entry.start + slot * (index + 1);
        source.outer = 76 + 21 * Math.sqrt(source.tokens / largest);
        // The bar's own lift vector: straight out along its mid-bearing, which is what
        // makes the highlighted bar stand proud of its set rather than sliding sideways.
        const barAngle = ((source.start + source.end) / 2) * Math.PI * 2;
        source.popDx = (Math.sin(barAngle) * COMPOSITION_BAR_POP).toFixed(2);
        source.popDy = (-Math.cos(barAngle) * COMPOSITION_BAR_POP).toFixed(2);
      });
    });
    compositionEntries = entries;
    compositionEntries.total = resolvedTotal;
    compositionSources = entries.flatMap((entry) => entry.sources);
    // CONTEXT-SECTION-MAGNIFY: a fresh render starts every bar at its resting size, and
    // registers the sources so the magnify animation can find a bar's geometry by key.
    compositionSourceByKey = new Map(compositionSources.map((source) => [source.key, source]));
    compositionGrow.clear();
    compositionHotGrow = 1;
    compositionChart.className = 'agent-context-composition-chart';
    compositionChart.style.background = 'var(--bg-1)';
    const barsMarkup = entries.map((entry) => {
      const track = `<path class="agent-context-composition-track" d="${compositionArcPath(entry.start, entry.end, 66, 97)}" fill="${entry.color}" fill-opacity=".16"/>`;
      const bars = entry.sources.map((source) => `<path class="agent-context-composition-bar${source.misc ? ' is-misc' : ''}" data-context-source="${source.key}" style="--context-bar-dx:${source.popDx}px;--context-bar-dy:${source.popDy}px" d="${compositionArcPath(source.start, source.end, COMPOSITION_BAR_INNER, source.outer)}" fill="${source.color}"/>`).join('');
      // CONTEXT-GROUP-POPOUT: a category is emitted as one <g> — its translucent track
      // plus all of its bars — so the whole set can lift off the ring together when the
      // pointer is over that category. The outward vector rides the group's own bisector
      // and is precomputed here, because the bearing is only known once the category's
      // angular span has been derived from its token share.
      const groupAngle = entry.bisector * Math.PI * 2;
      const groupDx = (Math.sin(groupAngle) * COMPOSITION_GROUP_POP).toFixed(2);
      const groupDy = (-Math.cos(groupAngle) * COMPOSITION_GROUP_POP).toFixed(2);
      return `<g class="agent-context-composition-group" data-context-group="${entry.key}" style="--context-group-dx:${groupDx};--context-group-dy:${groupDy}">${track}${bars}</g>`;
    }).join('');
    // RADIAL-TOP-TRAJECTORY: the rail the ball rides is no longer baked here. It is
    // rebuilt per focus from the live bar tips (compositionRail), so it can lift with a
    // popped category instead of staying on the ring that category has left behind. The
    // dashed connector that was once drawn along it is still gone: this is an invisible
    // placement path, not a rendered line.
    // RADIAL-LOCATOR: the cross hairline is always in the DOM so focus can reveal it;
    // it starts zero-length and hidden, and only the pending focus paints its angle.
    compositionBars.innerHTML = `${barsMarkup}<line class="agent-context-composition-cross" data-context-composition-cross x1="100" y1="100" x2="100" y2="100" hidden></line>`;
    const sizeLabel = `configured prompt and tools ${resolvedTotal.toLocaleString()} tokens`;
    compositionChart.setAttribute('aria-label', `Agent context: ${sizeLabel}. ${entries.map((entry) => `${entry.label} ${Math.round((entry.tokens / resolvedTotal) * 100)} percent`).join(', ')}. Each radial bar is an individual source; move around the chart or use arrow keys to inspect one.`);
    compositionTotal.innerHTML = `<span class="agent-context-composition-total-row"><b>Configured</b><i>${resolvedTotal.toLocaleString()} tokens</i></span>`;
    // AGENT-CONTEXT-COMPACT-LEGEND: one line per group — short name plus its inventory
    // count ("Tools (83-36-47)" = total-visible-discoverable) and the group's share
    // of the base context. The per-row token/source sub-line is gone: the centre
    // total and the bar sizes already carry it.
    compositionLegend.innerHTML = entries.map((entry) => {
      const percent = Math.round((entry.tokens / resolvedTotal) * 100);
      const count = counts?.[entry.key];
      return `<span class="agent-context-composition-item"><i style="background:${entry.color}"></i><span>${entry.label}${count ? ` (${count})` : ''}</span><strong>${percent}%</strong></span>`;
    }).join('');
  };

  const syncArea = overview.querySelector('.agent-abilities-sync');
  const addSync = (label, endpoint, confirmation) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ac-btn ac-btn-sm';
    button.textContent = label;
    button.addEventListener('click', async () => {
      if (!window.confirm(confirmation)) return;
      button.disabled = true;
      button.textContent = 'Syncing…';
      try {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          ...(endpoint.includes('push-to-template') ? { body: JSON.stringify({ user_id: app.currentUserId }) } : {}),
        });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.detail || 'Sync failed');
        }
        button.textContent = '✓ Synced';
        button.style.background = 'var(--success)';
      } catch (error) {
        button.textContent = '⚠ Failed';
        window.alert(error.message || 'Sync failed');
        button.disabled = false;
        setTimeout(() => { button.textContent = label; button.style.background = ''; }, 2000);
      }
    });
    syncArea.appendChild(button);
  };

  if (_userIsAdmin && agent.template_id) {
    const templateName = agent.template_origin || agent.template_id;
    addSync('Sync with template', `/api/v1/agents/${encodeURIComponent(agent.id)}/push-to-template`,
      `Sync this agent’s configuration and prompts to the “${templateName}” template?`);
  }
  const jsonTemplateId = agent.template_id || (agent.source === 'template' ? agent.id : '');
  if (_userIsAdmin && jsonTemplateId && (agent.template_has_json || agent.template_source === 'json_seed')) {
    addSync('Sync with JSON', `/admin/db/templates/${encodeURIComponent(jsonTemplateId)}/export-to-file`,
      `Sync “${agent.template_origin || jsonTemplateId}” to its JSON seed file?`);
  }

  let refreshVersion = 0;
  // A successful row save has already committed its value to the database.
  // Mark the card pending immediately and invalidate any older in-flight read;
  // the debounced refresh below then reads that saved state back.
  overview.markContextRefreshing = () => {
    refreshVersion += 1;
    // The composition card is the only context readout left, so a save parks the
    // chart in its pending state rather than the removed token/pricing rows.
    // CONTEXT-CHART-LOADING: park the chart in the SHARED skeleton state (sk-shimmer),
    // not a page-local flat fill, so its placeholder glimmers like the rest of the page.
    if (compositionChart) compositionChart.className = 'agent-context-composition-chart is-loading sk-shimmer';
    if (compositionLegend) compositionLegend.innerHTML = '<span class="is-loading">Updating…</span>';
    if (compositionTotal) compositionTotal.textContent = '—';
    hideCompositionFocus();
  };
  const refresh = () => {
    const version = ++refreshVersion;
    const contextParams = new URLSearchParams({ user_id: app.currentUserId });
    return Promise.allSettled([
      canViewContext
        ? request(`/api/v1/agents/${encodeURIComponent(agent.id)}/context-tokens?${contextParams}`)
        : Promise.reject(new Error('Context unavailable')),
      // Both loaders populate the ability table below the card — keep calling them
      // for that side effect; their resolved values no longer feed the summary.
      loadConnections(),
      loadAgentTools(),
      request(`/api/v1/agents/${encodeURIComponent(agent.id)}/my-prompts?user_id=${encodeURIComponent(app.currentUserId)}`),
      request(`/api/v1/agents/${encodeURIComponent(agent.id)}/skills-summary?user_id=${encodeURIComponent(app.currentUserId)}`),
    ]).then(([context, _connections, toolsResult, promptsResult, skillsResult]) => {
      if (version !== refreshVersion) return;
      const tokens = context.status === 'fulfilled' ? context.value : null;
      const tools = toolsResult.status === 'fulfilled' ? toolsResult.value : [];
      const visibleTools = tools.filter(tool => tool.sent === true || tool.mode === 'always' || tool.visibility === 'visible').length;
      const discoverableTools = tools.filter(tool => tool.mode === 'discoverable' || tool.visibility === 'discoverable').length;
      const promptCount = promptsResult.status === 'fulfilled' ? (promptsResult.value.slots || []).length : 0;
      const skills = skillsResult.status === 'fulfilled' ? skillsResult.value : {};
      const totalSkills = Number.isFinite(skills.total) ? skills.total : 0;
      const visibleSkills = Number.isFinite(skills.visible) ? skills.visible : 0;
      const discoverableSkills = Number.isFinite(skills.discoverable) ? skills.discoverable : 0;
      // LEGEND-SOURCE-COUNTS: the breakdown legend now carries the inventory that
      // used to have its own panel — the prompt-slot count, plus each group's
      // total-visible-discoverable split (e.g. "Tools (83-36-47)").
      const counts = {
        system_prompt: String(promptCount),
        skills: `${totalSkills}-${visibleSkills}-${discoverableSkills}`,
        tools: `${tools.length}-${visibleTools}-${discoverableTools}`,
      };
      const hasTotal = typeof tokens?.tokens === 'number';
      setComposition(
        hasTotal ? tokens.breakdown || {} : null,
        hasTotal ? tokens.tokens : 0,
        {},
        counts,
      );
    });
  };

  overview.refresh = refresh;
  refresh();
  return overview;
}

// ── Cross-section search (summary + Prompts + abilities + Custom abilities) ────
// The route-level Ask or Search launcher owns the ONLY visible input on the
// Agents page, so this composite filter is what its query drives in the Abilities
// tab. A query narrows EVERY searchable surface — the stat lines in the summary at
// the top, the Prompts group, the ability groups, and the Custom abilities group —
// while the TIER group (the level selector) stays constant. Ability cards keep
// using the table's own matcher; the synthetic Prompts/Custom rows are matched
// here because the table holds no index for them (see `data-agent-filter-self`).
function _runAgentsAbilitiesFilter(rawQuery, { overview, grid, tableFilter }) {
  const q = (rawQuery || '').trim().toLowerCase();

  // 1. Ability cards + ability groups — reuse the table's own filter untouched.
  if (typeof tableFilter === 'function') tableFilter(rawQuery);

  // 2. Summary stat lines. TIER is constant; every other line survives only when
  //    its own text matches, and a group whose lines all drop folds away.
  if (overview) {
    overview.querySelectorAll('.agent-abilities-summary-group').forEach(group => {
      if (group.classList.contains('agent-abilities-tier-group')) { group.style.display = ''; return; }
      const lines = group.querySelectorAll('.agent-abilities-summary-line, .agent-abilities-context-line');
      let any = false;
      lines.forEach(el => {
        const match = !q || el.textContent.toLowerCase().includes(q);
        el.style.display = match ? '' : 'none';
        if (match) any = true;
      });
      group.style.display = (!q || any) ? '' : 'none';
    });
  }

  // 3. Synthetic groups (Prompts / Custom abilities) own their own visibility.
  if (grid) {
    grid.querySelectorAll('[data-agent-filter-self]').forEach(group => _filterSyntheticGroup(group, q));
  }
}

// Match the real rows inside a synthetic group, building its lazy body first so a
// search works even when the group has never been opened. The group survives when
// a row matches, or when the query matches its own heading (e.g. "prompts",
// "custom abilities") — so section titles read as searchable lines too.
async function _filterSyntheticGroup(group, q) {
  if (typeof group._ensureBuilt === 'function') { try { await group._ensureBuilt(); } catch (_) { /* body shows its own error state */ } }
  const itemSel = group.dataset.agentFilterItem;
  const items = itemSel ? Array.from(group.querySelectorAll(itemSel)) : [];
  // A query naming the section itself ("prompts", "custom abilities") reveals the
  // WHOLE section — without this the heading would match while every row filtered
  // away, leaving an empty shell under a visible heading. Match the section TITLE
  // only (not the description), so a word like "skills" in a description can't
  // surface an unrelated section.
  const headText = (group.querySelector('.ac-ability-name')?.textContent || '').toLowerCase();
  const headMatch = !!q && headText.includes(q);
  let anyMatch = false;
  items.forEach(item => {
    const match = !q || headMatch || item.textContent.toLowerCase().includes(q);
    item.style.display = match ? '' : 'none';
    if (match) anyMatch = true;
  });
  const hasRows = items.length > 0 && anyMatch;
  group.style.display = (!q || hasRows || headMatch) ? '' : 'none';
  // Expand only when there is actually something to reveal.
  if (typeof group._setExpanded === 'function') group._setExpanded(!!q && (hasRows || headMatch));
}

export async function _renderConnectionsTab(body, agent) {
  // The route-level Ask or Search launcher owns the visible input. Clear any
  // controller left by a previous tab before this Abilities surface mounts.
  delete body._agentsSearchController;
  delete body.dataset.agentsSearchController;
  if (_isMockAgent(agent)) {
    body.innerHTML = '';
    const tabInner = document.createElement('div');
    tabInner.className = 'conn-tab-content';
    const hint = document.createElement('p');
    hint.className = 'conn-draft-hint';
    hint.textContent = 'Choose abilities now. They will be applied when you create this agent.';
    const abilityContainer = document.createElement('div');
    abilityContainer.className = 'conn-section';
    tabInner.append(hint, abilityContainer);
    body.appendChild(tabInner);

    const catalog = await loadAbilityCatalog();
    const saved = new Map(_mockAbilityConnections().map(connection => [connection.connection_type, connection]));
    const connections = Object.entries(catalog.abilities || {})
      .filter(([, ability]) => (ability.kind || 'ability') === 'ability' && !ability.placeholder)
      .map(([connection_type, ability]) => {
        const selected = saved.get(connection_type);
        return {
          connection_type,
          section: 'ability',
          enabled: !!selected?.enabled,
          config: selected?.config || {},
          display_name: ability.display_name || ability.name || connection_type,
          description: ability.description || '',
          status: ability.status || 'ready',
          locked_on: !!ability.locked_on,
          entitlement_allowed: ability.entitlement_allowed !== false,
        };
      });
    await buildAgentAbilityTable(abilityContainer, {
      agent,
      connectionsLoader: () => Promise.resolve({ connections, canEdit: true }),
      abilitiesByProvider: {},
      userId: app.currentUserId,
      agentToolsLoader: () => Promise.resolve([]),
      showSearch: false,
      onSave: async (abilityId, enabled, config) => {
        _saveMockAbilityConnection(abilityId, enabled, config);
      },
    });
    return;
  }

  body.innerHTML = '';
  // Constrain the abilities-tab content to a centered 750px column with 5px
  // padding while keeping the scrollbar full-width (the page/global scroller —
  // #agents-grid or the fixed-height .agent-detail-body — stays edge-to-edge so
  // the vertical scrollbar remains at the right of the viewport).
  const tabInner = document.createElement('div');
  tabInner.className = 'conn-tab-content';
  body.appendChild(tabInner);
  // Build the static skeleton SYNCHRONOUSLY after the selector so the panel never
  // shows blank: the ability table's own spinner appears on the very next line,
  // and the catalog/per-agent fetches run underneath it. The ability table loads
  // progressively — groups first, then toggle statuses, then a group's contents on
  // expand, then an ability's details on its own expand (see agent-ability-table.js).
  const noticeSlot = document.createElement('div'); noticeSlot.className = 'conn-notice-slot';
  tabInner.appendChild(noticeSlot);
  const abilitySection = document.createElement('div');
  abilitySection.className = 'conn-section';
  tabInner.appendChild(abilitySection);
  const abilityContainer = document.createElement('div');
  abilitySection.appendChild(abilityContainer);

  // Warm the catalog (ability icons / tool map) — the table also awaits it, but
  // kicking it off now lets it overlap the table's own setup. Cached after first.
  _ensureAbilityCatalog();

  // Track each ability's last enabled state so a CONFIG-only save (which reuses
  // the same onSave with the unchanged enabled flag) doesn't pop a misleading
  // "<ability> turned ON" notice — that notice is only for real toggle changes.
  const _lastEnabled = {};

  // Lazy, cached per-agent connections loader. The ability table calls this in
  // PARALLEL with rendering the group heads (so the first paint never waits on it),
  // then fills in each group's toggle status once it lands. Returns the ability
  // connections + whether this viewer can edit. The initial render needs only this
  // one light call; the agent's full resolved tool set (/tools) is fetched even
  // later, on first ability-expand, by loadAgentTools below.
  let _connPromise = null;
  let _memoryConnection = null;
  const loadConnections = (force = false) => {
    if (force) _connPromise = null;
    if (!_connPromise) {
      _connPromise = (async () => {
        const res = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const connections = (data.connections || []).filter(c => c.section === 'ability');
        const canEdit = (data.user_role || 'member') === 'admin';
        // Memory predates the descriptor-driven ability table. Until this agent
        // has saved the new connection row, seed the ability from its canonical
        // loop/tool state so existing recall/save choices migrate without a
        // database rewrite or a surprising reset to defaults.
        const memory = connections.find(c => c.connection_type === 'memory');
        _memoryConnection = memory || null;
        if (memory && !(memory.config && memory.config.ability_settings)) {
          const state = _memoryStateFromAgent(agent);
          memory.enabled = state.recall || state.save;
          memory.config = {
            ...(memory.config || {}),
            ability_settings: {
              memory_recall: state.recall,
              memory_save: state.save,
            },
          };
        }
        // Task Progress is a Knowledge ability, but its canonical settings live
        // in metadata.task_progress rather than a connection-config side store.
        // Seed the shared descriptor controls from that policy on every load.
        const taskProgress = connections.find(c => c.connection_type === 'task_progress');
        if (taskProgress) {
          const policy = agent.task_progress || {};
          // The card availability toggle is the sole Task Progress enable control.
          // Keep its rendered connection state aligned with the canonical policy;
          // settings themselves remain in metadata.task_progress.
          taskProgress.enabled = policy.enabled === true;
          taskProgress.config = {
            ...(taskProgress.config || {}),
            ability_settings: {
              completed_task_evidence_tokens: policy.completed_task_evidence_tokens ?? 20000,
              allow_self_completion: policy.allow_self_completion !== false,
            },
          };
        }
        connections.forEach(c => { _lastEnabled[c.connection_type] = !!c.enabled; });
        return { connections, canEdit };
      })();
    }
    return _connPromise;
  };

  // Lazy, cached loader for the agent's resolved tools. The ability table calls
  // this the first time an ability is expanded (and reuses the result for every
  // subsequent expand), so the heavy /tools resolution never blocks the initial
  // list and only runs if the user actually opens an ability.
  let _toolsPromise = null;
  const loadAgentTools = (force = false) => {
    if (force) _toolsPromise = null;
    if (!_toolsPromise) {
      _toolsPromise = fetch(`/api/v1/agents/${agent.id}/tools?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() })
        .then(r => (r.ok ? r.json() : { tools: [] }))
        .then(d => d.tools || [])
        .catch(() => []);
    }
    return _toolsPromise;
  };

  // Member-level policies are configuration, not an end-user capability view.
  // Resolve the existing role-aware connection response before mounting those
  // controls: only this agent's administrators may see or edit them. A failed
  // role lookup deliberately defaults to the safer read-only presentation; the
  // ability table retains its own error state for the failed connection request.
  let connectionAccess = null;
  try { connectionAccess = await loadConnections(); } catch (_) { /* read-only fallback */ }
  let levels = [];
  let levelSelector = null;
  if (connectionAccess && connectionAccess.canEdit) {
    try { levels = await loadMemberLevels(agent); } catch (error) {
      const notice = document.createElement('div'); notice.className = 'member-policy-error';
      notice.textContent = `Member levels could not be loaded: ${error.message}`; tabInner.appendChild(notice);
    }
    body._memberAbilityLevel ||= '__base_agent__';
    levelSelector = buildLevelSelector({
      levels,
      selected: body._memberAbilityLevel,
      label: 'Tier level configuration',
      showCue: false,
      onChange: (slug) => { body._memberAbilityLevel = slug; _renderConnectionsTab(body, agent); },
    });
  }
  const overview = _buildAbilitiesOverview(agent, {
    loadConnections,
    loadAgentTools,
    canViewContext: !agent.is_mock,
    levelSelector,
    levelCount: levels.length + 1,
  });
  tabInner.insertBefore(overview, abilitySection);

  // Capability saves can occur deep in progressive rows. Each successful save
  // bubbles one semantic event here; debounce it so a multi-control update gets
  // one fresh context and inventory read rather than a request per control.
  let overviewRefreshTimer = null;
  tabInner.addEventListener('agent-capability-change', () => {
    overview.markContextRefreshing?.();
    clearTimeout(overviewRefreshTimer);
    overviewRefreshTimer = setTimeout(() => {
      loadConnections(true);
      loadAgentTools(true);
      overview.refresh?.();
    }, 180);
  });

  if (connectionAccess && connectionAccess.canEdit) {
    if (!isBaseLevel(body._memberAbilityLevel)) {
      const profile = levels.find(level => level.slug === body._memberAbilityLevel);
      if (profile) { await _renderProfileAbilitiesMode(tabInner, agent, profile); return; }
      body._memberAbilityLevel = '__base_agent__';
    }
  }

  await buildAgentAbilityTable(abilityContainer, {
    agent,
    connectionsLoader: loadConnections,
    abilitiesByProvider: {},
    userId: app.currentUserId,
    agentToolsLoader: loadAgentTools,
    showSearch: false,
    onSearchController: (runFilter) => {
      // One input, the whole tab: the launcher's query drives the summary stat
      // lines, the Prompts group, the ability groups, and the Custom abilities
      // group together — the TIER selector stays put.
      body._agentsSearchController = (query) => _runAgentsAbilitiesFilter(query, {
        overview,
        grid: abilityContainer.querySelector('.conn-grid.ac-list'),
        tableFilter: runFilter,
      });
      body.dataset.agentsSearchController = 'abilities';
    },
    onSave: async (abilityId, enabled, config) => {
      if (abilityId === 'task_progress') {
        // The descriptor card owns the canonical policy. Do not persist a
        // parallel connection-config copy.
        const raw = (config && config.ability_settings) || {};
        const asBool = value => value === true || ['1', 'true', 'yes', 'on', 'enabled'].includes(String(value || '').trim().toLowerCase());
        const asNumber = (value, fallback) => {
          const parsed = Number(value);
          return Number.isFinite(parsed) ? parsed : fallback;
        };
        const current = { ...(agent.task_progress || {}) };
        delete current.active_task_evidence_tokens; // legacy sub-cap; no longer persisted
        const policy = {
          ...current,
          enabled: !!enabled,
          completed_task_evidence_tokens: asNumber(raw.completed_task_evidence_tokens, current.completed_task_evidence_tokens ?? 20000),
          allow_self_completion: asBool(raw.allow_self_completion),
        };
        const ok = await _putAgentField(agent, { task_progress: policy }, null, { silent: true });
        if (!ok) throw new Error('Task Progress settings could not be saved');
      } else if (abilityId === 'memory') {
        const savedConfig = (_memoryConnection && _memoryConnection.config) || {};
        const settings = (config && config.ability_settings) || savedConfig.ability_settings || {};
        const isOn = value => value === true || ['1', 'true', 'yes', 'on', 'enabled'].includes(String(value || '').trim().toLowerCase());
        const recallPref = settings.memory_recall == null ? true : isOn(settings.memory_recall);
        const savePref = settings.memory_save == null ? true : isOn(settings.memory_save);
        const recall = !!enabled && recallPref;
        const save = !!enabled && savePref;
        const previous = _memoryStateFromAgent(agent);
        const ok = await _putAgentField(agent, _memoryUpdatesFor(agent, recall, save), null, { silent: true });
        if (!ok) throw new Error('Memory settings could not be saved');
        const persistedConfig = {
          ...savedConfig,
          ...(config || {}),
          ability_settings: {
            memory_recall: recallPref,
            memory_save: savePref,
          },
        };
        try {
          const data = await _saveConnection(agent, _memoryConnection, null, enabled, {
            connection_type: abilityId,
            section: 'ability',
            enabled,
            ...persistedConfig,
          });
          if (_memoryConnection) {
            _memoryConnection.enabled = !!enabled;
            _memoryConnection.config = (data.connection && data.connection.config) || persistedConfig;
          }
        } catch (error) {
          await _putAgentField(agent, _memoryUpdatesFor(agent, previous.recall, previous.save), null, { silent: true });
          throw error;
        }
      } else {
        await _saveConnection(agent, null, null, enabled, { connection_type: abilityId, section: 'ability', enabled, ...(config || {}) });
      }
      if (_lastEnabled[abilityId] !== !!enabled) {
        _lastEnabled[abilityId] = !!enabled;
        _onAbilityToggle(agent, null, null, abilityId, enabled, 'ability');
      }
    },
  });

  // Prompts group — a synthetic collapsible row rendered ABOVE the ability groups
  // (Administrator, Core, …), listing the agent's prompt slots. Replaces the
  // standalone Prompts sub-page by surfacing the same /my-prompts slots as a row
  // inside the Abilities tab. It mirrors the ability-group row structure (.ac-row
  // .ac-group / .ac-group-head / .ac-group-body) so it reads as part of the table.
  const promptsGroup = await _buildPromptsGroup(agent);
  const grid = abilityContainer.querySelector('.conn-grid.ac-list');
  if (grid && promptsGroup) grid.insertBefore(promptsGroup, grid.firstChild);

  // Custom abilities group — a synthetic collapsible row appended at the BOTTOM
  // of the ability table (after the ability groups), listing the agent's custom
  // soft-abilities. Mirrors the ability-group row structure (.ac-row .ac-group /
  // .ac-group-head / .ac-group-body, styled by app3.css) so it reads as part of
  // the same table, with a "+ New ability" affordance in the group head.
  const customGroup = await _buildCustomAbilitiesGroup(agent);
  if (grid && customGroup) grid.appendChild(customGroup);
}

async function _renderProfileAbilitiesMode(body, agent, profile) {
  const wrap = document.createElement('section'); wrap.className = 'member-policy-editor member-policy-table-editor';
  wrap.innerHTML = `<div class="member-policy-editor-title">${_esc(profile.name || profile.slug)} abilities &amp; tools</div>
    <p class="member-policy-editor-hint">Use the same table to independently allow or remove abilities and tools for this level. Changes save to this level only; connection setup, credentials, visibility, configuration, and Auto/Ask/Deny remain base-agent settings.</p>`;
  body.appendChild(wrap);

  const status = document.createElement('div'); status.className = 'member-policy-save-status';
  status.textContent = 'Changes save to this level automatically.';
  wrap.appendChild(status);
  const table = document.createElement('div'); table.className = 'conn-section member-policy-table';
  wrap.appendChild(table);

  const policy = copyPolicy(profile);
  const base = `/api/v1/agents/${encodeURIComponent(agent.id)}`;
  let connectionsPromise = null;
  let toolsPromise = null;
  const loadConnections = () => {
    if (!connectionsPromise) connectionsPromise = fetch(`${base}/connections?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() })
      .then(async response => {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        return {
          connections: (data.connections || []).filter(connection => connection.section === 'ability' && connection.enabled && connection.entitlement_allowed !== false),
          canEdit: (data.user_role || 'member') === 'admin',
        };
      });
    return connectionsPromise;
  };
  const loadTools = () => {
    if (!toolsPromise) toolsPromise = fetch(`${base}/tools?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : { tools: [] }))
      .then(data => data.tools || [])
      .catch(() => []);
    return toolsPromise;
  };
  const updateList = (key, id, allowed, available) => {
    const selected = (available || []).filter(name => policyAllows(policy[key], name));
    const next = allowed ? [...new Set([...selected, id])] : selected.filter(name => name !== id);
    policy[key] = narrowedList(next, available);
  };
  const persist = async () => {
    status.textContent = 'Saving level access…';
    const saved = await saveMemberLevel(agent, profile, policy);
    profile.policy = saved.policy || policy;
    status.textContent = 'Level access saved.';
  };
  const setAccess = async (key, id, allowed, available) => {
    const previous = structuredClone(policy[key]);
    updateList(key, id, allowed, available);
    try { await persist(); }
    catch (error) {
      policy[key] = previous;
      status.textContent = `Level access was not saved: ${error.message}`;
      throw error;
    }
  };
  const levelPolicy = {
    allowsAbility: (abilityId) => policyAllows(policy.abilities, abilityId),
    allowsTool: (toolName) => policyAllows(policy.tools, toolName),
    setAbilityAllowed: async (abilityId, allowed) => {
      const { connections } = await loadConnections();
      await setAccess('abilities', abilityId, allowed, connections.map(connection => connection.connection_type));
    },
    setToolAllowed: async (toolName, allowed) => {
      const tools = await loadTools();
      const names = tools.map(tool => typeof tool === 'string' ? tool : (tool.name || tool.id)).filter(Boolean);
      await setAccess('tools', toolName, allowed, names);
    },
  };

  await buildAgentAbilityTable(table, {
    agent,
    connectionsLoader: loadConnections,
    abilitiesByProvider: {},
    userId: app.currentUserId,
    agentToolsLoader: loadTools,
    levelPolicy,
    showSearch: false,
    onSearchController: (runFilter) => {
      body._agentsSearchController = runFilter;
      body.dataset.agentsSearchController = 'abilities';
    },
    onSave: (abilityId, enabled) => levelPolicy.setAbilityAllowed(abilityId, enabled),
  });
}

// ── Custom abilities group (replaces the disjoined "Custom abilities" section) ──
// A synthetic collapsible row injected at the BOTTOM of the ability table, after
// the ability groups (Administrator, Core, …). Mirrors the ability-group row
// structure (.ac-row .ac-group / .ac-group-head / .ac-group-body, styled by
// app3.css) so it reads as part of the same table, but its body lists the agent's
// CUSTOM abilities (from /soft-abilities). The per-item editing UI is built by
// _softAbilityRow; the "+ New ability" affordance lives in the group head.

async function _buildCustomAbilitiesGroup(agent) {
  const groupEl = document.createElement('div');
  groupEl.className = 'ac-row ac-group ac-group-custom';
  groupEl.id = 'ac-group-custom';

  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-group-head';

  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = _chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = 'var(--brand)';
  iconWrap.innerHTML = _iconHtml('sparkles', '18px');
  head.appendChild(iconWrap);

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = '<div class="ac-ability-name">Custom abilities</div><div class="ac-ability-desc">Per-agent skills and workflows built from existing tools</div>';
  head.appendChild(label);

  const countEl = document.createElement('span');
  countEl.className = 'ac-group-custom-count';
  countEl.style.cssText = 'font-size:11px;color:var(--fg-3);';
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'ac-group-status';
  add.dataset.state = 'on';
  add.textContent = '+ New';
  add.style.cursor = 'pointer';
  add.style.textTransform = 'none';
  const headRight = document.createElement('div');
  headRight.style.cssText = 'margin-left:auto;display:flex;align-items:center;gap:8px;padding-right:10px;';
  headRight.append(countEl, add);
  head.appendChild(headRight);

  groupEl.appendChild(head);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'ac-group-body';
  bodyEl.id = 'ac-group-body-custom';
  bodyEl.innerHTML = '<div class="conn-loading" style="padding:12px;color:var(--fg-3);">Loading custom abilities…</div>';
  groupEl.appendChild(bodyEl);

  // The page-level Ask or Search filter owns this synthetic row's visibility.
  // NOTE: `data-agent-filter-self` is read by agent-ability-table.js's
  // `_runAgentFilter` — keep the two in sync (PREVENTIVE-COMMENTS).
  groupEl.dataset.agentFilterSelf = 'true';
  groupEl.dataset.agentFilterItem = ':scope > .ac-group-body > .ac-row.ac-group';

  let built = false;
  let abilities = [];

  const renderRows = () => {
    bodyEl.innerHTML = '';
    if (!abilities.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'padding:14px;color:var(--fg-3);font-size:12px;border:1px dashed var(--border);border-radius:8px;';
      empty.textContent = 'No custom abilities yet. Create one to add a reusable skill to this agent.';
      bodyEl.appendChild(empty);
      countEl.textContent = '0';
      return;
    }
    countEl.textContent = String(abilities.length);
    abilities.forEach(item => bodyEl.appendChild(_softAbilityRow(item, agent, async (saved, removed) => {
      if (removed) abilities = abilities.filter(a => a.id !== item.id);
      else {
        const idx = abilities.findIndex(a => a.id === saved.id);
        if (idx >= 0) abilities[idx] = saved; else abilities.push(saved);
      }
      renderRows();
      groupEl.dispatchEvent(new CustomEvent('agent-capability-change', { bubbles: true }));
    })));
  };

  const ensureBody = async () => {
    if (built) return;
    built = true;
    try {
      const res = await fetch(`/api/v1/agents/${agent.id}/soft-abilities?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const fetched = (await res.json()).abilities || [];
      // Preserve any just-added unsaved items (id null) the user created while the
      // list was still loading, so the in-flight fetch never clobbers them.
      abilities = [...abilities.filter(a => a.id == null), ...fetched];
    } catch (_) {
      bodyEl.innerHTML = '<div style="padding:12px;color:var(--danger);font-size:12px;">Custom abilities could not be loaded.</div>';
      return;
    }
    renderRows();
  };

  head.addEventListener('click', (e) => {
    if (e.target.closest('.ac-tri, button')) return;
    const willExpand = !groupEl.classList.contains('expanded');
    groupEl.classList.toggle('expanded');
    if (willExpand) ensureBody();
  });

  add.addEventListener('click', () => {
    if (!groupEl.classList.contains('expanded')) groupEl.classList.add('expanded');
    ensureBody();
    abilities.unshift({ id: null, slug: '', display_name: '', description: '', icon: 'sparkles', enabled: true, skill_summary: '', skill_body: '', allowed_tools: [], workflow: {}, credential_schema: [], policy: {}, status: 'ready' });
    renderRows();
    const first = bodyEl.firstElementChild;
    if (first) first.classList.add('expanded');
  });

  // Hooks for the page-level composite filter (_runAgentsAbilitiesFilter): build
  // the body on demand, and drive the expanded state without a head click.
  groupEl._ensureBuilt = ensureBody;
  groupEl._setExpanded = (on) => {
    groupEl.classList.toggle('expanded', !!on);
    if (on) ensureBody();
  };

  return groupEl;
}

function _softAbilityRow(item, agent, onChanged) {
  const row = document.createElement('div');
  row.className = 'ac-row ac-group';
  const head = document.createElement('div');
  head.className = 'ac-group-head';
  head.style.cursor = 'pointer';
  const title = document.createElement('div');
  title.innerHTML = `<strong>${_esc(item.display_name || 'New custom ability')}</strong><div style="font-size:11px;color:var(--fg-3);">${_esc(item.description || item.skill_summary || 'Configure this ability')}</div>`;
  const badge = document.createElement('span');
  badge.style.cssText = 'font-size:10px;color:var(--fg-3);margin-left:auto;margin-right:10px;';
  badge.textContent = item.id ? `Custom · v${item.version || 1}` : 'Unsaved';
  head.append(title, badge);
  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ac-group-body';
  body.style.padding = '12px';
  const field = (label, value, multiline = false) => {
    const wrap = document.createElement('label');
    wrap.style.cssText = 'display:block;font-size:11px;color:var(--fg-3);margin-bottom:10px;';
    wrap.append(document.createTextNode(label));
    const input = document.createElement(multiline ? 'textarea' : 'input');
    input.value = value || '';
    input.style.cssText = 'display:block;width:100%;box-sizing:border-box;margin-top:4px;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--fg);font:inherit;';
    if (multiline) input.rows = 6;
    wrap.appendChild(input); body.appendChild(wrap); return input;
  };
  const name = field('Name', item.display_name);
  const slug = field('Slug (lowercase letters, numbers, underscores)', item.slug);
  const desc = field('Description', item.description);
  const summary = field('When to use it', item.skill_summary);
  const skill = field('Skill instructions', item.skill_body, true);
  const tools = field('Allowed existing tools (comma separated)', (item.allowed_tools || []).join(', '));
  const workflow = field('Workflow JSON (optional)', JSON.stringify(item.workflow || {}, null, 2), true);
  workflow.rows = 4;
  const credentials = field('Credential schema JSON (metadata only; secrets remain in the vault)', JSON.stringify(item.credential_schema || [], null, 2), true);
  credentials.rows = 3;
  const enabledWrap = document.createElement('label');
  enabledWrap.style.cssText = 'display:flex;gap:7px;align-items:center;font-size:12px;margin-bottom:12px;';
  const enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = item.enabled !== false;
  enabledWrap.append(enabled, document.createTextNode('Enabled and available to the agent'));
  body.appendChild(enabledWrap);
  const actions = document.createElement('div'); actions.style.cssText = 'display:flex;gap:8px;';
  const save = document.createElement('button'); save.type = 'button'; save.className = 'btn btn-sm'; save.textContent = 'Save';
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn btn-sm'; remove.textContent = item.id ? 'Delete' : 'Cancel';
  actions.append(save, remove); body.appendChild(actions); row.appendChild(body);
  head.addEventListener('click', () => row.classList.toggle('expanded'));

  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const parsedWorkflow = JSON.parse(workflow.value || '{}');
      const parsedCredentials = JSON.parse(credentials.value || '[]');
      if (!parsedCredentials || !Array.isArray(parsedCredentials)) throw new Error('Credential schema must be a JSON array.');
      const payload = { user_id: app.currentUserId, slug: slug.value.trim(), display_name: name.value.trim(), description: desc.value.trim(), icon: item.icon || 'sparkles', enabled: enabled.checked, skill_summary: summary.value.trim(), skill_body: skill.value.trim(), workflow: parsedWorkflow, allowed_tools: tools.value.split(',').map(v => v.trim()).filter(Boolean), credential_schema: parsedCredentials, policy: item.policy || {}, status: 'ready' };
      const url = `/api/v1/agents/${agent.id}/soft-abilities${item.id ? '/' + encodeURIComponent(item.id) : ''}`;
      const res = await fetch(url, { method: item.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify(payload) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail && data.detail.errors ? data.detail.errors.join(' ') : (data.detail || `HTTP ${res.status}`);
        throw new Error(detail);
      }
      _showAbilitiesNotice(agent, `${payload.display_name} saved`);
      await onChanged(data.ability, false);
    } catch (err) { _showAbilitiesNotice(agent, err.message || 'Save failed'); }
    finally { save.disabled = false; }
  });
  remove.addEventListener('click', async () => {
    if (!item.id) return onChanged(null, true);
    if (!window.confirm(`Delete custom ability “${item.display_name}”?`)) return;
    const res = await fetch(`/api/v1/agents/${agent.id}/soft-abilities/${encodeURIComponent(item.id)}?user_id=${encodeURIComponent(app.currentUserId)}`, { method: 'DELETE', headers: authHeaders() });
    if (res.ok) onChanged(null, true); else _showAbilitiesNotice(agent, 'Delete failed');
  });
  return row;
}

function _showAbilitiesNotice(agent, text) {
  const slot = document.querySelector('.conn-notice-slot');
  if (!slot) return;
  slot.textContent = text;
  slot.style.display = 'block';
  setTimeout(() => { slot.style.display = 'none'; }, 4000);
}

async function _onAbilityToggle(agent, conn, cardEl, abilityId, enabled, source) {
  _showAbilitiesNotice(agent, `${abilityId} turned ${enabled ? 'ON' : 'OFF'}`);
}

async function _saveConnection(agent, conn, cardEl, enabled, _extConfig) {
  // Persist a connection / ability toggle to the backend. Two callers share this:
  //   • the OAuth connection cards  → conn + cardEl are real, _extConfig = { enabled }
  //   • the agent ability table     → conn = null, the ability id + its settings
  //                                    arrive via _extConfig.connection_type / config
  // Without this PUT the toggles only flipped in the DOM and reverted on refresh.
  // Throws on a non-OK response so the caller can revert the switch.
  const ext = _extConfig || {};
  const connectionType = (conn && conn.connection_type) || ext.connection_type;
  if (!connectionType) throw new Error('No connection type to save');

  // Build the config payload from _extConfig minus the control keys.
  const config = {};
  for (const [k, v] of Object.entries(ext)) {
    if (k === 'enabled' || k === 'connection_type' || k === 'section') continue;
    config[k] = v;
  }

  const res = await fetch(
    `/api/v1/agents/${agent.id}/connections/${connectionType}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: app.currentUserId, enabled, config }),
    }
  );
  let data = {};
  try { data = await res.json(); } catch (e) { /* tolerate a non-JSON error body */ }
  if (!res.ok) throw new Error(data.detail || `Save failed (HTTP ${res.status})`);

  // Reflect the saved state back into the in-memory connection + card, so a tab
  // re-render without a full reload stays consistent with what was persisted.
  if (conn) {
    conn.enabled = enabled;
    if (data.connection && data.connection.config) conn.config = data.connection.config;
  }
  if (cardEl) cardEl.classList.toggle('enabled', enabled);
  return data;
}

// ── Prompts group (replaces the standalone Prompts sub-page) ───────────────────
// A synthetic collapsible row injected ABOVE the ability groups in the agent
// Abilities tab. It mirrors the ability-group row structure (.ac-row .ac-group /
// .ac-group-head / .ac-group-body, styled by app3.css) so it reads as part of the
// same table, but its body lists the agent's PROMPT SLOTS (from /my-prompts)
// instead of ability toggles. Data source + slot naming mirror tab-prompts.js.

const _PROMPT_ICON_MAP = {
  system: 'terminal', agent: 'bot', user: 'user', skills: 'book', tasks: 'list-todo',
  misc: 'file-text', automation: 'clock', bootstrap_tools: 'wrench',
};

async function _buildPromptsGroup(agent) {
  const groupEl = document.createElement('div');
  groupEl.className = 'ac-row ac-group ac-group-prompts';
  groupEl.id = 'ac-group-prompts';

  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-group-head';

  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = _chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = 'var(--brand)';
  iconWrap.innerHTML = _iconHtml('file-text', '18px');
  head.appendChild(iconWrap);

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = '<div class="ac-ability-name">Prompts</div><div class="ac-ability-desc">Prompt slots configured for this agent</div>';
  head.appendChild(label);

  const countEl = document.createElement('span');
  countEl.className = 'ac-group-prompts-count';
  countEl.style.cssText = 'margin-left:auto;font-size:11px;color:var(--fg-3);padding-right:10px;';
  head.appendChild(countEl);

  groupEl.appendChild(head);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'ac-group-body';
  bodyEl.id = 'ac-group-body-prompts';
  bodyEl.innerHTML = '<div class="conn-loading" style="padding:12px;color:var(--fg-3);">Loading prompts…</div>';
  groupEl.appendChild(bodyEl);

  // The page-level Ask or Search filter owns this synthetic row's visibility.
  // NOTE: `data-agent-filter-self` is read by agent-ability-table.js's
  // `_runAgentFilter` — keep the two in sync (PREVENTIVE-COMMENTS).
  groupEl.dataset.agentFilterSelf = 'true';
  groupEl.dataset.agentFilterItem = ':scope > .ac-group-body > .prompt-item-wrap';

  let built = false;
  const ensureBody = async () => {
    if (built) return;
    built = true;
    try {
      const res = await fetch(`/api/v1/agents/${agent.id}/my-prompts?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const slots = data.slots || [];
      const overrides = data.overrides || {};
      bodyEl.innerHTML = '';
      if (!slots.length) {
        const empty = document.createElement('div');
        empty.style.cssText = 'padding:14px;color:var(--fg-3);font-size:12px;';
        empty.textContent = 'No prompts configured for this agent.';
        bodyEl.appendChild(empty);
      } else {
        slots.forEach(slot => bodyEl.appendChild(_buildPromptItem(slot, overrides)));
        countEl.textContent = `${slots.length} prompt${slots.length === 1 ? '' : 's'}`;
      }
    } catch (_) {
      bodyEl.innerHTML = '<div style="padding:12px;color:var(--danger);font-size:12px;">Prompts could not be loaded.</div>';
    }
  };

  head.addEventListener('click', (e) => {
    if (e.target.closest('.ac-tri, button')) return;
    const willExpand = !groupEl.classList.contains('expanded');
    groupEl.classList.toggle('expanded');
    if (willExpand) ensureBody();
  });

  // Hooks for the page-level composite filter (_runAgentsAbilitiesFilter): build
  // the body on demand, and drive the expanded state without a head click.
  groupEl._ensureBuilt = ensureBody;
  groupEl._setExpanded = (on) => {
    groupEl.classList.toggle('expanded', !!on);
    if (on) ensureBody();
  };

  return groupEl;
}

function _buildPromptItem(slot, overrides) {
  const slotId = slot.slot_name || slot.id || 'prompt';
  const isOverridden = overrides && slotId in overrides;
  const content = slot.override_content != null ? slot.override_content : (slot.content || '');

  // A collapsible row: the head keeps the icon + name + truncated preview +
  // lock/override badges; clicking it toggles a text-viewer panel that shows the
  // FULL prompt content (untruncated, pre-wrapped, scrollable) below.
  const item = document.createElement('div');
  item.className = 'prompt-item-wrap';
  item.dataset.promptSlot = slotId;

  const head = document.createElement('div');
  head.className = 'ac-ability-row prompt-item';
  head.style.cursor = 'pointer';

  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = _chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = 'var(--fg-2)';
  iconWrap.innerHTML = _iconHtml(_PROMPT_ICON_MAP[slotId] || 'file-text', '16px');
  head.appendChild(iconWrap);

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  const name = document.createElement('div');
  name.className = 'ac-ability-name';
  name.textContent = slotId.replace(/_/g, ' ');
  label.appendChild(name);
  const preview = document.createElement('div');
  preview.className = 'ac-ability-desc';
  preview.textContent = (content || '').trim().replace(/\s+/g, ' ').slice(0, 160) || '—';
  label.appendChild(preview);
  head.appendChild(label);

  const badges = document.createElement('div');
  badges.style.cssText = 'display:flex;gap:6px;align-items:center;';
  if (slot.locked) {
    const b = document.createElement('span');
    b.style.cssText = 'font-size:10px;color:var(--accent);border:1px solid var(--accent-soft);border-radius:3px;padding:1px 6px;';
    b.textContent = 'Admin lock';
    badges.appendChild(b);
  }
  if (isOverridden) {
    const b = document.createElement('span');
    b.style.cssText = 'font-size:10px;color:var(--warning);border:1px solid var(--warning);border-radius:3px;padding:1px 6px;';
    b.textContent = 'Override';
    badges.appendChild(b);
  }
  if (badges.childNodes.length) head.appendChild(badges);

  item.appendChild(head);

  const body = document.createElement('div');
  body.className = 'prompt-viewer';
  body.style.cssText = 'padding:2px 4px 8px;';
  body.style.display = 'none';
  const viewer = document.createElement('pre');
  viewer.className = 'prompt-text-viewer';
  viewer.style.cssText = 'margin:0;padding:12px 14px;white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono,monospace);font-size:12px;line-height:1.6;color:var(--fg-2);background:var(--bg-1);border:1px solid var(--border);border-radius:8px;max-height:380px;overflow:auto;';
  viewer.textContent = content || '(empty prompt)';
  body.appendChild(viewer);
  item.appendChild(body);

  head.addEventListener('click', () => {
    const expanded = item.classList.toggle('expanded');
    body.style.display = expanded ? '' : 'none';
    chevron.style.transform = expanded ? 'rotate(90deg)' : '';
  });

  return item;
}
