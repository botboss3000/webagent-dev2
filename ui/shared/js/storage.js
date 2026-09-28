'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Storage UI driver — the DB / Secrets / Encryption rows now live on the App
 * Config → Data Settings tab (#ac-section-data-settings), and the Config Files +
 * Stream Buffer rows on the App Settings tab; this driver binds all of them by
 * element id (prefix "ac-storage-" / "ac-cfg-"). Talks to /admin/storage/*.
 * The `root` gate below just needs a section that is always present when the
 * storage UI is mounted — the Data Settings section, home of the DB provider.
 */

import { app } from './state.js';
import { apiPath } from './config.js';
import { isAdmin } from './left-login.js';
import { _esc, _markSaving, _flashSaveCheck, _refreshLucideIcons } from './dom-utils.js';
import { hazardConfirm } from './confirm-dialog.js';
import { tipBadge } from './field-tip.js';
import { copyText } from './clipboard.js';
import { icon } from './icons.js';

let PAGE = null;

function qs(id) { return document.getElementById(id); }
function uid() { return app.currentUserId || localStorage.getItem('auth_user_id') || ''; }

function bindMount(prefix) {
  const m = {
    prefix,
    root: qs('ac-section-data-settings'),
    envBanner: qs(`${prefix}env-banner`),
    activeBadge: qs(`${prefix}active-badge`),
    provider: qs(`${prefix}db-provider`),
    fields: qs(`${prefix}db-fields`),
    btnTest: qs(`${prefix}db-test`),
    btnShowSQL: qs(`${prefix}db-show-sql`),
    btnBootstrap: qs(`${prefix}db-bootstrap`),
    btnSave: qs(`${prefix}db-save`),
    btnActivate: qs(`${prefix}db-activate`),
    output: qs(`${prefix}db-output`),
    outputCopy: qs(`${prefix}db-output-copy`),
    secretsBadge: qs(`${prefix}secrets-badge`),
    secretsProv: qs(`${prefix}secrets-provider`),
    secretsWarn: qs(`${prefix}secrets-warning`),
    secretsMultihost: qs(`${prefix}secrets-multihost`),
    secretsTest: qs(`${prefix}secrets-test`),
    secretsSave: qs(`${prefix}secrets-save`),
    secretsOutput: qs(`${prefix}secrets-output`),
    secretsFields: qs(`${prefix}secrets-fields`),
    secretsRestart: qs(`${prefix}secrets-restart`),
    genuiBadge: qs(`${prefix}genui-badge`),
    genuiMode: qs(`${prefix}genui-mode`),
    genuiNotes: qs(`${prefix}genui-notes`),
    genuiSave: qs(`${prefix}genui-save`),
    genuiMigrate: qs(`${prefix}genui-migrate`),
    genuiOutput: qs(`${prefix}genui-output`),
    encBadge: qs(`${prefix}enc-badge`),
    encVaultWarn: qs(`${prefix}enc-vault-warning`),
    // Per-secret field encryption is now a single hold-until-confirmed toggle
    // (replaces the old level <select> + Activate/Encrypt/Decrypt buttons). The
    // toggle ON activates the field level + encrypts existing rows; OFF decrypts
    // back to plaintext + sets level none. Only the expert key tools remain.
    encLevelTri: qs(`${prefix}enc-level-tri`),
    encFieldStatus: qs(`${prefix}enc-field-status`),
    encTest: qs(`${prefix}enc-test`),
    encKekGen: qs(`${prefix}enc-kek-generate`),
    encKekRotate: qs(`${prefix}enc-kek-rotate`),
    encTenants: qs(`${prefix}enc-tenants`),
    encOutput: qs(`${prefix}enc-output`),
    encLevelBadge: qs(`${prefix}enc-level-badge`),
    // Full-database (SQLCipher) controls now live INSIDE the Encryption row:
    // a master tri-toggle on the header + an Advanced disclosure for the
    // field-level tools above.
    encMaster: qs(`${prefix}enc-master`),
    encAdvToggle: qs(`${prefix}enc-adv-toggle`),
    encAdvBody: qs(`${prefix}enc-adv-body`),
    fulldbWarning: qs(`${prefix}fulldb-warning`),
    fulldbList: qs(`${prefix}fulldb-list`),
    fulldbOutput: qs(`${prefix}fulldb-output`),
    // Hybrid local-first (opt-in) — single hold-until-confirmed toggle.
    hybridToggle: qs(`${prefix}hybrid-toggle`),
    hybridBadge: qs(`${prefix}hybrid-badge`),
    hybridNote: qs(`${prefix}hybrid-note`),
    hybridOutput: qs(`${prefix}hybrid-output`),
    dbNotes: qs(`${prefix}db-notes`),
    secretsNotes: qs(`${prefix}secrets-notes`),
    encNotes: qs(`${prefix}enc-notes`),
    exportBtn: qs(`${prefix}export-btn`),
    importInput: qs(`${prefix}import-input`),
    migrateOutput: qs(`${prefix}migrate-output`),
  };
  if (!m.root || !m.provider) return null;
  return m;
}

function out(elt, text, ok) {
  if (!elt) return;
  elt.style.display = 'block';
  elt.style.color = ok === true ? 'var(--success)' : ok === false ? 'var(--danger)' : '';
  elt.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
  // Show the Copy bar (only the DB output box has one) whenever there is real
  // text to copy. Generic + harmless for outputs without a bar.
  const bar = elt.parentElement && elt.parentElement.querySelector(':scope > .ac-output-copy-bar');
  if (bar) bar.style.display = (elt.textContent || '').trim() ? 'flex' : 'none';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

// Copy-button feedback: swap the copy glyph + label to a green check (or red
// cross on failure) for ~1.5s, then revert — the same icon→check confirmation
// the app's other copy buttons (Remote Access, Deploy) use.
function flashCopied(btn, ok) {
  if (!btn) return;
  clearTimeout(btn._copyT);
  btn.innerHTML = ok
    ? `${icon('check', { size: '12px' })}<span>Copied</span>`
    : `${icon('alert-triangle', { size: '12px' })}<span>Failed</span>`;
  btn.style.color = ok ? 'var(--success)' : 'var(--danger)';
  btn._copyT = setTimeout(() => {
    btn.innerHTML = `${icon('copy', { size: '12px' })}<span>Copy</span>`;
    btn.style.color = 'var(--fg-3)';
  }, 1500);
}

// ── Trade-off notes shown beneath each selector ────────────────────────────

const GOOD = 'var(--success)';
const BAD = 'var(--danger)';

function notesHtml(pros, cons, extra) {
  const li = (items, color, sign) => items.map(t =>
    `<li style="margin:1px 0;"><span style="color:${color};font-weight:600;">${sign}</span> ${t}</li>`
  ).join('');
  const extraLine = extra ? `<div style="margin-top:6px;opacity:0.75;font-style:italic;">${extra}</div>` : '';
  return `<ul style="margin:0;padding-left:14px;list-style:none;">${li(pros, GOOD, '+')}${li(cons, BAD, '−')}</ul>${extraLine}`;
}

const DB_NOTES = {
  sqlite: notesHtml(
    ['Zero ops, zero latency, zero cost, works offline.', 'Single-file backup (just copy <code>local.db</code>).', 'Only backend that supports the full-DB SQLCipher encryption option.'],
    ['Single host — no horizontal scale or multi-instance deploys.', 'One writer at a time (readers stay concurrent in WAL mode) — figure dozens of writes/sec and a handful of simultaneously active users.', 'You own backups, retention, and at-rest disk encryption.'],
    'Typical capacity: comfortable into the tens of GB (281 TB hard ceiling) and ~100K requests/day. Best for: personal use, dev, embedded/edge deployments.'
  ),
  postgres: notesHtml(
    ['Connect to any Postgres you own — self-hosted, RDS, Azure, on-prem.', 'Strongest write/concurrency story and full SQL feature set.', 'Extensions, replication, logical decoding all available.'],
    ['You operate it: provisioning, backups, upgrades, monitoring.', 'Needs reachable network and credentials.', 'No managed PITR or HA unless you build it.'],
    'Best for: teams with existing Postgres infrastructure and DBA capacity.'
  ),
  aws_rds: notesHtml(
    ['The most widely deployed managed Postgres — RDS for standard Postgres, Aurora for its high-performance Postgres-compatible engine.', 'Automated backups, point-in-time restore, Multi-AZ failover, read replicas.', 'Deep AWS integration: IAM auth, VPC isolation, CloudWatch metrics.'],
    ['Always-on billing — no true scale-to-zero (Aurora Serverless v2 keeps a minimum floor).', 'Connectivity needs VPC security groups or a public endpoint.', 'Vendor lock-in to AWS networking & IAM; Aurora costs more than vanilla RDS.'],
    'Best for: AWS-native production where maturity and ecosystem breadth matter most.'
  ),
  gcp_cloud_sql: notesHtml(
    ['Fully managed Postgres with HA, automated backups, PITR, IAM auth.', 'Tight integration if your app runs on Cloud Run or GKE.', 'Private IP / IAM auth removes password-handling burden.'],
    ['Always-on billing — no scale-to-zero, smallest tier still costs monthly.', 'Needs Cloud SQL Proxy or private IP for safe connectivity.', 'Vendor lock-in to GCP networking & IAM.'],
    'Best for: GCP-native deployments where uptime matters more than idle cost.'
  ),
  azure_postgres: notesHtml(
    ['Fully managed Postgres (Flexible Server) with HA, automated backups, PITR.', 'Microsoft Entra ID (Azure AD) auth and tight integration with the Azure stack.', 'Dev/test tiers can auto-stop when idle to trim cost.'],
    ['Always-on billing on production tiers — no scale-to-zero there.', 'Connectivity needs VNet integration or firewall rules.', 'Vendor lock-in to Azure networking & identity.'],
    'Best for: Azure-native or Microsoft-shop deployments.'
  ),
  neon: notesHtml(
    ['Serverless Postgres with scale-to-zero — cheap when idle, autoscales under load.', 'Database branching for preview/test environments.', 'Pure Postgres wire protocol — trivial to migrate to or from.'],
    ['Cold-start latency on the first query after idling.', 'Connection pooling is mandatory for serverless app deployments.', 'Newer vendor — smaller ecosystem and tooling than RDS/Cloud SQL.'],
    'Best for: bursty workloads, preview environments, indie projects watching cost.'
  ),
};

// ── "How to set up an account / where to find the details" guides ───────────
// Rendered as a circled "?" help badge beside the Provider / Vault selectors
// (see field-tip.js). These answer "I picked this provider — now how do I get
// an account and the connection details to paste below?" — the same per-field
// guidance the Deploy form grew, applied to the data backends.

function setupGuide(intro, steps) {
  const lis = steps.map(s => `<li style="margin:3px 0;">${s}</li>`).join('');
  const head = intro ? `<div style="margin-bottom:6px;opacity:.85;">${intro}</div>` : '';
  return { html: `${head}<ol style="margin:0;padding-left:16px;line-height:1.45;">${lis}</ol>`, wide: true };
}

const DB_SETUP = {
  sqlite: { html: 'No account or setup needed. The database is a single file kept locally under <code>data/db/</code> — nothing to configure.' },
  postgres: setupGuide('Connect to any Postgres server you already run (self-hosted, on-prem, a VPS, etc.):', [
    'Make sure the server is reachable from this machine and accepts TCP connections (check <code>listen_addresses</code> and <code>pg_hba.conf</code>).',
    'Create a database and a user with rights on it (e.g. <code>CREATE DATABASE webagent;</code> and a role with full access).',
    'Fill in the host, port (usually 5432), database name, username and password below.',
    'Pick an SSL mode — use <b>require</b> or stronger for anything over a network.',
    'Click <b>Test Connection</b>, then <b>Auto-Create Tables</b>, then <b>Activate</b>.',
  ]),
  aws_rds: setupGuide('Managed Postgres on AWS (RDS or Aurora):', [
    'In the <b>AWS Console → RDS</b>, click <b>Create database</b> and choose <b>PostgreSQL</b> (or Aurora PostgreSQL).',
    'Set a master username and password, and note the initial database name.',
    'Under <b>Connectivity</b>, enable <b>Public access</b> (or place this app inside the same VPC), and open port 5432 in the security group to this server\'s IP.',
    'After it finishes creating, open the instance and copy its <b>Endpoint</b> hostname.',
    'Paste the endpoint, port, database name, username and password below, then <b>Test Connection</b> → <b>Auto-Create Tables</b> → <b>Activate</b>.',
  ]),
  gcp_cloud_sql: setupGuide('Managed Postgres on Google Cloud:', [
    'In the <b>Google Cloud Console → SQL</b>, click <b>Create instance</b> → <b>PostgreSQL</b>.',
    'Set an instance ID and a password for the <code>postgres</code> user; choose a region.',
    'Under <b>Connections</b>, add this server\'s IP to <b>Authorized networks</b> (or run the Cloud SQL Auth Proxy and connect to it locally).',
    'Create a database from the <b>Databases</b> tab.',
    'Copy the instance\'s <b>Public IP</b> (or the proxy host) into the host field, fill in the rest, then <b>Test</b> → <b>Auto-Create Tables</b> → <b>Activate</b>.',
  ]),
  azure_postgres: setupGuide('Managed Postgres on Azure (Flexible Server):', [
    'In the <b>Azure Portal</b>, create a resource → <b>Azure Database for PostgreSQL → Flexible Server</b>.',
    'Set an admin username and password; choose a region and tier.',
    'Under <b>Networking</b>, add a firewall rule for this server\'s IP (or allow public access for testing).',
    'After deployment, open the server\'s <b>Overview</b> and copy the <b>Server name</b> (e.g. <code>myserver.postgres.database.azure.com</code>).',
    'Paste it into the host field, fill in the database/username/password, then <b>Test</b> → <b>Auto-Create Tables</b> → <b>Activate</b>.',
  ]),
  neon: setupGuide('Serverless Postgres with a generous free tier:', [
    'Go to <b>neon.tech</b> and sign up (GitHub login works).',
    'Create a <b>Project</b> — a database and branch are created for you automatically.',
    'On the project dashboard click <b>Connect</b> / <b>Connection Details</b> to reveal the connection string.',
    'From that string, copy the <b>host</b> (e.g. <code>ep-xxxx.neon.tech</code>), database name, username (role) and password into the fields below.',
    'Click <b>Test Connection</b>, then <b>Auto-Create Tables</b>, then <b>Activate</b>.',
  ]),
};

const SECRETS_SETUP = {
  inline_db: { html: 'No setup — secrets are stored in the app database. Fine for local dev only; switch to a real vault before production.' },
  env: { html: 'No account. Secrets are read from environment variables your deployment injects (e.g. Docker / Cloud Run secrets). Read-only — the app can\'t write new tokens here.' },
  os_keyring: { html: 'No account. Uses this machine\'s own credential store (Windows Credential Manager / macOS Keychain / freedesktop Secret Service). Nothing to configure — it just works on the host the app runs on.' },
  hashicorp_vault: setupGuide('Store secrets in a HashiCorp Vault you run yourself (or HCP Vault) — cloud-agnostic, every machine shares it. Fill in the fields below:', [
    'Stand up a Vault server (self-host, Docker, or sign up for managed <b>HCP Vault</b> at <b>portal.cloud.hashicorp.com</b>) and note its address, e.g. <code>https://vault.example.com:8200</code>.',
    'Make sure a <b>KV version 2</b> secrets engine is enabled — the default mount is <code>secret/</code>.',
    'Create a <b>token</b> (or AppRole) with read/write on that path. For a quick test the root token works; for production use a scoped policy.',
    'In the fields below, enter the <b>Vault address</b> and <b>token</b> (and, if you changed them, the KV mount / key prefix). The token is saved in your OS keyring, not the database.',
    'Click <b>Test</b> to confirm the app can reach Vault, then <b>Activate</b>. <i>(Advanced: instead of the fields, you may set <code>VAULT_ADDR</code> / <code>VAULT_TOKEN</code> environment variables on the server.)</i>',
  ]),
  azure_key_vault: setupGuide('Store secrets in Azure Key Vault — the third major cloud vault, alongside GCP and AWS. Fill in the fields below:', [
    'In the <b>Azure Portal</b>, create a <b>Key Vault</b> resource and copy its <b>Vault URI</b> (e.g. <code>https://my-vault.vault.azure.net</code>).',
    'Grant the identity this app runs as the <b>Key Vault Secrets Officer</b> role (or a get/set/list/delete access policy) on that vault.',
    'In the fields below, enter the <b>Key Vault URL</b>. For a service-principal login also fill <b>Tenant ID</b>, <b>Client ID</b> and <b>Client secret</b> — or leave those three blank to use a Managed Identity / <code>az login</code> on the host. The client secret is saved in your OS keyring.',
    'Click <b>Test</b> to confirm access, then <b>Activate</b>. <i>(Advanced: instead of the fields, you may set <code>AZURE_KEY_VAULT_URL</code> and the standard Azure credential env vars on the server.)</i>',
  ]),
  gcp_secret_manager: setupGuide('Store secrets in Google Cloud Secret Manager. Fill in the fields below:', [
    'In the <b>Google Cloud Console</b>, enable the <b>Secret Manager API</b> for your project.',
    'Create a <b>service account</b> with the <b>Secret Manager Admin</b> role and download its JSON key (or, if this app runs on GCP, attach the role to its workload identity).',
    'In the fields below, enter your <b>GCP project ID</b> and paste the <b>service-account key JSON</b> (the whole file) — or leave the JSON blank to use the host\'s default Google credentials. The JSON is saved in your OS keyring.',
    'Click <b>Test</b> to confirm the app can reach Secret Manager, then <b>Activate</b>. <i>(Advanced: instead of the fields, set <code>GCP_PROJECT</code> and <code>GOOGLE_APPLICATION_CREDENTIALS</code> on the server.)</i>',
  ]),
  aws_secrets_manager: setupGuide('Store secrets in AWS Secrets Manager. Fill in the fields below:', [
    'In the <b>AWS Console</b>, no resource is needed up front — secrets are created on demand.',
    'Create an <b>IAM user or role</b> with permission to <code>secretsmanager:*</code> on your secrets (or attach the role to this app\'s EC2/ECS/Lambda).',
    'In the fields below, enter the <b>AWS region</b>. For an IAM user also fill the <b>Access key ID</b> and <b>Secret access key</b> — or leave those blank to use an instance/ECS/Lambda role or <code>~/.aws</code> credentials. The secret key is saved in your OS keyring.',
    'Click <b>Test</b> to confirm access, then <b>Activate</b>. <i>(Advanced: instead of the fields, set <code>AWS_REGION</code> / <code>AWS_ACCESS_KEY_ID</code> / <code>AWS_SECRET_ACCESS_KEY</code> on the server.)</i>',
  ]),
};

const SECRETS_NOTES = {
  inline_db: notesHtml(
    ['Zero setup — works out of the box on any DB backend.'],
    ['Secrets stored as plaintext in <code>auth_elements.secret_ref</code>.', 'Anyone with DB read access sees every OAuth token and API key.', 'Combining this with field-level encryption defeats the purpose: keys and ciphertext live in the same DB.'],
    'Acceptable for local dev only. Upgrade to a real vault before enabling encryption or going to production.'
  ),
  env: notesHtml(
    ['Read-only and inert — secrets injected by your deployment environment.', 'Excellent fit for Docker / Cloud Run / Kubernetes with sealed secrets.', 'Survives container restarts; no app-side write path to compromise.'],
    ['Read-only: the app cannot store new secrets (OAuth callbacks will fail to persist tokens).', 'You manage rotation outside the app.', 'Not suitable as the keystore for the encryption feature, which needs to write per-tenant DEKs.'],
    'Best for: read-only secret injection in immutable deployments.'
  ),
  os_keyring: notesHtml(
    ['Uses the OS credential store: Windows Credential Manager, macOS Keychain, freedesktop Secret Service.', 'Strong single-host security at zero cost.', 'Recommended pairing with SQLite + field-level encryption on a workstation.'],
    ['Tied to one machine — multi-instance deploys cannot share secrets.', 'Requires a logged-in user session on Linux (Secret Service / D-Bus).', 'No audit log, no programmatic rotation.'],
    'Best for: single-host installations on a laptop, workstation, or dedicated server.'
  ),
  hashicorp_vault: notesHtml(
    ['Cloud-agnostic: one shared vault for every machine, on any host or cloud (or HCP Vault).', 'Audit logging, fine-grained policies, dynamic secrets, leases and rotation built in.', 'Open source — no per-secret cloud bill if you self-host.'],
    ['You operate the Vault server (or pay for HCP): provisioning, unsealing, upgrades are on you.', 'Requires <code>VAULT_ADDR</code> + a token reachable wherever the app runs.', 'A KV v2 engine must be enabled on the mount you point at.'],
    'Best for: multi-machine or on-prem setups that want a shared, vendor-neutral vault.'
  ),
  azure_key_vault: notesHtml(
    ['Managed, audit-logged, RBAC-controlled secrets in Azure — every machine shares them.', 'Pairs naturally with Azure VMs / App Service / AKS via Managed Identity (no stored credentials).', 'Versioned secrets with soft-delete and purge protection.'],
    ['Per-secret and per-operation pricing.', 'Requires Azure credentials wherever the app runs.', 'Secret names allow only letters, digits and dashes, so internal key names are mapped to that character set.'],
    'Best for: Azure-hosted deployments (a natural match if your database is Azure Postgres).'
  ),
  gcp_secret_manager: notesHtml(
    ['Managed, audit-logged, IAM-controlled, versioned secrets in GCP.', 'Pairs naturally with Cloud Run / GKE workloads via workload identity.', 'Supports automatic version pinning and rotation policies.'],
    ['Per-secret-version pricing plus per-access cost.', 'Requires GCP credentials wherever the app runs (even if not deployed on GCP).', 'Cross-region latency if your app is outside GCP.'],
    'Best for: GCP-hosted production deployments.'
  ),
  aws_secrets_manager: notesHtml(
    ['Managed, audit-logged, IAM-controlled secrets with built-in rotation Lambdas.', 'Best fit for ECS / Lambda / EC2 deployments via IAM roles.', 'Integrates with CloudTrail for full access auditing.'],
    ['Per-secret monthly cost plus per-API-call cost.', 'Requires AWS credentials wherever the app runs.', 'Rotation Lambdas need to be written per provider.'],
    'Best for: AWS-hosted production deployments.'
  ),
};

// ── Remote-vault connection fields (configured in the UI after launch) ──────
// Each remote provider declares the inputs shown under its dropdown. Exactly ONE
// field per provider is the sensitive token (secret:true) — it stores
// keyring-first with a file fallback and is NEVER echoed back; the rest are
// non-secret locators saved to a local file outside the app database. `key` must
// match what the matching app/secrets/*.py backend reads from its saved config
// (the secret one is read via get_provider_token). Adding fields here + matching
// keys in the backend is all a new drop-in vault needs — no core edits.
const SECRETS_FIELDS = {
  hashicorp_vault: [
    { key: 'address', label: 'Vault address', placeholder: 'https://vault.example.com:8200', hint: 'The base URL of your Vault server.' },
    { key: 'token', label: 'Vault token', secret: true, placeholder: 's.xxxxxxxx', hint: 'A token with read/write on the KV path. Stored in your OS keyring.' },
    { key: 'kv_mount', label: 'KV mount', placeholder: 'secret', hint: 'KV v2 engine mount point. Default: secret', optional: true },
    { key: 'kv_prefix', label: 'Key prefix', placeholder: 'webagent', hint: 'Path prefix for this app\'s keys. Default: webagent', optional: true },
  ],
  azure_key_vault: [
    { key: 'vault_url', label: 'Key Vault URL', placeholder: 'https://my-vault.vault.azure.net', hint: 'Your Key Vault\'s DNS name.' },
    { key: 'tenant_id', label: 'Tenant ID', placeholder: '(optional)', hint: 'Azure AD tenant. Leave the next three blank to use a Managed Identity / az login instead.', optional: true },
    { key: 'client_id', label: 'Client ID', placeholder: '(optional)', hint: 'App registration (service principal) client ID.', optional: true },
    { key: 'token', label: 'Client secret', secret: true, placeholder: '(optional)', hint: 'The service-principal secret. Stored in your OS keyring. Leave blank to use Managed Identity / az login.', optional: true },
  ],
  gcp_secret_manager: [
    { key: 'project', label: 'GCP project ID', placeholder: 'my-gcp-project', hint: 'The project that holds your secrets.' },
    { key: 'token', label: 'Service-account key (JSON)', secret: true, textarea: true, placeholder: 'Paste the whole service-account JSON, or leave blank to use the host\'s default Google credentials.', hint: 'Optional. The full key file contents. Stored in your OS keyring.', optional: true },
  ],
  aws_secrets_manager: [
    { key: 'region', label: 'AWS region', placeholder: 'us-east-1', hint: 'The region your secrets live in.' },
    { key: 'access_key_id', label: 'Access key ID', placeholder: '(optional)', hint: 'Leave this and the next blank to use an instance/ECS/Lambda role or ~/.aws credentials.', optional: true },
    { key: 'token', label: 'Secret access key', secret: true, placeholder: '(optional)', hint: 'The secret half of the IAM key pair. Stored in your OS keyring.', optional: true },
  ],
};

// Render the connection fields for the selected provider into `m.secretsFields`.
// Prefills non-secret values from the saved config; the secret field shows a
// "saved — leave blank to keep" placeholder when a token already exists, so
// re-saving never forces re-entry. Hidden entirely for local providers.
function renderSecretsFields(m, state) {
  const el = m && m.secretsFields;
  if (!el) return;
  const provider = m.secretsProv ? m.secretsProv.value : '';
  const fields = SECRETS_FIELDS[provider];
  if (!fields) { el.style.display = 'none'; el.innerHTML = ''; return; }
  const sec = (state && state.secrets) || {};
  const saved = (sec.configs && sec.configs[provider]) || {};
  const tokenSaved = sec.token_saved && sec.token_saved[provider];
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  let html = '';
  for (const f of fields) {
    const req = f.optional ? '' : ' <span style="color:var(--danger);">*</span>';
    const idAttr = `ac-secrets-f-${esc(f.key)}`;
    if (f.secret) {
      const ph = tokenSaved
        ? `saved in ${esc(tokenSaved)} — leave blank to keep`
        : esc(f.placeholder || '');
      const input = f.textarea
        ? `<textarea id="${idAttr}" class="ac-input" data-skey="${esc(f.key)}" rows="3" placeholder="${ph}" style="resize:vertical;font-family:var(--font-mono);"></textarea>`
        : `<input id="${idAttr}" class="ac-input" data-skey="${esc(f.key)}" type="password" autocomplete="new-password" placeholder="${ph}" />`;
      html += `<label class="ac-label" for="${idAttr}">${esc(f.label)}${req}</label>${input}`;
    } else {
      const val = esc(saved[f.key] != null ? saved[f.key] : '');
      html += `<label class="ac-label" for="${idAttr}">${esc(f.label)}${req}</label>` +
        `<input id="${idAttr}" class="ac-input" data-skey="${esc(f.key)}" type="text" value="${val}" placeholder="${esc(f.placeholder || '')}" />`;
    }
    if (f.hint) html += `<div style="font-size:10px;color:var(--fg-2);margin:2px 0 8px;">${esc(f.hint)}</div>`;
  }
  el.innerHTML = html;
  el.style.display = '';
}

// Collect the entered fields into { config, token } for the save/test calls.
// Non-secret inputs → config; the single secret field → token (omitted when
// blank so the stored token is left untouched).
function collectSecretsFields(m) {
  const out = { config: {}, token: undefined };
  if (!m || !m.secretsFields) return out;
  const inputs = m.secretsFields.querySelectorAll('[data-skey]');
  for (const inp of inputs) {
    const key = inp.getAttribute('data-skey');
    // The secret field is whichever one is marked secret in SECRETS_FIELDS.
    const def = (SECRETS_FIELDS[m.secretsProv.value] || []).find((f) => f.key === key);
    if (def && def.secret) {
      if (inp.value) out.token = inp.value;
    } else {
      out.config[key] = inp.value.trim();
    }
  }
  return out;
}

// ── Clean-cutover server restart ─────────────────────────────────────────────
// Switching the active vault rebuilds the connection for new secret reads
// immediately, but anything already loaded into memory at boot isn't re-fetched
// until the process restarts. So when the live vault has diverged from the one
// the server booted with (state.secrets.restart_recommended), we surface a
// panel-scoped restart: a notice + button that spins until /health answers again,
// then invites the user to re-test — no full-app reload, no sign-out.
const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Probe /health (root path, never SW-cached). Resolves true only on a 2xx/3xx.
function _probeHealth() {
  return fetch(apiPath('/health'), { cache: 'no-store' })
    .then((r) => r.ok)
    .catch(() => false);
}

// Wait for the server to cycle: first watch it drop (so we don't false-positive on
// the old process), then watch it come back. Calls onStatus(text) as it advances.
async function _waitForServerRestart(onStatus) {
  const downBy = Date.now() + 30000;
  while (Date.now() < downBy) {                 // 1) wait for the old process to stop answering
    if (!(await _probeHealth())) break;
    await _sleep(700);
  }
  onStatus && onStatus('Waiting for the server to come back…');
  const upBy = Date.now() + 180000;
  while (Date.now() < upBy) {                    // 2) wait for the new process to answer
    if (await _probeHealth()) return true;
    await _sleep(1000);
  }
  return false;
}

// Build (or clear) the restart notice for the Secrets Vault row. Idempotent — safe
// to call on every applyState; rebuilds its button + status line fresh each time.
function renderSecretsRestart(m, state) {
  const el = m && m.secretsRestart;
  if (!el) return;
  const sec = (state && state.secrets) || {};
  if (!sec.restart_recommended) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = 'block';
  el.innerHTML = '';

  const msg = document.createElement('div');
  msg.innerHTML =
    `Now serving from <b>${esc(sec.provider || '')}</b>, but the server booted on ` +
    `<b>${esc(sec.boot_provider || '')}</b>. The switch is already live for new lookups; ` +
    `restart for a clean cutover so every secret is re-read from the new vault.`;
  el.appendChild(msg);

  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:10px;margin-top:10px;';
  const btn = document.createElement('button');
  btn.className = 'ac-btn';
  btn.type = 'button';
  btn.textContent = 'Restart server';
  const status = document.createElement('span');
  status.className = 'ac-hint';
  status.style.cssText = 'display:inline-flex;align-items:center;gap:6px;';
  row.appendChild(btn);
  row.appendChild(status);
  el.appendChild(row);

  const spinner = () => {
    const s = document.createElement('i');
    s.setAttribute('data-lucide', 'loader-2');
    s.style.cssText = 'width:13px;height:13px;animation:spin 1s linear infinite;';
    return s;
  };
  const setStatus = (text, withSpinner) => {
    status.innerHTML = '';
    if (withSpinner) status.appendChild(spinner());
    status.appendChild(document.createTextNode(text));
    _refreshLucideIcons(status);
  };

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.style.opacity = '0.55';
    setStatus('Checking…', true);
    // Confirm this host can revive itself before we ask it to stop.
    const info = await call(
      `/admin/storage/server/restart-info?requesting_user_id=${encodeURIComponent(uid())}`);
    if (!info.body || !info.body.auto_restart_available) {
      btn.disabled = false; btn.style.opacity = '1';
      setStatus((info.body && info.body.reason) ||
        'Automatic restart isn\'t available here — please restart the server manually.', false);
      return;
    }
    setStatus('Restarting server…', true);
    const r = await call('/admin/storage/server/restart', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    if (!r.ok) {
      btn.disabled = false; btn.style.opacity = '1';
      setStatus((r.body && r.body.detail) || 'Restart could not be started.', false);
      return;
    }
    const back = await _waitForServerRestart((t) => setStatus(t, true));
    if (!back) {
      btn.disabled = false; btn.style.opacity = '1';
      setStatus('Server hasn\'t answered yet — give it a moment, then reload the page.', false);
      return;
    }
    setStatus('Server restarted ✓ — you can Test again.', false);
    // Refresh the panel: the notice clears now that boot == active.
    loadConfig(m).then((s) => s && applyState(m, s));
  });

  _refreshLucideIcons(el);
}

// Every Application-Data backend EXCEPT local SQLite puts the data on a remote/
// networked database that more than one machine can point at. That's exactly the
// case where a single-host vault becomes a hazard (see renderVaultRecommendation).
const REMOTE_DB_PROVIDERS = new Set([
  'postgres', 'aws_rds', 'gcp_cloud_sql', 'azure_postgres', 'neon',
]);
// Single-host / in-DB vaults — fine on one machine, but they do NOT travel with a
// shared remote database. The two cloud vaults (gcp/aws) and env are excluded:
// they're already shared or externally managed.
const LOCAL_VAULTS = new Set(['os_keyring', 'inline_db']);

// Build the "you're on a remote database with a single-machine vault" guidance.
// Returns '' when the pairing is fine (local SQLite, or an already-shared cloud
// vault). The two local-vault cases read differently, so the copy is tailored:
//   • os_keyring  — secrets (incl. the encryption keys) live only in THIS machine's
//                   credential store; a 2nd machine can't share them.
//   • inline_db   — secrets live INSIDE the remote DB; the connection password is
//                   then trapped in the very database it's needed to reach.
function vaultRecommendationHtml(dbProvider, vaultProvider) {
  if (!REMOTE_DB_PROVIDERS.has(dbProvider) || !LOCAL_VAULTS.has(vaultProvider)) return '';
  const head = (t) =>
    `<div style="font-weight:700;color:var(--warning);margin-bottom:5px;">${t}</div>`;
  const limits = (items) =>
    `<div style="margin-top:6px;font-weight:600;color:var(--fg-2);">Limitations of a single-machine vault here</div>` +
    `<ul style="margin:4px 0 0;padding-left:16px;">${items.map(t => `<li style="margin:2px 0;">${t}</li>`).join('')}</ul>`;
  const rec = (t) => `<div style="margin-top:7px;">${t}</div>`;

  if (vaultProvider === 'os_keyring') {
    return head('This database is remote, but your vault lives only on this machine.') +
      `<div>Your secrets — the database password, the cloud key, and the at-rest <b>encryption keys</b> — are stored in this computer's own credential store. They do <b>not</b> travel with the shared database.</div>` +
      limits([
        'Another machine pointing at the same database won\'t see these secrets — each machine needs its own copy of the connection details.',
        'More serious: the <b>encryption keys</b> stay here too. A second machine that starts without them generates <b>different</b> keys and then can\'t read what this machine encrypted (and vice-versa) — it looks like unreadable values, not an error.',
        'No shared audit log or central rotation across machines.',
      ]) +
      rec('<b>Recommendation:</b> if this is your only machine, OS&nbsp;Keyring is the right choice — nothing to change. If you\'ll run the app on <b>more than one machine</b> against this database, switch to a shared remote vault (<b>HashiCorp&nbsp;Vault</b>, <b>Azure&nbsp;Key&nbsp;Vault</b>, <b>GCP&nbsp;Secret&nbsp;Manager</b>, or <b>AWS&nbsp;Secrets&nbsp;Manager</b>) so every machine reads the same secrets and encryption keys. Pick the provider in the dropdown above for step-by-step setup instructions.');
  }
  // inline_db
  return head('This database is remote, but your vault is stored inside it.') +
    `<div>With the <b>App DB</b> vault, secrets are saved as rows in the remote database itself. That creates a chicken-and-egg problem: the <b>password needed to reach the database is locked inside the database</b>.</div>` +
    limits([
      'On a restart the app can\'t recover the connection password (it lives in the DB it\'s trying to open) and may silently fall back to the local file — your remote data then looks like it vanished.',
      'Secrets are stored in plaintext rows, readable by anyone with database access.',
      'If you also turn on encryption, the keys would sit in the same database they\'re meant to protect — which defeats the purpose.',
    ]) +
    rec('<b>Recommendation:</b> move the vault off the database. On a single machine pick <b>OS&nbsp;Keyring</b>; for multiple machines pick a shared remote vault (<b>HashiCorp&nbsp;Vault</b>, <b>Azure&nbsp;Key&nbsp;Vault</b>, <b>GCP&nbsp;Secret&nbsp;Manager</b>, or <b>AWS&nbsp;Secrets&nbsp;Manager</b>) — select it in the dropdown above for setup instructions.');
}

// Show/refresh the remote-DB ⨉ local-vault recommendation banner from the two
// currently-selected providers (Application Data + Secrets Vault). Safe to call
// any time the user changes either dropdown.
function renderVaultRecommendation(m) {
  const el = m && m.secretsMultihost;
  if (!el) return;
  const html = vaultRecommendationHtml(
    m.provider ? m.provider.value : '',
    m.secretsProv ? m.secretsProv.value : '',
  );
  el.innerHTML = html;
  el.style.display = html ? 'block' : 'none';
}

const GENUI_NOTES = {
  filesystem: notesHtml(
    ['<b>On this device only.</b> Pages are files on this machine’s disk — nothing to set up.', 'Inspectable on disk, easy to hand-edit, git-commit, or back up.', 'Fastest serving path (static file read).'],
    ['<b>Pages do NOT follow you to your other devices</b> — each machine keeps its own separate set.', 'Wiped on container restart in stateless cloud deploys unless a persistent disk is mounted.', 'Not searchable across pages by content.'],
    'Default. Best when you only ever use this one machine.'
  ),
  database: notesHtml(
    ['<b>In your account — synced to all your devices.</b> A page you build on your laptop is already there on your phone.', 'Survives restarts everywhere; backed up with the rest of your account data; encrypted at rest with the database.', 'Searchable across page content and metadata.'],
    ['Loses the "real file on disk" feel — no hand-editing from a text editor.', 'Slightly slower serving (a database read per request instead of a static file).'],
    'Recommended if you use more than one device — this is what makes your pages sync.'
  ),
  hybrid: notesHtml(
    ['Page list (title, timestamps, owner) lives in your account and syncs across devices; the HTML body stays on disk.', 'The page catalog and search survive restarts; bodies remain hand-editable as files.'],
    ['<b>Only the list syncs — the page bodies stay on this device</b>, so other devices see the entry but not the full page unless the disk is shared.', 'Two writes per save (account metadata + file).'],
    'A middle option: a synced, searchable catalog plus direct file access to bodies. For full cross-device pages, use "In your account".'
  ),
};

const ENC_NOTES = {
  none: notesHtml(
    ['No setup, no key management, full performance.'],
    ['OAuth tokens and API keys are plaintext in the DB.', 'Anyone with DB read access can immediately use your tenants\' credentials.', 'A leaked database backup leaks every secret with it.'],
    'Acceptable only for ephemeral dev. Switch to <strong>field</strong> before going to production.'
  ),
  field: notesHtml(
    ['Per-tenant Fernet encryption on <code>auth_elements.secret_ref</code>.', 'Wrapped DEKs live in the Secrets Vault — never in the application DB.', 'One tenant\'s key compromise affects only that tenant\'s rows.', 'Works on every database backend.'],
    ['Adds ~1ms vault round-trip on the first read per tenant (cached after).', 'Requires a real vault provider (not <strong>inline_db</strong>) — otherwise keys and ciphertext live in the same DB.', 'Schema migration adds a <code>tenant_key_meta</code> table.'],
    'Recommended default for any deployment with real users.'
  ),
};

function renderNotes(elt, map, key) {
  if (!elt) return;
  const html = map[key];
  if (!html) { elt.style.display = 'none'; return; }
  elt.style.display = 'block';
  elt.innerHTML = html;
}

// Put a circled "?" account-setup guide beside a Provider/Vault <label>, swapped
// to match the currently-selected provider. The label text stays put; only the
// badge's guide changes, so we replace the single badge in place each time.
function renderSetupTip(labelEl, map, key) {
  if (!labelEl) return;
  const old = labelEl.querySelector(':scope > .ac-field-tip');
  if (old) old.remove();
  const badge = tipBadge(map[key]);
  if (badge) labelEl.appendChild(badge);
}

// ── Provider field templates ───────────────────────────────────────────────
const FIELD_SPECS = {
  // SQLite needs no connection fields — the database always lives at the fixed
  // default location under data/db/. There is deliberately no user-customisable
  // path (the file-path input was removed); an empty spec renders no fields and
  // the backend falls back to DEFAULT_DB_PATH.
  sqlite: [],
  postgres: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost or db.example.com', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432', default: 5432 },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'webagent', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'webagent', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '••••', keep: true },
    { key: 'ssl_mode', label: 'SSL', type: 'select', options: ['disable', 'require', 'verify-ca', 'verify-full'], default: 'require' },
  ],
  postgres: [
    { key: 'host', label: 'Host', type: 'text', required: true,
      tip: 'Hostname or IP of your Postgres server — from your provider\'s connection details. Use localhost only if Postgres runs on this same machine.' },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432',
      tip: 'TCP port Postgres listens on. The default is 5432; leave blank to use it.' },
    { key: 'database', label: 'Database name', type: 'text', required: true,
      tip: 'The database to use inside the server. Create one first (e.g. "webagent") if it does not exist yet.' },
    { key: 'username', label: 'Username', type: 'text', required: true,
      tip: 'A Postgres role that can read/write that database. Auto-Create Tables also needs rights to create tables.' },
    { key: 'password', label: 'Password', type: 'password',
      tip: 'Password for that role. Leave blank only if the server allows password-less (trust) auth.' },
    { key: 'ssl_mode', label: 'SSL mode', type: 'select', options: ['disable','require','verify-ca','verify-full'],
      tip: 'How strictly to encrypt the connection. Use "require" or stronger for anything reached over a network; "disable" is for localhost only.' },
  ],
  aws_rds: [
    { key: 'host', label: 'Endpoint (e.g. mydb.xxxx.us-east-1.rds.amazonaws.com)', type: 'text', required: true,
      tip: 'AWS Console → RDS → your instance → Connectivity & security → "Endpoint". Make sure its security group allows port 5432 from this server.' },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432',
      tip: 'Shown next to the endpoint in RDS. Default 5432.' },
    { key: 'database', label: 'Database name', type: 'text', required: true,
      tip: 'The "Initial database name" you set when creating the instance (or one you created afterwards).' },
    { key: 'username', label: 'Username', type: 'text', required: true,
      tip: 'The master username you set when creating the RDS instance (or a user you granted access to).' },
    { key: 'password', label: 'Password', type: 'password',
      tip: 'The master password you set at creation. Reset it from the RDS console if you have lost it.' },
  ],
  gcp_cloud_sql: [
    { key: 'host', label: 'Public IP / Cloud SQL proxy host', type: 'text', required: true,
      tip: 'Cloud Console → SQL → your instance → "Public IP address". Add this server\'s IP under Connections → Authorized networks, or use the Cloud SQL Auth Proxy and put its host here.' },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432',
      tip: 'Postgres port. Default 5432.' },
    { key: 'database', label: 'Database name', type: 'text', required: true,
      tip: 'A database created under the instance\'s Databases tab.' },
    { key: 'username', label: 'Username', type: 'text', required: true,
      tip: 'A database user from the instance\'s Users tab (e.g. the default "postgres" user).' },
    { key: 'password', label: 'Password', type: 'password',
      tip: 'The password set for that user when the instance or user was created.' },
  ],
  azure_postgres: [
    { key: 'host', label: 'Server name (e.g. myserver.postgres.database.azure.com)', type: 'text', required: true,
      tip: 'Azure Portal → your PostgreSQL flexible server → Overview → "Server name". Add a firewall rule for this server\'s IP under Networking.' },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432',
      tip: 'Postgres port. Default 5432.' },
    { key: 'database', label: 'Database name', type: 'text', required: true,
      tip: 'A database on the server (the default is often "postgres"); create one under the Databases blade.' },
    { key: 'username', label: 'Username', type: 'text', required: true,
      tip: 'The admin username you set when creating the server (Entra ID auth is also possible but not used here).' },
    { key: 'password', label: 'Password', type: 'password',
      tip: 'The admin password set at creation; reset it from the server\'s Overview if needed.' },
  ],
  neon: [
    { key: 'host', label: 'Endpoint (e.g. ep-xxxx.neon.tech)', type: 'text', required: true,
      tip: 'Neon dashboard → your project → Connect / Connection Details. Copy the host part of the connection string (ends in .neon.tech).' },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432',
      tip: 'Postgres port. Default 5432.' },
    { key: 'database', label: 'Database name', type: 'text', required: true,
      tip: 'The database name from the same Connection Details panel (often "neondb").' },
    { key: 'username', label: 'Username', type: 'text', required: true,
      tip: 'The role/username shown in the connection string.' },
    { key: 'password', label: 'Password', type: 'password',
      tip: 'The password shown in Neon\'s Connection Details (click "Show password"). You can roll it from the dashboard.' },
  ],
};

