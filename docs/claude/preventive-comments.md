# Preventive Comments — duplication invariant markers

This document catalogues all intentional-duplicate or pattern-duplicate markers in the codebase. These markers alert developers that code at two or more locations must be kept in sync.

## Convention: invariant comment box

Use the following boxed comment format when a piece of code is intentionally duplicated (cannot/should not be extracted into a shared function):

```javascript
// ╔═╗ PATTERN-NAME  ════════════════════════════════════════════════════╗
// ║ Description of what's duplicated, where the copies are, and what   ║
// ║ rule applies when editing one of them.                             ║
// ╚══════════════════════════════════════════════════════════════════════╝
```

**Rules:**
- Every copy must carry the box. A reader opens one file and immediately knows there are siblings.
- The box must list every file that has a copy (absolute or relative paths).
- If you fix a bug in one copy, fix it in all listed siblings.
- If you extract the pattern into a shared function, REMOVE all copy boxes.

## Registry

### RENAME-FIELD PATTERN

Inline-rename fields: create `<input>`, replace title element, attach Enter/Escape/blur commit handlers.

| File | Function | Notes |
|------|----------|-------|
| `ui/js/sessions.js` | `startRename(sid, row)` | Session list row |
| `ui/js/sessions.js` | `_headerRenameSession()` | Header dropdown label |
| `ui/js/genui.js` | `_startRenamePage(slug, row)` | Gen UI page row |
| `ui/js/files.js` | Inside `_tabLabelToInput` | Terminal tab label |

**Clone-group fingerprints (intentional duplicates):**

```
 16 lines  2 instances  dup:7d3611c8
    ui/js/genui.js:211-223
    ui/js/sessions.js:2119-2134

 17 lines  2 instances  dup:2725ae62
    ui/js/sessions.js:2135-2151
    ui/js/sessions.js:2213-2227

 15 lines  2 instances  dup:20bf4403
    ui/js/genui.js:217-231
    ui/js/files.js:2697-2708
```

**What to keep in sync:** The keydown handlers (Enter→commit, Escape→cancel), blur handler, outside-click commit pattern, and the `done` guard flag.

### CAROUSEL-WIRING PATTERN

Drag-to-scroll with chevron affordance for horizontal scroll containers.

| File | Function | Notes |
|------|----------|-------|
| `ui/js/agents/view.js` | `_wireSquaresCarousel(wrap)` | Agent squares |
| `ui/js/agents/view.js` | `_wireTabCarousel(wrap)` | Agent card tabs |

**Differences:** `_wireTabCarousel` adds a `dragging` CSS class toggle and has different min-page values. `_wireSquaresCarousel` has an extra `setTimeout(updateAffordances, 120)`. Both share the same pointer-events drag logic, affordance toggle, and ResizeObserver setup.

### SISTER-PANEL PATTERN

Two files that must remain visually and functionally in sync.

| File | Sister File | Notes |
|------|-------------|-------|
| `ui/js/admin-ability-table.js` | `ui/js/agent-ability-table.js` | Admin superset / agent subset |

**Clone-group fingerprint (after cleanup):**

```
152 lines  2 instances  dup:3f68f3e8
    ui/js/admin-ability-table.js:79-157
    ui/js/agent-ability-table.js:77-155
```

(This block covers the remaining intentionally duplicated visual skeleton:
`_buildGroup`, `_buildGroupHead`, `_appendTriAndChevron`. The shared utilities
`_esc`, `_iconHtml`, `_chevronSvg`, `_buildTriToggle`, `_wireTriToggle`, and
`_noop` were extracted to `ui/js/shared/dom-utils.js`. Dead code
(`_buildMemberRow`, `_appendToggle`) was removed from the agent file.)

