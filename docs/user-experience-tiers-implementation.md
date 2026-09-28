# User Experience Tiers and Model Rosters

This is the implementation and operations record for the entitlement system.
Server decisions are authoritative. Capability documents and browser catalogs
explain those decisions, but never replace enforcement.

## Sources of truth

| Concern | Authoritative source | Notes |
| --- | --- | --- |
| Policy schema and composition | `app/entitlements/policy.py` | Valid fields, limit units, fail-restrictive composition, and the minimal Anonymous emergency fallback. |
| Shipped system tiers | `app/defaults/experience-tiers/*.json` | Inserted only when missing; startup never overwrites an operator-owned row. |
| Live tiers | `experience_tiers` + `experience_tier_revisions` | The container holds a working draft and a pointer to an immutable published revision. |
| Shipped roster shapes | `app/defaults/model-rosters/*.json` | Deliberately contain no credentials. |
| Live rosters | `model_rosters` + `model_roster_revisions` | Draft edits do not alter the published snapshot; publication and rollback are atomic. |
| Platform credentials | encrypted app vault, keyed by roster and stable entry ID | Credentials never appear in roster JSON, capabilities, history, diffs, or audit payloads. |
| User BYO credentials | encrypted per-user LLM vault row | Subject to tier permission, entry quota, media-feature checks, and URL/schema validation. |
| Tier assignments | `user_tier_assignments` | Overlapping manual, billing, import, default, and system sources remain individually auditable. |
| Pages | page descriptors plus installation visibility configuration | Tier access is intersected with installed/operator-enabled pages. |
| Ability classification | ability/group descriptors | Unknown or invalid classifications default to `platform_admin`; the Python map is compatibility-only. |
| Agent templates | JSON manifests synchronized to the app plane | Admin-owned template configuration is not overwritten by later seed refreshes. |

## Evaluation order

The effective result is composed in this order:

1. Code-level invariants and schema validation.
2. Installation/operator enablement, registration mode, and admin role.
3. The caller's active published tier assignment.
4. The tier's referenced published model roster.
5. An allowed user BYO configuration, if present.
6. An agent-level configuration override.
7. A session-level stable slot or model override.
8. A final runtime entitlement clamp immediately before execution.
9. Existing membership, billing, wallet, subscription, trial, and exemption checks.

An administrator role is an overlay, not a product tier. Demoting an administrator
does not silently change their tier assignment. Missing, malformed, unpublished,
retired, or expired policy data never grants a richer fallback.

## Policy schema and units

A tier contains pages, backend features, descriptor-defined ability groups,
agent-template IDs, a roster/model policy, and non-negative limits (`null` means
unlimited). Limit definitions and enforcement boundaries are returned in the
capability schema. Current units are owned agents, active automations, enabled
connections, running sessions, messages per time window, seconds, bytes per
attachment, and bytes per account.

Adding a limit requires defining its unit and authoritative mutation/execution
boundary before adding it to a policy.

## Publication workflow

Settings → Entitlements provides roster and tier editors:

1. Edit the working draft.
2. Validate it.
3. Review the diff and affected-tier/user impact.
4. Publish with the expected draft revision.
5. Inspect immutable history.
6. Roll back by republishing an older snapshot as a new revision, or retire the
   resource while retaining history.

Optimistic revision checks prevent silent administrator overwrite. Roster
credentials are write-only and managed per stable entry ID. Assignment changes
require a reason and may have start/expiry times; Users shows source, dates,
reason, actor, and history.

## Enforcement boundaries

- Page catalogs are caller-aware, while sensitive routes also enforce direct
  request and deep-link decisions.
- Agent creation, restoration, template materialization, orchestration tools,
  and management tools share template/feature/quota enforcement.
- Connection REST writes, tools, and OAuth callbacks share create-versus-update
  quota and ability-group decisions.
- Automation API, sync, tools, restore, scheduler, and event execution re-check
  the current tier; demotion stops already-scheduled work.
- Runtime tools and prompt-advertised skills use the same descriptor group
  filter. Dynamic tools and data-source tools are classified at injection.
  Filter failures restrict to core chat.
- Chat admission applies the tier message window and the per-user/global
  concurrency minimum. Browser-authority sends use the same gates and release
  leases in `finally` cleanup.
- Model options are clamped before display and after all runtime overrides.
  Custom selections and effort keys use `entry:<stable-id>`; legacy positional
  session data remains readable.
- BYO only changes billing behavior when the effective tier permits BYO.
- Wiki, browser, GenUI, transcription, uploads, attachments, and alternate entry
  points verify caller identity and the relevant feature/page decision.

## Capabilities and caching

`GET /api/v1/entitlements/me` is secret-free for authenticated and Anonymous
callers. It includes the effective tier/assignment, allow/deny reasons, safe model
metadata and credential state, limit definitions, and a composite evaluation
revision. `/boot` primes it with the caller-aware page catalog.

Server caching is identity-scoped, short-lived, invalidated by admin mutations,
and bounded by assignment expiry. Browser page caching is identity-scoped,
revision-tagged, and time-bounded, so stale revoked access is not painted
indefinitely after a fetch failure.

## Migration and compatibility

Startup imports missing shipped tiers/rosters without overwriting operator data.
Legacy mutable publications are backfilled into immutable history in monolithic
and app-plane SQLite layouts. Legacy provider configuration remains readable and
may seed named rosters, but cannot overwrite an admin-owned named roster.

Bootstrap bundles carry named rosters and tiers, validate versions/references,
and retain version-1 LLM-only compatibility. Secret-bearing exports remain inside
the setup-code encrypted bundle; use stripped exports for diagnostics or source
control.

## Extending safely

- New tier: add JSON only for a shipped default, or create a draft in Settings;
  validate, preview, then publish.
- New roster: use stable entry IDs, write credentials through the vault endpoint,
  publish, then reference it from a tier.
- New page: add its descriptor/backend capability contract and protect the
  sensitive router/service boundary with the shared page decision.
- New ability: declare entitlement group, risk/action class, and required
  integration in its descriptor or group descriptor.
- New template: add a stable manifest ID; keep operator edits admin-owned.
- New quota: document units, implement an atomic check at every path, and add
  denial, concurrency, and demotion tests before exposing it in policies.

## Explicit operational limits

Message windows and concurrency leases are process-local. Multi-worker deployments
need shared atomic counters/leases with expiry and crash recovery. Cross-backend
storage quota reservation is not yet atomic, so remote/concurrent uploads need a
shared reservation implementation for a hard bound. Voice transcription has
identity and feature enforcement, but no voice-minute billing/quota contract yet.
Installation page visibility remains operator configuration rather than revisioned
entitlement DB state. These are known boundaries, not distributed guarantees.
