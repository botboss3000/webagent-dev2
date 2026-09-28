# Agent loop guidance (read before changing loop nodes)

Read this before adding, removing, or renaming any node in the agent loop pipeline or its diagram.

For durable requirements, checkpoints, context budgets and isolated manager inputs, see [task-progress.md](task-progress.md).

## The live loop visualizer is ONE shared controller, two mounts

The streaming loop visualizer (`ui/main-panel/agents/agent-loop/js/loop-logic.js`) is a reusable controller, **`createLoopView(opts)`** — do **not** fork it. Two places mount it:

| Mount | File | Source | Gate |
|-------|------|--------|------|
| **Primary** — the agent card's **Agent Loop** tab | `ui/main-panel/agents/js/tab-agent-loop.js` | the active chat session | streams **only when** `app.currentAgentId === agent.id` (the active chat's agent matches the card); otherwise draws the static blueprint + a hint. Keeps node-click → edit panels and the Run test pill as a fallback. |
| **Secondary** — the admin **Runtime Loop** page | `ui/admin-tools/runtime-loop/runtime-loop-view.js` | the active chat session | none (always follows the active session). |

How it fits together:

- `createLoopView(opts)` holds **all** per-view state (pages, buffers, node states, panels). Each mount passes its own `getGraphArea` / `getPages` / `getSidebar` elements, a `gate()`, an `onBlocked()` (what to show when gated off), `isAlive()` (auto-destroys when its DOM is gone), an optional `onNodeClick` override, and a unique `markerPrefix` (admin `lv`, agent tab `agl`) so two diagrams never clash on SVG marker IDs.
- The old module functions — `initLoopVisual`, `startLoopVisual`, `stopLoopVisual`, `renderRuntimeLoopSidebar`, `loopVisualSessionChanged` — are **kept as thin wrappers**: they drive the admin view and fan session-changes out to **every** live view. Their callers (`main.js`, `files.js`, `session-init`/`session-core`, `optimizer-stats`) need no changes.
- The WebSocket handler `app._loopVisualHandler` fans each event to **all** live views; each view's own `gate()` + `isAlive()` decide whether to act.
- The graph chrome is styled by **both** id and class so the agent tab can host its own copy: every `#loop-visual-…` rule in `loop-visual.css` is paired with a `.loop-visual-…` class selector — keep them paired.

## Agent loop diagram — keep both views in sync

The agent loop diagram (`ui/js/loop-diagram.js`) has **two completely independent, hardcoded layouts**. Adding or changing any node requires updating **both**:

| Layout | Location in file | Description |
|--------|-----------------|-------------|
| Horizontal | `LOOP_NODES` array + `LOOP_EDGES` | Used when the diagram is zoomed out |
| Vertical | Inline array inside `buildVerticalLayout()` | Used when the diagram is zoomed in |

Neither layout is derived from the other. A node missing from one view simply won't render there.

**Full checklist for adding a new loop node:**

1. `LOOP_NODES` array — add node with correct `cx`, `cy`, `hw`, `hh`
2. `LOOP_EDGES` — wire in/out edges
3. `_H_GROUPS` — add node ID to the correct stage group (controls horizontal compression)
4. `buildVerticalLayout()` inline array — add node with correct `cy`, `hw`, `hh`; shift all subsequent `cy` values down; extend stage band `y2`; update `genuiH`
5. Update the comment at the top of `buildVerticalLayout()` with the new node count
6. `ui/js/loop-logic.js` `eventToNodeId()` — add a `case` mapping the pipeline event name to the node ID
7. `ui/loop-nodes.json` — add entries to `NODE_STATIC_ITEMS` and `NODE_PANEL_INFO`
8. `ui/js/agents.js` `_INFO_NODES` set — add node ID if it should show info-only panel (no edit bar)
9. `app/agent/loop_executor.py` `DEFAULT_NODE_ORDER` list — add node in the correct position
10. All agent JSON templates in `app/defaults/agents/` (or a `data/agents/` override) — add node ID to each `loop_logic` array
11. Any existing agents' `loop_logic` DB field — update via migration or seed script

## The `attachment_describe` node is a capability-driven type-router