**Rule:** These are intentionally split (admin has delete buttons and other
admin-only features). Any change to the shared skeleton functions
(`_buildGroup`, `_buildGroupHead`, `_appendTriAndChevron`, and the main build
function's group-iteration logic) must be mirrored in both files. The imported
utility functions (`_esc`, `_iconHtml`, etc.) are shared by both and should
only be changed in `shared/dom-utils.js`.

### SHARED-UTILITY PATTERN

Functions extracted into `ui/js/shared/` that had duplicate implementations across the app.

| Function | Module | Files that previously defined it locally |
|----------|--------|------------------------------------------|
| `_esc` | `shared/dom-utils.js` | `automations.js`, `sessions-page.js`, `data-sources.js`, `wiki.js` |
| `_btn` | `shared/dom-utils.js` | `data-sources.js` |
| `_fmtTime` | `shared/dom-utils.js` | `automations.js`, `sessions-page.js` |
| `_statusBadge` | `shared/dom-utils.js` | `automations.js` |
| `_typeIcon` | `shared/dom-utils.js` | `automations.js` |
| `_enabledToggle` | `shared/dom-utils.js` | `automations.js` |
| `_iconHtml` | `shared/dom-utils.js` | `admin-ability-table.js`, `agent-ability-table.js` |
| `_chevronSvg` | `shared/dom-utils.js` | `admin-ability-table.js`, `agent-ability-table.js` |
| `_buildTriToggle` | `shared/dom-utils.js` | `admin-ability-table.js`, `agent-ability-table.js` |
| `_wireTriToggle` | `shared/dom-utils.js` | `admin-ability-table.js`, `agent-ability-table.js` |
| `_noop` | `shared/dom-utils.js` | `admin-ability-table.js`, `agent-ability-table.js` |
| `_buildComingSoonPill` | `shared/dom-utils.js` | `admin-ability-table.js` |
| `buildRowWhere` | `ui/js/db/columns.js` | `ui/js/db/delete.js`, `ui/js/db/edit.js` |
| `_refreshLucideIcons` | `shared/dom-utils.js` | `ui/js/files.js` (15 call sites), `ui/js/chat.js` (2 call sites) |

**Resolved clone groups (extracted to shared):**

```
 15 lines  2 instances  dup:5c438efb  →  resolved: buildRowWhere() in db/columns.js
 18 lines  2 instances  dup:a30cd3a7  →  resolved: _stripToolCalls() in sessions.js
 13 lines  2 instances  dup:c9cb5922  →  resolved: fetchAndCache() in sw.js
 13 lines  2 instances  dup:6eaede63  →  resolved: fetchInteractionRows() in loop-events.js
 16 lines  2 instances  dup:a9baa109  →  resolved: _refreshLucideIcons() in shared/dom-utils.js
 12 lines  2 instances  dup:fac955d5  →  resolved: _renderSavedColumnCells() in agent-settings.js
```

### LOOP-ARC PATTERN

Adjacent SVG path-computation arms in the edge-rendering function `_computeEdgePathVertical`. These look similar (same `M..C` syntax, genui-derived `arcX` values) but each branch handles a different connection type.

| File | Lines | Edge type |
|------|-------|-----------|
| `ui/js/loop-diagram.js` | 365-377 | "below" arc (guardrails → check_continue) |
| `ui/js/loop-diagram.js` | 381-391 | "above" / llm_call→check_continue bypass |

**Clone-group fingerprint:**

```
 13 lines  2 instances  dup:0f3f35ca
```

**What to keep in sync:** `genuiW` scaling factors, SVG path formatting, label positioning — but the conditional (`edge.below` vs `edge.from === 'llm_call'`) and `arcX` values are intentionally different per edge type.

## Remaining intentional duplicate groups (documented, not extracted)

These cross-file clone groups are too tightly coupled to their calling context to benefit from extraction without excessive parameterization.

### DETAILS-LIST PATTERN (dup:06584882)

Same `info.details.forEach(...)` DOM-building block in two loop-panel renderers.

| File | Context | Differences |
|------|---------|-------------|
| `ui/js/agents/tab-agent-loop.js` | `_lvRenderNodePanelInfo` | Adds a "Details" label, uses `lv-edit-desc`, compact code style |
| `ui/js/loop-logic.js` | `_lvRenderBlueprintPanel` | No label, uses `lv-bp-meta`, expanded code style |

The 6-line forEach body is identical, but the wrapper (label creation, CSS classes, code formatting) differs enough that extraction would need over-parameterization.

### AGENT-PUT PATTERN (dup:f55482a5) — RESOLVED

This duplication is gone: the agent card's **Tools tab was removed** and its `tab-tools.js` (which held the `persist()` inline closure) was deleted. The guardrail/execution controls it owned moved into the Config tab, which persists via the single exported `_putAgentField()` in `ui/main-panel/agents/js/utils.js`. There is now one PUT path.

### ADMIN-TOOLS UTILITY TRIO (dup:df27b307)

Small utility functions (`_qs`, `_userId`, `_setStatus`) in two admin-tools files.

| File | Element ID | Color values |
|------|-----------|-------------|
| `ui/js/data-management.js` | `ac-attach-status` | Hard-coded hex (`#9ece6a`, `#f7768e`) |
| `ui/js/remote-access.js` | `ac-ra-status` | CSS variables (`var(--success)`, `var(--danger)`) |

The 12-line block is the same structure with different element IDs and color values. Extracting would require parameterizing both, which provides minimal benefit over the current approach.

## Remaining fallow clone groups (systemic patterns)

The remaining ~44 clone groups flagged by fallow are all **intra-file duplicates** (5-13 lines each) across the following files. They fall into three categories that are inherent to DOM-heavy JavaScript:

### Category A: DOM-building patterns (~20 groups, 3-7 lines each)
Same-element creation sequences that cannot be extracted without over-parameterization (e.g., `document.createElement` + `className` + `textContent` + `appendChild`).

**Files:** `ui/js/agents/view.js`, `ui/js/agents/tab-agent-loop.js`, `ui/js/agents/tab-automation.js`, `ui/js/chat.js`, `ui/js/files.js`, `ui/js/files-git.js`, `ui/js/sessions.js`

### Category B: Event-handler wiring (~12 groups, 4-8 lines each)
Identical `addEventListener` w/ `stopPropagation` or `toggle` patterns on similar elements.

**Files:** `ui/js/agents/view.js`, `ui/js/agents/tab-automation.js`, `ui/js/chat.js`, `ui/js/sessions.js`, `ui/main-panel/app/settings/agent-settings.js` (OAuth scope "all"/"none" toggles)

### Category C: State-update boilerplate (~12 groups, 3-6 lines each)
Short blocks that toggle CSS classes, update `innerHTML`, and call a render function.

**Files:** `ui/js/agents/view.js`, `ui/js/agents/tab-automation.js`, `ui/js/files.js`, `ui/js/files-git.js`, `ui/js/sessions.js`, `ui/main-panel/app/settings/agent-settings.js`

**Assessment (2026-06-09):** These clusters are too small and too tightly coupled to surrounding context to benefit from extraction. They are the natural footprint of a jQuery-era codebase using vanilla DOM APIs. Adding a bulkhead now would increase complexity without reducing maintenance burden.

### Clone families summary

| File | Groups | Lines | Status |
|------|--------|-------|--------|
| `ui/js/agents/view.js` | 5 | 47 | Intentional (carousel + square rendering) |
| `ui/js/sessions.js` | 4 | 43 | Systemic (bubble rendering, lucide, rename) |
| `ui/js/agents/tab-automation.js` | 3 | 19 | Systemic (DOM building) |
| `ui/js/agents/tab-agent-loop.js` | 2 | 11 | Systemic (DOM building) |
| `ui/js/chat.js + sessions.js` | 2 | 17 | Systemic (bubble rendering) |
| `ui/js/files.js` | 2 | 23 | Systemic (DOM building) |
| `ui/js/files-git.js` | 2 | 18 | Systemic (DOM building) |
| `ui/js/storage.js` | 3 | 20 | Systemic (event-wiring + badge update) |
| `ui/main-panel/app/settings/agent-settings.js` | 2 | 20 | Systemic (column heads + scope toggles) |
| `ui/main-panel/app/settings/data-management.js + remote-access.js` | 1 | 12 | Intentional (different color/theming) |

All systemic groups are documented here for tracking. No extraction or suppression is warranted.

**Rule:** Never redefine these locally in a new file. Import from `shared/dom-utils.js`. If the shared module doesn't have what you need, add it there and update importers.

## When to add a new invariant marker

Add a boxed comment when:

1. **Two or more code locations are intentionally identical** and extracting a shared function would add unacceptable complexity (e.g., closures over different state, different DOM selectors that can't be parameterized cleanly).

2. **A function was extracted to a shared module** but the old local definition was kept (e.g., `agents/utils.js`'s `_esc` is a legacy implementation while `shared/dom-utils.js` is the canonical one — the old one cannot be removed yet because it's deeply wired into the agents subsystem).

3. **A pattern is known to recur** across files (e.g., inline-rename) and you want to prevent divergent bug fixes.

## Review checklist

During code review, check for:

- [ ] Does the PR contain a new copy of an existing function? If so, was an invariant marker added?
- [ ] Does the PR fix a bug in one copy of a known pattern? Were all siblings updated?
- [ ] Does the PR remove one copy? Were the other copies' invariant markers updated/removed?
- [ ] Is a shared utility being redefined locally instead of imported from `shared/dom-utils.js`?