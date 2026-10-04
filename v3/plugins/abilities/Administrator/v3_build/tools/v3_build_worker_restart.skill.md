# v3_build_worker_restart

Restart only the idle dedicated host `v3-build-worker.service` used by V2 build tools. Supply a stable request_id and reuse it if the result is uncertain. The worker refuses active builds or deployments atomically; the V3 host broker also refuses while its own job is active. A successful result confirms a fresh worker process. This does not build or deploy an image, restart V2, or restart the V3 app. Follow the effective tool policy and obtain any required approval before calling.
