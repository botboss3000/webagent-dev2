# V3 ability port ledger

Source audit: 2026-10-02. This ledger records source wiring, not deployment or API acceptance. No tests, Cargo commands, or live journeys were run for this audit. Runtime databases were not opened.

## Inventory and evidence rules

The original audit catalog had **82 manifests: 2 ready, 68 pending, and 12 placeholders**. The current source has **10 ready, 1 partial, 60 pending, and 12 placeholders**. All eight selected Administrator coding abilities are ready after authenticated API acceptance; Base remains partial. Computer Control and Program Screenshot are explicitly excluded by the user. Administrator includes the newer v3_build and v3_workspace declarations. The counts are derived from each actual `plugins/abilities/<group>/<id>/<id>.json`, not from the presence of a Python module.

V2 references below are relative to the read-only `v3/v2_ref/` snapshot. A module or stable descriptor establishes a source implementation, not that its inherited contract is safe in V3. Some small Commerce Python modules are adapter markers into `app/integrations/`; they are not placeholder implementations. OAuth identity connectors and installation services are distinct from executable model tools.

Each table row is a per-ability readiness decision. `pending` means that its declared supported V2 behavior still needs a V3 execution or configuration contract and API acceptance; it must not be promoted by changing JSON alone. The 12 product placeholders stay placeholders until separately specified. `ready` is the current manifest value, and still needs caller-specific access and configuration resolution.

All pending executable rows share these missing acceptance requirements: compiled adapter and bounded schema ownership, V3 authority/configuration storage, credential references when needed, cancellation and explicit error classification, approval/no-replay recovery, policy-matrix tests, and a real authenticated V3 API execution test. Skill counts show assets shipped now; their presence does not establish valid V3 guidance. Imported V2 skills must be reviewed for obsolete routes, ownership, credentials, and permissions.

Authenticated acceptance on 2026-10-03 exercised each selected Administrator ability through V3 APIs. The locked image build passed 320 Rust tests (2 ignored); focused worker tests, real Chromium fixture and frontend syntax checks passed. The API checks verified one persisted assistant reply per successful run, account and agent policy boundaries, protected build input digests, healthy V3 containers and readiness. No UI journey was run. The isolated source passed fixed `cargo check --locked` and `cargo test --locked` after the development API memory limit was raised to 2 GiB and Cargo output was bounded to its final 8 KiB.

## Shared V3 seams

- Catalog authority: `src/abilities.rs`. It bounds manifests/skills, rejects duplicate installed tool ownership, and checks directory/manifest presence on use. Ready and partial ability/virtual_core entries expose only compiled tools; oauth/channel/service currently remain metadata. Contents refresh requires restart. Removal immediately withdraws presence; reconfiguration must also be enforced live.
- Registered schema/read classification: `src/agent_loop/tools/mod.rs`. Native/worker dispatch: `src/agent_loop/tools/dispatch.rs`. A manifest cannot install a schema or register worker execution.
- Ready Codebase Admin: native prompt slots, state inspection, and promotion proposal; bounded filesystem/shell operations delegated to the isolated worker. Native implementations are in `src/api/sessions.rs` and `src/promotion.rs`. A worker is never a policy authority.
- Ready Web Access: compiled `plugins/abilities/Web/web_access/adapter.rs`; public web search/geocoding/weather with no credential import.
- Discovery and activation: `src/agent_loop/run.rs`, `src/context/assembly.rs`, `src/api/sessions.rs`. Bootstrap list_abilities/load_ability are loop-native today; their declarations in pending orchestration/base entries must not accidentally duplicate registered ownership.
- Tenant access: `src/access/policy.rs`, `src/storage/access.rs`; ability-ID account grants and agent-owned membership capability levels now use a shared live calculation. Tenant ability administration flows through `src/api/ability_admin.rs`; account-tier ceilings remain installation-scoped.
- Existing integral infrastructure is narrower than V2 parity: `src/context/compaction.rs` is automatic compaction; `src/context/memory.rs` is explicit per-session bounded notes; `src/attachments.rs` handles inline text attachments; `src/agent_loop/scheduler.rs` is queued-run lease scheduling. None establishes the similarly named full V2 ability/service.
- Memory API writes in `src/api/chat.rs` require a completed source run in the same session. A future memory tool must preserve provenance and publication ordering; do not directly call memory_save with the currently executing run.
- Text attachment API reads in `src/api/chat.rs` and `src/api/user.rs` check owning session/user routing. A read_attachment tool can reuse this check; binary/image processing remains separate.

