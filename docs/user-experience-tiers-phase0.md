# User Experience Tiers — Phase 0 Product Policy

Status: proposed baseline for implementation  
Scope: app-wide user experience and entitlements  
Out of scope: database schema, resolver implementation, admin UI, payment pricing

## 1. Decisions

This specification adopts the following product decisions. They are defaults for
the implementation unless deliberately amended before Phase 1 begins.

1. Identity assurance, platform role, entitlement tier, and agent membership are
   independent axes. `channel_identities.user_tier` remains an identity-assurance
   field and is not reused for product entitlements.
2. The initial entitlement tiers are `anonymous`, `free`, and `pro`.
3. Administrator is a platform-role overlay, not a fourth entitlement tier. An
   administrator retains their underlying entitlement assignment for billing and
   reporting.
4. Entitlements are app-wide. Existing per-agent trials, subscriptions, credits,
   wallets, and exemptions remain a separate billing/access layer.
5. Product tier and usage payment are separate in v1. `pro` unlocks capabilities;
   it does not imply unlimited provider usage. Existing billing rules still decide
   whether a particular run is free, subscription-covered, trial-covered, or
   credit-funded.
6. A running turn uses a policy snapshot taken when the turn starts. A tier,
   roster, or role change applies to the next turn or HTTP operation; it does not
   mutate an in-flight LLM request.
7. The server is authoritative. The browser consumes a safe capability document
   for presentation but never decides access.
8. Missing assignment for a registered account resolves to `free`. Anonymous
   identities resolve to `anonymous` without a stored assignment. An explicit
   assignment to an unknown, malformed, expired, retired, or unpublished tier
   resolves to the restrictive `anonymous` policy and emits an audit event.
9. Platform roster credentials are never returned to a browser. User BYO APIs may
   report only the caller's own masked credential state.
10. A platform administrator may manage tiers and rosters and may use explicitly
    admin-only capabilities. The overlay does not silently grant a paid product
    subscription or change the user's stored tier.

## 2. Policy composition

The effective policy is the intersection of independent controls:

1. installation availability and safety controls;
2. entitlement tier;
3. platform-role overlay;
4. agent membership and the agent's configured policy;
5. page, ability, and tool configuration;
6. agent and session model selection, clamped to the effective model policy;
7. billing, wallet, trial, and subscription access;
8. current quota state.

Composition rules are deterministic:

- allow-lists intersect;
- deny-lists union;
- numeric maxima use the lowest applicable positive maximum;
- `null` means unlimited; zero means none/disabled;
- a feature is available only if every required non-admin layer permits it;
- admin bypasses are explicit per capability, never inferred from tier rank;
- a default resource must itself be allowed by the final policy;
- restrictions are checked both when an override is saved and when it is used.

Tiers are not numerically ordered for authorization. Code must ask for a named
capability instead of relying on comparisons such as `tier >= pro`.

## 3. Experience matrix

Values marked **provisional** are safe starting limits, not pricing commitments.
They should be validated against provider cost and expected deployment size before
the feature flag is enabled by default.

