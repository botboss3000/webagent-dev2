# Journey Recorder

A removable **application plugin** for troubleshooting the chat transcript. It
lives in the same Docker project as WebAgent, but is not an agent ability.
Installation is discovery of `plugins/recorder/plugin.json`; no ability registry
entry or additional recorder-specific dependency installation is required.
The .dockerignore file excludes this folder from normal application images; development
loads it through the existing source bind mount. Production source mounts must
also omit the folder. No production deployment is changed by this feature.

The recorder toggle starts browser capture and scoped backend diagnostics.
Use the app, optionally use the camera to mark an issue, then stop recording.
**GenUI → Journey Reports** shows the recorded sessions, evidence, mechanical
checks, and investigation plan. Reports describe the observed journey; they do
not replace the acceptance journey in the Diagnostics ability's `journey_testing`
skill (`plugins/abilities/Administrator/diagnostics/skills/journey_testing.skill.md`).

## Architecture and ownership

| Module | Responsibility |
| --- | --- |
| `plugin.json`, `plugin.py` | Application discovery, lifecycle and plugin-owned GenUI page |
| `api.py` | Admin/owner checks, short capture leases, intake, artifacts, report actions |
| `service.py` | Worker subscriptions, bounded event queues, periodic writes/audits, stop barrier |
| `store.py` | Recorder archive, document identities, shared scopes, reports and approval revisions |
| `frontend/toggle.js` | Tab-local lifecycle, scope handshakes, screenshot permission and issue editor |
| `frontend/recorder.js` | DOM capture, semantic checkpoints, stream/cache traces and timing witnesses |
| `frontend/outbox.js`, `frontend/uploader.js` | Acknowledged chunk persistence and independent per-capture upload jobs |
| `frontend/ring.js`, `prebuffer.py` | Browser DOM mirror and bounded per-worker rolling memory |
| `frontend/controls.css` | All recorder controls and screenshot-editor styling |
| `rules.py` | Versioned mechanical checks with independent evidence predicates |
| `investigation.py` | Bounded tool-free analysis and separately authorized execution dispatch |
| `genui/` | Live report UI; summaries and references rather than raw evidence in page state |
| `redaction.py` | Server-side structured-field and text redaction |
| `legacy.py` | Historical recording access and compatible capture preferences/lease helpers |

Data flow:

```text
browser toggle → signed document lease → backend session scope
browser DOM / stream / cache facts ─┐
backend stream / diagnostics / logs ├→ recorder archive → mechanical report
user marker / screenshot ──────────┘                      ↓
                                             tool-free investigation
                                                        ↓
                                       human approves a plan revision
                                                        ↓
                                      separate Execute action → agent session
```

The API prefix remains `/api/v1/recordings`. New capture endpoints live under
`/captures/{id}`; assets use `/plugin-assets/recorder/{asset}`. The application
plugin loader discovers frontend code only for administrators. Artifact access
also requires authentication and capture ownership; screenshots are not public
static files.

The .dockerignore RECORDER-HOOK excludes the plugin from image packaging; remove
that exclusion only when intentionally packaging this development feature.

Discovered assets use /plugin-assets/recorder/v-{hash}/{asset}. The hash covers
the complete frontend, keeping relative JS/CSS imports in one immutable build.
Unversioned URLs are compatibility aliases with revalidation.

## Multiple server workers and finalization

The current development deployment runs **four application workers**. Capture
must work when the toggle request, a stream, and the stop request reach different
workers. Do not replace this mechanism with process-local subscription state.

Each worker registers itself and observes its own stream, diagnostic and
application-process log events. The archive publishes a token-free
`data/recorder/active.json` scope snapshot through atomic replacement. Workers
check its mtime and refresh scopes before accepting observations. Registered
workers retain light callbacks while the plugin is installed; without active
scopes they do not archive events. Ordinary diagnostics settings are unchanged.

Callbacks enqueue; SQLite writes run off the event loop. The service normally
flushes every two seconds. The backend queue is bounded at 20,000 events and
overflow/write failures become explicit capture gaps.

