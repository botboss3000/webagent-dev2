# Gate plugins

Run-contract **gate types** live here. One module per type, auto-discovered by
`app/agent/gate_registry.py`. This is the repository's drop-in plugin pattern —
the same shape as `plugins/billing/processors/` and `plugins/app_functions/` —
so adding a gate type is a new file, never a new branch in the core loop.

## The three tiers

- **Types are code** (this directory). A type describes a gate and can implement
  its policy. Reviewed code only — never user-authored logic.
- **Instances are data** (`metadata['gates']` on the agent's own row). Which
  types an agent runs, their params, and which modes they apply to. Changing
  these is a DB edit that takes effect on that agent's next run: no restart,
  no deploy, independent per custom agent.
- **Composition is the user's.** A gate is a *policy over observables the loop
  already tracks* — see `new_contract_state()` in `app/agent/contracts.py` for
  the full, versioned surface. A gate that needs a NEW observable needs a new
  state producer in core code; that boundary is deliberate.

## Instance shape

```json
{
  "schema_version": 1,
  "gates": [
    {"type": "pre_tool_progress_message", "enabled": true},
    {"type": "tool_call_cap", "params": {"limit": 40}, "scope": ["auto"]}
  ]
}
```

`scope` lists execution-mode ids. `["*"]`, `[]`, or an omitted scope means every
mode. An instance with `"enabled": false` is inert and kept only so the UI can
round-trip it. An unknown `type` is ignored with a warning — a typo must never
silently wedge a run.

## Module contract

Expose a module-level `FEATURE` dict and, optionally, `contract_view(instance)`:

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Must equal the filename stem. |
| `display_name` | yes | Shown in the gate-type dropdown. |
| `description` | yes | One line explaining what the gate does. |
| `phase` | yes | `pre_tool`, `completion`, or `prompt`. |
| `inject` | no | Where the body instruction is allowed to render. `prompt` (default) = the standing `## Contract` block; `completion` = the end-of-run injection only; `pre_tool` = only the message the gate's own rule feeds back when it first fires. A `completion` or `pre_tool` type never reaches the standing block or the context estimate, even if its view key were added to `active_rules()`. |
| `order` | no | Sort position in the UI and prompt (default 50). |
| `view` | yes | `{"key", "kind"}` — the effective-contract key this type drives. `kind` is `bool` or `int`; `int` also names its `param` and `default`. |
| `params` | no | Editable param schema. Number param: `{"type": "int", "default", "min", "max", "label"}` (bounded number input). Text param: `{"type": "string", "multiline": true, "label", "default"}` (free text — must not carry `min`/`max`, or the Modes UI routes it to the number input and coerces it to 0). |
| `status` | no | `stable` (default) or `experimental`. |

`view` is what makes the tier work: an instance projects onto the same
effective-contract key the legacy `metadata['contract']` block used, so a gate
instance behaves exactly like the hard-wired key it replaces and every
downstream consumer — the prompt block, the pre-tool dispatcher, both completion
gates, the API projection, the Modes UI — keeps working unchanged.

A newly added type file is discovered the first time a process reads the catalog
and cached for that process's lifetime (`gate_types()`), so it shows up in the
Modes UI and the `## Contract` projection **after the server restarts**. This is
the opposite of an instance edit, which takes effect on the agent's next run with
no restart — types are code, instances are data. (A development loop dropping in
a gate can call `gate_registry.reload_gate_types()` instead of restarting.)

A type whose policy is not expressible as a contract key may implement
`contract_view(instance)` to project its own dict, and `evaluate(ctx)` /
`requirement(ctx)` to run its own logic. Those run in-process, so they are
reviewed code, not data.

## Back-compat

An agent with no `metadata['gates']` block is projected from its legacy
`metadata['contract']` keys, so nothing changes for agents that have not opted
in. Once a `gates` block exists it is authoritative: a gate the agent does not
list is OFF rather than inherited.