| Area | Anonymous | Free | Pro | Admin role overlay |
|---|---|---|---|---|
| Identity | `anon_*` guest | Registered and approved account | Registered account with active/manual Pro assignment | Verified `user_profiles.is_admin` |
| Main experience | Chat only | Chat, Agents, Browser, Wiki, Instances | All Free pages plus Automations and Gen UI | Admin Tools plus every installed page needed for operations |
| Agent templates | Shared/default public agent only | Default and non-admin discoverable templates | All non-admin discoverable templates | Admin-only templates and system agents |
| Agent creation | No | 1 owned/custom agent | 10 owned/custom agents **provisional** | Unlimited for operations |
| Agent cloning | No | Within the one-agent cap | Within the ten-agent cap | Unlimited for operations |
| Automations | No | No | 10 active automations **provisional** | Unlimited for operations |
| External connectors | Public agent-owned connections only | 2 user-scoped connections **provisional** | 25 user-scoped connections **provisional** | Platform connector/app configuration |
| Model roster | `roster-anonymous` | `roster-free` | `roster-pro` | Explicit admin roster override; underlying tier remains unchanged |
| Model picker | Hidden | Standard model plus allowed free alternatives | Full published Pro roster | Full admin roster |
| Reasoning effort | Fixed/default or low | Up to medium | Up to high | Up to roster/provider maximum |
| Image input | Off by default | On when the free roster has an approved vision entry | On | On when configured |
| Image generation | Off | Off | On when the Pro roster has an approved generator | On when configured |
| Voice input | Browser-native only | Browser-native; LLM voice only if explicitly enabled in Free policy | Browser-native and configured LLM voice | Configured platform maximum |
| File attachments | 5 MiB each, 10 MiB/session **provisional** | 25 MiB each, 100 MiB/account **provisional** | 100 MiB each, 5 GiB/account **provisional** | Installation maximum |
| Concurrent running sessions per user | 1 | 1 | 4 **provisional** | Installation maximum |
| New anonymous sessions | 20/IP/minute | Not applicable | Not applicable | Not applicable |
| Chat rate | 30 messages/identity/5 min and 90/IP/5 min | 60 messages/5 min **provisional** | 300 messages/5 min **provisional** | Safety ceiling only |
| Gen UI | No | No | Yes | Yes |
| User BYO database | No | No | Allowed when the installation enables User BYOD | Platform storage administration |
| User BYO LLM | No | One text model; still subject to Free feature/effort limits | Multiple models; still subject to Pro non-admin restrictions | Platform roster management and optional personal BYO |
| Upgrade prompts | Sign in | Upgrade to Pro | None for tier features | None for role-authorized features |

### Page IDs

The page policy uses stable catalog IDs, not labels or DOM selectors.

| Page ID | Anonymous | Free | Pro | Admin overlay |
|---|---:|---:|---:|---:|
| `agents` | No | Yes | Yes | Yes |
| `automations` | No | No | Yes | Yes |
| `browser` | No | Yes | Yes | Yes |
| `genui` | No | No | Yes | Yes |
| `instances` | No | Yes | Yes | Yes |
| `wiki` | No | Yes | Yes | Yes |
| `admin-tools` | No | No | No | Yes |

Chat is shell functionality rather than a catalog page. Public agent URLs remain
subject to the selected agent's anonymous-access mode in addition to this matrix.
An installation may turn a page off globally; a tier cannot re-enable it.

## 4. Ability and tool policy

Tier policy controls maximum availability. The agent must also have the ability
enabled, and the caller must satisfy the ability's own access requirement.

### Capability groups

| Group | Anonymous | Free | Pro | Admin overlay |
|---|---:|---:|---:|---:|
| `chat_core` — basic agent loop and safe app interaction | Yes | Yes | Yes | Yes |
| `memory` — context and personal wiki memory | No | Yes | Yes | Yes |
| `user_files` — caller-owned files only | No | Yes | Yes | Yes |
| `web_read` — read-only web access | No | Yes | Yes | Yes |
| `browser_control` — stateful browser operation | No | Yes, ask policy | Yes | Yes |
| `image_vision` | No | Policy/roster dependent | Yes | Yes |
| `image_generation` | No | No | Policy/roster dependent | Yes |
| `model_switching` | No | Free roster only | Pro roster only | Admin roster only |
| `automation` | No | No | Yes, within quota | Yes |
| `agent_orchestration` | No | No | Yes, within agent/run quotas | Yes |
| `personal_integrations` — user OAuth/API credentials | No | Yes, within connector cap | Yes, within connector cap | Yes |
| `financial_actions` — payments/marketplaces/banking writes | No | No by default | Explicit opt-in plus normal ask/deny policy | Explicit opt-in; never implicit auto-allow |
| `developer_write` — repository/issue/project mutation | No | No by default | Explicit opt-in plus normal ask/deny policy | Explicit opt-in |
| `tool_creation` — create executable tools | No | No | No | Yes |
| `platform_admin` — configuration, diagnostics, tunnels, machine control | No | No | No | Yes |
| `platform_infra` — P2P, device fleet, storage, schedulers | No | No | No | Yes |