Stop first publishes a finalizing boundary so workers stop accepting new capture
facts. Each live worker commits previously queued facts and acknowledges its
flush. The stop handler waits up to eight seconds for the worker barrier before
finalizing the report. A missing acknowledgement is a gap, not a successful
flush. The browser independently submits its final sequence and upload
acknowledgement. Lease expiration, interrupted workers and missing browser
batches cannot silently produce a complete report.

## Settings and storage

Optional non-secret settings are read from `data/config/recorder.json`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Availability while the plugin is installed |
| `auto_investigate` | `true` | Queue tool-free analysis after a finalized report has findings or user markers |
| `retention_days` | `30` | Expire old completed/interrupted raw evidence while retaining report metadata |
| `execution_agent_id` | empty | Optional explicit execution-agent default; no shared agent is chosen implicitly |

Investigation currently enforces a fixed 120-second analysis timeout.

The browser uses required error, console, network, lag and snapshot collection
during capture even if historical switches disabled them. Current capture has
no automatic duration or total-operation limit. Individual payloads and queues
remain bounded, with truncation or dropped evidence reported. Default scope is
the chat transcript; decorative backgrounds are excluded. Compatible per-key
capture preferences are still read through `legacy.py` from
`data/config/app-settings.json`; migration does not rewrite those settings.

New captures, events, screenshots, reports and approvals belong exclusively to
`data/recorder/recorder.sqlite3`. This is a diagnostic archive, **not a session
database**. Inspect session state only through the WebAgent API. Retention removes
old raw events/image blobs, marks evidence expired, and blocks further approval
or execution against expired evidence. It does not delete application sessions,
uploads or historical recorder data.

The earlier recorder ability and frontend entry points were migrated into this
package. Existing recording data is not copied, rewritten, pruned or populated
by migration. Historical read/statistics routes remain available through the
compatibility router; its pre-existing admin deletion endpoints also remain.
New capture intake writes the new recorder archive, not the legacy recorder
table. Removing this plugin preserves both old and new stored evidence.

## Browser evidence, performance and privacy

Capture intent and short-lived leases are private to the browser tab. A genuine
reload keeps the recording ID but creates a fresh document ID; duplicating a tab
does not activate recording in the duplicate. Ten-minute leases renew while
recording. Scope handshakes before session loads/sends have a two-second timeout;
failure is recorded and does not prevent normal chat.

Browser chunks retain stable recording/document/sequence identities until the
server acknowledges them. IndexedDB follows the existing browser persistence
policy. When persistence is disabled or unavailable, the bounded memory fallback
reports that reload durability is unavailable. Short leases stay in tab
sessionStorage, never in shared IndexedDB. A terminated tab can still lose its
last asynchronous transaction; finalization must account for that uncertainty.

DOM mutations retain frame timestamps. Semantic geometry sampling is coalesced
during streaming; text fingerprints are invalidated for changed nodes rather
than recomputed for the whole transcript every frame. Geometry checks are capped
at 2,000 elements with explicit incomplete coverage. No recorder path disables
normal cache-first assets, bypasses transcript caches, or adds history polling.

Network capture strips query strings and records metadata, not request bodies or
authorization headers. Backend stream capture stores IDs, lengths and hashes
rather than repeating every content delta. Masked inputs and structured secret
fields are redacted, but transcript content and chosen screenshots can contain
whatever the user displayed. This is diagnostic content, not an anonymized
analytics feed.

Screenshot sharing is explicitly enabled through the browser picker and accepts
browser-tab surfaces. The user must select the app tab; the browser API does not
prove the selected tab's origin to the recorder. Frames are captured before the
note editor opens. Marker and frame timestamps are distinct. Upload failure
retains the original frame and note for **Retry**; discarding requires an explicit
second click, and the user may save the image locally. Reloading/closing the
whole document can still lose an unsaved image. Sharing permission is not
restored after reload. There is no rolling video buffer.

See `frontend/README.md` for the browser implementation contract.

**Coverage boundary:** server evidence here means application-process logs,
stream emission and structured diagnostics. It does **not** collect Docker-host,
container-runtime or Cloudflare-tunnel logs. Process log entries without session
coordinates are labeled uncorrelated context. Delegated child-session events are
matched only when they include an explicit parent-session relationship or that
child session has been scoped by the browser; parent-only capture does not prove
complete delegated-session coverage. Pixel occlusion, exact paint order
and glitches between samples require screenshot or additional journey evidence.