function renderFields(m, provider, existing) {
  if (!m || !m.fields) return;
  // Account-setup guide beside the "Provider" label (changes with the choice).
  renderSetupTip(m.provider && m.provider.previousElementSibling, DB_SETUP, provider);
  applyProviderButtons(m, provider);     // hide/relabel buttons per provider
  m.fields.innerHTML = '';
  const specs = FIELD_SPECS[provider] || [];
  for (const spec of specs) {
    const wrap = document.createElement('div');
    wrap.style.gridColumn = (spec.type === 'number' || spec.key === 'ssl_mode') ? 'span 1' : 'span 2';
    const lbl = document.createElement('label');
    lbl.style.cssText = 'display:block;font-size:11px;margin-bottom:2px;opacity:0.8;';
    if (spec.labelLink) {                     // the label text itself is a link (e.g. "Project URL" → org dashboard)
      const a = document.createElement('a');
      a.href = spec.labelLink;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = spec.label;
      a.style.cssText = 'color:var(--accent);text-decoration:none;';
      lbl.appendChild(a);
      if (spec.required) lbl.appendChild(document.createTextNode(' *'));
    } else {
      lbl.textContent = spec.label + (spec.required ? ' *' : '');
    }
    const fieldTip = tipBadge(spec.tip);     // "where do I find this value?" "?" badge
    if (fieldTip) lbl.appendChild(fieldTip);
    if (spec.linkFrom) {                      // live deep-link beside the label (e.g. "Project API Keys")
      const link = document.createElement('a');
      link.className = 'ac-field-deeplink';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = spec.linkFrom.label;
      link.style.cssText = 'margin-left:8px;font-size:11px;font-weight:500;color:var(--accent);text-decoration:none;display:none;';
      link.dataset.linkFor = spec.key;
      lbl.appendChild(link);
    }
    wrap.appendChild(lbl);
    let input;
    if (spec.type === 'select') {
      input = document.createElement('select');
      for (const opt of spec.options) {
        const o = document.createElement('option');
        o.value = opt; o.textContent = opt;
        input.appendChild(o);
      }
    } else {
      input = document.createElement('input');
      input.type = spec.type;
      if (spec.placeholder) input.placeholder = spec.placeholder;
    }
    input.dataset.fieldKey = spec.key;
    input.style.cssText = 'width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;background:transparent;color:inherit;';
    if (existing && existing[spec.key] != null) input.value = existing[spec.key];
    wrap.appendChild(input);
    m.fields.appendChild(wrap);
  }
  // Now that every field exists, wire each live deep-link to its source field so
  // it appears/updates as the user types the source value (and on first render).
  for (const spec of specs) {
    if (!spec.linkFrom) continue;
    const link = m.fields.querySelector(`.ac-field-deeplink[data-link-for="${spec.key}"]`);
    const src = m.fields.querySelector(`[data-field-key="${spec.linkFrom.sourceKey}"]`);
    if (!link || !src) continue;
    const refresh = () => {
      const href = spec.linkFrom.build(src.value);
      if (href) { link.href = href; link.style.display = ''; }
      else { link.removeAttribute('href'); link.style.display = 'none'; }
    };
    src.addEventListener('input', refresh);
    refresh();                                // reflect any pre-filled URL on load
  }
}

