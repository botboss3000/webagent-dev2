# v3_build_worker_restart

Restart only the dedicated host `v3-build-worker.service` with a stable request_id. The worker atomically refuses when a build or deployment job is active. Only a signed-in platform administrator may use this policy-controlled lifecycle action. It never builds an image or restarts either app. A busy worker leaves its job running. Success requires a fresh worker process to answer after restart. An uncertain result requires host inspection before another attempt; no job is automatically retried.
