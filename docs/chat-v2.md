# Controlled chat V2

The V2 checkout began as an independent source snapshot of development commit
`a896b7760c12fb19fee4f8d1458a5884e2bd219c`.
Snapshot SHA-256: `bab5316739370624309f6cbbcee0c87abc3473c438ab277d72fb955e40a84ffb`.
Runtime data, databases, uploads, secrets, environment files and caches were excluded.

## Isolation

Checkout: `/home/ar/webagent-dev2`. Container: `webagent-dev-v2`.
Image: `webagent:dev-v2`. Binds `100.112.23.15:8081:8080` and `127.0.0.1:8082:8080`.
Mount only V2 checkout-local data; never mount existing development or production data.
The independent instance starts with fresh split storage and field encryption.
Credentials must be configured independently.

Private access:

```sh
ssh -N -L 18082:127.0.0.1:8082 ar@100.112.23.15
```

Open http://127.0.0.1:18082/app for private access.

The user authorized replacing the existing development origin on 2026-09-28.
`https://dev.webagent.live/app` now routes through the existing named connector
`cloudflared-webagent-dev-named` to V2 on `100.112.23.15:8081`.
No Cloudflare DNS or account configuration was changed.
The original `webagent-dev` container is stopped with its data and configuration
preserved. The previous private V2 container is preserved as
`webagent-dev-v2-pre-public-20260928T163408Z`.

Historical rollback reference only: the original development app is outside the
default V2 target. Do not run this procedure without a separate explicit user
instruction (it interrupts the development site):

```sh
docker stop cloudflared-webagent-dev-named
docker stop webagent-dev-v2
docker start webagent-dev
# Require HTTP 200 before restarting the connector:
curl --fail http://100.112.23.15:8081/health/ready
docker start cloudflared-webagent-dev-named
```

The V2 origin and container health passed after the 2026-09-28 switch. The
connector received the expected hostname/origin configuration and registered
connections. An initial public HTTP check returned 403. On 2026-09-30 the
public development `/app` endpoint returned 200 and authenticated V2 API
checks passed. This confirms HTTP/API reachability; current attached-Chrome
visual acceptance remains outstanding.

## Contract

Execution events are private. Public items are validated, encrypted at rest,
ordered durably, and committed atomically with their delivery record.
Clients consume committed publications, never raw execution messages.
App/user/agent ownership remains in the canonical schema registry.

## Implementation status

This rebuild is incomplete and is not ready for end-to-end acceptance.
The original development container remains preserved for comparison or rollback.

Implemented in the isolated instance:

- Fresh app/user/agent storage initialization and independent instance identity.
- Owner-scoped encrypted chat records, idempotent acceptance, inbox consumption,
  exclusive leases, generation fencing, and atomic publication/delivery commits.
- Public snapshots and authenticated bounded SSE replay under `/api/v2/chat`.
  The client reconnects immediately at its committed cursor after each response.
- Private execution journal and separately authorized diagnostic reads.
- Frozen finalization candidates/policies, bounded validation repairs, safe failure
  notices, and linked Continue commands.
- Expired-lease recovery that fails uncertain execution without repeating tools;
  unconsumed accepted input remains queued.
- Core-owned closer/visibility modules and policy descriptors, with the existing
  settings registry retained. General application event delivery now lives in
  `app/events/delivery.py`.
- The main panel now loads, sends, stops, and refreshes through the V2 client.
  Its keyed renderer retains accepted and optimistic user node identity; complete
  publications bypass browser envelope stripping and adjacent-answer merging.
- Main chat raw WebSocket publication handlers and private-tail polling no longer
  supply the visible transcript. Account/session changes fence pending network work.
- Cached and streamed publications share the reducer; pending sends use the
  independent instance/subject cache partition.
- The main panel exposes final-response retry for failed publications with a
  frozen finalization candidate. Its older-message loader requests bounded
  snapshot pages and preserves the visible scroll anchor.
- V2 terminal notifications and acknowledgements now drive the header,
  session dropdown, and Sessions view. The notification API pages across all
  active sessions, and a read/unread change updates those surfaces.
- Deleting a server-backed chat session now waits for V2 Stop before removing
  the session; the unused browser-storage V1 interrupt helper was removed.

Still required: broader agent and provider configuration coverage, remaining
integrations, and removal of legacy helpers, APIs, and browser authority paths.
Manual compaction, voice routing/control, and the served embed chat have V2 wiring;
their full live integration matrix is not complete. Trusted save_file/attach_file
results, generated images, and browser screenshots now publish durable, owner-scoped
attachment items with stable artifact IDs. The main panel, floating widget, and
served embed resolve fresh content grants and render media previews or downloads.
Other agent-generated files must use the User Files producer contract to enter
the public transcript. The Background Output
Closer now projects its persisted summary once as a public notice; its legacy
system interaction remains the recovery and audit source.
Elements are owner-only session resources, synchronized separately from the public
transcript through the V2 components endpoint. Admin activity groups are now connected
as described below. The complete browser/fault/performance/regression matrix remains
outstanding; this is not a finished or accepted chat rebuild.

