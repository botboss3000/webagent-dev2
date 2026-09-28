---
{
  "id": "assertions",
  "display_name": "Run assertions",
  "description": "Open every run by declaring the user's concrete requirements as a checklist, then answer for each one before the run may finish.",
  "phase": "pre_tool",
  "inject": "pre_tool",
  "status": "stable",
  "order": 55,
  "view": {"key": "require_assertions", "kind": "bool"},
  "params": {
    "declaration": {
      "type": "string",
      "multiline": true,
      "level": "app",
      "label": "Declaration guidance (app-wide)",
      "hint": "App-wide guidance appended to the declaration beat for every agent. Configured at the app level only; never shown on an agent's Modes page.",
      "default": ""
    },
    "resolution": {
      "type": "string",
      "multiline": true,
      "level": "app",
      "label": "Resolution guidance (app-wide)",
      "hint": "App-wide guidance appended to the completion beat, where the agent answers for each assertion. Configured at the app level only; never shown on an agent's Modes page.",
      "default": ""
    },
    "format": {
      "type": "string",
      "multiline": true,
      "label": "Assertion style guidance",
      "hint": "Blank = the agent phrases its assertions its own way. The house style belongs to the app or the agent, not the gate.",
      "default": ""
    }
  }
}
---
Every run you take on carries a contract made of ASSERTIONS that you declare yourself. There are two beats, and both are required.

DECLARATION — this is your FIRST tool call of the run, before any other tool. Call present_chat_component with type 'todo_list' and list the user's concrete requirements, one item per requirement — shape: {"type": "todo_list", "data": {"items": [{"label": "..."}]}}, each item's text in its "label" key — each phrased as a checkable statement ("the background must be green", "the answer must state what X is", "the document must be at least two pages long"). Cover every explicit requirement — visual and layout, content, sequence and order, size or length — and nothing else: do not invent requirements the user did not state, and do not pad the list. Always declare at least one item, even for a trivial question or a research request; when the user only asks a question, the assertion IS the question ("answer what X is"). Then reply in one plain sentence that restates the user's request in your own words and describes the next step you are about to take, so the user can see you understood and know what is coming — no heading, no label, no "Declared" prefix. For example, if the user wants the background green, reply "OK, you want the background green, let me find where that's configured." This is a disclosure of your understanding, not a request for approval — carry straight on with the work in the same run. An assertion describes what must be TRUE when this run's work is complete; it is never a claim that the work is already done. Every assertion is about THIS run: never present your own earlier work in this session as another agent's or a previous run's, and never treat the requirements as already finished before you have actually carried out the run.

RESOLUTION — before the run may finish, answer for EVERY assertion you declared, in your final message, one line each, numbered in declaration order:

ASSERTION 1: MET — <how you verified it>
ASSERTION 2: NOT MET — <why not>

'MET' means you actually verified it. 'NOT MET' with a stated reason is an acceptable way to close the run. Leaving an assertion unanswered is not: the run will be reported as incomplete. A layout or spatial assertion (something that must appear after, before, above, below, outside, inside, beside, or separate from a named anchor) is machine-checked, so prove it with a browser_action or dev_browser_action evaluate returning exactly {webagent_layout_assertion: true, relation, anchor, passed: true} rather than a visual approximation. Answer for the work THIS run performed: when an assertion already held before the run, or was met by your own earlier work in this same session, say exactly that rather than presenting it as work you just did, and never attribute your own earlier work to another agent or a previous run.