## Mechanical checks and investigations

Every check returns `pass`, `fail`, `blocked` or `not_run`. Reports remain
provisional during recording. Missing ownership, incompatible revisions,
same-frame timing, partial pagination, truncation, mid-run starts and hidden-tab
intervals are limitations; they are not implicit passes.

Current rules cover ordering, activity-group/closer uniqueness, counters,
finalized-content fingerprints, Stop lifecycle, progress-before-content,
compaction notice duration, session ownership, cache revisions and potential
scroll jumps. Visibility means measured DOM/viewport evidence. A missing closer
cannot be declared a failure merely because pagination removed its region.
Theme, keyboard and recovery acceptance require their own exercised evidence.

Rules carry a stable ID, rule-set version and guideline fingerprint. Extend
`RULES` and its independent predicate, then add behavioral fixtures in
`tests/test_recorder_rules.py`. Never import renderer ordering/projection logic
as the rule's expected result. New producers should emit facts and reasons,
not fields claiming that the application passed its own requirement.

Investigation uses the user's configured System/background model with **no
tools or commands**. It receives bounded event neighborhoods, allowlisted source
snippets and limited linked screenshots. Unsupported visual input or unavailable
model configuration is reported; the mechanical report remains usable.
Report/evidence fingerprints deduplicate analysis.

A plan is a proposal. The backend stores the human's approve/reject decision,
exact plan revision and selected execution agent. Approval and **Execute** are
separate actions. Execution claims that stored revision, checks reviewed source
fingerprints, and dispatches through the normal authenticated chat path into a
linked execution session. GenUI page state, model prose, retries and automatic
investigation cannot approve themselves. Dispatch success is not proof that the
agent completed or verified a fix; inspect the linked session.

## Removal and external breadcrumb index

For ordinary removal, finish active recordings, remove only this plugin package,
then use the deployment's normal restart/cache-generation procedure and reload
open browsers. Preserve `data/recorder`, legacy recordings and administrator
settings unless cleanup was separately requested. Already-loaded browser modules
cannot be unloaded merely by deleting their source directory.

The application imports generic discovery/observation seams, not this optional
package on the chat boot path. Missing manifests, assets or plugin imports are
isolated from normal chat. Removing the plugin does not require removing its
small inactive observation hooks. To remove those permanently, search
`RECORDER-HOOK` in both tracked and newly added source files and follow each
comment's caller/consumer/removal boundary.

| External file | Seam and removal boundary |
| --- | --- |
| `app/application_plugins.py` | Generic manifest/router/asset/lifecycle loader; retain if another application plugin uses it |
| `app/observation.py` | Optional fact bus; remove only after all producers/subscribers are removed |
| `app/main.py` | Generic catalog, startup mounting, per-worker lifecycle and shutdown flush |
| `app/visualizer/genui.py` | Generic plugin page discovery and plugin-owned HTML rendering |
| `ui/main-panel/genui/js/genui.js` | `GENUI-CSP-NONCE` passes the active document nonce to authorized page scripts; retain when removing recorder |
| `sw.js` | `CACHE-GENERATION` qualifies executable cache misses by build to avoid stale CDN responses; retain cache-first behavior when removing recorder |
| `app/api/chat.py` | Stamped stream-emission tap; retain sequence stamps, durable writes and normal delivery |
| `app/agent/diagnostics.py` | Scoped diagnostic tap before ordinary logging gate; preserve normal diagnostic configuration |
| `index.html` | Dormant recorder mount and generic loader script; remove the mount independently, loader only if unused |
| `ui/shared/js/application-plugins.js` | Optional admin frontend discovery; remove with its index script if no other plugin uses it |
| `ui/shared/js/main.js` | Breadcrumb documenting removal of the old hard recorder import |
| `ui/shared/js/agentWs.js` | Received/queued/observer/replay and reconnect-boundary facts; retain every actual transport and replay operation |
| `ui/shared/js/chat-activity.js` | Activity and run-start/completion observations; retain note/control lifecycle |
| `ui/chat/js/chat-stream.js` | Queue/apply/discard and monotonic-buffer decisions; retain rendering and monotonic guards |
| `ui/chat/js/chat-message-cache.js` | Cache merge/miss/manifest observations; retain cache writes and invalidation logic |
| `ui/chat/js/chat-reconcile.js` | Durable-tail request/response decisions; retain interaction cursor handling and session guard |
| `ui/chat/js/session-load.js` | Cache/load observations and optional scope await; remove its added await/associated epoch guard together |
| `ui/chat/js/storage/storage-adapter.js` | Storage-mode reads and projection-write facts; retain adapter operations |
| `ui/chat/js/chat-send.js` | Optional pre-send scope await on normal/retry/portal sends; retain payload and send code |