// Every remaining provider is plain Postgres that can run DDL directly,
// so the one-click "Auto-Create Tables" button is always shown.
function applyProviderButtons(m, provider) {
  if (!m) return;
  if (m.btnBootstrap) m.btnBootstrap.style.display = '';
  if (m.btnShowSQL) m.btnShowSQL.textContent = 'Show Schema SQL';
}

function collectFields(m) {
  if (!m) return {};
  const provider = m.provider.value;
  const specs = FIELD_SPECS[provider] || [];
  const o = { provider };
  for (const spec of specs) {
    const el = m.fields.querySelector(`[data-field-key="${spec.key}"]`);
    if (!el) continue;
    let v = el.value;
    if (spec.type === 'number' && v) v = parseInt(v, 10);
    if (v === '') v = null;
    o[spec.key] = v;
  }
  return o;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

/** Race fetch() against an AbortController timeout (default 20s). On timeout,
 *  throws with name 'AbortError' so callers can distinguish timeout from other
 *  failures. Prevents badges from hanging on "loading…" forever. */
async function _fetchWithTimeout(url, opts, ms = 20000) {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { ...(opts || {}), signal: ctrl.signal });
  } finally {
    clearTimeout(id);
  }
}

// ── API ─────────────────────────────────────────────────────────────────────

