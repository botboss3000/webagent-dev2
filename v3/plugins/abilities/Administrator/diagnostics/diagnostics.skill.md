# Diagnosing a chat session — the fast recipe

Load this skill before investigating any session, run, or tool problem. One or
two well-parameterized calls answer most questions. Don't guess or code-dive
before pulling the data.

Each tool below carries its own mini-skill, delivered when that tool becomes
callable.

## Data map — which tool gets which data

| What you need | Tool | Where / what to query |
|---|---|---|
| Turn-by-turn transcript | `run_python` + `sqlite3` | the **session's per-user DB** → `interactions` (filter by `session_id`, order by `session_seq`) |
| Run state (stop_cause, resume budget) | `run_python` + `sqlite3` | the **session's per-user DB** → `session_runs` (one row per session) |
| Session metadata (agent, status) | `run_python` + `sqlite3` | the **session's per-user DB** → `sessions` (PK is `id`, NOT `session_id` — see below) |
| Agent config / prompt slots | `db_query` | context documents (NOT raw SQL — reads/writes prompt slot documents) |
| Server errors, tracebacks, run lifecycle | `read_diagnostics` | `data/db/logs.db` → `diagnostics` table |
| Tool timing / failure rates | `read_diagnostics(view='tools')` | `data/db/logs.db` → `tool_executions` table |
| Source code | `read_source`, `search_source` | `plugins/`, `app/`, `docs/` |

Key point: `db_query` is for **prompt slot documents** on the agent — it is NOT
a SQL interface. For raw DB queries use `run_python` with the `sqlite3` module.

**Where the session tables actually live — read this before the first query.**
The database is split into planes, and session data is **per user**, not in one
file. `sessions`, `interactions`, and `session_runs` live in
`data/user_data/<user_id>/<user_id>.db` (resolved in code by
`app/db/user_store.py` → `_user_db_path(user_id)`). The app plane is
`data/db/app.db`; logs stay at `data/db/logs.db`. On a **split-layout** install
(the current default — see the `storage_layout` table) `data/db/local.db` is a
**legacy stub**, often 0 bytes, and querying it fails with `no such table`. Only
if `local.db` actually holds the tables are you on the older single-DB layout,
where you may query it directly.

## Scenario → parameters cheat sheet

### "Why won't this session resume?"

One flight-recorder call + one DB query:

1. `read_diagnostics(session_id="…", levels="info,warning,error,critical", categories="run,recovery", since_minutes=1440)`
   — **must include `info` level**; resume/recovery events are recorded at info, not warning
2. Query `session_runs` for that session_id via `run_python` — look at
   `stop_cause`, `resume_attempts`, `max_resume_attempts`, `status`, `error`

Auto-resume only fires for **involuntary** stop causes:

- ✅ `server_restart`, `zombie`, `frozen`, `crash`, `empty_response` — auto-resumed on boot/watchdog
- ❌ `complete`, `user_stop`, `replaced`, `needs_manual_resume` — voluntary, never auto-resumed

The resume budget: `max_resume_attempts` (default 3, from `app-settings.json` →
`run_max_resume_attempts`). Once `resume_attempts >= max`, the run is marked
failed and auto-resume stops. Manual resume (`POST /resume`) still works — it
bypasses the budget.

Resume engine code: `app/agent/runner.py` → `_classify_resume()`,
`_RESUMABLE_CAUSES`, `resume_one()`.

### "What error crashed this turn?"

`read_diagnostics(levels="error,critical", categories="server,tool", session_id="…", since_minutes=60)`

The `detail` field holds the full Python traceback. Cross-reference timestamps
with the `interactions` transcript to pinpoint which tool call failed.

### "Which tool is slow or failing often?"

`read_diagnostics(view="tools", since_minutes=1440)` — returns per-tool call
count, failure rate, avg/max duration, and recent failures.

### "What happened in the conversation?"

`run_python` querying the session's **per-user DB** (see how to find it just below):

```python
SELECT session_seq, role, tool_name, substr(content,1,300) AS content, status, created_at
FROM interactions WHERE session_id=? ORDER BY session_seq
```

