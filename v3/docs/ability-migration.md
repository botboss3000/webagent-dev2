# V3 ability migration inventory

Catalog foundation only: 82 source directories across 13 groups. V2 remains intact.
No v2 Python runtime, accounts, credentials or database is imported. Pending and
service/connection/placeholder entries are configuration metadata, not callable tools.
The existing Codebase Admin adapter retains isolated workspace/promotion semantics.

## Contract

`kind`: ability, virtual_core, oauth, channel, service, placeholder.
`readiness`: ready, pending, placeholder. Legacy manifests infer readiness only
from installed Rust schemas; a ready declaration cannot install an adapter.
`config` retains non-secret descriptor defaults as metadata, not live settings.
Installed schemas remain Rust-owned; manifests cannot import Python or authorize
execution. Installed tool names have one catalog owner. Operator, caller, agent,
approval, suppression and live dispatch checks remain independent authoritative gates.

Only ready ability/virtual_core entries may enter model-facing discovery. Unsupported
tool rows remain visible in the configuration API but cannot expose schemas or tool
skills. Pending v2 instructions are not delivered to the model. Catalog versions cover
typed metadata and loaded skill assets. Changes require v3 restart/rebuild; this source
migration does not deploy or activate new functionality.

## Per-directory disposition

### Administrator

- `code_index`: ability; pending. Adapt independently in a later functional wave.
- `codebase_admin`: ability; ready. Preserve existing v3 adapter.
- `computer_control`: ability; pending. Adapt independently in a later functional wave.
- `diagnostics`: ability; pending. Adapt independently in a later functional wave.
- `git_control`: ability; pending. Adapt independently in a later functional wave.
- `program_screenshot`: ability; pending. Adapt independently in a later functional wave.
- `remote_tunnel`: ability; pending. Adapt independently in a later functional wave.
- `terminal_control`: ability; pending. Adapt independently in a later functional wave.
- `v3_build`: ability; pending. Adapt independently in a later functional wave.
- `v3_workspace`: ability; pending. Adapt independently in a later functional wave.

### CRM

- `hubspot`: placeholder; placeholder. Retain placeholder.
- `mailchimp`: placeholder; placeholder. Retain placeholder.
- `salesforce`: placeholder; placeholder. Retain placeholder.

### Commerce

- `amazon`: oauth; pending. Adapt independently in a later functional wave.
- `bigcommerce`: ability; pending. Adapt independently in a later functional wave.
- `btcpay`: ability; pending. Adapt independently in a later functional wave.
- `ebay`: oauth; pending. Adapt independently in a later functional wave.
- `etsy`: oauth; pending. Adapt independently in a later functional wave.
- `paypal`: ability; pending. Adapt independently in a later functional wave.
- `shopify`: oauth; pending. Adapt independently in a later functional wave.
- `square`: ability; pending. Adapt independently in a later functional wave.
- `stripe`: ability; pending. Adapt independently in a later functional wave.
- `woocommerce`: ability; pending. Adapt independently in a later functional wave.

### Communication

- `discord`: placeholder; placeholder. Retain placeholder.
- `email`: placeholder; placeholder. Retain placeholder.
- `slack`: placeholder; placeholder. Retain placeholder.
- `telegram`: channel; pending. Adapt independently in a later functional wave.
- `twilio`: placeholder; placeholder. Retain placeholder.
- `whatsapp`: placeholder; placeholder. Retain placeholder.

### Core

- `agent_management`: ability; pending. Adapt independently in a later functional wave.
- `agent_orchestration`: ability; pending. Adapt independently in a later functional wave.
- `app_control`: ability; pending. Adapt independently in a later functional wave.
- `automation`: ability; pending. Adapt independently in a later functional wave.
- `base`: virtual_core; pending. Adapt independently in a later functional wave.
- `create_tools`: ability; pending. Adapt independently in a later functional wave.
- `image_generation`: ability; pending. Adapt independently in a later functional wave.
- `image_vision`: ability; pending. Adapt independently in a later functional wave.
- `model_switcher`: ability; pending. Adapt independently in a later functional wave.
- `p2p`: ability; pending. Adapt independently in a later functional wave.
- `ui_admin`: ability; pending. Adapt independently in a later functional wave.
- `user_files`: ability; pending. Adapt independently in a later functional wave.
- `visualizer`: ability; pending. Adapt independently in a later functional wave.

### Developer

- `github`: placeholder; placeholder. Retain placeholder.
- `gitlab`: placeholder; placeholder. Retain placeholder.
- `jira_linear`: placeholder; placeholder. Retain placeholder.

### Devices

- `run_on_device`: ability; pending. Adapt independently in a later functional wave.

### Memory

- `context_control`: virtual_core; pending. Adapt independently in a later functional wave.
- `document_libraries`: ability; pending. Adapt independently in a later functional wave.
- `managed_knowledge_files`: ability; pending. Adapt independently in a later functional wave.
- `memory`: ability; pending. Adapt independently in a later functional wave.
- `sql_databases`: ability; pending. Adapt independently in a later functional wave.
- `task_progress`: virtual_core; pending. Adapt independently in a later functional wave.
- `wiki_context`: ability; pending. Adapt independently in a later functional wave.
- `workspaces_wikis`: ability; pending. Adapt independently in a later functional wave.

### Payments

- `bank_accounts`: placeholder; placeholder. Retain placeholder.

### Productivity

- `dropbox`: oauth; pending. Adapt independently in a later functional wave.
- `google`: oauth; pending. Adapt independently in a later functional wave.
- `microsoft`: oauth; pending. Adapt independently in a later functional wave.
- `yahoo`: oauth; pending. Adapt independently in a later functional wave.

### Social

- `linkedin`: oauth; pending. Adapt independently in a later functional wave.
- `meta`: oauth; pending. Adapt independently in a later functional wave.
- `pinterest`: oauth; pending. Adapt independently in a later functional wave.
- `reddit`: oauth; pending. Adapt independently in a later functional wave.
- `snapchat`: oauth; pending. Adapt independently in a later functional wave.
- `tiktok`: oauth; pending. Adapt independently in a later functional wave.
- `twitch`: oauth; pending. Adapt independently in a later functional wave.
- `twitter`: oauth; pending. Adapt independently in a later functional wave.

### System

- `boot_orphan_resume`: service; pending. Adapt independently in a later functional wave.
- `device_worker`: service; pending. Adapt independently in a later functional wave.
- `diagnostics_recorder`: service; pending. Adapt independently in a later functional wave.
- `event_runtime`: service; pending. Adapt independently in a later functional wave.
- `hybrid_sync`: service; pending. Adapt independently in a later functional wave.
- `message_length_cap`: service; pending. Adapt independently in a later functional wave.
- `run_manager`: service; pending. Adapt independently in a later functional wave.
- `scheduler`: service; pending. Adapt independently in a later functional wave.
- `task_grouping`: service; pending. Adapt independently in a later functional wave.
- `watchdog`: service; pending. Adapt independently in a later functional wave.

### Web

- `browser_control`: ability; pending. Adapt independently in a later functional wave.
- `host_management`: ability; pending. Adapt independently in a later functional wave.
- `ssh_control`: ability; pending. Adapt independently in a later functional wave.
- `web_access`: ability; pending. Adapt independently in a later functional wave.
- `web_scraper`: ability; pending. Adapt independently in a later functional wave.

