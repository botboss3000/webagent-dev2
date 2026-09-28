# Ssh Request Connection

Use `ssh_request_connection` only when it is the exact action the request calls for. This belongs to the SSH Control ability.

Follow the tool schema that arrives with this file. Supply every required field,
inspect the returned result before taking a dependent action, and surface any
reported warning, limitation, or refusal accurately rather than retrying the
same failed request unchanged.

For ability-wide selection rules, safety boundaries, and cross-tool workflows,
follow `ssh_control.skill.md` alongside this tool skill.