## Per-ability decisions

Paths named `plugin <id>.py` refer to the owning V2 folder matching the ID and group in this table. Every row has its actual V3 manifest at `plugins/abilities/<group>/<id>/<id>.json`.

| Ability | V2 source/maturity | V3 decision and execution gap | Configuration/credential needs | Tools / shipped skills |
| --- | --- | --- | --- | --- |
| Administrator/code_index | plugin code_index.py; source handlers/adapter | ready; bounded workspace index adapter passed API scan, lookup, coverage, trace and progress | Derived workspace index; no external credentials | 5 declared / 6 skills |
| Administrator/codebase_admin | plugin codebase_admin.py; source handlers/adapter | ready; compiled runtime present, access/tenant tests still required | No external secret indicated; scoped settings/runtime authority | 14 declared / 33 skills |
| Administrator/computer_control | plugin computer_control.py; source handlers/adapter | pending; explicitly excluded by user | No device connector installed; no port attempted | 7 declared / 8 skills |
| Administrator/diagnostics | plugin diagnostics.py; source handlers/adapter | ready; current-run diagnostics and private fixed-origin browser passed API calls and artifact ownership | Private current-caller browser authorization; protected PNG artifacts | 5 declared / 7 skills |
| Administrator/git_control | plugin git_control.py; source handlers/adapter | ready; scoped Git push, credential policy and conflict resolution exercised through V3 APIs | Agent-owned sealed HTTPS connection/token; revisioned notify setting | 4 declared / 5 skills |
| Administrator/program_screenshot | plugin program_screenshot.py; source handlers/adapter | pending; explicitly excluded by user | Not installed | 1 declared / 2 skills |
| Administrator/remote_tunnel | plugin remote_tunnel.py; source handlers/adapter | ready; owned preview lease, unowned-port denial and expiry passed API checks | Session-owned server port and separate quicktunnel connector | 2 declared / 3 skills |
| Administrator/terminal_control | plugin terminal_control.py; source handlers/adapter | ready; supervised PTY input/output and revocation closure passed API checks | Session workspace, assigned ports and revisioned caution setting | 6 declared / 7 skills |
| Administrator/v3_build | plugin v3_build.py; source handlers/adapter | ready; protected image build, immutable inputs, durable job and rollback deployment verified | Installation-administrator host authority; durable job/lease receipts | 6 declared / 7 skills |
| Administrator/v3_workspace | plugin v3_workspace.py; source handlers/adapter | ready; seed, checked patch, proposal and fixed Cargo check/test passed API checks | Session-owned snapshot and pinned review digest | 3 declared / 4 skills |
| CRM/hubspot | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| CRM/mailchimp | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| CRM/salesforce | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Commerce/amazon | app/integrations/marketplace_tools.py | pending; no registered V3 adapter/configuration path | seller SP-API/OAuth | 4 declared / 0 skills |
| Commerce/bigcommerce | app/integrations/commerce_bigcommerce.py | pending; no registered V3 adapter/configuration path | agent store_hash/access_token | 4 declared / 0 skills |
| Commerce/btcpay | app/integrations/commerce_btcpay.py | pending; no registered V3 adapter/configuration path | agent server_url/store_id/api_key | 3 declared / 0 skills |
| Commerce/ebay | app/integrations/marketplace_tools.py | pending; no registered V3 adapter/configuration path | developer OAuth | 6 declared / 0 skills |
| Commerce/etsy | app/integrations/marketplace_tools.py | pending; no registered V3 adapter/configuration path | reviewed app OAuth | 4 declared / 0 skills |
| Commerce/paypal | app/integrations/commerce_paypal.py | pending; no registered V3 adapter/configuration path | agent client_id/client_secret + sandbox/live | 3 declared / 0 skills |
| Commerce/shopify | app/integrations/marketplace_tools.py | pending; no registered V3 adapter/configuration path | store app OAuth | 4 declared / 0 skills |
| Commerce/square | app/integrations/commerce_square.py | pending; no registered V3 adapter/configuration path | agent access_token/location_id + sandbox/live | 4 declared / 0 skills |
| Commerce/stripe | app/integrations/commerce_stripe.py | pending; no registered V3 adapter/configuration path | agent secret_key | 3 declared / 0 skills |
| Commerce/woocommerce | app/integrations/commerce_woocommerce.py | pending; no registered V3 adapter/configuration path | agent base_url/consumer_key/consumer_secret | 4 declared / 0 skills |
| Communication/discord | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Communication/email | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Communication/slack | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Communication/telegram | channel/bot delivery integration | pending; no registered V3 adapter/configuration path | agent bot token; delivery destination | 0 declared / 0 skills |
| Communication/twilio | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Communication/whatsapp | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Core/agent_management | plugin agent_management.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority; 7 declared settings | 15 declared / 0 skills |
| Core/agent_orchestration | plugin agent_orchestration.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority; 7 declared settings | 15 declared / 0 skills |
| Core/app_control | plugin app_control.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority; 3 declared settings | 1 declared / 0 skills |
| Core/automation | plugin automation.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority; 6 declared settings | 14 declared / 0 skills |
| Core/base | app/tools/core_tools.py; app/tools/loader.py | partial; 20 compiled tools include session mode, lexical private search, compaction/recall, self-edits, checkpoints and declarative presentation; 341 pinned Rust tests passed and authenticated API runs exercised checkpoint and cross-session search tools | Session and agent owner fences, CAS revisions, human approval for write-mode elevation and manager self-edits; remaining tools have focused Rust coverage but not separate live model runs | 31 declared / 0 skills |
| Core/create_tools | plugin create_tools.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority | 1 declared / 0 skills |
| Core/decision | Native TypeSafe System One adapter and directory scorer | ready in source; Rust resolves the assigned Decision role and returns typed answers or scores up to 32 directory entry names per page | Agent needs a permitted TypeSafe Decision model and V3 API key; directory scoring also requires automatic file_list permission and uses names only | 2 declared / 3 skills |
| Core/image_generation | plugin image_generation.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | provider image credential/endpoint | 1 declared / 0 skills |
| Core/image_vision | plugin image_vision.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | vision provider credential/endpoint | 1 declared / 0 skills |
| Core/model_switcher | plugin model_switcher.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority; 1 declared settings | 5 declared / 0 skills |
| Core/p2p | plugin p2p.py; app/p2p/ | pending; no registered V3 adapter/configuration path | peer identity/keys and sync transport | 4 declared / 0 skills |
| Core/ui_admin | plugin ui_admin.py; plugins/admin/ui_tools.py | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority | 7 declared / 0 skills |
| Core/user_files | plugin user_files.py; app/user_workspace.py | pending; no registered V3 adapter/configuration path | No external secret indicated; scoped settings/runtime authority | 4 declared / 0 skills |
| Core/visualizer | plugin visualizer.py; app/visualizer/ | pending; no registered V3 adapter/configuration path | user vault + artifact service; 1 declared settings | 16 declared / 0 skills |
| Developer/github | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Developer/gitlab | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Developer/jira_linear | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Devices/run_on_device | plugin run_on_device.py; app/devices/ | pending; no registered V3 adapter/configuration path | device identity/shared-store transport | 2 declared / 0 skills |
| Memory/context_control | plugin context_control.py; app/agent/context_control.py; app/agent/compaction.py | pending; automatic Rust compaction exists; config/manual tool unported | No external secret indicated; scoped settings/runtime authority; 10 declared settings | 0 declared / 0 skills |
| Memory/document_libraries | plugin document_libraries.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | user OAuth + approved folders; 3 declared settings | 3 declared / 0 skills |
| Memory/managed_knowledge_files | plugin managed_knowledge_files.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | agent-owned encrypted files + recall config; 3 declared settings | 3 declared / 0 skills |
| Memory/memory | app/tools/core_tools.py; app/agent/memory_dedup.py | pending; session-note APIs exist; no memory tool / cross-session recall | none; user/session note storage; 2 declared settings | 1 declared / 0 skills |
| Memory/sql_databases | plugin sql_databases.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | agent approved PostgreSQL connection + table grants; 3 declared settings | 4 declared / 0 skills |
| Memory/task_progress | configuration shell task_progress.py; app/agent/task_progress_config.py; app/agent/task_state.py | pending; no task checkpoint tools or v2 task state policy | No external secret indicated; scoped settings/runtime authority; 2 declared settings | 0 declared / 0 skills |
| Memory/wiki_context | plugin wiki_context.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | native wiki storage; owner ACL | 11 declared / 0 skills |
| Memory/workspaces_wikis | plugin workspaces_wikis.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | workspace connector + approved sources; 3 declared settings | 3 declared / 0 skills |
| Payments/bank_accounts | Product placeholder; no executable contract | placeholder; intentionally unavailable | Separate product specification required | 0 declared / 0 skills |
| Productivity/dropbox | OAuth connector; document_libraries.py | pending; no registered V3 adapter/configuration path | user OAuth scopes | 0 declared / 0 skills |
| Productivity/google | app/integrations/email_tools.py, calendar_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth scopes | 0 declared / 0 skills |
| Productivity/microsoft | app/integrations/email_tools.py, calendar_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth scopes | 0 declared / 0 skills |
| Productivity/yahoo | OAuth identity only | pending; no registered V3 adapter/configuration path | user OAuth identity | 0 declared / 0 skills |
| Social/linkedin | app/integrations/social_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth + provider approval | 0 declared / 0 skills |
| Social/meta | app/integrations/social_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth + provider approval | 0 declared / 0 skills |
| Social/pinterest | app/integrations/social_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth + provider approval | 0 declared / 0 skills |
| Social/reddit | app/integrations/social_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth | 0 declared / 0 skills |
| Social/snapchat | OAuth identity/Bitmoji only | pending; no registered V3 adapter/configuration path | user OAuth identity | 0 declared / 0 skills |
| Social/tiktok | app/integrations/social_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth + provider approval | 0 declared / 0 skills |
| Social/twitch | app/integrations/social_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth | 0 declared / 0 skills |
| Social/twitter | app/integrations/social_tools.py; OAuth | pending; no registered V3 adapter/configuration path | user OAuth + paid entitlement | 0 declared / 0 skills |
| System/boot_orphan_resume | app/agent/runner.py (resume_orphans_at_boot) | pending; Rust run recovery exists; v2 service config unported | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| System/device_worker | app/devices/ | pending; isolated local worker is not multi-device dispatch | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| System/diagnostics_recorder | app/agent/diagnostics.py | pending; bounded Rust receipts exist; v2 recorder controls unported | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| System/event_runtime | app/events/ | pending; no persisted subscription/delivery runtime | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| System/hybrid_sync | app/db/sync/ | pending; foreign shared-DB sync not in current V3 storage contract | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| System/message_length_cap | app/chat/execution.py | pending; Rust workflow size checks exist; v2 truncate policy unported | No external secret indicated; scoped settings/runtime authority; 1 declared settings | 0 declared / 0 skills |
| System/run_manager | app/agent/manager.py | pending; Rust gate protocols exist; v2 manager behavior unported | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| System/scheduler | app/scheduler/ | pending; Rust queue leases exist; scheduled jobs unported | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| System/task_grouping | app/admin/tasks.py | pending; no v2 task classification/evidence policy | No external secret indicated; scoped settings/runtime authority; 4 declared settings | 0 declared / 0 skills |
| System/watchdog | app/agent/watchdog.py | pending; Rust cancellation/leases exist; v2 watchdog service unported | No external secret indicated; scoped settings/runtime authority | 0 declared / 0 skills |
| Web/browser_control | plugin browser_control.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | user encrypted cookie/login vault + browser backend | 9 declared / 0 skills |
| Web/host_management | plugin host_management.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | provider-scoped DNS credentials | 4 declared / 0 skills |
| Web/ssh_control | plugin ssh_control.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | user-owned external SSH connections/keys | 9 declared / 0 skills |
| Web/web_access | plugin web_access.py; source handlers/adapter | ready; compiled runtime present, access/tenant tests still required | No external secret indicated; scoped settings/runtime authority | 3 declared / 4 skills |
| Web/web_scraper | plugin web_scraper.py; source handlers/adapter | pending; no registered V3 adapter/configuration path | scraper api_key/provider/endpoint; 1 declared settings | 2 declared / 0 skills |