`attachment_describe` (in `app/api/chat.py` `_maybe_describe_images`) is no longer
image-specific glue — it is the **attachment type-router**. It maps each attached
file to the ability that handles its mime-type (today: images → Image Vision; future
document/video abilities drop in the same way) via `app/agent/attachment_router.py`,
then picks a path from the agent's *actual* model capability
(`media_routing` / `model_sees_images` in `app/admin/settings.py`):

- **inline** — the turn model can see the media → leave it inlined;
- **describe** — it can't, but the handling ability + a capable worker model exist →
  one-shot describe (synchronous), folded into the user turn with guidance;
- **unreadable** — the ability is off or no capable model is configured → DO NOT
  inline; the "you can't read this; tell the user, don't hallucinate" directive is
  given to the model for that turn **and surfaced as a foldable `route_attachment`
  tool row** (carrying the reason: `ability_disabled` vs `no_vision_model`), but it
  is **not** written into the user's message row — so it never shows in the chat
  bubble. The directive's wording attributes the gap to the *ability being off / no
  vision model*, not to a model limitation. Same rule for the **describe** path's
  guidance *note*: the image description is persisted into the turn (kept across
  turns), but the agent-directive note is delivered to the model only — not the bubble.

Both foldable rows (`process_image` for describe, `route_attachment` for unreadable)
are **persisted as `role=tool` interaction rows** (parent = the user turn, mirroring
`memory_search`), so they survive a reload instead of being live-only stream events.
They carry **`tool_call_id = None`**, which keeps these synthetic rows out of the
model's rebuilt history (`interactions_to_openai_messages` drops un-paired tool rows)
— a *real* model-issued `process_image` call has a `tool_call_id` and is kept. On
reload, the describe path's folded `[Attached image — …]` block is **stripped from the
user bubble for display** (`_strip_folded_attachments` in `app/api/db_viewer.py`) and
shown via the `process_image` row instead; the raw folded row is left intact for the
model path (`fetch_interactions`), so the description still persists across turns.

**Rendering the foldable row (both render paths must agree).** Because these rows
have no assistant `tool_call` to pair against, the normal renderer — which matches
tool results to an assistant's saved `tool_calls` — skips them. They are rendered
explicitly instead, as their own foldable tool-only bubble in chronological
position. **This same "synthetic standalone tool" machinery also renders the
loop-node memory tools** — `memory_search` (before the turn, parented to the user)
and `memory_save` (after the turn, parented to the search row, tagged
`metadata.brain`) — so brain reads/writes show up in the transcript just like any
other tool call (search before the reply, save after):
- **Reload:** `/session-messages` tags every such row `_synth_tool` — vision by
  parent role == user, memory by `tool_name ∈ {memory_search, memory_save}` +
  `metadata.brain` — and keeps its body even in light mode (`_slim` exemption); the
  chat builds a call entry straight from the row (`_buildSynthCall` /
  `_isSynthToolRow` in `ui/chat/js/session-load.js`) — no lazy
  `/session-turn-detail` fetch.
- **Live:** these calls fire outside a reply bubble's normal tool accounting —
  vision/`memory_search` during ingestion (before `turn_start`), `memory_save`
  *after* the turn's `response`. `chat-activity.js` therefore renders them
  out-of-band via `_renderSynthToolBubble`, drawing the completed call straight
  onto the transcript the moment it finishes, independent of turn accounting, so it
  survives every event ordering. Args arrive per-tool: vision via its `tool_call`
  event, `memory_search` via `memory_search_start` (rendered on its `tool_result`),
  `memory_save` via the args+result the backend rides on `memory_save_end`. Regular
  tool calls are unaffected.
The pasted image **thumbnail** likewise re-renders on reload: `/session-messages`
resolves the user row's `input.attachment_ids` into a frontend-shaped
`attachments` array (read from the RAW input column so it survives light mode) and
`session-load.js` re-renders it with the same component the live send flow uses.

A new file-type ability adds itself by shipping a companion `<ability>.json`
(`handles` + `worker_system` + `guidance`) next to its `.py` — **no edit to this
node**. The node id and its pipeline events (`attachment_describe_start/end`) are
unchanged, so the loop diagram needs no update.
