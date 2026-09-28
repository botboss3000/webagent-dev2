"""Regression contracts for live-saved agent ability context estimates."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAB = (ROOT / "ui/main-panel/agents/js/tab-abilities.js").read_text()
TABLE = (ROOT / "ui/shared/js/agent-ability-table.js").read_text()
AGENTS_API = (ROOT / "app/api/agents.py").read_text()
AGENTS_CSS = (ROOT / "ui/main-panel/agents/agents.css").read_text()


def test_abilities_summary_identifies_the_persisted_context_value():
    # The saved context value is now read off the composition card: the chart's
    # centre total is the single readout, fed by the live context-tokens endpoint.
    assert "data-context-composition-total" in TAB
    assert "/context-tokens?${contextParams}" in TAB
    assert "new URLSearchParams({ user_id: app.currentUserId })" in TAB
    assert "/restart" not in TAB


def test_every_successful_ability_configuration_save_requests_a_summary_refresh():
    event = "new CustomEvent('agent-capability-change', { bubbles: true })"

    # Access, per-ability settings, and debounced tool permissions all persist
    # independently. Each must notify the overview only after the write succeeds.
    # The access path writes the new exposure onto the card first and notifies after;
    # it no longer assigns through the old `conn.available_to` field, so the contract
    # is anchored on the state write that now precedes the event.
    assert "card.dataset.exposure = next;" in TABLE
    assert "card.dispatchEvent(" + event in TABLE
    assert "prevVal = f.value;\n            el.dispatchEvent(" + event in TABLE
    assert "if (ok) el.dispatchEvent(" + event in TABLE


def test_saved_context_refresh_is_debounced_and_stale_responses_are_ignored():
    assert "let refreshVersion = 0;" in TAB
    assert "refreshVersion += 1;" in TAB
    assert "if (version !== refreshVersion) return;" in TAB
    assert "overview.markContextRefreshing?.();" in TAB
    assert "overviewRefreshTimer = setTimeout" in TAB


def test_context_composition_uses_direct_breakdown_radial_bars():
    assert '"breakdown": breakdown' in AGENTS_API
    assert '"system_prompt"' in AGENTS_API
    assert '"skills"' in AGENTS_API
    assert '"tools"' in AGENTS_API
    assert "data-context-composition-bars" in TAB
    assert "agent-context-composition-bar" in TAB
    assert "hasTotal ? tokens.breakdown || {} : null" in TAB


def test_context_composition_ball_is_neutral_with_no_connector_line():
    # The dashed connector that joined the source-bar tips — and the charge trail
    # painted along it — was removed from the chart. Only the glowing ball is left,
    # and it is now painted with the neutral TEXT token (white in the dark theme)
    # instead of the active segment's category colour. The rail it travels is still
    # computed, but it is an invisible placement path rather than a drawn line.
    assert "RADIAL-TOP-TRAJECTORY" in TAB
    assert "agent-context-composition-trend" not in TAB
    assert "agent-context-composition-charge" not in TAB
    assert "stroke-dashoffset" not in TAB
    assert "trendPointAt" in TAB
    # RADIAL-RAIL-DYNAMIC: the rail is rebuilt per focus rather than baked once at render
    # time, so it can lift with a popped category. Nothing caches it on the instance any
    # more — a baked rail is exactly what left the ball behind on the resting ring.
    assert "compositionTrend" not in TAB
    assert "const compositionRail = (poppedKey, hotKey) => {" in TAB
    assert "const trend = compositionRail(poppedKey, hotKey);" in TAB
    assert "agent-context-composition-trend" not in AGENTS_CSS
    assert "agent-context-composition-charge" not in AGENTS_CSS
    assert "background: radial-gradient(circle at 50% 50%, var(--fg-1) 0 52%, transparent 96%);" in AGENTS_CSS
    assert "box-shadow: 0 0 4px var(--fg-1), 0 0 10px 1px var(--fg-1);" in AGENTS_CSS
    # The variable survives as the active-source key the arrow-key handler matches
    # on, so removing it would silently break keyboard navigation around the ring.
    assert "compositionNode.style.setProperty('--context-node-color', source.color);" in TAB


def test_context_composition_marker_can_only_land_on_a_bar():
    # CONTEXT-BAR-MARKER: the glowing ball reads its radius from the knot of the bar the
    # pointer is ON, never from the space between two bars. Interpolating across the drawn
    # gap produced a radius that belonged to neither neighbour, so the ball came to rest
    # floating in the dead space between them. Measured live before the change: 718 of 720
    # bearings around the ring placed the ball off the highlighted bar.
    assert "CONTEXT-BAR-MARKER" in TAB
    assert "const knot = source && trend.points.find((item) => item.key === source.key);" in TAB
    assert "const radius = Math.hypot(knot.x - 100, knot.y - 100);" in TAB
    # The rail carries each knot's source key, which is what lets the marker find its bar.
    assert "key: source.key," in TAB
    # The chord interpolation is gone, along with the arc-length bookkeeping that existed
    # only to feed it — the ball no longer rides a chord at all.
    assert "startRadius + (endRadius - startRadius) * progress" not in TAB
    assert "const cycle = [...knots, { ...knots[0], fraction: knots[0].fraction + 1 }];" not in TAB
    assert "return { points: knots, segments, total: length };" not in TAB
    # CONTEXT-BAR-EDGE-HIT: the signal regions tile each group edge to edge, so a bearing
    # always resolves to a real bar and never falls through to the group's end bar — which
    # is what used to "grow the wrong bar" whenever the pointer sat between two of them.
    assert "source.hitStart = entry.start + slot * index;" in TAB
    assert "source.hitEnd = entry.start + slot * (index + 1);" in TAB
    assert "wrapped >= item.hitStart && wrapped < item.hitEnd" in TAB
    # The selection is resolved by region, not by a nearest-bar heuristic that could drift.
    assert "const source = compositionSourceAtFraction(fraction);" in TAB


def test_context_composition_legend_is_compact_and_carries_the_inventory_counts():
    # The breakdown legend absorbed the two panels that used to sit under the chart
    # (Base Context token/pricing and Agent Abilities), so those blocks and their
    # long group names are gone and the counts now ride the legend rows.
    assert "BASE CONTEXT COMPOSITION" not in TAB
    assert "Input estimate" not in TAB
    assert "AGENT ABILITIES" not in TAB
    assert "data-context-composition-legend" in TAB
    assert "label: 'Prompt'" in TAB
    assert "label: 'Skills'" in TAB
    assert "label: 'Tools'" in TAB
    assert "label: 'Gates'" not in TAB
    assert "counts?.[entry.key]" in TAB
    # Prompts contribute a single total; skills/tools contribute the
    # total-visible-discoverable split shown as e.g. "Tools (83-36-47)".
    assert "system_prompt: String(promptCount)" in TAB
    assert "tools: `${tools.length}-${visibleTools}-${discoverableTools}`" in TAB
    # One compact line per group: colour key, name, share — no token/source row.
    assert "grid-template-areas: 'key label percent';" in AGENTS_CSS
    assert ".agent-abilities-pricing" not in AGENTS_CSS


def test_context_composition_radial_locator_mirrors_the_model_cross():
    # The model chart's vertical cross (.ac-model-analytics-cross — var(--accent) at
    # 1px, opacity .8) is turned radially: one hairline holding the pointer's angle,
    # spanning from the bottom of the bars out to the edge of the graph container.
    assert "RADIAL-LOCATOR" in TAB
    assert "agent-context-composition-cross" in TAB
    assert "RADIAL-LOCATOR" in AGENTS_CSS
    # Same colour and width as the model locator, held at exactly one screen pixel
    # whatever the SVG viewBox is scaled to.
    assert ".agent-context-composition-cross {\n  stroke: var(--accent);\n  stroke-width: 1px;" in AGENTS_CSS
    assert "vector-effect: non-scaling-stroke;" in AGENTS_CSS
    # The needle starts where the bars start, not at the disc centre, and the bars read
    # the same constant so the two can never drift apart.
    assert "const COMPOSITION_BAR_INNER = 68;" in TAB
    assert "compositionArcPath(source.start, source.end, COMPOSITION_BAR_INNER, source.outer)" in TAB
    # RADIAL-LOCATOR-START: the inner end is released from the bar ring. Outside the band
    # it stays pinned to the bottom of the bars; inside the centre circle it follows the
    # pointer inward, so the cursor is always a point ON the line.
    assert "const compositionLocatorStart = (pointerRadius) => {" in TAB
    assert "return Math.max(0, Math.min(COMPOSITION_BAR_INNER, pointerRadius));" in TAB
    assert "const start = compositionLocatorStart(pointerRadius);" in TAB
    assert "crossLine.setAttribute('x1', (100 + dx * start).toFixed(2));" in TAB
    assert "crossLine.setAttribute('y1', (100 + dy * start).toFixed(2));" in TAB
    # The pointer's own distance from the disc centre, in the SVG's 0 0 200 200 units,
    # is what drives that inner end.
    assert "const pointerRadius = rect.width > 0 ? (Math.hypot(x, y) * 200) / rect.width : NaN;" in TAB
    # RADIAL-LOCATOR-HIT: contact detection uses the same container box the needle's
    # outer end is measured against, so the locator appears for the pointer anywhere in
    # the graph container rather than only over the 164px disc.
    assert "const compositionHitRegion = compositionChart?.parentElement || compositionChart;" in TAB
    assert "compositionHitRegion?.addEventListener('pointermove'" in TAB
    assert "compositionChart?.addEventListener('pointermove'" not in TAB
    assert "RADIAL-LOCATOR-HIT" in AGENTS_CSS
    assert ".agent-context-composition-primary:has(.agent-context-composition-chart:not(.is-loading):not(.is-empty)) { cursor: crosshair; }" in AGENTS_CSS
    # The outward end reaches the edge of the graph container (the chart's own parent
    # panel), so the needle reads across the whole plot region instead of stopping at
    # the ring; the container box is what bounds it.
    assert "const compositionLocatorReach = (dx, dy) => {" in TAB
    assert "const container = compositionChart?.parentElement;" in TAB
    assert "crossLine.setAttribute('x2', (100 + dx * reach).toFixed(2));" in TAB
    assert "crossLine.setAttribute('y2', (100 + dy * reach).toFixed(2));" in TAB
    assert "RADIAL-LOCATOR-REACH" in TAB
    # CONTEXT-BAR-MARKER: the ball is placed ON the ray — the pointer's angle, with the
    # radius taken from the knot of the bar the pointer is on. Interpolating between the
    # two tips that bracket the bearing gave the ball a radius belonging to neither bar,
    # which is what let it float in the dead space between two bars; an intersection solve
    # has the same failure whenever a displaced chord meets the ray outside its own span.
    assert "const knot = source && trend.points.find((item) => item.key === source.key);" in TAB
    assert "const radius = Math.hypot(knot.x - 100, knot.y - 100);" in TAB
    assert "startRadius + (endRadius - startRadius) * progress" not in TAB
    assert "return glide" not in TAB
    assert "const glide = {" not in TAB
    # The ball must never be placed off the pointer's bearing: both coordinates come from
    # the same sin/cos pair, and nothing else may shift them.
    assert "x: 100 + Math.sin(angle) * radius," in TAB
    assert "y: 100 - Math.cos(angle) * radius," in TAB
    # A zero-token group must not emit a phantom source bar: its outer radius divides by
    # a zero "largest" bar and went NaN, which poisoned a bar AND the closing trend knot,
    # blanking the locator at the top of the ring.
    assert "return entry.tokens > 0 ? [{ label: entry.label, tokens: entry.tokens, misc: false }] : [];" in TAB
    # Non-finite trend geometry degrades to the coarse ring locator instead of NaN.
    assert "if (!knot || ![knot.x, knot.y].every(Number.isFinite)) return null;" in TAB
    # The old radial dead zone that hid the locator whenever the pointer left the
    # trend band is gone, so it tracks the whole disc the way the model cross tracks
    # the whole plot — ring hole and bar gaps included.
    assert "radius < 0.64" not in TAB
    assert "allowCentre" not in TAB


def test_context_composition_legend_leads_the_panel_and_the_chart_is_enlarged():
    # DOM-FIRST-LEGEND: the legend panel now leads the composition grid, so the grid's
    # narrow track is its left column and the chart takes the wide right one. The panel's
    # rule flips to its right edge, against the chart it now precedes.
    assert TAB.index('class="agent-context-composition-details"') < TAB.index('class="agent-context-composition-primary"')
    assert "grid-template-columns: minmax(180px, 0.35fr) minmax(0, 1fr);" in AGENTS_CSS
    assert "DOM-FIRST-LEGEND" in AGENTS_CSS
    assert "border-right: var(--border-width) solid var(--border);" in AGENTS_CSS
    assert "border-left: var(--border-width) solid var(--border);" not in AGENTS_CSS
    # Stacked on a narrow screen the chart keeps the top slot, even though the legend
    # panel leads the DOM.
    assert ".agent-context-composition-primary {\n    order: -1;" in AGENTS_CSS
    # The heading is renamed; the old label and its marker comment are gone.
    assert "<h4>AGENT CONTEXT</h4>" in TAB
    assert 'aria-label="Agent context breakdown"' in TAB
    assert "BREAKDOWN" not in TAB
    assert "BREAKDOWN" not in AGENTS_CSS
    # The disc, its centre hole, and the panel that frames it all grow together, so the
    # hole stays just inside the bar band (124/188 ≈ 108/164) instead of swallowing it.
    # CONTEXT-SKELETON-PARAMS: those three numbers are declared ONCE on the detail body
    # (188/124/248) and read back through the vars, so the disc rule carries no page-local
    # literal any more and the Abilities skeleton is built from the very same numbers.
    assert "--context-chart-size: 188px;" in AGENTS_CSS
    assert "--context-chart-plate: 124px;" in AGENTS_CSS
    assert "--context-chart-min: 248px;" in AGENTS_CSS
    assert (
        "  width: var(--context-chart-size, 188px);\n"
        "  height: var(--context-chart-size, 188px);\n"
        "  place-items: center;"
    ) in AGENTS_CSS
    assert (
        "  width: var(--context-chart-plate, 124px);\n"
        "  height: var(--context-chart-plate, 124px);"
    ) in AGENTS_CSS
    assert "min-height: var(--context-chart-min, 248px);" in AGENTS_CSS


def test_context_composition_pops_the_hovered_category_group():
    # CONTEXT-GROUP-POPOUT: every category is emitted as one <g> carrying its own outward
    # pop vector, so hovering a category lifts its whole bar set — track included — off
    # the ring as a single piece. The pointer's category is resolved from the same angular
    # span the bars are drawn with, and the pop is cleared whenever focus is hidden so no
    # group is left stranded off the ring.
    assert 'class="agent-context-composition-group"' in TAB
    assert 'data-context-group="${entry.key}"' in TAB
    assert "--context-group-dx:${groupDx};--context-group-dy:${groupDy}" in TAB
    assert "entry.bisector = (entry.start + entry.end) / 2;" in TAB
    assert "const COMPOSITION_GROUP_POP = 9;" in TAB
    assert "source.categoryKey = entry.key;" in TAB
    # The pop carries no aim: the set lifts straight out along its own bisector, and the
    # per-group vector baked into the markup is the only thing that moves it.
    assert "popCompositionGroup(source.categoryKey, source.key);" in TAB
    assert "popCompositionGroup(null);" in TAB
    # Only one category is ever lifted: the toggle is keyed on the group's own data attr.
    assert "group.dataset.contextGroup === popped" in TAB
    # The pop is a translate along the bisector, not a scale — a scale would widen each
    # bar into its neighbours rather than moving the set as one piece.
    assert ".agent-context-composition-group.is-popped {" in AGENTS_CSS
    assert "transform: translate(calc(var(--context-group-dx, 0) * 1px), calc(var(--context-group-dy, 0) * 1px));" in AGENTS_CSS
    assert "transform: translate(0px, 0px);" in AGENTS_CSS
    assert ".agent-context-composition-track { transition: fill-opacity 220ms ease; }" in AGENTS_CSS
    assert ".agent-context-composition-group.is-popped .agent-context-composition-track { fill-opacity: .3; }" in AGENTS_CSS
    # The track keeps its presentation attribute as the resting opacity; the pop is the
    # only thing that overrides it, and it does so from CSS.
    assert 'fill-opacity=".16"' in TAB
    # DYNAMIC-RAIL: the rail the ball rides follows the group that has popped — and the one
    # bar that has popped further — so the ball stays on the visible tips instead of on the
    # ring they lifted off. The offsets are read from each element's own computed transform
    # rather than mirrored from the intended vector, which is what keeps the ball on the
    # bars through the animation instead of snapping to the settled tips the instant a class
    # flips. An element's computed transform excludes its ancestors', so the group's slide
    # and the bar's lift are summed explicitly.
    assert "const compositionLiveOffset = (element) => {" in TAB
    assert "getComputedStyle(element).transform" in TAB
    assert "const lifted = Boolean(poppedKey) && source.categoryKey === poppedKey;" in TAB
    assert "const proud = Boolean(hotKey) && source.key === hotKey;" in TAB
    assert "(lifted ? group.dx : 0) + (proud ? hot.dx : 0)" in TAB
    assert "(lifted ? group.dy : 0) + (proud ? hot.dy : 0)" in TAB
    assert "const point = trendPointAt(fraction, compositionPoppedKey, compositionHotKey);" in TAB
    # The hot key must survive the whole chain: a signature that does not forward it drops
    # the highlighted bar's lift silently, which is exactly how it went missing once.
    assert "const trendPointAt = (fraction, poppedKey, hotKey) => {" in TAB
    assert "const trend = compositionRail(poppedKey, hotKey);" in TAB
    # CONTEXT-BAR-HOTPOP: the single bar under the pointer lifts further than its set, along
    # its own mid-bearing, and is keyed by a stable per-source key so the rail can single it
    # out. Labels are not unique, so the key is category plus slot index.
    assert "const COMPOSITION_BAR_POP = 8;" in TAB
    assert "source.key = `${entry.key}:${index}`;" in TAB
    assert 'data-context-source="${source.key}"' in TAB
    assert "style=\"--context-bar-dx:${source.popDx}px;--context-bar-dy:${source.popDy}px\"" in TAB
    assert "popCompositionGroup(source.categoryKey, source.key);" in TAB
    # Only ever one bar and one group are lifted, and the toggle is keyed on each own's data
    # attribute; both are cleared together when focus is hidden.
    assert "bar.dataset.contextSource === hot" in TAB
    assert "compositionHotKey = hot;" in TAB
    assert "const changed = compositionPoppedKey !== popped || compositionHotKey !== hot;" in TAB
    assert "if (changed) startCompositionRailSettle();" in TAB
    # The bar's extra lift is a CSS translate on its own class, declared after the thin-bar
    # opacity rule so a highlighted remainder bar is still fully opaque.
    assert ".agent-context-composition-bar.is-hot {" in AGENTS_CSS
    assert "transform: translate(var(--context-bar-dx, 0), var(--context-bar-dy, 0));" in AGENTS_CSS
    assert "CONTEXT-BAR-HOTPOP" in AGENTS_CSS
    # A stationary pointer emits no pointermove, so the pop re-paints the geometry on a
    # bounded rAF loop; it must stop, and it must be cancelled when focus is hidden.
    assert "const startCompositionRailSettle = () => {" in TAB
    assert "if ((moving || compositionFocusState) && performance.now() < until) {" in TAB
    assert "cancelAnimationFrame(compositionRailFrame)" in TAB
    # Painting the geometry is separate from the tooltip, so the loop repaints position
    # only — it must not rebuild the tooltip or re-read the pointer every frame.
    assert "const paintCompositionGeometry = () => {" in TAB
    assert "compositionFocusState = { source, fraction, pointerRadius };" in TAB
    assert "paintCompositionGeometry();" in TAB
    # CONTEXT-SECTION-MAGNIFY: the hovered section's bars grow radially (1.25x) on top of
    # the slide, and the bar under the pointer grows 1.5x again. The growth is radial with
    # the inner edge pinned, which is what keeps every bar's angular width unchanged — a
    # uniform scale about the disc centre would preserve angles but drag the inner edge off
    # the ring, and a scale about the bar's own inner point would widen it by the same
    # factor and overlap the set. So it is baked into the path geometry, not a transform,
    # and rebuilt each frame while it eases.
    assert "const COMPOSITION_GROUP_GROW = 1.25;" in TAB
    assert "const COMPOSITION_BAR_GROW = 1.5;" in TAB
    assert "const compositionSourceGrow = (source) =>" in TAB
    assert "const compositionSourceOuter = (source) =>" in TAB
    assert "COMPOSITION_BAR_INNER + (source.outer - COMPOSITION_BAR_INNER) * compositionSourceGrow(source)" in TAB
    assert "const renderCompositionBarGeometry = () => {" in TAB
    assert "bar.setAttribute('d', compositionArcPath(source.start, source.end, COMPOSITION_BAR_INNER, compositionSourceOuter(source)));" in TAB
    assert "const stepCompositionGrow = () => {" in TAB
    assert "entry.key === compositionPoppedKey ? COMPOSITION_GROUP_GROW : 1" in TAB
    assert "const hotTarget = compositionHotKey ? COMPOSITION_BAR_GROW : 1;" in TAB
    assert "const moving = stepCompositionGrow();" in TAB
    # The easing is asymptotic and the loop is deadline-bounded, so the final frame snaps
    # to the exact factor — otherwise the bars visibly stop short of their intended size.
    assert "const settleCompositionGrow = () => {" in TAB
    assert "compositionGrow.set(entry.key, entry.key === compositionPoppedKey ? COMPOSITION_GROUP_GROW : 1);" in TAB
    assert "} else if (moving) {" in TAB
    assert "settleCompositionGrow();" in TAB
    # The ring helpers are hoisted so the animation can rebuild one bar's path without
    # re-running the whole chart build; the markup must use the hoisted builder.
    assert "const compositionArcPath = (start, end, inner, outer) => {" in TAB
    assert "const compositionPolar = (fraction, radius) => {" in TAB
    assert "d=\"${compositionArcPath(entry.start, entry.end, 66, 97)}\"" in TAB
    assert "d=\"${compositionArcPath(source.start, source.end, COMPOSITION_BAR_INNER, source.outer)}\"" in TAB
    # A fresh render resets every bar to its resting size and re-registers the sources.
    assert "compositionSourceByKey = new Map(compositionSources.map((source) => [source.key, source]));" in TAB
    assert "compositionGrow.clear();" in TAB
    # The rail rides the *grown* tip, so the ball stays on the enlarged bars.
    assert "const tip = compositionSourceOuter(source);" in TAB
    assert "100 + Math.sin(angle) * tip + (lifted ? group.dx : 0) + (proud ? hot.dx : 0)" in TAB
    assert "100 - Math.cos(angle) * tip + (lifted ? group.dy : 0) + (proud ? hot.dy : 0)" in TAB


def test_context_composition_mobile_uses_a_square_touch_region():
    # CONTEXT-TOUCH-SQUARE: on the narrow layout the chart's container shrink-wraps to a
    # square around the disc and becomes the marker's contact region in its own right.
    # width:max-content is the 156px chart plus symmetric padding, aspect-ratio holds it
    # to an exact 1:1 box, and min-height:0 clears the desktop panel's floor so the square
    # hugs the disc instead of stretching to the card.
    # The square carries the surface as well as the touch region, so the painted
    # container the chart reads against is the same box a finger has to land in — its
    # painted edges and its scroll-blocking edges are one and the same.
    assert (
        ".agent-context-composition-primary {\n"
        "    order: -1;\n"
        "    justify-self: center;\n"
        "    width: max-content;\n"
        "    min-height: 0;\n"
        "    aspect-ratio: 1;\n"
        "    /* CONTEXT-SKELETON-PARAMS: the square's inset — the same parameter the Abilities\n"
        "       skeleton square sizes itself from. */\n"
        "    padding: var(--context-chart-pad, 18px);\n"
        "    border: var(--border-width) solid var(--border);\n"
        "    border-radius: 10px;\n"
        "    background: var(--bg-1);\n"
        "    touch-action: none;\n"
        "  }"
    ) in AGENTS_CSS
    assert "CONTEXT-TOUCH-SQUARE" in AGENTS_CSS
    # The card itself sheds its surface on the narrow layout — transparent, borderless,
    # unrounded, and free to overflow — so no rectangle wider than the square is painted
    # around or below the disc; the legend becomes its own matching panel instead.
    assert (
        "  .agent-context-composition {\n"
        "    grid-template-columns: 1fr;\n"
        "    gap: 12px;\n"
        "    background: transparent;\n"
        "    border: 0;\n"
        "    border-radius: 0;\n"
        "    overflow: visible;\n"
        "  }"
    ) in AGENTS_CSS
    # touch-action:none inside the square is what tells the browser a drag there drives
    # the locator rather than panning the page. The base container rule must stay free of
    # it, because that is what lets a touch outside the square scroll normally at every
    # other width; only the disc itself is inert on desktop.
    primary_base = AGENTS_CSS.split(".agent-context-composition-primary {")[1].split("}")[0]
    assert "touch-action" not in primary_base
    # The narrow breakpoint keeps the square exact: symmetric padding and no margin of its
    # own on the chart, which an asymmetric 4px top offset used to break.
    # CONTEXT-SKELETON-PARAMS: the narrow breakpoint only SHRINKS the parameters (156/103/
    # 14) on the shared ancestor (#agents-grid), and the square reads its inset through the
    # var — so the wide 18px literal is gone and both skeleton surfaces inherit the phone
    # numbers with the live container instead of drifting from them.
    assert (
        "  #agents-grid { --context-chart-pad: 14px; }\n"
        "  .agent-context-composition-primary { padding: var(--context-chart-pad, 14px); }\n"
        "  .agent-context-composition-legend { grid-template-columns: 1fr; }"
    ) in AGENTS_CSS
    assert (
        "  #agents-grid {\n"
        "    --context-chart-size: 156px;\n"
        "    --context-chart-plate: 103px;\n"
        "  }"
    ) in AGENTS_CSS
    assert ".agent-context-composition-chart { margin-top: 4px; }" not in AGENTS_CSS
    # The square is the contact region because the listeners bind to the chart's parent,
    # so the pointer is only read inside the square and only there does the drag not pan.
    assert "const compositionHitRegion = compositionChart?.parentElement || compositionChart;" in TAB


def test_context_composition_clicking_outside_clears_the_readout():
    # CONTEXT-OUTSIDE-CLICK: a pointerdown outside the chart's contact region drops the
    # focus. The listener is on the document because the requirement is about the click
    # that lands *away* from the chart; containment against the contact region is what
    # keeps a click on the chart with the chart, whose own pointerdown already handles it.
    assert "CONTEXT-OUTSIDE-CLICK" in TAB
    assert "const onCompositionOutsidePointerDown = (event) => {" in TAB
    assert "if (compositionHitRegion.contains(event.target)) return;" in TAB
    assert "document.addEventListener('pointerdown', onCompositionOutsidePointerDown);" in TAB
    # A tab re-render must not stack document listeners, so the handler unbinds itself
    # once the chart it closes over has left the DOM.
    assert "if (!compositionHitRegion?.isConnected) {" in TAB
    assert "document.removeEventListener('pointerdown', onCompositionOutsidePointerDown);" in TAB
    # The chart can hold DOM focus (tabindex="0"), so the outside click releases it too.
    assert "if (document.activeElement === compositionChart) compositionChart.blur();" in TAB
    # All three parts of the requirement are the chart's own teardown: the marker leaves
    # the ring, the tooltip panel closes, and the lifted set slides back.
    hide_body = TAB.split("const hideCompositionFocus = () => {")[1].split("};")[0]
    assert "compositionNode?.setAttribute('hidden', '')" in hide_body
    assert "compositionTip?.setAttribute('hidden', '')" in hide_body
    assert "popCompositionGroup(null);" in hide_body


def test_context_composition_skeleton_is_built_from_the_chart_container_parameters():
    # CONTEXT-SKELETON-PARAMS: the chart container's numbers — disc, centre plate, square
    # inset, panel floor, card radius — are declared ONCE on #agents-grid, the nearest
    # ancestor that hosts the live composition card AND every skeleton surface that stands
    # in for it: the page-level phantom first paint and the body's own
    # :empty::before/::after. Those surfaces are siblings of the card rather than
    # descendants, so they could never inherit from the chart element; declaring the
    # numbers on the shared ancestor is what lets each placeholder be rebuilt from the
    # container's own parameters, and the live rules read the same vars, so the two can
    # never drift apart.
    assert "CONTEXT-SKELETON-PARAMS" in AGENTS_CSS
    assert "#agents-grid {\n  --context-chart-size: 188px;" in AGENTS_CSS
    params_block = AGENTS_CSS.split("/* CONTEXT-SKELETON-PARAMS")[1].split("}")[0]
    for param in (
        "--context-chart-size: 188px;",
        "--context-chart-plate: 124px;",
        "--context-chart-pad: 18px;",
        "--context-chart-min: 248px;",
        "--context-card-radius: 10px;",
    ):
        assert param in params_block
    # Every consumption keeps the literal as its fallback, so a chart mounted outside a
    # detail body still sizes correctly.
    assert "var(--context-chart-size, 188px)" in AGENTS_CSS
    assert "var(--context-chart-pad, 18px)" in AGENTS_CSS
    # CONTEXT-SKELETON-GEOMETRY: on the tab that owns the chart container the phantom
    # content block IS the placeholder for the composition card — the card's own height,
    # radius and rule. The tab is scoped off the ACTIVE TAB BUTTON, so every other tab
    # keeps the generic block.
    assert (
        '.agent-row:has(.agent-card-tabs .agents-detail-tab[data-tab="abilities"].active)\n'
        "  .agent-detail-body:empty::after {\n"
        "  height: var(--context-chart-min, 248px);\n"
        "  border-radius: var(--context-card-radius, 10px);\n"
        "  border: var(--border-width) solid var(--border);\n"
        "}"
    ) in AGENTS_CSS
    assert "  height: 320px;" in AGENTS_CSS
    # Stacked, the live container IS the square the pointer reads against, so the skeleton
    # becomes that square: the disc plus its symmetric inset, held to 1:1 by aspect-ratio
    # and centred like the real one, rather than a fixed height.
    assert (
        '.agent-row:has(.agent-card-tabs .agents-detail-tab[data-tab="abilities"].active)\n'
        "    .agent-detail-body:empty::after {\n"
        "    width: calc(var(--context-chart-size, 156px) + var(--context-chart-pad, 18px) * 2);\n"
        "    height: auto;\n"
        "    aspect-ratio: 1;\n"
        "    margin-inline: auto;\n"
        "  }"
    ) in AGENTS_CSS
    # CONTEXT-CHART-LOADING: the chart's own pending state (a save re-parks it) wears the
    # SHARED skeleton paint rather than a page-local flat fill, so the placeholder for the
    # disc glimmers like the rest of the page. The flat surface survives only behind
    # :not(.sk-shimmer), and .is-empty stays flat on purpose — no data at all is an
    # outcome, not a pending state. A class PAIR outranks the single primitive, which is
    # exactly why the flat fill has to be scoped away.
    assert "agent-context-composition-chart is-loading sk-shimmer" in TAB
    assert ".agent-context-composition-chart.is-loading:not(.sk-shimmer)," in AGENTS_CSS
    assert ".agent-context-composition-chart.is-empty { background: var(--bg-3); }" in AGENTS_CSS
    # One shimmer paint and keyframe for the whole app: the primitive lives in the
    # globally-loaded sheet (ui/shared/css/app3.css) and the page consumes it by class.
    assert "animation: skShimmer 1.4s ease-in-out infinite;" in AGENTS_CSS


def test_page_level_skeleton_glimmer_uses_the_chart_container_parameters():
    # CONTEXT-SKELETON-GEOMETRY (page first paint): the page-level phantom in agents.html is
    # the FIRST placeholder for the composition card, and it opens before any tab is known,
    # so it must already wear the chart container's geometry — the container's height floor,
    # its card radius and its own rule — rather than a bare 320px content card that then
    # snaps to the chart's shape on handover. It reaches those numbers because the
    # parameters are declared on #agents-grid, the ancestor shared by the phantom and the
    # live chart; declared on .agent-detail-body they were out of its reach.
    html = (ROOT / "ui/main-panel/agents/agents.html").read_text()
    assert '<div class="agents-phantom-detail" aria-hidden="true">' in html
    assert '<div class="agents-phantom-card sk-shimmer"></div>' in html
    assert ".agent-detail-body {\n  --context-chart-size" not in AGENTS_CSS
    phantom_card = AGENTS_CSS.split("#agents-grid.agents-phantom .agents-phantom-card {")[1].split("}")[0]
    assert "height: var(--context-chart-min, 248px);" in phantom_card
    assert "border-radius: var(--context-card-radius, 10px);" in phantom_card
    assert "border: var(--border-width) solid var(--border);" in phantom_card
    assert "height: 320px;" not in phantom_card
    # The generic content card for every other tab is untouched.
    assert "  height: 320px;" in AGENTS_CSS
    # Stacked, the phantom becomes the same square the live container becomes, from the same
    # parameters, so the first paint is already the container the finger lands in.
    stacked = AGENTS_CSS.split("CONTEXT-SKELETON-GEOMETRY (stacked)")[1]
    assert (
        "  #agents-grid.agents-phantom .agents-phantom-card {\n"
        "    width: calc(var(--context-chart-size, 156px) + var(--context-chart-pad, 18px) * 2);\n"
        "    height: auto;\n"
        "    aspect-ratio: 1;\n"
        "    margin-inline: auto;\n"
        "  }"
    ) in stacked


def test_context_group_pop_never_leans_toward_the_pointer():
    # CONTEXT-GROUP-POPOUT: the lifted set travels straight out along its own bisector and
    # nothing else. The pop was briefly handed the pointer's bearing and re-aimed the set at
    # the cursor mid-hover (`...-POPOUT-AIM`); that lean is gone. The direction is baked per
    # group at render time and the focus path only toggles the group's class, so hovering one
    # category at any two bearings moves the set by exactly the same vector.
    assert "const popCompositionGroup = (key, hotKey = null) => {" in TAB
    assert "popCompositionGroup(source.categoryKey, source.key);" in TAB
    assert "popCompositionGroup(source.categoryKey, source.key, aim)" not in TAB
    # No bearing is threaded into the pop, and nothing re-writes the vector after the render.
    assert "COMPOSITION_AIM_MIN_RADIUS" not in TAB
    assert "aimCompositionGroupPop" not in TAB
    assert "compositionGroupAim" not in TAB
    assert "setProperty('--context-group-dx'" not in TAB
    assert "setProperty('--context-group-dy'" not in TAB
    # The markup's per-group vector survives as the one and only pop direction, and the
    # settle loop is still driven by a change of selection alone.
    assert "const groupDx = (Math.sin(groupAngle) * COMPOSITION_GROUP_POP).toFixed(2);" in TAB
    assert "const groupDy = (-Math.cos(groupAngle) * COMPOSITION_GROUP_POP).toFixed(2);" in TAB
    assert "--context-group-dx:${groupDx};--context-group-dy:${groupDy}" in TAB
    assert "if (changed) startCompositionRailSettle();" in TAB
    # The CSS turns that same var pair into the translate for the popped group alone, and
    # states the no-lean rule so the next reader does not re-add the aim.
    assert ".agent-context-composition-group.is-popped {" in AGENTS_CSS
    assert "transform: translate(calc(var(--context-group-dx, 0) * 1px), calc(var(--context-group-dy, 0) * 1px));" in AGENTS_CSS
    assert "never leans toward the pointer or the" in AGENTS_CSS
    assert "POPOUT-AIM" not in AGENTS_CSS


def test_context_composition_uses_one_fresh_session_configured_estimate():
    # The Abilities page is agent-configuration-only: it must not vary with the
    # currently open chat session or present competing MIN/MAX/LIVE values.
    assert "contextParams.set('session_id', app.currentSessionId);" not in TAB
    assert "liveTotal" not in TAB
    assert "resolvedMax" not in TAB
    assert "['MIN', resolvedTotal]" not in TAB
    assert "<b>Configured</b>" in TAB
    assert "typeof tokens?.tokens === 'number'" in TAB
    assert "hasTotal ? tokens.breakdown || {} : null" in TAB
    assert "{ key: 'tools', label: 'Tools'" in TAB
    # The endpoint mirrors the runtime's static fresh-session layers and native
    # provider schema accounting, returning a single deterministic baseline.
    assert "fresh-session Prompt & tools baseline" in AGENTS_API
    assert "tool_hidden_by_ability(name, ability_modes, (), (), (), ability_default)" in AGENTS_API
    assert "render_ability_index(ability_entries)" in AGENTS_API
    assert 'return {"tokens": total, "breakdown": breakdown}' in AGENTS_API
    assert '"live_tokens": live_tokens,' not in AGENTS_API
    assert '"max_tokens": max_tokens,' not in AGENTS_API