## Ordered implementation slices

### 1. Access and administration foundation

Define migration before exposure: retain the two current ready abilities for existing intended users, use explicit account-tier ability-ID grants, and deny newly introduced IDs by default. Agent base policy and membership capability levels are separate; owner/delegated administrator authority never follows from a capability grant. Seed visitor/member/administrator levels with explicit revisioned policies. Owners retain immutable ownership except explicit transfer.

One Rust resolver must return configured and effective decisions with reasons across installation policy, account tier, agent base policy, membership level, readiness/configuration, session mode, and activation/suppression. Visibility grants no execution. Preserve deny, require_approval, and allow. Include relevant revisions in context fingerprints and diagnostics; edits quiesce/recheck affected work, including previously approved and recovered dispatch.

Expose selected-agent management to owner/delegated administrators, sanitized reads to members, and global tier edits only to installation administrators. Keep CSRF, optimistic conflicts, audit receipts, private tenant projections, and agent-before-store routing. Existing installation oversight must not expose another tenant's credentials or silently grant ownership.

Acceptance: two users/two agents/two levels; account ceiling beats agent grant; delegated admin cannot raise their ceiling or cross tenant; deny removes discovery, load, schema and execution; stale writes reject; revocation after approval and restart rejects dispatch.

### 2. Native Core and narrow session knowledge

