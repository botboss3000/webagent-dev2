# Host Management

Use this ability for hosting-provider account connections and DNS management.

- Start with `host_list_connections`. Provider secrets are encrypted and never
  returned to the agent.
- Use `host_list_zones` before reading or changing DNS so the provider-specific
  zone id and canonical zone name are known.
- Use `host_list_dns_records` to inspect current state before proposing a change.
- `host_upsert_dns_record` creates or replaces a record with the same name and
  type. Treat it as an external write: explain the exact record, value, TTL, and
  Cloudflare proxy state before calling it, and obtain confirmation when required.
- A Cloudflare proxied record hides the origin behind Cloudflare. Leave
  `proxied=false` unless the user explicitly wants the orange-cloud proxy.
- Never ask the user to paste a provider token into chat. Direct them to the
  secure Host Management connection form in the agent's Abilities panel.