Initial mapping rules:

- all abilities under `plugins/abilities/Administrator/` map to `platform_admin`;
- `Core/ui_admin`, `Core/create_tools`, and `Core/p2p` are admin-only;
- `Core/automation` maps to `automation`;
- `Core/agent_orchestration` maps to `agent_orchestration`;
- `Core/image_vision`, `Core/image_generation`, and `Core/model_switcher` map to
  their corresponding groups;
- `Memory/*` maps to `memory`;
- `Web/web_access` and `Web/web_scraper` map to `web_read`, while
  `Web/browser_control` maps to `browser_control`;
- Payments and Marketplaces abilities map to `financial_actions` for mutating
  operations. Read operations may later be split into a lower-risk group;
- Developer abilities map to `developer_write` when they can mutate external
  state; read-only discovery may later be separately granted;
- Communication, CRM, Productivity, and Social abilities map to
  `personal_integrations`, with each tool's normal permission still enforced.

Tool permission (`auto`, `ask`, `deny`) remains a separate axis. A tier grant never
turns a tool from `ask` or `deny` into `auto`.

## 5. Model and BYO policy

### Platform rosters

- `roster-anonymous` contains one inexpensive, tool-capable standard text model.
- `roster-free` contains a standard model and optionally one approved vision
  worker. It contains no premium/high-effort or image-output entry.
- `roster-pro` contains standard, premium/high-effort, vision, image-output, and
  optional voice/system entries.
- An admin roster may contain operational models but is selected only by the
  explicit role overlay.

Every roster entry has an immutable `entry_id`. Session and agent selections
store that ID, not an arbitrary model string. Legacy model strings are translated
once during migration and rejected when ambiguous.

### Override rules

1. Agent configuration may narrow its inherited roster or select a permitted
   default. It cannot add a platform credential or model entry unavailable to the
   effective tier.
2. Session selection may choose only an entry in the already-clamped agent roster.
3. Reasoning effort is capped after session resolution.
4. Image, voice, and high-effort routing use allowed entry IDs, not capability
   checkboxes supplied by the browser.
5. A stale or deleted entry falls back to the tier's published default and emits
   `model_selection_stale`.

### User BYO

- Anonymous users cannot configure BYO.
- Free users may configure one text/tool-capable model. BYO removes platform LLM
  usage charges but does not unlock Pro pages, automation, image generation,
  high effort, or higher quotas.
- Pro users may configure multiple entries and media workers, subject to all
  non-admin restrictions and installation safety controls.
- BYO credentials are caller-owned and are never inherited by another user.
- Whether a key is BYO is determined from the credential actually used for the
  run, not merely from the existence of any user LLM row.

## 6. Billing and assignment policy

The entitlement assignment source is recorded as one of:

- `default`: automatic Free assignment for a registered account;
- `manual`: administrator assignment with reason and optional expiration;
- `billing`: app-wide product subscription or purchase;
- `import`: deployment/bootstrap migration;
- `system`: built-in Anonymous resolution or safety fallback.

Precedence for simultaneous assignments is:

1. active manual safety downgrade;
2. active manual grant;
3. active billing assignment;
4. registered-account Free default;
5. Anonymous fallback.

Every non-default assignment has `starts_at`, optional `expires_at`, actor/source,
and reason. Expiration is evaluated on every cache miss and invalidates cached
capabilities.

Existing per-agent billing is evaluated after entitlement. A user can therefore
be entitled to open an agent or select a model but still receive a billing denial
for that particular operation. The UI must distinguish `upgrade_required` from
`credits_required`.

For compatibility, administrators retain the current billing exemption initially.
The exemption is represented explicitly as a billing rule, not inferred by the
entitlement tier resolver.

## 7. Capability and denial contract

The browser receives a secret-free document shaped like:

