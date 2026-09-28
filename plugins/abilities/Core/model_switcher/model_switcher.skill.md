# Changing your own model and thinking depth

You can right-size **this conversation** — both *which* model runs it and *how hard*
that model thinks (its reasoning effort) — to fit the task in front of you. Every
change applies to this chat only, persists until changed, and takes effect on the
**next** turn.

Each tool below carries its own mini-skill, delivered when that tool becomes
callable, so you don't need this whole skill loaded to use one.

## Rate the task first

Before a demanding step, quickly judge how much real thinking it needs:

- **Light / mechanical** (look something up, fetch a value, simple edit, small talk)
  → the cheap default model at low/minimal effort is right. Don't burn a stronger
  model or high effort on trivia.
- **Heavy reasoning** (tricky code, multi-step planning, careful analysis, a hard
  decision) → raise the effort, and/or move onto a stronger model, for that stretch.

You can't read a true probability dial — this is a judgement call, the same way the
posture setting (Conservative / Balanced / Eager) nudges you. Make the call and act;
there is no closing "revert" step to plan — see "Cleanup is automatic" below.

## Model switching is unrestricted — spend wisely

Raising effort or upgrading the model costs more, and the loop no longer puts the
model-switch tools (`set_model`, `use_premium_model`, `set_effort`) behind a
confirmation gate in **any** mode — plan and ask both exempt them, and auto was
never gated. So switch freely, whenever right-sizing the model helps the task,
without stopping to ask. The cost discipline is on you: switch up *when the task
genuinely warrants it* — don't leave a premium model or high effort running for
trivia.

## Cleanup is automatic — you don't need a closing revert

A stronger model and high effort are for the **hard stretch**, not the whole chat —
but you never have to schedule a "switch back". The backend drops agent-driven
model/effort overrides automatically when the run ends, so the NEXT user turn
always starts on the agent's default model at default effort. Do NOT make
`reset_to_default()` a deliberate final step: it adds a tool call and an extra
LLM round-trip after your answer is done, delays the final message, and the user
doesn't care. Just write the final answer — the cleanup happens invisibly.

`reset_to_default()` still exists and is fine to call **mid-run** when YOU want to
drop back before the run ends (e.g. a long session where the hard stretch is over
but you're continuing to work). (To clear just one dial: `set_model('default')`
reverts the model only; `set_effort('default')` clears the effort only.)

## How a switch takes effect — timing

The override is written to **this session** and applies on the **next** turn, not the
current one. So if you change a dial and revert it **in the same turn**, only the last
call wins and the conversation never actually runs on the changed setting. To genuinely
run a stretch at high effort or on a stronger model: change the dial, **end your turn**,
do the work over the following turns — the override stays active for the rest of the
run and is cleaned up automatically when the run ends.

## Rules of thumb

- Unsure what's available → **`list_models()`** first, then switch by capability.
- User named a model → **`set_model('<id>')`**.
- Hard reasoning ahead, weak model → **`use_premium_model()`**.
- Image work (generate/see images) → **`list_models()`**, pick the one badged
  `makes_images` / `sees_images`, then **`set_model('<id>')`**.
- Hard reasoning ahead, model is fine → **`set_effort('high')`**.
- Light/mechanical work → **`set_effort('low')`** (or `'minimal'`) for speed.
- Hard part done → nothing to do: the run-end cleanup resets to the default
  model and effort automatically.
- Already strong / none configured → the tool tells you; don't force it.

---

## Planning Mode — Model Strategy

When the user sets execution mode to `'plan'`, follow this model-switching protocol:

### Phase 0 — Load the tools
First, load the Model Switcher tools you'll need: `load_tool("list_models")`, `load_tool("use_premium_model")`, `load_tool("set_effort")`, `load_tool("reset_to_default")`, `load_tool("set_model")`. These are all load-on-demand. Get them ready before you start planning.

### Phase 1 — Draft the plan (standard model)
- Start on your **default model** at **default effort**.
- Do the initial planning work — gather information, analyze requirements, outline options.
- Keep the cheap model for this phase; don't burn premium resources on early exploration.

### Phase 2 — Assess if premium is warranted
Once you have a solid draft, judge the task's complexity:

**Use premium for the final plan if the task involves:**
- Complex multi-step architecture or system design
- Tricky debugging with many interdependent variables
- High-stakes code changes (production, security, data integrity)
- Ambiguous requirements needing careful trade-off analysis
- Large-scale orchestration (multiple agents, complex automation)

**Stick with standard model if:**
- The task is straightforward (simple edits, routine queries, known patterns)
- The requirements are clear and the solution path is obvious
- It's a small, contained change

### Phase 3 — Polish with the right model
- If premium is warranted → switch directly, no approval needed (the loop exempts model-switch tools in plan and ask): call `use_premium_model()` and optionally `set_effort('high')`, then finalize the plan.
- If standard is fine → stay on standard, optionally dial `set_effort('medium')` for a bit more polish without the cost.

### Phase 4 — Deliver the plan with a model recommendation
When presenting the final plan, include a **one-line recommendation** at the end:

> **Model recommendation:** This task is [simple / moderately complex / complex]. I recommend [standard / premium] model for execution.

This gives the user a clear signal before they approve moving forward.

### Phase 5 — No closing revert needed
After the plan is delivered, just answer — the run-end cleanup drops any upgrade
automatically when this turn finishes. (Call `reset_to_default()` mid-run only if you
want to continue working on the default model right away.)
