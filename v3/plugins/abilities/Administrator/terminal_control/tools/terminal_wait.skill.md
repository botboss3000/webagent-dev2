# terminal_wait

Wait for terminal output, then return a fresh bounded observation. Provide `terminal_id`, optional byte `cursor`, and `timeout_seconds` (1–30, default 5). The default cursor starts at current output end; pass the prior cursor to collect all new output, or zero to read the retained ring. A timeout returns the current observation and does not send input.