The current shared-default agent has a Standard role assigned to a model. On
2026-09-29 a new development chat generated one real, completed answer through
the V2 main panel. Credential readiness for other agents and model roles remains
unverified. The shared agent engine still supplies context, model calls, and tools;
V2 owns acceptance, lease, private journal, publication, stop, and recovery.
A scripted-provider integration test now drives a real safe tool and final answer
through the agent loop into V2 storage and the authenticated snapshot/event
endpoints. Additional deterministic checks exercise two agent identities across
Ask, Plan, Auto, and Workspace modes; model-role authorization and publication;
private reasoning; provider open/stream failures; DeepSeek transport fallback;
OpenAI subscription request construction; a failing tool; interactive-tool
privacy; attachment ownership; and image prompt formatting. Provider responses
and authentication in those checks are scripted. Other live agent credentials,
provider variants, process interruption, and the full attachment/vision journey
remain unverified. On 2026-09-30, an authenticated development API run in Auto
mode used save_file and published one attachment. Repeated snapshots kept its
public item ID, bounded SSE replay returned it, and a freshly minted content
grant fetched the stored bytes. This verifies the live server path, not the
browser rendering. The same agent's Output Closer gate is disabled in Auto mode,
so that run did not exercise a live Closer summary. The authenticated V2 voice
route returned a deterministic status answer, and an idle Stop request reached
the V2 control route. Its `stopped` field now has a boolean contract. Closer
recovery, generated image and screenshot rendering, and visible voice and embed
journeys remain unverified. The attached Chrome browser needed for final UI
acceptance was unavailable. The current development agent roster also has no
enabled UUID agent to exercise the served embed route.

Verification uses disposable databases and deterministic fixtures. The core suite
covers acceptance conflicts, privacy, snapshot/replay equivalence, terminal
uniqueness, validation failure, metadata repair, inbox boundaries, stop fencing,
owner denial, rollback, concurrent claims, restart recovery, frozen candidates,
Continue linkage, and malformed controls. Run it on the server:

```sh
docker run --rm --network none --user 1000:1000 --entrypoint python \
  -v /home/ar/webagent-dev2:/app:ro -w /app webagent:dev-v2 \
  -m pytest -q -p no:cacheprovider tests/test_chat_v2_core.py
```

Focused checks on 2026-09-29: 90 V2 backend tests (plus 8 agent/mode subcases)
and 50 client tests passed, including notification paging, acknowledgement,
older-message state/transport, compaction, and the scripted engine and provider
matrices. The main panel's 66-module dependency graph links successfully.
Projection tests use a deterministic DOM double, not a browser; they establish
identity/reconciliation behavior but do not establish visual or latency acceptance.
A live V2 API run confirmed send deduplication, conflicting-key rejection,
malformed-payload rejection, Stop terminality, linked Continue, acknowledgement
bounds, concurrent deduplication, and public replay matching a snapshot across
a V2 restart. Anonymous event access returned 401. Idle SSE requests completed
in about 1.1 seconds. The bounded event feed is a sequence of authenticated
SSE responses, each followed immediately by a cursor-based reconnect. The
2026-09-29 real-answer check used the public development site in a new Codex
in-app browser tab; attached Chrome acceptance remains unavailable.

Run client tests on the server:

```sh
node --experimental-default-type=module --test tests/chat_v2_state.test.mjs \
  tests/chat_v2_transport.test.mjs tests/chat_v2_projection.test.mjs
```

The required older `docs/claude/ui-guidance.md` path is absent from the source
checkout. The user approved using the current Codebase Admin and Diagnostics
journey guides in its place. No browser acceptance is asserted.

## Resume work (2026-09-30)

The handoff target is the remote `/home/ar/webagent-dev2` checkout on
`ar@100.112.23.15`, with `app.v2:app` in `webagent-dev-v2`. Use the
project control instructions for SSH, data boundaries, deployment, and UI
journeys. Start with `git status --short --branch` and inspect existing
diffs: source, tests, documentation, and development data have uncommitted
changes. Preserve unrelated work. Do not use the older development checkout
or production to fill gaps.

Follow the active path from `app/v2.py` into `app/chat/api.py`,
`runtime.py`, `execution.py`, `publication.py`, and the relevant
integration. The public main-panel client is in `ui/chat/core/`; the floating
widget is in `ui/chat-widget/`; the served embed is in `ui/embed/`.
`ui/chat/js/README.md` inventories inherited modules and is not a statement
that their legacy stream paths own the V2 transcript.