```json
{
  "subject": {"class": "registered", "is_admin": false},
  "tier": {"id": "pro", "revision": 3, "source": "billing"},
  "pages": {"agents": true, "automations": true, "admin-tools": false},
  "features": {"model_picker": true, "image_generation": true},
  "limits": {"max_agents": 10, "concurrent_sessions_per_user": 4},
  "models": {
    "roster_id": "roster-pro",
    "revision": 5,
    "allowed_entry_ids": ["standard", "premium", "vision", "image-out"],
    "credential_state": "platform"
  },
  "evaluated_at": "2026-01-01T00:00:00Z"
}
```

Denials use stable codes and include the capability/resource when applicable:

| Code | Meaning | Recommended UX |
|---|---|---|
| `authentication_required` | Anonymous user requested a registered feature | Show sign-in/register action |
| `upgrade_required` | Valid identity but tier lacks capability | Show Pro upgrade action |
| `admin_role_required` | Operational capability requires platform role | Explain that an administrator is required; no upgrade CTA |
| `agent_membership_required` | Tier allows feature but caller lacks agent access | Request access or change agent |
| `feature_disabled_by_installation` | Operator disabled feature globally | Explain unavailable on this installation |
| `model_not_allowed` | Requested entry is outside effective roster | Revert to tier default and explain |
| `model_selection_stale` | Stored entry no longer exists in published roster | Revert silently plus non-blocking notice |
| `byo_not_allowed` | Tier does not permit caller-owned credentials | Show tier-specific explanation |
| `quota_exceeded` | Tier numeric limit reached | Show reset/usage information and upgrade action when applicable |
| `rate_limited` | Short-window safety/rate limit reached | Show retry time, not an upgrade prompt |
| `credits_required` | Entitled operation lacks billing funds | Open billing/credits flow |
| `trial_expired` | Existing per-agent trial ended | Open billing flow |
| `policy_unavailable` | Policy cannot be safely resolved | Generic temporary-unavailable message; log/audit internally |

HTTP behavior remains conventional: 401 for missing authentication, 403 for
role/tier/membership denial, 402 for billing denial, 409 for resource-count
conflicts where appropriate, and 429 for rate/quota windows with retry metadata.

## 8. Acceptance scenarios for Phase 0

The policy is considered sufficiently specified when the following expected
results are unambiguous:

1. An anonymous visitor can use a public agent with the anonymous roster but
   cannot open Agents, enumerate models, configure BYO, or create automations.
2. A new approved account resolves to Free without a stored manual assignment.
3. A Free user cannot select a Pro model by writing session metadata directly.
4. A Free BYO key avoids platform model charges but does not unlock image
   generation or Pro concurrency.
5. A Pro user can use Gen UI and automations but cannot open Admin Tools or load
   Administrator abilities.
6. Promoting a Free user to admin grants the role overlay but leaves the stored
   Free assignment intact.
7. Demoting that user removes admin capabilities immediately on the next request
   without changing their Free assignment.
8. Editing an admin's personal LLM configuration does not change any platform
   roster.
9. Disabling Browser globally removes it for Free, Pro, and administrators unless
   a specifically documented emergency-admin bypass exists.
10. An expired or corrupt Pro assignment never produces Pro access.
11. An active Pro entitlement does not bypass an agent membership or billing
    denial.
12. No safe capability response or model-picker response contains a platform API
    key, token, vault reference, or encrypted secret.

## 9. Deferred decisions

These are intentionally deferred because they require pricing, deployment, or
legal/product input rather than authorization architecture:

- Pro price and whether a future plan includes a monthly provider-cost allowance;
- exact Free/Pro storage and message quotas after cost testing;
- whether multiple paid tiers are needed after Pro;
- organization/team entitlements and shared seats;
- regional model restrictions and data-residency rosters;
- whether financial/developer mutation capabilities become separate paid add-ons;
- distributed quota infrastructure for multi-worker/multi-instance deployments;
- grace-period behavior after failed payment.

None of these changes the separation of identity, role, entitlement, membership,
and billing established above.
