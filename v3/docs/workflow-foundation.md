# V3 workflow and storage decision (Phase 0)

Status: accepted for the in-process v3 foundation. Scope: the current administrator
and agent, with ownership rules that remain valid when more users and agents arrive.
This document defines contracts; it does not claim v2 feature parity.

## Fixed execution boundary

The Rust runtime owns admission, attachment handling, context assembly, model
dispatch and output, pre-tool authorization, tool-result handling, and final
publication, in that order. A model may return tools and repeat the context/model
portion of the loop. The Python tool worker executes only a dispatch authorized
by Rust. It cannot admit work, alter policy, approve itself, or publish chat.

Each checkpoint records a typed, versioned input digest and a Pass or Reject
decision in the private run document. The receipt binds the run generation,
policy revision, turn, and call index. A receipt is evidence of a decision,
not an authorization token. Tool dispatch rechecks live permission, agent
visibility, session mode, cancellation, and shutoff. An approval is bound to
the relevant revisions; a changed restriction invalidates it. Recovery may
resume a safe read, but an interrupted write remains uncertain until resolved.

The installed module set is empty. Module configuration names only fixed
checkpoints; unknown checkpoints and uninstalled modules fail validation.
A later module adapter must accept a versioned checkpoint input and return
a bounded typed contribution or decision. It cannot skip or reorder checkpoints,
mutate another plane, lower a native denial, or publish directly. Module errors
must have an explicit fail-closed or optional-skip policy before installation.

Context contributions have History, Run, Turn, or Call lifetime. A History
contribution is committed by its owning producer before projection and is not
reinserted as an instruction. Run contributions last one run; Turn and Call
contributions expire at their respective boundaries. Only trusted Rust
producers can submit contributions. Model text, tool output, and client JSON
never become trusted instructions by default. The projection leaves durable
history intact. The current module list contributes nothing.

Final output is accepted by the Rust publication service only after nonempty
and size checks. The run state, private model interaction, and one terminal
assistant chat row commit in one user-plane transaction. A failed commit keeps
the caller's recoverable run unchanged. Drafts, tool results, failures, and
cancellations remain private.

Workflow defaults are embedded in the binary. An authenticated, validated
application-plane override may tighten limits within shipped ceilings. The
effective policy revision is a digest of resolved settings, not an independent
counter. Live changes are read at loop boundaries; the current limit is checked
again before publication. Tool permission policy remains independent and
mandatory. Existing model selection and base instructions are pinned at
admission; live revocation still applies before dispatch.

## Authoritative record map

Each family has one owner. Cross-plane references are checked in application
code because separate SQLite files cannot enforce cross-file foreign keys.
All persisted sensitive values use the v3-only encryption key; identifiers and
indexes needed for lookup may remain clear. No v2 data or credentials are imported.

| Record family | Authority and current location | Sensitivity | Lifecycle |
| --- | --- | --- | --- |
| Accounts, profiles, role, status, tier assignment, agent catalog membership | Application; `db/app.db` | Private identity and access | Create or revise by administration; retain audit of changes |
| Global shutoff, workflow/tool policy, installation policy, model catalog and grants | Application; `db/app.db` | Security configuration | Revisioned; recheck at dispatch and recovery |
| Provider credentials, machine tokens, auth sessions, encryption key | Secrets interface backed by application documents and `v3/data/encryption.key` | Secret | Never expose through chat, receipts, logs, or migration from v2; rotate/revoke explicitly |
| Agent definition, prompt slots, ability visibility, connections and gate configuration | Agent; `agent_data/<agent>/<agent>.db` | Private agent configuration | Revisioned; delete/disable after reference checks |
| Conversation, public chat, run, private interaction, approval, receipt, context checkpoint, attachment, memory, user artifact | User; `user_data/<user>/<user>.db` and bounded workspace/upload storage | Private user content | Owned by one user; preserve ordering and encryption; apply explicit retention |
| Operational events, diagnostics and measurements | Telemetry; existing app/user event streams and runtime logs | Redacted operational metadata | Bounded retention; no credentials, raw tool arguments, or provider payloads |

Current schema declarations and document routing are mapped below. "Canonical"
means the authoritative mutable record; "projection" can be rebuilt from it.
Encrypted content and sensitive metadata follow the sensitivity noted above.