### "Given a session id or message id, find it and open it from the DB"

**Step 0 — the cheapest path is the `session_search` tool, not SQL.**
`session_search(session_id="…")` resolves the owning DB for you and returns the
session plus its latest rows (including `latest_session_seq`). Reach for SQL only
when you need the full transcript, run state, or a column it does not return.

**Finding the DB for a session id.** Session data is sharded per user, so a given
id lives in exactly one `data/user_data/<user_id>/<user_id>.db`. Scan the user
plane with a **scoped** glob — a recursive `**` glob over the whole tree can time
out:

```python
import glob, sqlite3
sid = "PUT-SESSION-ID-HERE"
for db in sorted(glob.glob("data/user_data/*/*.db")):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        if con.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            print("OWNING DB:", db)
            break
    except Exception:
        pass          # DB without a sessions table, or busy — skip
```

Then transcript and run state are one query each in that DB:
`SELECT … FROM interactions WHERE session_id=? ORDER BY session_seq` and
`SELECT * FROM session_runs WHERE session_id=?`.

**Message / interaction id → its session** (same DB):
`SELECT session_id, session_seq, role, tool_name FROM interactions WHERE id=?`.
The `interactions.id` value is the message/interaction id; `session_seq` is its
position in the turn order. The reverse (session → every message) is the
`WHERE session_id=?` query above.

**Schema facts that trip people up:**
- `sessions` primary key is **`id`** — there is no `session_id` column on it
  (a `WHERE session_id=?` on `sessions` errors, and inside a scan loop that
  exception can silently skip the very DB that holds the row).
- `interactions` keys on **`session_id`** (→ `sessions.id`) and orders by
  **`session_seq`**, not `created_at` (created_at can tie within a turn).
- `session_runs` also keys on `session_id`, one row per session.
- One session may span **more than one run** after a resume — read `session_runs`
  for state, `interactions` for the full turn history.

## Validating / testing / reviewing a feature as the signed-in user

When the user asks you to **validate, test, or review a feature on a site they use**
(logged-in pages, dashboards, stores, panels), act **as them** — you do NOT need
their login details. The agent reuses the user's **live browser session**:

1. **Check the login is available first:** `web_session_status` — reports whether a
   live logged-in session (or pasted cookies) exists for the user.
2. **Drive the shared browser tab** (what the user is looking at in the Web tab):
   `browser_action` — `navigate`, `click`, `type`, `get_text`, `screenshot`, `wait`,
   `evaluate`. This is the logged-in session, so you act as the user directly.
3. **Fast HTTP/GraphQL as the user:** `web_session_fetch(url, method, params/body)`
   and `web_session_graphql(...)` — fire requests carrying the user's cookies, so
   sites without a public API treat you as the user. Read the `cookie_source` field:
   `live` = the user's real login, `pasted` = pasted cookies.
4. **Real-browser fallback for hard logins (2FA/CAPTCHA):** `browser_backend(mode="local")`
   — switch the session onto the real Chrome on the user's device, which carries
   their everyday logins. Switch back with `browser_backend(mode="headless")`; the
   login carries forward.

### Do I need the user's credentials in the vault?

**No — not for sites the user is already logged into.** The tools above use the
user's live browser session automatically:

- Signed in on the **in-app Browser page** → `web_session_*` and `browser_action`
  reuse that login with zero setup.
- Signed in on their **real Chrome** → `browser_backend(mode="local")` attaches it.
- `vault_login` (encrypted vault credentials — the agent never sees the password)
  is only the **fallback** for a site where the user has NO active login, and
  `cookie_allowlist(action="add", domain="…")` pins logins across restarts.

> Requires the **Browser Control** ability to be enabled for this agent — without
> it the browser tools aren't available. The diagnostics-only toolset (this skill's
> core) covers the flight recorder, not live browsing.

## UI diagnosis guardrail

For WebAgent UI diagnostics, keep the default localhost origin. Before treating an
empty state as evidence, run `login_as_current_user` and require its `verified`
user ID, stored user ID, and active user ID to agree. Then click the visible
named tab/control and re-read its selected content; a hash change alone does
not prove the SPA selected that panel. Any browser-observable 401, 403, or 404
app API response invalidates the browser context for a capability conclusion.
Re-establish identity/origin before reporting that a current user's data is empty.

