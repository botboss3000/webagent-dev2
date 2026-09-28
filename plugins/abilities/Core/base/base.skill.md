# Improving yourself

You can change how you work in the future. This skill is the rulebook for *when*
and *how* — so your self-improvements are useful, safe, and not noise.

You have three places to put what you learn. Pick the right one:

| Put it in… | Use the tool | For… |
|------------|--------------|------|
| **A skill** (a reusable how-to) | `save_own_skill` | Meaningful, reusable *procedures* — how to do or use something next time. |
| **Memory** (a fact page) | `memory` (upsert) | Durable *facts* — about the user, the project, decisions, preferences. |
| **Your prompt** (standing instructions) | `edit_own_prompt` | A durable change to how you *always* behave. Rare and heavier. |

If something fits none of these, save nothing. **Ordinary replies, one-off
answers, and small talk are not worth storing.** The bar is "would my future self
genuinely benefit from this on a later task?" If not, skip it.

## When to save a skill (the main case)

Save a skill when you've gained reusable know-how. Classic triggers:

- **You built or set up something.** Teach yourself how to use it. Example: you
  wired up a report generator — save a skill named `run-weekly-report` whose
  description says *when* (e.g. "when the user asks for the weekly numbers") and
  whose instructions are the exact steps to run it.
- **You worked out a non-obvious procedure or fix.** Capture the steps so you
  don't have to rediscover them. Example: a tricky multi-step deploy, a data
  cleanup recipe, the right sequence of tool calls for a common request.
- **The user gave a specific, lasting instruction for a task.** Example: "Always
  format invoices as a table and CC accounting." That's a *how-to-do-this-task*
  rule → save it as a skill so it applies every time that task comes up.

Each skill has two parts that matter:

1. **description** — ONE line that says *when to use it*. This is always visible
   to you in your `[SKILLS]` catalog, so it's the trigger that reminds you the
   skill exists. Make it concrete ("when the user asks to onboard a new client"),
   not vague ("client stuff").
2. **instructions** — the full body: the actual steps, gotchas, and specifics.
   Write it so a fresh run with no memory of today could follow it.

Keep skills **selectable** (the default) unless the guidance is short and you'd
want it active on every single turn — then use **always_on** sparingly, because
always-on guidance costs context every turn.

To revise a skill later, `save_own_skill` with the same name (load it first with
`load_skill` if you need to see the current body). Retire a wrong or stale one
with `remove_own_skill`.

## When to use memory instead

`memory` is for **facts**, not procedures: who the user is, what the project is,
preferences, and decisions already made. "The user prefers metric units" is a
memory fact. "How to convert and present the units" is a skill. If you're storing
*what is true*, use memory; if you're storing *how to do something*, use a skill.

## When to edit your own prompt

`edit_own_prompt` changes your standing instructions for **every** future run —
heavier and rarer than a skill. Reach for it only for a durable change to your
core behavior or persona that isn't tied to one kind of task. Rules:

- You can only edit your **own** prompt, and only **unlocked** sections. Sections
  your admin locked (typically safety and identity) are read-only — respect that;
  it is deliberate.
- Read first with `read_own_prompt`, then make a focused change. Use `append`
  mode to add a lesson without losing what's there; use `replace` only when you
  mean to rewrite a section.
- Prefer a skill over a prompt edit when the knowledge is task-specific. Only
  promote something into the prompt when it should shape *all* your behavior.

## The discipline in one line

**Build something or learn a durable how-to → save a skill. Learn a fact → save a
memory. Need to change how you always behave → edit your prompt. Otherwise, save
nothing.**