async function loadConfig(m) {
  const u = uid();
  const res = await _fetchWithTimeout(apiPath(`/admin/storage/config?requesting_user_id=${encodeURIComponent(u)}`));
  if (!res.ok) {
    out(m.output, `Failed to load config (status ${res.status}). Are you signed in as an admin?`, false);
    return null;
  }
  return res.json();
}

function applyState(m, state) {
  if (!state) return;
  m.envBanner.style.display = state.env_locked ? 'block' : 'none';
  const dbCfg = state.db.active || {};
  m.provider.value = dbCfg.provider || 'sqlite';
  renderFields(m, m.provider.value, dbCfg);
  renderNotes(m.dbNotes, DB_NOTES, m.provider.value);
  m.activeBadge.textContent = `active: ${dbCfg.provider || 'sqlite'}`;
  const sec = state.secrets || {};
  m._lastState = state;  // stashed so the provider-change handler can re-render fields
  m.secretsProv.value = sec.provider || 'inline_db';
  renderNotes(m.secretsNotes, SECRETS_NOTES, m.secretsProv.value);
  renderSetupTip(m.secretsProv.previousElementSibling, SECRETS_SETUP, m.secretsProv.value);
  renderSecretsFields(m, state);
  m.secretsBadge.textContent = `active: ${sec.provider || 'inline_db'}`;
  m.secretsWarn.style.display = (sec.provider === 'inline_db') ? 'block' : 'none';
  renderVaultRecommendation(m);
  renderSecretsRestart(m, state);
  const pgs = state.genui || {};
  if (m.genuiMode) {
    m.genuiMode.value = pgs.mode || 'filesystem';
    renderNotes(m.genuiNotes, GENUI_NOTES, m.genuiMode.value);
    if (m.genuiBadge) m.genuiBadge.textContent = `active: ${pgs.mode || 'filesystem'}`;
  }
  const lock = !!state.env_locked;
  for (const btn of [m.btnSave, m.btnActivate, m.secretsSave, m.genuiSave, m.genuiMigrate, m.encKekGen, m.encKekRotate]) {
    if (btn) { btn.disabled = lock; btn.style.opacity = lock ? '0.45' : '1'; }
  }
  // The field-encryption toggle is disabled (pointer-events off via the class)
  // when config is env-locked, like the whole-DB toggles.
  if (m.encLevelTri) {
    m.encLevelTri.classList.toggle('ac-tri-disabled', lock);
    m.encLevelTri.setAttribute('tabindex', lock ? '-1' : '0');
  }
}

