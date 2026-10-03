// Ported from ui/main-panel/agents/js/tab-abilities.js: v2 chart markup and radial interactions.
export function buildAbilitiesOverview() {
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
        <h3>CAPABILITY LEVEL <span data-stat="tier-count"></span></h3>
        <div class="agent-abilities-tier-control" data-tier-selector></div>
      </section>
    </div>
    <div class="agent-abilities-sync" aria-label="Template synchronization"></div>`;

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

  // The chart markup and radial interactions above are copied from v2's
  // _buildAbilitiesOverview. This adapter supplies v3's agent data.
  overview.update = (composition, abilities, slots) => {
    const active = abilities.filter(ability => ability.visibility !== 'off');
    const skills = active.filter(ability => ability.has_skill);
    const tools = active.flatMap(ability => ability.tools || [])
      .filter(tool => tool.supported && tool.mode !== 'off');
    const count = (items, mode) => items.filter(item =>
      (mode === 'visible' ? (item.skill_visibility || item.mode || item.visibility) === 'visible'
        : (item.skill_visibility || item.mode || item.visibility) === 'discoverable')).length;
    const counts = {
      system_prompt: String(slots.length),
      skills: `${skills.length}-${count(skills, 'visible')}-${count(skills, 'discoverable')}`,
      tools: `${tools.length}-${count(tools, 'visible')}-${count(tools, 'discoverable')}`,
    };
    setComposition(composition.breakdown, composition.tokens, composition.sources, counts);
    const heading = compositionTotal.querySelector('b');
    if (heading) heading.textContent = 'Estimated';
  };
  return overview;
}
