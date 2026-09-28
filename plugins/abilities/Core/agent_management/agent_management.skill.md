# Agent Management — configure agents safely

Load this skill before creating, changing, reviewing, or organizing the user’s agents and sessions. Agent Management controls only agents the user owns; it does not grant unrelated abilities or bypass their permissions.

## Before every write

1. Read current state first: `list_my_agents`, `get_agent`, `list_agent_tools`, and `list_agent_templates` as relevant.
2. In Plan or Ask mode, show the current state, proposed change, capability/permission impact, and wait for approval. In Auto, make the smallest requested change and report it.
3. Never widen an agent’s own permissions, modify the agent currently running, or attempt to bypass ownership checks. The only self-change exception is the tool-enforced partial `chat_ui` update.
4. Treat dangerous tools as dangerous even when a prompt asks otherwise. `ask` can tighten a tool and `deny` can block it; neither can relabel an inherently destructive action as safe.

## Create or substantially change an agent

Clarify the job, user, success condition, trigger, data/credential needs, autonomy, and failure boundary. Choose the smallest `capability_profile` (`simple`, `standard`, or `advanced`) and only justified `ability_extensions`. After `create_agent`, call `get_agent` to verify the effective result.

Configure deliberately:

- `update_agent` sets identity, model, limits, trigger, and partial `chat_ui` only.
- `set_agent_ability` enables one ability. Use `read_only=true`, `deny_tools`, or `ask_tools` to keep the profile narrow.
- `set_agent_tool` controls one available tool’s availability (`sent` versus `discoverable`) and permission (`auto`, `ask`, `deny`). Check exact names with `list_agent_tools`; do not assume a supporting ability is enabled.
- `manage_agent_skills` stores guidance, not tools. Use `selectable` for task-specific playbooks and `always_on` only for short, indispensable rules.
- `edit_agent_prompt` defines the role and durable operating rules. Avoid copying tool schemas or another ability’s implementation contract into a prompt or skill; tell the target agent to load that ability’s canonical skill when needed.

For a visual agent, enable the Visualizer ability rather than restating Gen UI mechanics. For external-site work, enable Browser Control and retain its vault/login boundaries. Set `max_tokens` high enough for a complete requested artifact, then set finite turn, wall-time, repeated-call, and stall limits appropriate to the job.

## Session work

Use `list_user_sessions` to summarize the latest state before renaming, hiding, recycling, creating, or kicking a session. Classify from the actual last message, not its title. Propose any batch of lifecycle changes before performing it unless Auto mode and the user explicitly asked for autonomous cleanup. Recycle is reversible; do not imply permanent deletion.

Use `create_user_session` for a coherent workstream, not every bullet in a list. Use `kick_user_session` only after the user has approved the concrete task, target agent, and execution mode. Report the session identifiers and any remaining user action.
