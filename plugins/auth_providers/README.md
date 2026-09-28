# Social sign-in providers (drop-in)

Each folder here is **one "Sign in with …" provider**. Drop a folder in → the
provider appears in **Admin Tools → App Config → Data Settings → App Access → Social sign-in** (turn
it on, paste its keys) and, once enabled + configured, a button appears on the
app's sign-in screen. Delete the folder → the provider disappears everywhere.
No core edits, no registry — the manager (`app/auth_providers/`) discovers this
tree at runtime and the login router (`app/api/social_auth.py`) drives the flow.

This is **login/identity** only. It is deliberately separate from the per-agent
"connect your Gmail so the agent can use it" OAuth in `app/api/oauth.py` +
`app/integrations/` — different purpose, different token store.

## What a provider is

At minimum, one JSON descriptor: `<id>/<id>.json`. Copy `_TEMPLATE/_TEMPLATE.json`
and fill it in. Fields:

| Field | Meaning |
|-------|---------|
| `id` | stable machine id (must match the folder + file name) |
| `display_name` | button/label text ("Google") |
| `status` | `stable` / `beta` / `experimental` (informational) |
| `brand_color` / `text_color` / `border` / `icon` | button look (icon = a key the login UI knows, or a Lucide name) |
| `authorize_url` / `token_url` / `userinfo_url` | the provider's OAuth2 endpoints |
| `scopes` / `scope_sep` | requested scopes and their separator |
| `authorize_extra` | extra query params on the authorize URL (e.g. `prompt`) |
| `profile_map` | where to read `external_id` / `email` / `name` / `avatar` in the userinfo JSON |
| `client_id_field` | which credential field is the OAuth "client_id" (default `client_id`; Apple uses `service_id`) |
| `credential_fields` | the keys the admin pastes (`secret:true` ones are stored encrypted, write-only). Each field may carry an optional `"tip"` — a short "where to get this" hint shown as a "?" tooltip beside the label in the admin panel (like the cloud-deploy fields). Omit it for no tooltip. |
| `requires` / `setup_url` / `setup_hint` | shown in the admin card to guide setup |

## When you also need a `<id>.py`

Most standard OAuth2 / OpenID providers need **only** the JSON. Ship a sibling
`<id>.py` when the provider deviates. It may expose either hook:

- `async def extract_profile(*, descriptor, token_data, access_token, http, creds, **ctx) -> dict`
  — return `{external_id, email, name, avatar}`. Overrides the generic
  userinfo+`profile_map` read. (GitHub uses this to fetch the primary email.)
- `def build_client_secret(*, descriptor, creds) -> str` — return a computed
  `client_secret` for the token exchange. (Apple signs an ES256 JWT.)

See `github/github.py` and `apple/apple.py` for the two live examples.

## The redirect URI

Every provider lands back at **`<app base URL>/api/v1/auth/social/<id>/callback`**.
The admin card shows the exact string to register with the provider. Social
sign-in needs a **secure context** (https) at the provider's side, so on a plain
`http://` LAN address use a tunnel (see `docs/claude/deployment.md`).