The tool is confirm-gated (destructive): in Ask/Plan mode the agent pauses for
approval before minting a session as the user.

## Direct deep links

Do not click through the application to reach a test target. Resolve a registry
route first and pass it to `dev_browser_action(action="open", path="/app?tab=…")`,
which authenticates and opens that page in its first load. For a chat, call
`dev_browser_url(path="/app", chat_session_id="…")` to verify
current-user ownership and return its direct route, or pass that
`chat_session_id` directly to `dev_browser_action(action="open")`. This opens
`/app?session=…` with the session preloaded.

**Registry first, then the bundle's own JS.** Take the route from
`docs/claude/ui-journeys.md`. Its "Existing parameterized subviews" entries are the
deep links you can use today (e.g. `agents.page` =
`/app?tab=agents&agent=<agent id>&agentView=<page id>`). If the target you want is
not listed there, resolve it from source **before** opening the browser:

    grep -n "replaceRouteParams({\|searchParams.get(" ui/<bundle>/**/js/*.js

That single grep is the whole method — it is how the registry entries were derived.
Example: `ui/main-panel/agents/js/view.js` calls
`replaceRouteParams({ agent: agent.id, agentView: pageId })`, which yields
`/app?tab=agents&agent=shared_default&agentView=modes`. Clicking through instead is a
measured waste: a top-level tab (`?tab=agents`, "Config") lands on a *directory* of
pages, not the subview you are verifying, and reaching the right one by hand costs a
half-dozen calls.

**Collapsed panels lie to `innerText`.** WebAgent config and mode cards are
`<details>` elements. A collapsed card contributes only its summary text to
`innerText`, so probing for a control that is actually rendered returns a false
negative and reads like a broken build. Expand first — `evaluate` over
`document.querySelectorAll('details')`, match the `summary` text, set `.open = true` —
then take the assertion with `get_text`.

## Gotchas

- **`read_diagnostics` defaults to `warning,error,critical`** — run lifecycle and
  resume events are `info` level. Always include `levels="info,warning,error,critical"`
  when diagnosing run state.
- **`read_source` uses `offset` and `limit`** — NOT `start`/`end`. `offset` is 1-indexed.
- **`db_query` is NOT SQL** — it reads/writes agent prompt slot documents. Use
  `run_python` + `sqlite3` for database tables.
- **Session tables are per-user, not in one `local.db`.** `sessions` /
  `interactions` / `session_runs` live in `data/user_data/<user_id>/<user_id>.db`;
  `data/db/local.db` is a legacy stub on split-layout installs. To find the DB for
  a session id, see *"Given a session id or message id"* above.
- **`sessions` PK is `id`; `interactions` orders by `session_seq`.** Querying
  `WHERE session_id=?` on `sessions`, ordering by `created_at`, and recursive `**`
  globbing of the whole tree are the three usual time-wasters.
- **The flight recorder has a time window** — default `since_minutes=120`. Widen
  it for older incidents (e.g. `since_minutes=1440` for the last 24h).
- **`read_diagnostics` and `read_diagnostics(view='tools')` query different
  databases** — diagnostics reads the `diagnostics` table in `data/db/logs.db`;
  tools view reads `tool_executions`, also in `logs.db`.
- **Older sessions may have no flight-recorder entries** — the ring buffer is
  RAM-based and the durable store auto-prunes. Widen `since_minutes` or drop the
  time filter entirely.

## Quick recipe (the original, still valid)

From `docs/claude/diagnosing-sessions.md`:

1. Dump `interactions` for the `session_id` (from the session's **per-user DB** —
   see the Data map) → read the turn-by-turn flow, find the broken step.
2. Query `diagnostics` (errors, same time window / session) → get the real traceback behind any vague tool error or HTTP 500.
3. Check `tool_executions` for the specific failing tool if needed.
4. Decide: model/skill issue (instructions) vs. ability/core logic (code) — then read that source.