Frontend hooks are guarded optional calls inside isolated try blocks. Their
arguments must remain small: do not pass full messages, raw socket events,
credentials or query strings. Backend producers use the generic observation bus.
Each hook comment identifies the consumer and the code that can be removed.
Recorder CSS is owned by this plugin, not left in the shared application sheets.

## Verification

From the approved development checkout/container, run the recorder API, plugin,
worker, rule and investigation tests:

```sh
python -m pytest -q tests/test_recorder_api.py tests/test_recorder_plugin.py tests/test_recorder_workers.py tests/test_recorder_rules.py tests/test_recorder_investigation.py
```

The server's Node runtime can run the focused browser-module contract tests:

```sh
node --experimental-vm-modules plugins/recorder/tests/frontend.cjs
node plugins/recorder/tests/cache.cjs
node --experimental-vm-modules plugins/recorder/tests/witnesses.cjs
```

These checks cover persistence/permissions, worker coordination, conservative
rules, retry semantics and browser-module contracts. They do not establish real
pixel rendering, native sharing permission behavior or full journey acceptance.
Use the repository's attached-Chrome journey rules for final UI acceptance.

If Journey Reports remains at "Loading recordings…", check the browser console
for CSP violations and verify the GenUI host script is from the current build.
The host must use the active app document nonce when mounting inline page scripts.
A service-worker generation qualifies executable network cache misses with
`__wa_build`; the original request remains the CacheStorage key, so repeat loads
still use cache-first without an extra network request. Browser HTTP reload alone
does not invalidate a CDN cache. Never weaken CSP or disable caching to work
around this failure.

Run the supplementary report journey in the development container with
`python plugins/recorder/tests/journey.py --record`. It creates one admin test
capture and verifies the exact report, markers, keyboard refresh, both themes,
narrow layout, and warm reload/reopen under the real CSP. Use `--capture-id ID`
instead to inspect an existing recording without creating one. This uses an
isolated server browser, not the attached user Chrome required for acceptance.

## Requirement measurement wiring

frontend/recorder.js samples semantic transcript checkpoints on mutations and
every 500 ms while enabled. The periodic observation includes footer controls,
theme changes and quiet terminal/recovery states that do not mutate transcript
DOM. frontend/witnesses.js owns scroll intent, pointer/keyboard observations and
reload/reconnect boundaries; disabling the recorder tears down all its listeners.
The small reconnect hooks in ui/shared/js/agentWs.js are inert without recorder.

Checkpoints join existing cache/source interaction IDs to DOM IDs for session
ownership; unknown ownership stays unknown. Group identity includes its segment.
Content fingerprints measure message bodies, excluding clocks and action labels.
Short diagnostic summaries are intentionally abbreviated and are not evidence
loss; truncated required HTML still reports a capture gap. Optional screenshots
do not determine the outcome of otherwise complete mechanical observations.

rules.py consumes those fields plus normalized cache traces. Every requirement
has an evaluator and a concrete action hint when its evidence is missing.
Themes and keyboard results describe sampled structure/interaction evidence, not
pixel perfection or a complete accessibility audit. Recording an idle page cannot
exercise progress, compaction or recovery: send a task, use the relevant controls,
change themes, and keep recording through reload/reconnect as appropriate.
Do not turn unexercised requirements into passes.

Existing saved reports retain their original rule version and observations.
New probes require a new recording; missing historical evidence cannot be
backfilled. Extend a producer and its matching behavioral test together when
adding a requirement. The controlled measurement journey in
tests/measurement_journey.py verifies browser-to-report wiring with a synthetic
transcript; it is not the attached-user Chrome acceptance journey.