Manual compaction is already implemented through the V2 compaction endpoint,
`app/chat/compaction.py`, and its focused tests in
`tests/test_chat_v2_compaction.py`. Inspect those paths before proposing new
compaction work. The remaining work is to complete the live integration and
acceptance matrix: attached-Chrome chat journeys; generated image and browser
screenshot rendering and grant refresh; Output Closer publication and recovery
with its gate enabled; visible voice and served-embed journeys; and the wider
agent/provider and interruption matrix. The shared-default agent's Closer gate
was disabled in the tested Auto mode, the development roster lacked an enabled
UUID embed agent, and no attached Chrome browser was available. These are
fixture/access gaps, not passing acceptance results.

The 2026-09-30 core suite passed 96 tests plus 8 agent/mode subcases; focused
embed, security, integration, and core checks passed 78 tests. Voice checks
passed 25 tests, with one Node-dependent test deselected inside the container
because Node is unavailable there; its JavaScript path was checked on the
server host. Re-run relevant suites after changes. Distinguish deterministic
tests and authenticated API checks from browser and live-provider acceptance.
The development container was running and healthy and the public `/app` URL
returned 200 at that check; verify both again when resuming.

## Main-panel Elements (2026-09-28)

Plan Overview, canonical Task Progress, checklists, choices, approvals, forms and
scratchpads remain durable owner-only session resources. The V2 main panel reads
`GET /api/v2/chat/sessions/{id}/components`, which returns validated active
components and saved panel selection, with ownership checked before and after
the read. It reuses the existing component persistence and action services, so
previously saved elements remain available. Completed and deleted items leave
both the panel and menu.

A cancellable, in-memory watcher refreshes the active session every two seconds.
Session/account changes and revoked access clear the panel and fence pending
responses; fresh local actions invalidate older requests. Unchanged component
nodes and focused editors survive background snapshots. Restored elements start
closed, and selecting an Elements menu entry opens the existing full-height
panel. Element loading never blocks the public transcript.

These resources do not consume private tool events and are not inserted into
the participant-visible transcript or its IndexedDB cache. Generic
`kind: component` transcript publications remain reserved; they are not the
transport for Elements. The legacy owner-scoped action endpoints remain in use.

Focused coverage:
`tests/test_chat_v2_components.py` and
`node --experimental-default-type=module --experimental-vm-modules --test tests/chat_v2_components.test.mjs`.
The client test loads the actual element panel module in addition to exercising
snapshot ordering, access revocation, reconciliation, and focused draft retention.
Supplemental public-development in-app browser checks cover opening elements,
live checkpoint updates, checklist reload persistence, choice/approval/form
completion, and scratchpad saving. Attached Chrome acceptance remains unavailable.

## Main-panel activity groups (2026-09-28)

The admin main panel now renders private activity groups from the V2 execution
journal. Enable with `chat_common.show_activity_groups` in
`data/config/chat_ui.json` (enabled in development). The existing
`mirror_activity_in_transcript` flag controls the legacy mirror and is not
needed for this view.

`GET /api/v2/chat/sessions/{id}/activity` remains admin-only and returns
paginated events, stable segment boundaries, a cursor, and `has_more`.
The view drains every page, keeps private data in memory only, and cancels and
clears it on session/account changes or access revocation. It never feeds raw
execution data into the public reducer or IndexedDB conversation cache.
Final-response retry activity anchors after its parent's terminal publication.
When a public item closes a run's segment but that run later emits valid work,
the next event opens a new segment after the newest public item. This keeps
finalization work after an intervening user message. The last group of a
terminal run shows Completed, Failed, or Stopped; earlier closed groups remain
Finished.

Collapsed counters are derived from the same semantic rows shown on expansion:
tool calls/results share one row, assistant updates/responses share message IDs,
and mode/compaction events have dedicated rows. A nested raw-event disclosure
retains the complete diagnostic payloads, including late stale records.
Disclosure nodes preserve expansion and focus as new results arrive.
This is an admin debugging view, not a customer-facing activity permission.

Focused checks: `tests/test_chat_v2_activity.py` and
`tests/chat_v2_activity.test.mjs`. The public state/transport/projection tests
remain applicable. Browser verification used the Codex in-app browser on the
public development URL; attached-Chrome acceptance remains unverified.

The footer reasoning-effort slider receives model-supported, tier-filtered levels
from the agent-models endpoint, independently of the model's public alias.
GPT-6.1 Sol and GPT-6 Astra offer Low, Medium, High, Extra High, and Max; Default
clears the per-entry session override. Admin accounts can use Max, while other
tiers retain their configured ceilings. The v2 model-effort endpoint rejects
unsupported levels before storing them. Responses requests send
`reasoning.effort`; native OpenAI Chat Completions sends `reasoning_effort`.
Legacy Minimal hints for these two models resolve to Low within the tier ceiling.
The slider is disabled until a session exists, and for models without supported
reasoning levels. Deterministic API/transport checks cover these paths; attached
browser and live-provider inference acceptance remain separate checks.
