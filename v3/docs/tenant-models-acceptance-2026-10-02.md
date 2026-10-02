# Tenant agents and per-agent models acceptance — 2026-10-02

<!-- TENANT-MODELS-ACCEPTANCE: evidence for ownership, explicit roles, migration, and deployed behavior. -->

Implemented immutable agent ownership, separately delegated agent administration,
existing-account membership assignment, tenant-scoped model catalogs, six explicit
roles, ordered role-local backups, and owner-controlled credential sharing.
Standard routes normal chat; System routes compaction. Premium, image and voice
appointments are configurable but their workflows remain unavailable.

## Verification

- Pinned Rust image build: 253 tests passed, 0 failed, 2 opt-in tests ignored.
- Both API and loop images built with 316 unchanged allowlisted input files.
  Input manifest SHA-256:
  `a40f78a7fbec713e7747232ab6a8408e1cb2915b35affb9c686a29fd78f8750e`.
- API image: `d59e13a5665f0be990cbc6e658087ee7175a3ba0ef7cefec0f07a59bd95b29e6`.
- Loop image: `6ce183e14e21629351b45ebd077f24e8648a66f7de60033ef2eec1b3a281012c`.
- API, loop and existing gate containers healthy; direct readiness returned true.
- Live legacy default migration retained its connected gpt-6-luna Standard model.
  System is deliberately not inferred from Standard.
- Two separate owners created agents and configured distinct Standard models.
  Cross-tenant reads/writes and shared-credential authorization attempts returned
  forbidden. Both saved role maps and ordered backups survived API reload.
- Each agent completed one bounded message with one persisted assistant reply.
  Reload preserved exactly one reply; publication diagnostics passed.
- Chrome Models-page reload verified both selected agents, their Standard/System
  appointments and ordered backups. Expanded connection row showed Connected.
- Browser acceptance found an oversight UI issue: configuration eligibility was
  conflated with the administrator's personal usage permission. The bind-mounted
  UI was corrected after the image builds, passed JavaScript syntax and diff
  checks, and was verified after browser reload. Runtime binaries did not change.
- Disposable owner accounts were disabled after acceptance. Configurations and
  session evidence remain available to installation oversight.

## Live fixtures

| Agent | Standard | Ordered Standard backups | Session | Run | Reply |
| --- | --- | --- | --- | --- | --- |
| agent-488a282c6746b96b3e3603bdd7c2f440 | gpt-6-luna | Validation Sol, Validation Luna Backup | 86284818-3f8a-4195-ada5-7a48ca5a8bf9 | f3137497-c8b6-46ed-9ec7-8f86556d5fb7 | TENANT_A_OK |
| agent-2738872b04c3f9abe36d5b20519e6dfc | gpt-6-sol | Validation Luna, Validation Luna Backup | 0f7e1d9f-5d38-437b-b0e3-e7b2d1d90878 | f8019292-86da-4a16-8895-411e4871325d | TENANT_B_OK |

Both System assignments use Validation Luna with Validation Luna Backup.
Model-entry IDs have prefix `tenant-20261002-174811-model-`.
Live acceptance used authenticated APIs, never direct session database inspection.

## Rollback material and verification limits

Stopped previous containers:
`webagent-dev-v3-pre-tenant-models-20261002-174411` and
`webagent-dev-v3-pre-loop-tenant-models-20261002-174411`.

Pre-migration whole-state backup:
`v3/rollback/tenant-models-20261002-174411.tar.gz` (root-only).

Clippy completed with existing warnings. Repository-wide formatting check fails
on broad existing formatting differences; no bulk reformat was applied. Code
Index scanning is unavailable for this v3 environment, so index freshness is
unverified. Compaction, pinned recovery, tier limits, stale revisions, credential
rotation, and revocation have focused automated coverage; the two live messages
were bounded normal-chat tests, not live fault-injection or compaction tests.
