# GenUI Live Data Standard

Every genui page that wires agent sessions to its UI needs a live link —
items that advance their status, threads that grow, plan steps that complete,
all without the user refreshing. This standard provides that link.

## The three layers

### 1. Backend — `GET /api/v1/genui/{slug}/data`

Every genui page can poll its own data bag via this endpoint. It's a cheap
JSON poll (`user_id` query param, Bearer auth from localStorage) that
returns `{status, slug, data}`. The data is exactly what the agent writes
via `set_genui_data`.

The companion `POST /api/v1/genui/{slug}/data` persists the data bag from
the page itself (e.g. QA status after a user action).

### 2. Host — `api.onSessionActivity(cb)` + slug‑scoped background reload

The page subscribes to `api.onSessionActivity(callback)` and receives
`{session_id, type, tool}` for *any* agent session's WebSocket events —
not just the chat panel's current session. This is the instant‑wake pipe.

Additionally, `render_visual` / `edit_genui` / `refresh_genui` tool results
whose slug matches the **currently displayed** page trigger a debounced
(3s) remount even from background sessions.

### 3. Standard snippet — `genui-live.standard.js`

A self‑contained `GenUILive` constructor any page pastes in:

- **`GenUILive.init({slug, user_id, onData})`** — polls the data endpoint,
  does a stable JSON diff, fires `onData(fresh, prev)` only when something
  changed, coalesces renders (≥1s apart), pauses when the tab is hidden,
  switches between fast (3s when watched sessions are active) and idle
  (15s otherwise) intervals.

- **`live.watchSessions({api, getSessions, onRun})`** — links the page's
  session ids to the live loop: instant wake on WS activity for tracked
  sessions, periodic (5s) run‑state checks via `/session-tail`, and an
  `onRun(sid, run)` callback so the page can update per‑item live
  indicators.

- **`live.refresh()`** — force an immediate poll (skip the coalesce
  debounce). `live.destroy()` tears everything down.

## Wiring checklist for any page

1. **Paste** the standard body (inline or via `<script>`) into your page.
2. **Store the page's slug** in `var SLUG = '<your-slug>';`.
3. **Write an `applyLiveData(fresh, prev)` function**:
   - Merge `fresh` into your `STATE` object.
   - Re‑render only the parts that changed (surgical update).
   - **Preserve user‑entered text** in input/textarea fields across re‑renders.
4. **In `boot()`**, after the first render:
   ```js
   var live = GenUILive.init({slug:SLUG, onData:applyLiveData});
   var api = window.WebagentGenui && window.WebagentGenui.api;
   if (api) {
     live.watchSessions({
       api: api,
       getSessions: function() {
         // return array of session ids for items that are in‑progress
       },
       onRun: function(sid, run) {
         if (!run || !run.active) live.refresh();
       }
     });
   }
   ```
5. **Add "Next step" indicators** so the user sees what's coming:
   - Executing items: show the next plan step (index‑matched from
     `plan.steps` and `execution_log`).
   - `plan_ready`: show "Next: Review & accept the plan".
6. **Test**: start an item's research, open it, and watch the pill,
   thread, plan, and next‑step advance while the page is open and
   untouched. Also verify the hidden‑tab pause and that other sessions
   don't remount unrelated pages.

## Reference implementation

`data/user_data/admin/genui/home/index.html` (the Project Development
Tracker) is the canonical reference — it uses this standard for its QA
item flow (research → plan → execute → done).

## Gotchas

- **User text must survive re‑renders.** Save input values before any
  DOM rebuild (e.g. `_captureInputs()`) and restore them after
  (`_restoreInputs()`). The host persists scroll and open‑panel state
  automatically.
- **stable JSON diff** uses sorted keys so that key reordering doesn't
  trigger a spurious update.
- **Coalesce** doesn't drop data — it just delays the next `onData` call.
  The very last poll's data is always applied.
- **Hidden‑tab pause** saves bandwidth & server load; the page polls
  immediately when the tab becomes visible again.
- **Canonical file location**: `ui/main-panel/genui/genui-live.standard.js`.
  The inline version in the home page is equivalent but self‑contained.
- **Auth**: reads `localStorage.auth_token` and `auth_user_id`. Falls
  back to `'admin'` for single‑user local installs.
