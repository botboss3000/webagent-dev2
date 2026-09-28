# Authoring ability skills

Each ability folder has one ability-level Markdown lead and one Markdown skill per
runtime-gated tool:

```text
plugins/abilities/<Group>/<ability_id>/
  <ability_id>.json
  <ability_id>.py
  <ability_id>.skill.md
  tools/
    <tool_name>.skill.md
```

## Ownership and delivery

- `<ability_id>.skill.md` is the **ability lead**. Write the capability's purpose,
  when to use it, how its tools fit together, cross-tool safety rules, selection
  guidance, and shared failure handling here.
- `tools/<tool_name>.skill.md` is that tool's **personal operational skill**. It
  is delivered only when that exact tool becomes callable: immediately for visible
  tools and with `load_tool` for discoverable tools.
- A tool name is the exact runtime-gated name from `ABILITY_TOOLS`, not an
  approximation and not merely a stale descriptor entry. Its filename is the
  contract: `tools/save_file.skill.md`, for example.
- The ability descriptor's `skill_summary` remains the concise, always-visible
  explanation of **when to load the ability**. Tool one-line descriptions continue
  to come from handler docstrings; detailed tool guidance belongs in its Markdown
  file.

There is deliberately **no** `## tool:<name>` syntax and no legacy parser. A root
skill is ordinary Markdown only; `## tool:` headings do not create tool skills.

## Required structure

Every runtime-gated ability must have a root skill file and one non-empty tool
file for every runtime-gated tool. Abilities with zero gated tools (for example,
`base` and `context_control`) keep only their root lead and have no `tools/`
directory requirement.

Keep each tool document focused on:

1. when this exact tool is the correct choice;
2. its argument, sequencing, safety, and failure conventions;
3. how to interpret its result before the next action.

Do not duplicate ability-wide policy in every tool file. Put shared policy in the
root lead and use a short reference to it when needed. Do not write tool guidance
into the descriptor JSON: JSON owns metadata; Markdown owns human-readable,
reviewable operational instructions.

## Example

```text
plugins/abilities/Core/user_files/
  user_files.skill.md
  tools/
    save_file.skill.md
    list_user_files.skill.md
    read_user_file.skill.md
```

`user_files.skill.md` explains the boundary between repository files and user
deliverables. `tools/save_file.skill.md` explains filenames, rooms, binary content,
and returning the resulting URL. The other two files cover listing and reading
existing user files.

## Validation

The test suite rejects legacy `## tool:` headings, unknown tool filenames,
duplicates, missing tool documents, empty tool documents, and unexpected tool
files. Run the focused ability-skill tests after every authoring change.