async function loadEncryption(m) {
  if (!m || !m.encLevelTri) return;
  const u = uid();
  const res = await _fetchWithTimeout(apiPath(`/admin/storage/encryption/config?requesting_user_id=${encodeURIComponent(u)}`));
  if (!res.ok) {
    if (m.encBadge) m.encBadge.textContent = `error ${res.status}`;
    return;
  }
  const s = await res.json();
  const on = !!(s.level && s.level !== 'none');
  m.encLevelTri.dataset.state = on ? 'on' : 'off';
  renderNotes(m.encNotes, ENC_NOTES, on ? 'field' : 'none');
  // The header badge reflects whole-file encryption (set by loadFullDb); this
  // field-level badge + status sub-line live inside the Advanced disclosure.
  if (m.encLevelBadge) m.encLevelBadge.textContent = on ? `on: ${s.level}` : 'off';
  if (m.encVaultWarn) m.encVaultWarn.style.display = s.warning ? 'block' : 'none';
  if (s.warning && m.encVaultWarn) m.encVaultWarn.textContent = s.warning;
}

// ── Per-secret field encryption toggle ───────────────────────────────────────
// One hold-until-confirmed toggle replaces the former level <select> + Activate
// Level + Encrypt Existing Rows + Decrypt Back to Plaintext buttons. Turning it
// ON = activate the "field" level then encrypt existing rows; turning it OFF =
// decrypt every row back to plaintext then set the level to "none". The knob
// moves only after BOTH steps succeed (like the whole-DB toggles); any failure
// surfaces in the output box and leaves the toggle where it was.

/** POST a new field-encryption level. Returns true on a confirmed save. */
async function _setFieldLevel(m, level, confirmFlag) {
  const r = await call('/admin/storage/encryption/level', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: uid(), level, confirm: !!confirmFlag }),
  });
  const ok = !!(r.body && r.body.ok);
  if (!ok) out(m.encOutput, r.body || { error: 'no response' }, false);
  return ok;
}

/** Encrypt all existing plaintext rows under the active level (idempotent). */
async function _migrateRows(m) {
  const r = await call('/admin/storage/encryption/migrate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: uid() }),
  });
  const ok = !!(r.body && r.body.ok);
  if (!ok) out(m.encOutput, r.body || { error: 'no response' }, false);
  return ok;
}

/** Decrypt every encrypted row back to plaintext (required before level none). */
async function _decryptAllRows(m) {
  const r = await call('/admin/storage/encryption/decrypt-all', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: uid() }),
  });
  const ok = !!(r.body && r.body.ok);
  if (!ok) out(m.encOutput, r.body || { error: 'no response' }, false);
  return ok;
}

/** The field-encryption toggle — hold-until-confirmed, two server steps. */
async function _toggleFieldEnc(m) {
  const tri = m.encLevelTri;
  if (!tri || tri.classList.contains('ac-tri-disabled')) return;
  const want = tri.dataset.state !== 'on';
  const opts = want
    ? { tone: 'warning', title: 'Turn on per-secret field encryption?', confirmLabel: 'Turn on',
        message: 'Each secret value will be encrypted under a per-tenant key, and your existing secrets will be converted now. Requires a real secrets vault (not the plaintext app DB).' }
    : { tone: 'warning', title: 'Turn off field encryption?', confirmLabel: 'Turn off',
        message: 'Your encrypted secrets will be converted back to plaintext, then field encryption will be switched off.' };
  if (!await hazardConfirm(opts)) return;

  _markSaving(tri);
  out(m.encOutput, want ? 'Activating field encryption…' : 'Removing field encryption…', null);
  let ok;
  if (want) {
    // ON: set the level first, then encrypt the existing rows under it.
    ok = await _setFieldLevel(m, 'field', false);
    if (ok) ok = await _migrateRows(m);
  } else {
    // OFF: decrypt every row back to plaintext BEFORE dropping the level to none
    // (rows would be unreadable through the decorator path otherwise).
    ok = await _decryptAllRows(m);
    if (ok) ok = await _setFieldLevel(m, 'none', true);
  }
  if (ok) {
    tri.dataset.state = want ? 'on' : 'off';                 // move knob post-save
    if (m.encLevelBadge) m.encLevelBadge.textContent = want ? 'on: field' : 'off';
    renderNotes(m.encNotes, ENC_NOTES, want ? 'field' : 'none');
    out(m.encOutput, want
      ? 'Field encryption is on — existing secrets encrypted.'
      : 'Field encryption is off — secrets returned to plaintext.', true);
    loadTenants(m);
  }
  _flashSaveCheck(tri, ok);
}

async function loadTenants(m) {
  if (!m || !m.encTenants) return;
  const u = uid();
  const res = await _fetchWithTimeout(apiPath(`/admin/storage/encryption/tenants?requesting_user_id=${encodeURIComponent(u)}`));
  if (!res.ok) return;
  const body = await res.json();
  const tenants = body.tenants || [];
  if (!tenants.length) {
    m.encTenants.style.display = 'block';
    m.encTenants.innerHTML = '<div style="opacity:0.6;">No tenants with key material yet. Run "Encrypt Existing Rows" to provision DEKs.</div>';
    return;
  }
  m.encTenants.style.display = 'block';
  m.encTenants.innerHTML = '';
  for (const t of tenants) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border);';
    const label = document.createElement('span');
    label.style.cssText = 'flex:1;font-family:monospace;';
    label.textContent = `${t.user_id} — active v${t.active_version ?? '–'} (${t.total_versions} versions)`;
    const btn = document.createElement('button');
    btn.className = 'ac-btn';
    btn.style.cssText = 'padding:2px 8px;font-size:10px;';
    btn.textContent = 'Rotate DEK';
    btn.onclick = async () => {
      if (!confirm(`Rotate DEK for user "${t.user_id}"? This re-encrypts all of their rows.`)) return;
      out(m.encOutput, `Rotating DEK for ${t.user_id}...`, null);
      const r = await call('/admin/storage/encryption/dek/rotate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: uid(), user_id: t.user_id }),
      });
      out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
      loadTenants(m);
    };
    row.appendChild(label);
    row.appendChild(btn);
    m.encTenants.appendChild(row);
  }
}

async function call(path, opts = {}) {
  const res = await _fetchWithTimeout(apiPath(path), opts);
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body };
}

// ── Full-database (whole-file SQLCipher) encryption ───────────────────────────
// Rendered as the ability-tree group pattern: a MASTER tri-toggle on the
// Encryption row header (Off · Mixed · On, over the private databases) for
// one-click activate/deactivate, plus a per-database toggle for each file so any
// single one can be untoggled. The public Wiki is a documented EXCEPTION (it is
// git-tracked public content — encrypting it would diverge the tracked file from
// the repo), so it is kept OUT of the master's all-on/all-off scope and shown as
// its own labelled row.
//
// HOLD-UNTIL-CONFIRMED: every toggle is the same click → spinner → ✓/⚠ flow the
// agent ability toggles use. The knob is moved ONLY after the change is saved
// (never optimistically), so a failed save leaves the toggle exactly where it
// was. The whole-file conversion itself still happens at the next restart; a
// toggle here only records intent.

const _DB_SHORT = { local: 'Application data', vault: 'Secrets vault', logs: 'Activity logs', recordings: 'Recordings', wiki: 'Public wiki' };
const _DB_ICON = { local: 'server', vault: 'key-round', logs: 'file-text', recordings: 'clapperboard', wiki: 'globe' };

/** Status sub-line for one database row (encrypted / plaintext / pending). */
function _dbStatusHtml(d) {
  const pending = (!!d.enabled !== !!d.encrypted_on_disk)
    ? ' · <span style="color:var(--warning);">pending restart</span>' : '';
  if (!d.exists) return `<span style="opacity:0.5;">Not created yet</span>${d.enabled ? pending : ''}`;
  if (d.encrypted_on_disk) return `<span style="color:var(--success);">Encrypted on disk</span>${pending}`;
  return `<span style="opacity:0.6;">Plaintext on disk</span>${pending}`;
}

/** Re-derive the master tri-toggle + header badge from the cached status. The
 *  master spans the PRIVATE databases only (Wiki is the exception). */
function _syncMaster(m) {
  const s = m._fulldbStatus;
  if (!s) return;
  const privates = (s.databases || []).filter(d => d.id !== 'wiki');
  const onCount = privates.filter(d => d.enabled).length;
  if (m.encMaster) {
    m.encMaster.dataset.state = onCount === 0 ? 'off'
      : (onCount === privates.length ? 'on' : 'mixed');
  }
  if (m.encBadge) {
    m.encBadge.textContent = !s.sqlcipher_available ? 'engine not installed'
      : (onCount === 0 ? 'all plaintext' : `on: ${onCount}/${privates.length}`);
  }
}

/** Persist one database's intent. Returns true on a confirmed save; surfaces the
 *  reason in the output box on failure. Never touches the toggle. */
async function _setDb(m, dbId, enabled) {
  const r = await call('/admin/storage/encryption/full-db/set', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: uid(), db_id: dbId, enabled }),
  });
  const ok = !!(r.body && r.body.ok);
  if (!ok) out(m.fulldbOutput, r.body || { error: 'no response' }, false);
  return ok;
}