First ports with the smallest authority footprint: get_time/get_date with bounded timezone validation, calculate with a bounded parser (no eval/process execution), read_attachment with exact current-session ownership, and safe current-session search/read projections. Reuse prompt slot/state inspection boundaries rather than creating second mutable stores. Do not reuse V2's unrestricted session search without caller ownership filtering.

Session memory has a tested-source foundation but differs from V2 long-term recall/automatic note saving. Implement a clearly versioned narrow session-note tool contract before claiming full memory parity. Manual compaction requires a durable loop checkpoint request at a safe transaction boundary, not direct mutation while tool output is incomplete. Task progress needs its own fenced revisioned state and provenance before exposing checkpoint writes.

Agent management can reuse existing directory/membership authority for listing and scoped configuration, but session creation/watch, agent deletion, templates, and skill mutation need individual V3 contracts. Keep model_switcher/model controls explicitly outside this work's implementation scope until a separately authorized workflow specification.

### 3. Integral orchestration, automation, and services

Orchestration requires durable parent/child relationships, explicit independent user/agent admission, capacity/depth/budget limits, cancellation propagation, result delivery exactly once, and approval/recovery semantics. Existing tokio run spawning is infrastructure, not spawn_agent parity.

Automation requires durable scheduled/event records, ownership, revision/idempotency, delivery-channel authorization, exactly-once terminal publication, and no automatic replay of uncertain deliveries. V3 queue lease scheduler must remain distinct from timed jobs and event subscriptions.

