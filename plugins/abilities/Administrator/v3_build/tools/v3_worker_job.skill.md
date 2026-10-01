# v3_worker_job

Poll the returned worker job ID for durable status and bounded build output. Report success only for succeeded; report readiness and rollback fields for deployments. Failed/interrupted jobs are not successful deployments. Do not busy-poll.
