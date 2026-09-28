Returns this conversation to the agent's **default model** at **default effort**.

- You do **not** need this as a closing step — the backend drops agent-driven
  model/effort overrides automatically when the run ends, so the next user turn starts
  on the default anyway. Making it a deliberate final call just adds a tool call and an
  extra LLM round-trip after your answer is done, which delays the final message.
- It *is* fine to call **mid-run**, when YOU want to drop back before the run ends —
  e.g. a long session where the hard stretch is over but you're still working.
