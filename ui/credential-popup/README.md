# ui/credential-popup

The **shared credential popover** — a small floating form for entering and
saving cloud/provider credentials, reused across the app. First consumer: the
Instances ↑ HTTPS button.

- `credential-popup.js` — `openCredentialPopup(opts)` pops a themed **popover**
  (a card anchored near the trigger, floating OVER the content — no full-screen
  dim, no blur; `placement: 'center'` opts into a dimmed modal) with a provider
  picker + dynamic fields (or a bare field list), saves via your
  endpoint/callback, and returns a handle `{ el, close(), showNote(text, kind) }`.
  Self-contained: injects its own `<style>` once (design-system tokens only →
  correct in dark **and** light), owns its overlay lifecycle (Escape / outside
  click / single-popover dedupe), and re-focuses instead of stacking duplicates.

**Trigger.** `ui/main-panel/app/instances.js` `_urlUpgradeHttps` →
`_deviceConnectPopup()` when the self device isn't linked to a cloud VM yet.
The popover replaces the inline device-connect panel for that path (the inline
panel stays for the Device-facts "Connect cloud provider →" button).

**Reuse.** Any page needing a credential entry popover should import
`openCredentialPopup` from here instead of building its own. Two shapes:

- `providers` — the Instances `/admin/instances/providers` shape. Renders the
  saved-credentials summary (`mode: 'summary'`) and/or the entry form
  (`mode: 'form'`) with a provider picker; saves via `POST <endpoint>` (default
  `/admin/instances/connect`) with `{ provider, values, ...extraBody }`.
  The summary lists each saved credential row ("Use saved") plus a dashed
  **"Save new credentials"** row that opens the same form (which carries the
  provider picker) — no separate select/Continue step. With nothing saved the
  popover goes straight to the form.
- `fields` — a bare `{key,label,secret,textarea,placeholder,required,value,tip}`
  list for a custom form; pass `save(values, providerId, popup)` to handle
  persistence yourself.

**Field filtering — show only what the flow needs.** Every consumer decides
which fields its use case requires:

- `includeFields: ['service_account_json']` — render only these keys.
- `excludeFields: ['github_token', 'admin_password']` — skip these keys.
- `fieldFilter(field, kind)` — full control (kind: `'connect'` | `'credential'` | `'form'`).
- `fieldTips: { key: 'text' | {html, wide} }` — per-field "?" info bubbles.

The HTTPS/linking flow passes `includeFields: ['service_account_json']` — only
the Google service-account key is requested, because the JSON embeds the
project id, which the backend extracts on save (`app/deploy/manager.py`
`save_connection`). The JSON box also accepts a **drag-and-dropped .json file**
(same `.ac-dropzone` affordance as the Deploy target form) — dropping the
downloaded key fills the box as if pasted. A future Git-control use can pass
`includeFields: ['github_token']` instead.

**Info dialog.** Any field with a `tip` renders the same circled "?" help badge
the App Settings → Deploy target section uses (`ui/shared/js/field-tip.js`
`tipBadge`) — click/tap it for a floating help bubble, hover to preview. The tip
can carry **screenshots** (`{ html, wide, images: [url, …] }`), rendered as a
clickable gallery with a full-size lightbox — the HTTPS flow reuses the exact
two "create role" / "add key" screenshots the Deploy target form shows.

Callbacks: `onSaved(popup, info)` after a successful save (the popover stays
open until you `close()` it or `showNote()` a message),
`onUseSaved(providerId, popup)` for saved-credential rows, `onCancel()` when
dismissed. `anchor` pins the popover to an element and re-tracks it on
scroll/resize.

Sibling: `ui/vault-credential/vault-credential-card.js` — the agent-triggered
vault secret card (a different, security-critical flow; do not merge).
