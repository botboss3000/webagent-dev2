# Prompt slot read

Use `action: list` to see ordered agent instruction slots and their revisions. Use `action: get` with `slot_name` for one slot. This reads only the configured `shared_default` agent slots, never raw database records or another session. Full content may be returned; keep it out of public chat unless needed for the user’s request.