## Upload limits and recovery

RECORDER-HOOK transport.js batches by encoded UTF-8 JSON size (256 KiB),
including retries of old stored batches. The application request boundary remains
1 MiB; recorder does not raise or bypass it. HTML evidence is stored losslessly
as numbered html.part events linked by payload_id; concatenate html in part
order to reconstruct the parent snapshot/mutation. Checkpoint parts similarly
carry checkpoint_id and are joined by rules.py before evaluation; a missing part
blocks completeness. Editable fields are masked before HTML is split.

Outbox writes try IndexedDB before using bounded emergency memory. Stop removes
observers but keeps the lease/outbox retryable until every queued upload is
acknowledged. The switch turns off immediately; the panel shows reports finishing in the background.
Independent jobs retry automatically while another recording may start. Only after
successful upload does the server finalize the report. A
recovered upload-pending condition is not permanently recorded as evidence loss.
Previously dropped or truncated evidence cannot be reconstructed from the archive.

Run the isolated public-browser stress test with
python plugins/recorder/tests/upload_journey.py inside webagent-dev. It sends
more than 8 MiB of synthetic events, verifies lossless large-HTML parts, injects
one upload outage, waits for automatic retry, and checks every event plus the final gap list.
It does not replace attached-user Chrome acceptance.

Recorder controls: the camera requests tab screenshot access on first use; there is no separate enable button. Stop labels the upload drain separately from report finalization. Browser storage waits are bounded to five seconds, and the final upload drain shares a twenty-second budget across batches. Unreadable durable queues remain pending for retry and never count as an empty acknowledged queue. See RECORDER-STORAGE-BOUNDARY and RECORDER-STOP-DRAIN in the frontend.

## Five-second pre-buffer

Click **Recorder** to open configuration. Enable **Include the previous 5 seconds**
to arm browser and authorized application-server observations. The preference
defaults off and is remembered per account in this browser when its storage
policy permits persistence. The configuration stays open until outside click or
Escape; automatic status popups close after two seconds.

Arming creates no capture archive or outbox events. Browser evidence lives in a
32 MiB bounded memory ring; the DOM baseline advances as older full-value patches
expire. Server workers keep a shared-budget 16 MiB RAM buffer and share only
lease/promotion metadata in `data/recorder/armed.json`. Armed leases last thirty
seconds and renew every ten seconds. Scope switches preserve the previous
authorized session for five seconds. Uncorrelated server logs, Docker/host/tunnel
logs and screen images are not buffered.

Clicking the switch pins the browser prefix while a capture lease is obtained.
Workers promote their own retained evidence through the existing writer.
Browser event sequences and elapsed times are assigned after promotion; the
`prebuffer-trigger` metadata marks where the click occurred. GenUI shows actual
browser/worker coverage, missing workers and limitations. Server timing is based
on receipt, so network latency is reported rather than inventing a perfectly
synchronized five seconds. Warm-up, memory pressure and worker loss can shorten
coverage. Older reports are not rewritten.

Stop detaches the capture's upload job and immediately resumes the ring when
enabled. A new recording can start while previous reports finish. Jobs own their
original capture IDs, tokens and final sequence numbers; retries never restart
their observers. Pending finalization metadata is saved separately from the
rolling ring when browser storage policy allows it, so a same-account reload
can resume the retained outbox and finalize after acknowledgements. This recovery uses same-tab session storage: keep the tab open until uploads
finish, although navigation inside the app and reloads can resume them.
Server-side report work continues after uploads have been acknowledged.
Turning buffering off or losing eligibility detaches armed collectors and clears
rolling evidence. Manual screenshots still require the browser picker.

Maintenance seams: RECORDER-ARMING, RECORDER-PREBUFFER, RECORDER-DETACHED-UPLOAD
and RECORDER-DETACHED-FINALIZATION. All implementation remains in this removable
plugin; existing external RECORDER-HOOK integration stays optional.

Additional checks:
```sh
node --experimental-vm-modules plugins/recorder/tests/prebuffer.cjs
node --experimental-vm-modules plugins/recorder/tests/panel.cjs
python -m pytest -q tests/test_recorder_prebuffer.py tests/test_recorder_prebuffer_rules.py
```