| Table or key family | Plane and authority type | Sensitivity | Lifecycle |
| --- | --- | --- | --- |
| `user_accounts`, `user_profiles` | App; canonical identity/profile | Private | Revisioned account lifecycle |
| `entitlement_tiers`, `agent_memberships` | App; canonical grants and caller relationships | Private security configuration | Revisioned; changes rotate account generation and recheck running work |
| `agent_directory` | App; canonical agent ID and owner index | Private identity metadata | Add after validated agent storage; check each referenced agent file |
| `agents`, `agent_connections`, `agent_prompts` | Agent; canonical configuration | Private | Revisioned agent lifecycle; nondefault agents use separate validated files |
| `prompt_slots` | Agent; legacy compatibility source | Private | Retain for rollback; active reads use `agent_prompts` |
| `chat` | User; canonical accepted publication | Private content | Append once per accepted message |
| `interactions` | User; canonical private execution history | Private content | Append by run step; retain for replay |
| `run_index` | User; projection of run documents | Private metadata | Update with run; rebuild or verify against it |
| `events` | App or user; operational event stream | Redacted metadata | Append; bounded retention |
| `legacy_event_map` | App or user; migration mapping | Private metadata | Preserve through rollback window |
| `docs`: `session:`, `session_mode:`, `session_ability_state:`, `run:`, `run_view:`, `attachment:`, `promotion:`, `receipt:session:`, `receipt:message:`, `receipt:run:`, `receipt:approval:` | User; canonical state and idempotency receipts | Private | Revisioned or immutable by key; user ownership check |
| `docs`: `agent:`, `agent_abilities:` | Agent; compatibility/configuration document | Private | Migrate to typed agent tables when replaced |
| `docs`: all remaining keys, including `workflow_policy`, provider tokens, shutoff, auth, model catalog, `storage:split_migrated`, `storage:access_v1`, and `storage:agent_split_v1` | App; canonical policy, secret, control, or migration marker | Security sensitive or secret | Revisioned, revoked, or versioned by family |

The `docs` routing is implemented in `Store::pool_for`; new prefixes must be
declared here and routed deliberately. A key without a declared owner must not
be treated as user or agent state merely because the generic app fallback can
store it.

The table declares ownership for both implemented and planned records. Confirm
runtime wiring and migration markers before treating newly added accounts, tiers,
or agent stores as live capabilities. Inline text attachments now use encrypted
`attachment:` user documents and a same-transaction interaction copy. They are
scoped to the owning session, retained with its user history, and never treated
as trusted instructions. Binary attachments, memories, and optional modules
remain planned. For any new table or document, add its owner, authority,
sensitivity, retention, and cross-plane checks here before migration.

## Migration and rollback contract

Migrations are numbered and idempotent. Stage new schema without deleting the
old representation; copy and validate record IDs, ordering, encryption, counts,
and references; then write a completion marker only after all records verify.
Retry must detect conflicting destinations instead of overwriting them. Back
up the whole v3 data directory, including its key, before a deployment that
migrates live state. A compatible rollback may read the preserved old records;
an incompatible rollback requires restoring that whole backup. Never copy v2
accounts, tokens, credentials, keys, or session data into v3 as part of this
phase. Existing split-storage migration uses `storage:split_migrated` version 2;
future schema changes need their own versioned marker and tested retry path.

The Phase 3 agent split keeps `agent_data/shared_default/shared_default.db` as the
default agent store and preserves legacy nondefault agent rows there as rollback
material. The additive `storage:agent_split_v1` marker binds a digest of those
source rows; an initial copy or interrupted retry must match every destination
record before the marker is written. After completion, each nondefault agent's
database is authoritative. Later configuration edits go only to that database,
so the preserved legacy rows become stale and cannot provide a current rollback.
Rolling back after such edits requires restoring a whole-v3-data backup captured
at a compatible point or explicitly reconciling agent changes. The runtime must
fail on changed legacy source rows, missing agent databases, ownership conflicts,
foreign rows, or malformed encrypted records rather than silently choosing one
copy. This migration never opens or copies user session databases.

## Acceptance evidence

Phase 0: review this ownership map against every new record and verify the
versioned migration, conflict rejection, and encrypted reopen tests. Phase 1:
run locked Rust tests for checkpoint contracts, live policy validation,
context lifetimes, publication idempotency, approval invalidation, cancellation,
shutoff, and uncertain-write recovery. Then deploy the tested image to
`webagent-dev-v3` and exercise authenticated chat, read tool, approval gate,
reload, and recovery in the v3 development UI. A healthy container or unit-test
pass alone does not meet the Phase 1 exit condition.