/** Per-database toggle — hold-until-confirmed. */
async function _toggleDb(m, d, switchEl, statusEl) {
  if (switchEl.classList.contains('ac-tri-disabled')) return;
  const want = !d.enabled;
  const short = _DB_SHORT[d.id] || d.label || d.id;
  let opts;
  if (d.id === 'wiki') {
    opts = want
      ? { tone: 'danger', title: 'Encrypt the public Wiki database?', confirmLabel: 'Encrypt anyway',
          message: 'The Wiki is tracked in git as PUBLIC content. Encrypting it makes the tracked file unreadable and permanently diverges it from the repository. Only continue if you understand this.' }
      : { tone: 'warning', title: 'Decrypt the Wiki database?', confirmLabel: 'Decrypt',
          message: 'It will be converted back to plaintext on the next server restart.' };
  } else {
    opts = want
      ? { tone: 'warning', title: `Encrypt the "${short}" database?`, confirmLabel: 'Encrypt',
          message: 'It will be encrypted at rest (backup taken first) on the next server restart.' }
      : { tone: 'warning', title: `Decrypt the "${short}" database?`, confirmLabel: 'Decrypt',
          message: 'It will be converted back to plaintext on the next server restart.' };
  }
  if (!await hazardConfirm(opts)) return;
  _markSaving(switchEl);
  const ok = await _setDb(m, d.id, want);
  if (ok) {
    d.enabled = want;                       // move the knob ONLY now (post-save)
    switchEl.dataset.state = want ? 'on' : 'off';
    if (statusEl) statusEl.innerHTML = _dbStatusHtml(d);
    out(m.fulldbOutput, want
      ? `"${short}" will be encrypted at rest on the next server restart.`
      : `"${short}" will be decrypted on the next server restart.`, true);
    _syncMaster(m);
  }
  _flashSaveCheck(switchEl, ok);
}

/** Master toggle — turn ALL private databases on (right half) or off (left
 *  half), hold-until-confirmed across the batch. */
async function _setAll(m, enable) {
  const s = m._fulldbStatus;
  if (!s) return;
  // Disabled master is kept clickable (pointer-events:auto below) so it can EXPLAIN
  // why rather than silently letting the click expand the row — surface the reason.
  if (m.encMaster && m.encMaster.classList.contains('ac-tri-disabled')) {
    out(m.fulldbOutput, { error: s.reason || 'Encryption cannot be managed on this host.' }, false);
    return;
  }
  const privates = (s.databases || []).filter(d => d.id !== 'wiki');
  const targets = privates.filter(d => !!d.enabled !== enable);
  if (!targets.length) return;
  if (enable && !s.can_enable) { out(m.fulldbOutput, { error: s.reason || 'Cannot enable here' }, false); return; }
  const n = targets.length;
  const plural = n > 1 ? 's' : '';
  if (!await hazardConfirm(enable
    ? { tone: 'warning', title: `Encrypt all ${n} database${plural}?`, confirmLabel: 'Encrypt all',
        message: `Every database is encrypted at rest (each backed up first) on the next server restart.` }
    : { tone: 'warning', title: `Decrypt all ${n} database${plural}?`, confirmLabel: 'Decrypt all',
        message: `Every database is converted back to plaintext on the next server restart.` })) return;
  _markSaving(m.encMaster);
  let allOk = true;
  for (const d of targets) {
    const ok = await _setDb(m, d.id, enable);
    if (ok) {
      d.enabled = enable;
      const row = m._dbRows && m._dbRows[d.id];
      if (row) {
        row.switchEl.dataset.state = enable ? 'on' : 'off';
        if (row.statusEl) row.statusEl.innerHTML = _dbStatusHtml(d);
      }
    } else allOk = false;
  }
  _syncMaster(m);
  out(m.fulldbOutput, allOk
    ? `All databases will be ${enable ? 'encrypted' : 'decrypted'} on the next server restart.`
    : 'Some databases could not be updated — see above.', allOk);
  _flashSaveCheck(m.encMaster, allOk);
}

/** Wire the master tri once: left half → all off, right half → all on. */
function _wireMaster(m) {
  if (!m.encMaster || m.encMaster._wired) return;
  m.encMaster._wired = true;
  m.encMaster.addEventListener('click', (e) => {
    e.stopPropagation();
    const r = m.encMaster.getBoundingClientRect();
    _setAll(m, (e.clientX - r.left) > r.width / 2);
  });
  m.encMaster.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _setAll(m, m.encMaster.dataset.state !== 'on'); }
  });
}

async function loadFullDb(m) {
  if (!m || !m.fulldbList) return;
  const u = uid();
  const res = await _fetchWithTimeout(apiPath(`/admin/storage/encryption/full-db/status?requesting_user_id=${encodeURIComponent(u)}`));
  if (!res.ok) {
    if (m.encBadge) m.encBadge.textContent = `error ${res.status}`;
    return;
  }
  const s = await res.json();
  m._fulldbStatus = s;
  const dbs = s.databases || [];
  const canToggle = !!s.can_enable && !s.env_locked;

  // Banner: why it can't be enabled (engine missing / no keyring / env-locked).
  if (m.fulldbWarning) {
    if (!s.can_enable && s.reason) {
      m.fulldbWarning.style.display = 'block';
      m.fulldbWarning.textContent = s.reason;
    } else {
      m.fulldbWarning.style.display = 'none';
    }
  }

  // Build per-database rows. Privates first; the public Wiki last as a labelled
  // exception (sits outside the master's scope).
  m.fulldbList.innerHTML = '';
  m._dbRows = {};
  const ordered = [...dbs.filter(d => d.id !== 'wiki'), ...dbs.filter(d => d.id === 'wiki')];
  for (const d of ordered) {
    const isWiki = d.id === 'wiki';
    const row = document.createElement('div');
    row.className = 'ac-ability-row enc-db-row';
    if (isWiki) row.style.cssText = 'border-top:var(--border-width) solid var(--border);margin-top:6px;padding-top:10px;';

    const ic = document.createElement('span');
    ic.className = 'ac-ability-icon';
    ic.innerHTML = `<i data-lucide="${_DB_ICON[d.id] || 'database'}" class="lucide-icon" style="width:16px;height:16px;color:var(--${isWiki ? 'muted' : 'accent'});"></i>`;

    const lab = document.createElement('div');
    lab.className = 'ac-ability-label';
    const tag = isWiki ? ' <span style="font-size:10px;color:var(--muted);border:var(--border-width) solid var(--border);padding:0 5px;border-radius:4px;margin-left:4px;">exception</span>' : '';
    lab.innerHTML = `<div class="ac-ability-name" style="font-size:13px;">${esc(_DB_SHORT[d.id] || d.label || d.id)}${tag}</div><div class="ac-ability-desc enc-db-status"></div>`;
    const statusEl = lab.querySelector('.enc-db-status');
    if (isWiki && !d.enabled) {
      statusEl.textContent = 'Plaintext — public SEO content, tracked in git. Left readable on purpose.';
      statusEl.style.color = 'var(--muted)';
    } else {
      statusEl.innerHTML = _dbStatusHtml(d);
    }

    const sw = document.createElement('span');
    sw.className = 'ac-tri' + (canToggle ? '' : ' ac-tri-disabled');
    sw.dataset.state = d.enabled ? 'on' : 'off';
    sw.setAttribute('role', 'button');
    sw.setAttribute('tabindex', canToggle ? '0' : '-1');
    sw.title = canToggle ? 'Encrypt this database at rest' : (s.reason || 'Cannot enable here');
    sw.innerHTML = '<span class="ac-tri-knob"></span>';
    if (canToggle) {
      sw.addEventListener('click', (e) => { e.stopPropagation(); _toggleDb(m, d, sw, statusEl); });
      sw.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleDb(m, d, sw, statusEl); } });
    }

    row.appendChild(ic);
    row.appendChild(lab);
    row.appendChild(sw);
    m.fulldbList.appendChild(row);
    m._dbRows[d.id] = { switchEl: sw, statusEl };
  }

  // Master state + wiring.
  if (m.encMaster) {
    m.encMaster.classList.toggle('ac-tri-disabled', !canToggle);
    // The disabled tri's CSS sets pointer-events:none, which would let the click
    // pass THROUGH to the row header and expand the row. Override it so the
    // master always receives its own click (its handler stops propagation and
    // shows the reason) — the master lives in the header, unlike per-db rows.
    m.encMaster.style.pointerEvents = canToggle ? '' : 'auto';
    m.encMaster.setAttribute('tabindex', canToggle ? '0' : '-1');
    _wireMaster(m);
  }
  _syncMaster(m);
  _refreshLucideIcons(m.fulldbList);
}

/** Hybrid local-first — one hold-until-confirmed toggle. Off by default; only
 *  meaningful with a shared remote DB (the badge/note say so when there's none).
 *  Applied on the next restart, like full-DB encryption. See app/db/hybrid.py. */
async function loadHybrid(m) {
  if (!m || !m.hybridToggle) return;
  let s;
  try {
    const res = await _fetchWithTimeout(apiPath(`/admin/storage/hybrid/status?requesting_user_id=${encodeURIComponent(uid())}`));
    if (!res.ok) { if (m.hybridBadge) m.hybridBadge.textContent = `error ${res.status}`; return; }
    s = await res.json();
  } catch (e) { if (m.hybridBadge) m.hybridBadge.textContent = 'error'; return; }

  const canToggle = !s.env_locked;
  m.hybridToggle.dataset.state = s.enabled ? 'on' : 'off';
  m.hybridToggle.classList.toggle('ac-tri-disabled', !canToggle);
  if (m.hybridBadge) {
    m.hybridBadge.textContent = s.enabled
      ? (s.remote_active ? 'on' : 'on · no remote yet')
      : 'off';
  }
  if (m.hybridNote) {
    if (!s.remote_active) {
      m.hybridNote.style.display = 'block';
      m.hybridNote.textContent = 'No shared remote database is active here, so hybrid mode stays a no-op until a Postgres remote is connected.';
    } else {
      m.hybridNote.style.display = 'none';
    }
  }
  m.hybridToggle.title = canToggle ? 'Turn hybrid local-first on/off' : 'Config is env-locked on this deployment';
  if (!canToggle || m.hybridToggle._wired) return;
  m.hybridToggle._wired = true;

  const doToggle = async () => {
    if (m.hybridToggle.classList.contains('ac-tri-disabled')) return;
    const want = m.hybridToggle.dataset.state !== 'on';
    const ok = await hazardConfirm(want
      ? { tone: 'warning', title: 'Turn on hybrid local-first?', confirmLabel: 'Enable',
          message: "New chat detail (heavy tool payloads) will be kept in this device's local database; only the shared skeleton goes to the remote. Takes effect on the next server restart." }
      : { tone: 'warning', title: 'Turn off hybrid local-first?', confirmLabel: 'Disable',
          message: 'Full chat detail will be written to the remote database again on the next server restart.' });
    if (!ok) return;
    _markSaving(m.hybridToggle);
    const r = await call('/admin/storage/hybrid/set', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), enabled: want }),
    });
    const saved = !!(r.body && r.body.ok);
    if (saved) {
      m.hybridToggle.dataset.state = want ? 'on' : 'off';
      out(m.hybridOutput, (r.body && r.body.message) || 'Saved — takes effect on the next restart.', true);
      loadHybrid(m);  // refresh badge + note
    } else {
      out(m.hybridOutput, r.body || { error: 'no response' }, false);
    }
    _flashSaveCheck(m.hybridToggle, saved);
  };
  m.hybridToggle.addEventListener('click', (e) => { e.stopPropagation(); doToggle(); });
  m.hybridToggle.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); doToggle(); } });
}

function buildBody(m, extra) {
  return JSON.stringify({ requesting_user_id: uid(), ...collectFields(m), ...(extra || {}) });
}

// ── Config share / import — REMOVED (folded into the app-level setup bundle) ─
// The per-row "Share (QR)" / "Paste Config" that carried just the Application
// Data connection to another device were replaced by the whole-install setup
// bundle on the Deployment card's "This app" row (deploy.js _bootShareBody /
// _bootImportBody → app/admin/bootstrap_bundle.py), which shares DB + vault +
// LLM + admin as ONE encrypted, admin-password-protected code. The encode/
// decode/QR-popover helpers and the wire() bindings for those buttons were
// deleted with them.

// ── Handlers ────────────────────────────────────────────────────────────────

