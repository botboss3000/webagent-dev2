# Platform (marketplace) billing — optional add-on

This package is the **platform tier** of billing. It is **private**: a public /
agent-only edition ships without it (stripped by `scripts/build_edition.py`).

## What it adds

On top of the always-present **agent tier** (`plugins/billing` — an agent admin
charges their users and keeps 100%), this package layers a marketplace:

- **A platform cut** of every charge (`extension.split_charge` / `record_charge`).
- **Platform-wide policy**: app-wide default pricing + allowed-strategy / allowed-
  processor ceilings every agent inherits (`augment_config` / `validate_agent_config`).
- **Payout routing**: Connect onboarding so the platform pays agent admins out,
  and a marketplace fee on subscriptions (`subscription_params`).
- **Platform-admin endpoints** (`api.py`): `GET/PUT /api/v1/billing/config/platform`,
  `POST /api/v1/billing/connect/onboard`, `GET /api/v1/billing/connect/status`.

## How it plugs in (no core edits)

The core billing engine exposes a neutral **billing-extension seam**
(`plugins/billing/extensions.py`). At startup the host calls
`load_billing_extensions()`, which discovers any `plugins/<group>/billing/`
package and imports it. This package's `__init__` then:

1. registers `PlatformBillingExtension()` into the seam, and
2. exposes a `router` the host mounts.

The agent tier **never imports this package** — the dependency points only
platform → agent. Deleting this folder removes the platform tier with no
dangling reference: the seam's no-op defaults take over and the agent admin
keeps 100%.

## Files

| File | Role |
|------|------|
| `__init__.py` | Registers the extension + exposes the `router`. |
| `extension.py` | The `BillingExtension` implementation (the marketplace logic). |
| `api.py`       | The platform-admin FastAPI routes. |
| `store.py`     | Low-level platform-tier data access (platform config, payout accounts, fee record). |