System services need a versioned installation configuration contract and operational tests rather than artificial empty tool schemas. Map recovery, telemetry, run gates, watchdog, and message limits individually; existing native safety constraints remain mandatory when optional service configuration is off. Device worker, hybrid sync, and P2P depend on a new secure multi-instance protocol; full V2 shared-database routing is outside this handoff and cannot be enabled by metadata.

### 4. Administrator and Web worker/adapter slices

Workspace-scoped Git is a feasible worker slice after source_review/source_patch reuse; remote push adds credentials and irreversible-effect reconciliation. Code Index requires a workspace-scoped index service and authoritative coverage, not a fake scan success. V3 build/workspace tools require a separate audited broker; the model and low-trust worker must not acquire Docker sockets or direct live-checkout write authority.

Diagnostics must return bounded redacted metadata and current-user development auth through separate services. Browser/computer/terminal/screenshot/tunnel ports require their real backend, lifecycle, session isolation, cancellation, and credentials; inherited Windows pointer behavior is not Linux support. Browser credentials must never enter model-visible tool output. SSH is external-device-only and requires connection ownership, host-key verification, bounded jobs, and uncertain command reconciliation.

Scraper and DNS ports require encrypted scoped connection records, allowlisted or validated outbound targets, provider-specific schemas/errors, and approval boundaries for writes. The ready public Web Access adapter is not an arbitrary HTTP/browser adapter.

