# v3_worker_job

Provide only `job_id`. Poll a durable host receipt owned by the current caller and workspace. Status can be queued, running, completed, stale, failed, cancelled, interrupted or uncertain. Public logs return at most 16 KiB with log_bytes and log_truncated; retained host logs are bounded separately. Output is scrubbed. Polling has no execution side effect. Missing or another session's jobs are denied. An interrupted job is never automatically replayed.
