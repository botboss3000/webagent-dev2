# Trace exact static references

Required `kind` is `symbol`, `selector`, `event`, `endpoint`, `data_attribute`, `import` or `asset`; required `value` is matched exactly. `root` defaults to the current session's `webagent-v3` snapshot.

Results include source path, line/span, owner and a live stale flag. Dynamic runtime edges are outside this static index. Reads do not rescan. Follow `next_offset` for pages; narrow values when the bounded query reports `result_cap_reached`.
