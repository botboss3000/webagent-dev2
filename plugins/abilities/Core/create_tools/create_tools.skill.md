# Create Tools — defining a new tool at runtime

This ability lets you mint a **new callable tool** and store it in the database so
you (and other agents of the allowed types) can call it for the rest of this and
future conversations. Use it when the user asks for a capability that doesn't
exist yet and can be expressed as a small, self-contained Python function.

## When to use it

- The user describes a reusable operation ("make me a tool that converts °C to °F",
  "a tool that adds two numbers", "a tool that slugifies a string").
- You find yourself about to hand-compute the same transformation repeatedly.

Do **not** use it to do a one-off calculation you can just answer directly, and do
not use it to reach the network, the filesystem, or the codebase — the safety
scanner rejects code that touches the project, and there are dedicated abilities
for files (User Files), the web (Web Access), and the repo (Codebase Admin).

## `create_tool` — the five required pieces

| Field | What it is |
|---|---|
| `name` | The tool identifier, e.g. `add_numbers`. This is also the **function name** your `code` must define. Re-using an existing name you own **overwrites** that tool (upsert by name + owner). |
| `description` | One line shown to the model when choosing the tool. Be concrete about what it returns. |
| `parameters` | A **JSON Schema object** describing the inputs: `{"type":"object","properties":{...},"required":[...]}`. Match these names to your function's arguments exactly. |
| `code` | Full Python source. It **must contain an `async def` whose name equals `name`**, taking the same arguments as `parameters`, and `return`ing the result. |
| `stages` | REQUIRED, non-empty list of loop-node IDs where the tool is callable. **For a normal action tool this is `["execute_tools"]`.** Memory tools use `["memory_search"]` / `["memory_save"]`. An empty or invalid list is rejected. |

Optional: `destructive` (true if it writes/deletes/has irreversible effects — shows
a warning badge), `agent_types` (limit which agent types may call it; empty = all).

### Shape of a good call

- Function signature, `parameters` properties, and `required` list all agree.
- The function is `async`, named exactly like the tool, and returns a value (don't
  just `print`).
- `stages` is `["execute_tools"]` unless you have a specific reason otherwise.
- Keep the body pure: compute from the arguments and return — no imports of project
  modules, no file/network/shell access.

## After creating

The tool becomes callable immediately in this conversation — call it by name to
verify it works (e.g. create `add_numbers`, then call `add_numbers(num1=2, num2=3)`
and confirm you get `5`). If creation reports an error, read the `message`: a
`stages`-related message means fix the stage list; a `blocked` status means the
safety scanner rejected the code (it referenced the filesystem/codebase) — rewrite
it to be self-contained. Do not silently retry the identical call.
