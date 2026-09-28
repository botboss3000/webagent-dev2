# Browser capture maintenance

The generic plugin loader imports `frontend/toggle.js`. It loads this directory's
CSS and initializes `recorder.js`; nothing is registered as an agent ability.
Core integration points are found with `git grep RECORDER-HOOK -- ui`.
Remove each marked observation block (and any scope epoch guard added with it)
when removing integration permanently; retain neighboring rendering logic.

- **toggle.js:** document-private lease and reload intent, scope handshakes,
  lifecycle state, browser tab sharing, user issue markers, stop acknowledgement.
  Only a genuine reload restores intent. Cloned tabs do not auto-record.
- **recorder.js:** baseline, DOM mutations, compact cause traces, render
  checkpoints, errors and successful/failed fetch timing. Fetch URLs exclude
  query strings. All traces are IDs, lengths, decisions, or metadata.
- **outbox.js:** IndexedDB chunks with stable recording/document/sequence IDs.
  Acknowledgement deletes chunks. Failed sends retry the same IDs. Short-lived
  upload leases stay in tab sessionStorage; shared IndexedDB stores no leases.
  The existing browser persistence policy controls whether durable storage is
  permitted. Memory fallback is bounded to 8 MiB and reports explicit gaps.
- **controls.css:** owns both migrated recorder styles and the screenshot/note
  controls. It uses the shared theme variables and reduced-motion rules.

## Evidence contract and extension points

Core code invokes `window.__recorderTrace?.(kind, detail)` inside a marked try
block. The callback exists only during active capture or explicitly armed buffering. Optional-call arguments
must remain compact: never pass an entire event, credential, URL query, or
message body. For a new source, emit received/queued/applied/discarded decisions
separately. An attempted render is not proof of what painted.

`window.__recorderScope` exists during active capture or explicitly armed buffering; async send/load
boundaries may await it before performing the action. Scope API timeouts are
2 seconds and appear as evidence gaps. No scope hook changes caching policy.

Checkpoint element IDs are kind-prefixed durable IDs where present, otherwise
document-local node IDs (`stable_id:false`). DOM ownership or an observed
interaction-to-session association is used for session IDs. Missing ownership
remains null. The checkpoint records actual ordering, independently observed
visibility, viewport intersection, scroll geometry, and content fingerprints.
The analyzer must never interpret missing fields as passes.

DOM mutation frames retain their own timestamps. Semantic geometry sampling is
coalesced to approximately 10 Hz during streaming; fingerprints are invalidated
only for mutated nodes/ancestors. Checkpoints cap at 2,000 elements and explicitly
report incompleteness above that. Geometry cannot prove pixel occlusion, exact
paint ordering, or intermediate flicker. Screenshots are separate pixel evidence.

A completed-turn event schedules a settled checkpoint after one second.
This does not manufacture a terminal witness for runs that completed before
recording. Progress timing samples the visible activity note and first new visible tool/content
after an observed send. Same-frame observations and hidden-tab intervals stay
blocked. Compaction notice intervals are measured from DOM presence and viewport
visibility; notices already present at capture start have incomplete timing.

The issue editor retains its original frame, marker ID, timestamps, and note
on upload failure. Retry reuses those values until acknowledgement. Discard
requires an explicit second click; a user-selected image download is also
available. Pending screenshots remain in the document, so closing/reloading
the entire browser can still lose an unsaved frame.

Screenshot capture is opt-in through the browser surface picker and accepts
browser-tab surfaces only. Users must choose the app tab. The button stores a
single frame before displaying the optional note editor; marker/frame timestamps
are separate. Sharing permission is not restored after reload. No rolling video
buffer is implied.

## Persistence and failure boundaries

The toggle owns automatic stop so capture flush and report finalization share one
lifecycle. The backend receives the last sequence, acknowledgement state, and
all accumulated gaps. Lost uploads, masking/truncation limits, unavailable
storage, and pending old-document batches make the report degraded.
A pagehide flush is best effort: previously acknowledged chunks are durable;
the browser can terminate before its last asynchronous transaction finishes.

Never add cache-bypass reloads, history polling, full-event logs, or unbounded
memory buffers to get evidence. Preserve the app's warm-cache behavior.

Armed evidence uses ring.js: a bounded memory-only event window and advancing DOM mirror. uploader.js owns immutable per-capture upload jobs so stopping can re-arm immediately. toggle.js owns account-scoped preference and server armed leases. prepareRecorderCapture pins the prefix at the click; promotion attaches capture identity without restarting observers. See the plugin README for memory, coverage and lifecycle contracts.