function wire(m) {
  if (!m) return;

  m.provider.addEventListener('change', () => {
    renderFields(m, m.provider.value, null);
    renderNotes(m.dbNotes, DB_NOTES, m.provider.value);
    // The DB provider feeds the remote-DB ⨉ local-vault recommendation too.
    renderVaultRecommendation(m);
  });

  if (m.secretsProv) {
    m.secretsProv.addEventListener('change', () => {
      renderNotes(m.secretsNotes, SECRETS_NOTES, m.secretsProv.value);
      renderSetupTip(m.secretsProv.previousElementSibling, SECRETS_SETUP, m.secretsProv.value);
      renderSecretsFields(m, m._lastState);
      renderVaultRecommendation(m);
    });
  }

  if (m.genuiMode) {
    m.genuiMode.addEventListener('change', () => {
      renderNotes(m.genuiNotes, GENUI_NOTES, m.genuiMode.value);
    });
  }

  // Field-encryption toggle — left/right half both flip it (it's binary); the
  // _toggleFieldEnc handler runs the two-step activate/deactivate with the same
  // hold-until-confirmed flow as the whole-DB toggles.
  if (m.encLevelTri) {
    m.encLevelTri.addEventListener('click', (e) => { e.stopPropagation(); _toggleFieldEnc(m); });
    m.encLevelTri.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleFieldEnc(m); }
    });
  }

  // Advanced disclosure — folds away the field-level (per-secret) encryption +
  // key tools, mirroring the model table's Advanced row. Default collapsed.
  if (m.encAdvToggle && m.encAdvBody) {
    const chev = m.encAdvToggle.querySelector('.ac-row-chevron');
    const toggleAdv = (e) => {
      if (e) e.stopPropagation();
      const open = m.encAdvBody.style.display === 'none' || !m.encAdvBody.style.display;
      m.encAdvBody.style.display = open ? 'block' : 'none';
      if (chev) chev.style.transform = open ? 'rotate(90deg)' : '';
    };
    m.encAdvToggle.addEventListener('click', toggleAdv);
    m.encAdvToggle.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleAdv(e); }
    });
  }

  // Copy the current output box (schema SQL, test result, guidance) to the
  // clipboard. copyText handles localhost (secure context) and falls back to
  // execCommand on plain http://<lan-ip> so it works in every deploy.
  if (m.outputCopy) {
    m.outputCopy.addEventListener('click', () => {
      const text = m.output ? (m.output.textContent || '') : '';
      if (!text.trim()) return;
      copyText(text).then(() => flashCopied(m.outputCopy, true))
                    .catch(() => flashCopied(m.outputCopy, false));
    });
  }

  m.btnTest && m.btnTest.addEventListener('click', async () => {
    out(m.output, 'Testing connection...', null);
    const r = await call('/admin/storage/db/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: buildBody(m),
    });
    out(m.output, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.btnShowSQL && m.btnShowSQL.addEventListener('click', async () => {
    const provider = m.provider.value;
    const dialect = provider === 'sqlite' ? 'sqlite' : 'postgres';
    const r = await call('/admin/storage/db/schema-sql', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), dialect, idempotent: true }),
    });
    if (r.body && r.body.ddl) {
      out(m.output, r.body.ddl, true);
    } else {
      out(m.output, r.body || { error: 'no response' }, false);
    }
  });

  m.btnBootstrap && m.btnBootstrap.addEventListener('click', async () => {
    if (!confirm('Connect to the configured DB and create all WebAgent tables there? Idempotent.')) return;
    out(m.output, 'Bootstrapping schema...', null);
    const r = await call('/admin/storage/db/bootstrap', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: buildBody(m),
    });
    // Display plain-text guidance when the backend returns it (newlines preserved).
    // (Supabase removal: needs_manual_sql is never returned now, but the handler
    // stays for any future backend that needs multi-step setup instructions.)
    if (r.body && r.body.needs_manual_sql && r.body.error) {
      out(m.output, r.body.error, false);
      return;
    }
    out(m.output, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.btnSave && m.btnSave.addEventListener('click', async () => {
    out(m.output, 'Saving config...', null);
    const r = await call('/admin/storage/db/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: buildBody(m),
    });
    out(m.output, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.btnActivate && m.btnActivate.addEventListener('click', async () => {
    if (!confirm('Switch live database backend to this provider? In-flight requests will use the new backend on their next call.')) return;
    // Persist the form's provider + fields FIRST. The /db/activate endpoint reads
    // the SAVED connection config, not this form — so without a save here, changing
    // the dropdown and clicking Activate would re-activate the previously-saved
    // re-activate the previously-saved provider and bounce straight back to it.
    out(m.output, 'Saving config...', null);
    const saveRes = await call('/admin/storage/db/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: buildBody(m),
    });
    if (!(saveRes.body && saveRes.body.ok)) {
      out(m.output, saveRes.body || { error: 'Could not save config before activating.' }, false);
      return;
    }
    out(m.output, 'Activating...', null);
    const r = await call('/admin/storage/db/activate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    const ok = !!(r.body && r.body.ok);
    // A refused Activate (e.g. tables missing, or a silent local
    // fallback) carries a multi-line guidance string — show it as plain text.
    if (!ok && r.body && typeof r.body.error === 'string') {
      out(m.output, r.body.error, false);
    } else {
      out(m.output, r.body || { error: 'no response' }, ok);
    }
    loadConfig(m).then(s => applyState(m, s));
  });

  // (DB config share / import moved to the app-level setup bundle — see the
  // removal note above the Handlers section.)

  // Persist the selected provider's connection fields + token (only for
  // providers that have any). Returns the response, or null if there's nothing
  // to save. Used by both Test and Activate so the backend always has the
  // entered details before it tries to construct the vault.
  const saveSecretsCfg = async () => {
    const provider = m.secretsProv.value;
    if (!SECRETS_FIELDS[provider]) return null;
    const { config, token } = collectSecretsFields(m);
    return call('/admin/storage/secrets/provider-config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), provider, config, token }),
    });
  };

  m.secretsTest && m.secretsTest.addEventListener('click', async () => {
    const provider = m.secretsProv.value;
    // Save first so the probe reads the just-entered details (the token lands in
    // the keyring); then test the SELECTED provider without activating it.
    const saved = await saveSecretsCfg();
    if (saved && !(saved.body && saved.body.ok)) {
      out(m.secretsOutput, saved.body || { error: 'could not save connection details' }, false);
      return;
    }
    const r = await call('/admin/storage/secrets/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), provider }),
    });
    out(m.secretsOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
    // Refresh so the token field flips to "saved — leave blank to keep".
    loadConfig(m).then(s => applyState(m, s));
  });

  m.secretsSave && m.secretsSave.addEventListener('click', async () => {
    if (!confirm(`Switch secrets vault to "${m.secretsProv.value}"?`)) return;
    // Save the connection details + token first, then flip the active provider.
    const saved = await saveSecretsCfg();
    if (saved && !(saved.body && saved.body.ok)) {
      out(m.secretsOutput, saved.body || { error: 'could not save connection details' }, false);
      return;
    }
    const r = await call('/admin/storage/secrets/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), provider: m.secretsProv.value }),
    });
    out(m.secretsOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
    m.secretsWarn.style.display = (m.secretsProv.value === 'inline_db') ? 'block' : 'none';
    m.secretsBadge.textContent = `active: ${m.secretsProv.value}`;
    loadConfig(m).then(s => applyState(m, s));
  });

  m.genuiSave && m.genuiSave.addEventListener('click', async () => {
    if (!confirm(`Switch genui storage to "${m.genuiMode.value}"?`)) return;
    const r = await call('/admin/storage/genui/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), mode: m.genuiMode.value }),
    });
    out(m.genuiOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
    if (r.body && r.body.ok && m.genuiBadge) {
      m.genuiBadge.textContent = `active: ${m.genuiMode.value}`;
    }
    // After switching TO a synced store, offer to carry existing on-disk pages
    // across so they aren't left behind on this machine.
    if (r.body && r.body.ok && (m.genuiMode.value === 'database' || m.genuiMode.value === 'hybrid')) {
      if (confirm('Storage switched. Move the pages already on this device into your account now, so they sync to your other devices?')) {
        await _migrateGenui(m);
      }
    }
  });

  // Copy existing filesystem pages into the account (database) store via the
  // migrate endpoint. Non-destructive — the on-disk copies are left in place.
  async function _migrateGenui(m) {
    out(m.genuiOutput, 'Moving your pages into your account…', null);
    const r = await call('/admin/storage/genui/migrate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    const t = (r.body && r.body.totals) || {};
    if (r.body && r.body.ok) {
      const parts = [`${t.copied || 0} moved`];
      if (t.skipped) parts.push(`${t.skipped} already in your account`);
      if (t.failed) parts.push(`${t.failed} failed`);
      out(m.genuiOutput, `Done — ${parts.join(', ')}. Your pages now sync across your devices.`,
        t.failed ? false : true);
    } else {
      out(m.genuiOutput, r.body || { error: 'no response' }, false);
    }
  }

  m.genuiMigrate && m.genuiMigrate.addEventListener('click', () => _migrateGenui(m));

  m.exportBtn && m.exportBtn.addEventListener('click', async () => {
    out(m.migrateOutput, 'Exporting...', null);
    const u = encodeURIComponent(uid());
    const res = await fetch(apiPath(`/admin/storage/migrate/export?requesting_user_id=${u}`), { method: 'POST' });
    if (!res.ok) { out(m.migrateOutput, `Export failed (status ${res.status})`, false); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `webagent-export-${Date.now()}.json`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    out(m.migrateOutput, 'Exported. File downloaded.', true);
  });

  m.encTest && m.encTest.addEventListener('click', async () => {
    out(m.encOutput, 'Running round-trip probe...', null);
    const r = await call(`/admin/storage/encryption/test?requesting_user_id=${encodeURIComponent(uid())}`);
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.encKekGen && m.encKekGen.addEventListener('click', async () => {
    if (!confirm('Generate a new KEK and store it in the active secrets vault? Fails if one already exists — use Rotate KEK for replacements.')) return;
    out(m.encOutput, 'Generating KEK...', null);
    const r = await call('/admin/storage/encryption/kek/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.encKekRotate && m.encKekRotate.addEventListener('click', async () => {
    if (!confirm('Rotate KEK? Re-wraps every per-tenant DEK in the vault. Row data is not touched.')) return;
    out(m.encOutput, 'Rotating KEK...', null);
    const r = await call('/admin/storage/encryption/kek/rotate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.importInput && m.importInput.addEventListener('change', async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    if (!confirm(`Import ${file.name} into active backend? Rows inserted (skipped on conflict).`)) return;
    out(m.migrateOutput, 'Reading file...', null);
    const text = await file.text();
    let payload;
    try { payload = JSON.parse(text); } catch (e) { out(m.migrateOutput, `Invalid JSON: ${e.message}`, false); return; }
    out(m.migrateOutput, 'Importing...', null);
    const u = encodeURIComponent(uid());
    const res = await fetch(apiPath(`/admin/storage/migrate/import?requesting_user_id=${u}`), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    let body = null;
    try { body = await res.json(); } catch {}
    out(m.migrateOutput, body || { error: 'no response' }, res.ok);
    ev.target.value = '';
  });
}

// ── Stream Buffer (in-memory event replay window) ──────────────────────────

async function loadStreamBufferSetting() {
  const sbufInput = qs('ac-storage-sbuf-seconds');
  const sbufStatus = qs('ac-storage-sbuf-status');
  const sbufBadge = qs('ac-storage-sbuf-badge');
  if (!sbufInput) return;
  try {
    const r = await _fetchWithTimeout(apiPath('/admin/settings/app'));
    if (!r.ok) {
      if (sbufStatus) sbufStatus.textContent = `Load failed: ${r.status}`;
      return;
    }
    const data = await r.json();
    const sec = (typeof data.stream_buffer_retention_seconds === 'number')
      ? data.stream_buffer_retention_seconds
      : 60;
    sbufInput.value = sec;
    if (sbufBadge) sbufBadge.textContent = `active: ${sec}s`;
  } catch (e) {
    if (sbufStatus) sbufStatus.textContent = 'Load failed';
  }
}

function wireStreamBuffer() {
  const sbufInput = qs('ac-storage-sbuf-seconds');
  const sbufSave = qs('ac-storage-sbuf-save');
  const sbufStatus = qs('ac-storage-sbuf-status');
  const sbufBadge = qs('ac-storage-sbuf-badge');
  if (!sbufSave || !sbufInput) return;

  sbufSave.addEventListener('click', async () => {
    let val = parseInt(sbufInput.value, 10);
    if (isNaN(val) || val < 0) val = 0;
    if (val > 3600) val = 3600;
    sbufInput.value = val;
    if (sbufStatus) sbufStatus.textContent = 'Saving…';
    try {
      // Read current settings first so we don't clobber siblings like access_mode.
      const cur = await fetch(apiPath('/admin/settings/app'));
      const curData = cur.ok ? await cur.json() : {};
      curData.stream_buffer_retention_seconds = val;
      const r = await fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(curData),
      });
      if (!r.ok) {
        if (sbufStatus) sbufStatus.textContent = `Save failed: ${r.status}`;
        return;
      }
      const saved = await r.json();
      const ssec = (typeof saved.stream_buffer_retention_seconds === 'number')
        ? saved.stream_buffer_retention_seconds
        : val;
      if (sbufStatus) sbufStatus.textContent = `Saved (${ssec}s)`;
      if (sbufBadge) sbufBadge.textContent = `active: ${ssec}s`;
    } catch (e) {
      if (sbufStatus) sbufStatus.textContent = `Save failed: ${e.message || e}`;
    }
  });
}

// ── Public entry ────────────────────────────────────────────────────────────

// ── Agent Prompt Templates panel ───────────────────────────────────────────
//
// Renders #ac-tpl-* widgets in the Storage section. Talks to
// /admin/db/templates (GET) and /admin/db/templates/seed[-force] (POST).

async function loadTemplatePanel() {
  const listEl   = qs('ac-tpl-list');
  const badgeEl  = qs('ac-tpl-sync-badge');
  const hashRow  = qs('ac-tpl-hash-row');
  if (!listEl) return;
  try {
    const r = await _fetchWithTimeout(apiPath('/admin/db/templates'));
    if (!r.ok) {
      listEl.innerHTML = `<div class="ac-hint" style="color:var(--danger);">Failed: HTTP ${r.status}</div>`;
      if (badgeEl) { badgeEl.textContent = 'error'; badgeEl.style.color = 'var(--danger)'; }
      return;
    }
    const data = await r.json();
    if (badgeEl) {
      badgeEl.textContent = data.in_sync ? 'in sync with JSON' : 'JSON changed — re-seed pending';
      badgeEl.style.color = data.in_sync ? 'var(--success)' : 'var(--warning)';
    }
    if (hashRow) {
      const cur = (data.current_manifest_hash || '').slice(0, 12);
      const sto = (data.stored_manifest_hash  || '').slice(0, 12) || '(none)';
      hashRow.textContent = `JSON manifest: ${cur} · stored: ${sto}`;
    }
    const templates = data.templates || [];
    if (!templates.length) {
      listEl.innerHTML = `<div class="ac-hint">No templates seeded yet — click Re-Seed.</div>`;
      return;
    }
    listEl.innerHTML = templates.map(t => {
      const adminBadge = t.admin_edited_slots > 0
        ? `<span style="background:var(--purple);color:var(--bg-0);font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px;">${t.admin_edited_slots} admin-edited</span>`
        : '';
      const slotPills = (t.slots || []).map(s => {
        const color = s.source === 'admin' ? 'var(--purple)' : 'var(--muted)';
        return `<span style="font-size:10px;color:${color};margin-right:8px;">${_esc(s.slot_name)} v${s.version}</span>`;
      }).join('');
      return `
        <div style="border:1px solid var(--border);border-radius:6px;padding:8px 10px;background:var(--bg-elev);">
          <div style="display:flex;align-items:center;gap:8px;">
            <strong style="font-size:12px;">${_esc(t.name)}</strong>
            <span style="font-size:10px;color:var(--muted);">${_esc(t.id)}</span>
            ${adminBadge}
            <span style="margin-left:auto;font-size:10px;color:var(--muted);">${t.slot_count} slots · v${t.min_version}${t.min_version !== t.max_version ? '–v' + t.max_version : ''}</span>
          </div>
          <div style="margin-top:6px;">${slotPills}</div>
        </div>`;
    }).join('');
  } catch (e) {
    listEl.innerHTML = `<div class="ac-hint" style="color:var(--danger);">Load failed: ${_esc(e.message || e)}</div>`;
    if (badgeEl) { badgeEl.textContent = 'error'; badgeEl.style.color = 'var(--danger)'; }
  }
}

async function runTemplateSeed(force) {
  const outEl = qs('ac-tpl-output');
  if (force) {
    const ok = window.confirm(
      'Force Re-Seed will OVERWRITE any agent_prompt_templates rows that were edited via the admin UI ' +
      '(source = admin). Their content + version will be reset to the on-disk JSON values.\n\n' +
      'Continue?'
    );
    if (!ok) return;
  }
  try {
    const r = await fetch(apiPath(force ? '/admin/db/templates/seed-force' : '/admin/db/templates/seed'), {
      method: 'POST',
    });
    const body = await r.json().catch(() => ({}));
    if (outEl) {
      outEl.style.display = 'block';
      outEl.style.color = r.ok ? '' : 'var(--danger)';
      outEl.textContent = JSON.stringify(body, null, 2);
    }
  } catch (e) {
    if (outEl) {
      outEl.style.display = 'block';
      outEl.style.color = 'var(--danger)';
      outEl.textContent = `Request failed: ${e.message || e}`;
    }
  }
  await loadTemplatePanel();
}

function wireTemplatePanel() {
  const refresh = qs('ac-tpl-refresh-btn');
  const seed    = qs('ac-tpl-seed-btn');
  const force   = qs('ac-tpl-seed-force-btn');
  if (refresh) refresh.addEventListener('click', () => loadTemplatePanel());
  if (seed)    seed.addEventListener('click',    () => runTemplateSeed(false));
  if (force)   force.addEventListener('click',   () => runTemplateSeed(true));
}


// ── Config Files panel ──────────────────────────────────────────────────────
//
// Renders #ac-cfg-* widgets. Talks to /admin/storage/config-library/{plan,seed}.
// Deterministic seed of the default config files from in-code templates; the
// "Generate Missing Files" action NEVER overwrites an existing file.

async function loadConfigLibraryPanel() {
  const listEl  = qs('ac-cfg-list');
  const badgeEl = qs('ac-cfg-seed-badge');
  if (!listEl) return;
  try {
    const r = await _fetchWithTimeout(apiPath('/admin/storage/config-library/plan?requesting_user_id=' + encodeURIComponent(uid())));
    if (!r.ok) {
      listEl.innerHTML = `<div class="ac-hint" style="color:var(--danger);">Failed: HTTP ${r.status}</div>`;
      if (badgeEl) { badgeEl.textContent = 'error'; badgeEl.style.color = 'var(--danger)'; }
      return;
    }
    const data = await r.json();
    const missing = (data.will_write || []).length;
    if (badgeEl) {
      badgeEl.textContent = missing ? `${missing} missing` : 'all present';
      badgeEl.style.color = missing ? 'var(--warning)' : 'var(--success)';
    }
    const row = (name, title, right, color) => `
      <div style="display:flex;align-items:center;gap:8px;border:1px solid var(--border);border-radius:6px;padding:6px 10px;background:var(--bg-elev);">
        <code style="font-size:11px;">${_esc(name)}</code>
        <span style="font-size:10px;color:var(--muted);">${_esc(title)}</span>
        <span style="margin-left:auto;font-size:10px;color:${color};white-space:nowrap;">${_esc(right)}</span>
      </div>`;
    const parts = [];
    (data.will_write || []).forEach(e => parts.push(row(e.name, e.title, 'will create', 'var(--warning)')));
    (data.already_exist || []).forEach(e => parts.push(row(e.name, e.title, 'exists — skip', 'var(--success)')));
    (data.auto_managed || []).forEach(e => parts.push(row(e.name, e.title, 'auto-managed', 'var(--muted)')));
    listEl.innerHTML = parts.join('') || '<div class="ac-hint">Nothing to show.</div>';
  } catch (e) {
    listEl.innerHTML = `<div class="ac-hint" style="color:var(--danger);">Load failed: ${_esc(e.message || e)}</div>`;
    if (badgeEl) { badgeEl.textContent = 'error'; badgeEl.style.color = 'var(--danger)'; }
  }
}

async function runConfigSeed() {
  const outEl = qs('ac-cfg-output');
  // Pull the current plan so the confirm names exactly what will be created.
  let willWrite = [];
  try {
    const pr = await fetch(apiPath('/admin/storage/config-library/plan?requesting_user_id=' + encodeURIComponent(uid())));
    if (pr.ok) willWrite = (await pr.json()).will_write || [];
  } catch (_) { /* fall through to the no-op guard below */ }
  if (!willWrite.length) {
    window.alert('All default config files already exist — nothing to create.\n(Existing files are never overwritten.)');
    return;
  }
  const ok = window.confirm(
    'Create ' + willWrite.length + ' default config file(s):\n\n' +
    willWrite.map(e => '  • ' + e.name).join('\n') +
    '\n\nExisting files are never overwritten. Continue?'
  );
  if (!ok) return;
  try {
    const r = await fetch(apiPath('/admin/storage/config-library/seed'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    const body = await r.json().catch(() => ({}));
    if (outEl) {
      outEl.style.display = 'block';
      outEl.style.color = r.ok ? '' : 'var(--danger)';
      outEl.textContent = JSON.stringify(body, null, 2);
    }
  } catch (e) {
    if (outEl) {
      outEl.style.display = 'block';
      outEl.style.color = 'var(--danger)';
      outEl.textContent = `Request failed: ${e.message || e}`;
    }
  }
  await loadConfigLibraryPanel();
}

function wireConfigLibraryPanel() {
  const refresh = qs('ac-cfg-refresh-btn');
  const seed    = qs('ac-cfg-seed-btn');
  if (refresh) refresh.addEventListener('click', () => loadConfigLibraryPanel());
  if (seed)    seed.addEventListener('click',    () => runConfigSeed());
}


// ── Public entry ────────────────────────────────────────────────────────────

export function initStorageUi() {
  if (PAGE && PAGE.root) return;  // already initialized successfully
  PAGE = bindMount('ac-storage-');
  if (!PAGE || !PAGE.root) return; // still not in DOM
  wire(PAGE);
  wireStreamBuffer();
  wireTemplatePanel();
  wireConfigLibraryPanel();

  if (PAGE) {
    renderNotes(PAGE.dbNotes, DB_NOTES, PAGE.provider && PAGE.provider.value);
    renderNotes(PAGE.secretsNotes, SECRETS_NOTES, PAGE.secretsProv && PAGE.secretsProv.value);
    renderNotes(PAGE.encNotes, ENC_NOTES, PAGE.encLevelTri && PAGE.encLevelTri.dataset.state === 'on' ? 'field' : 'none');
    renderVaultRecommendation(PAGE);
  }

  if (PAGE && PAGE.root) {
    window.__refreshStorageSection = () => {
      if (PAGE && isAdmin()) {
        loadConfig(PAGE).then(s => applyState(PAGE, s));
        loadEncryption(PAGE);
        loadTenants(PAGE);
        loadFullDb(PAGE);
        loadHybrid(PAGE);
      }
      loadStreamBufferSetting();
      loadTemplatePanel();
      loadConfigLibraryPanel();
    };
    // Data fetching deferred to startStorageUi() — runs only when the
    // Storage section is visible.
  }
}

/** Fetch storage config, encryption status, and tenants. Called when the
 *  Storage section becomes visible (from startAdminTools in files.js). */
export function startStorageUi() {
  // Lazy bail-out: initStorageUi() runs at boot (main.js) but the Data Settings
  // section is typically not in the DOM yet, so bindMount returns null and PAGE
  // stays null. Re-init now — the section IS visible.
  if (!PAGE || !PAGE.root) {
    initStorageUi();
  }
  // Fire storage loads directly — don't rely on __refreshStorageSection which
  // may have been set by initDataManagement() before initStorageUi had a chance
  // to run, breaking the chain.
  if (PAGE && PAGE.root && isAdmin()) {
    loadConfig(PAGE).then(s => applyState(PAGE, s));
    loadEncryption(PAGE);
    loadTenants(PAGE);
    loadFullDb(PAGE);
    loadHybrid(PAGE);
  }
  loadStreamBufferSetting();
  loadTemplatePanel();
  loadConfigLibraryPanel();
}
