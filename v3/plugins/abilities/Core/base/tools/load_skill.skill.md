# Read bundled guidance

Use `load_skill` with exact `ability_id`, `kind` (`ability`, `tool`, or `selectable`) and `skill_id` returned by `list_skills`. Optionally send `expected_catalog_version` to reject a bundle change since listing. Paths, custom skills and other sessions are unsupported.

Rust rechecks installation rules, the account tier, the agent base policy, membership capability level, readiness, visibility and session suppression. Missing, changed or symlinked assets are withdrawn until validated catalog reload. Returned text carries `source_kind: untrusted_bundled_guidance` and its exact digest; it does not activate abilities, edit prompts or authorize a tool. Historical output remains past evidence and cannot grant execution.
