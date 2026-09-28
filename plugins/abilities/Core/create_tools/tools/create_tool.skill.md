# Create Tool

Use `create_tool` only when it is the exact action the request calls for. This belongs to the Create Tools ability.

Follow the tool schema that arrives with this file. Supply every required field,
inspect the returned result before taking a dependent action, and surface any
reported warning, limitation, or refusal accurately rather than retrying the
same failed request unchanged.

For ability-wide selection rules, safety boundaries, and cross-tool workflows,
follow `create_tools.skill.md` alongside this tool skill.
