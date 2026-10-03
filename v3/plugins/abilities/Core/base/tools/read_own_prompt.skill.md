# Read your current prompt

Call `read_own_prompt` with an empty object to read the prompt configuration of the agent bound to this conversation. Rust supplies the caller, session, agent and run. User IDs, agent IDs and other target arguments are not accepted.

The result contains ordered slot names, content, revisions, locked status and editability. Internal slot names beginning with `__` are hidden. A locked slot or a caller without agent management authority is never editable. This tool performs no writes; an editable flag does not grant a tool or approval.

Rust rechecks installation rules, the account tier, the agent base policy, membership capability level, readiness, tool visibility, current-run activation and session suppression before and after reading. Results exceeding 64 KiB of serialized JSON fail with a configuration error instead of returning truncated instructions.