### 5. Knowledge, files, artifacts, and external providers

User Files needs encrypted user-owned artifact storage and authenticated download ownership. Managed knowledge files are agent-owned, while document libraries use user-owned OAuth sources; keep these planes distinct. SQL requires approved connection/table scope and a read-only query boundary. Wiki and workspace connectors require their actual own ACL/history/reference contracts. Visualizer/custom tools need reviewed artifact/adaptor installation and credential isolation; arbitrary plugin code is never policy.

External connector slices: agent-scoped Commerce credentials; user-owned productivity/social OAuth; Telegram agent bot credentials and authorized destinations. Source-backed shared integration modules need V3 equivalents and bounded provider tests; a connection badge is not execution readiness. Yahoo and Snapchat identity-only source contracts must not advertise unsupported mail/posting. Preserve all CRM/Developer/Payments/Communication placeholders.

### 6. UI, API acceptance, and deployment

The selected agent owns the Abilities page. Show declaration, installation, configuration, caller availability, and tested readiness separately; never collapse them into a green catalog badge. Provide base policy, capability-level selector, user/level effective preview with reasons, owner-scoped settings/connections, installation-tier ceilings, read-only member views, stale-save handling, search and grouping. Custom abilities become configurable only after a real reviewed registration backend exists.

The user requested **API-only acceptance; no browser journeys**. Validate saved page API responses across two agents after reload-equivalent rereads, and report the resulting rendered-UI verification gap. Add tests for all resolver paths, missing/disabled plugins, credential absence/rotation, approval revocation, tier change during work, session suppression, recovery, and exactly one persisted terminal reply for each accepted agent run.

Coordinate writers and Cargo targets, capture the full allowed build context before/after, and reject raced evidence. Build/deploy affected API/loop/worker images together where registry/policy contracts require it, retaining stopped rollback containers and encrypted runtime data. Readiness/health alone do not establish ability parity.

## Completion evidence to maintain

For each completed row, record V3 adapter/owner, configuration and encrypted credential record version, schema/skill ownership, policy coverage, tests, authenticated API acceptance ID/result, image input digest, and deployment identity. Do not replace pending with ready without that evidence. Source audits and unit tests may justify a port plan; only the real implemented configuration/execution path plus required acceptance justify readiness.

## Implementation progress after the audit

The tenant policy and administration slice was deployed on 2026-10-02. Fresh pinned-image tests: 265 passed, 2 ignored. Authenticated API checks exercised distinct owners, delegated administration, member restrictions, different capability levels across users/agents, sanitized views, stale revisions, CSRF, and two bounded state_inspect runs with exactly one persisted assistant reply each. Live capability revocation stopped a waiting approval and rejected the obsolete approval with HTTP 409. The stopped API and loop containers were retained for rollback. No browser journeys were run, as requested.

