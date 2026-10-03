# terminal_read

Read `terminal_id`, optionally from byte `cursor`. Returns bounded `output`, next `cursor`, `sequence`, `running`, `exit_code` and `truncated`. This observation authorizes no execution. Use its exact sequence for one subsequent send; new output makes it stale. Escape sequences are returned as text rather than rendered screen state.
