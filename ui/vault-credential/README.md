# ui/vault-credential

The **secure credential card** the agent's `request_credential` tool (Visualizer
ability) surfaces in chat.

- `vault-credential-card.js` — `renderVaultCredentialCard(payload)` pops a themed
  modal asking the user for a secret. Self-contained: injects its own `<style>`
  (design-system tokens only → correct in dark **and** light), owns its overlay
  lifecycle, dedupes by `key_id`.

**Trigger.** `ui/shared/js/chat-activity.js` watches live `tool_result` events; when
`request_credential` returns a payload with `ui: "vault_credential_form"`, it calls
the renderer. Live events only — a session reload won't re-pop a stale card.

**The guarantee.** The value the user types here POSTs **straight to the vault**
(`POST /api/v1/genui/vault/keys/{key_id}`) — it never returns to the agent, the
chat transcript, or any log. The agent only ever holds the `key_id`; the dashboard
uses the secret by reference via `api.callWithKey` (server-side proxy in
`app/api/genui.py`). Store/usage contract: `app/abilities/vault_store.py` and the
Visualizer skill's **Logins & secrets → pattern 3**.