The next Core slice implements UTC/fixed-offset get_time/get_date, bounded finite calculate, current-session text read_attachment, and literal search_this_session. Base is explicitly partial; unsupported declarations remain unavailable. Account-tier and membership defaults still grant only the two original ready abilities, so Base needs explicit grants. These five tools were deployed and passed bounded authenticated Luna API acceptance on 2026-10-02, with exactly one persisted assistant reply per run. Fresh pinned-image tests passed 277 tests (2 ignored); both images were built on 330 unchanged allowlisted inputs, and API/loop/readiness were healthy after replacement. The original calculator schema exposed extreme numeric bounds and caused an upstream incomplete response even on a 7,529-byte request; removing those schema bounds resolved the same small addition while Rust retained finite/magnitude checks. The completed Core API checks measured at most 15,526 logical JSON bytes, well below the requested 128,000 context ceiling. These are byte measurements, not provider token counts. Own-session attachment reads, cross-session 404, suppression withdrawal and stale session revisions were also verified. Evidence is recorded in target/core-api-acceptance.json and target/calculator-schema-build-evidence.json. Full Base parity, other pending abilities, custom ability configuration, and credential-backed integrations remain open.

The next deployed Base slice adds list_skills, load_skill and read_own_prompt,
bringing the partial port to eight compiled tools. Bundled skill reads use exact
IDs, bounded assets, current target permissions and live bundle fingerprints.
Loading grants no activation. Revoked native guidance is masked from derived
recovery context while the original interaction journal remains intact; summary
projection omits it before compaction. Dispatch rechecks fingerprints after
credential retrieval and first verifies run ownership/status. Prompt reads bind
to the caller-owned current session/agent, hide internal slots, retain slot
revisions and reject serialized outputs above 64 KiB.

Fresh pinned tests passed 292 tests (2 ignored). API and loop images were built
on 337 unchanged allowlisted inputs and replaced on 2026-10-02. Previous
containers were renamed and stopped during replacement. The final Docker check
found both those containers and their old image IDs absent; the cause is not
established. An older rollback archive exists but was not opened and is not
verified as a substitute. Rollback preservation requires coordination before
another deployment. Six authenticated Luna runs
exercised skill listing/loading, stale catalog versions, denied target skills,
prompt reads and arithmetic with all eight schemas. Each completed with exactly
one persisted assistant reply and passing tool checkpoints. The largest measured
logical envelope was 21,011 JSON bytes. Diagnostics confirmed the conservative
119,808-byte Luna input ceiling below the requested 128,000 context limit; these
are byte proxies, not provider token counts or an upstream output-limit setting.
Session suppression withdrew Base, stale session writes returned 409, and an
ordinary non-owner's attempt to read another agent's prompt returned 403.
Installation administration retains explicit oversight without changing tenant
ownership; that is distinct from ordinary non-owner access. Both deployed worker
UID/peer-isolation checks passed and API/loop/gate/readiness were healthy.
Evidence: target/skills-final-tests-evidence.json, target/skills-build-evidence.json,
target/skills-deployment.json, target/skills-api-acceptance.json and
target/skills-worker-identity.json. Validation used authenticated APIs, not browser
journeys. Full Base parity and the remaining pending ability ports remain open.

## Administrator coding port — 2026-10-03

Source implementations now exist for all eight requested Administrator abilities,
including the newer V2 `repository_access` metadata tool. Computer Control and Program
Screenshot are excluded by explicit user instruction. New manifests are partial while
API acceptance is pending; no catalog-only readiness claim is made. See
[Administrator coding](administrator-coding.md) for tool/configuration paths.

Focused Python adapter tests cover index freshness, Git HEAD/review and path-race
fences, scoped HTTPS credential transport, interactive PTYs and cancellation,
sandboxed seeding, fixed Cargo tasks, browser protocol boundaries, preview proxy
authentication, and durable host job revisions/revocation. Real authenticated API
acceptance, Chromium fixture validation, stable full image inputs and deployment
health are still outstanding. No UI journeys are required by the user.
