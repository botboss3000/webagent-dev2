# v3_worker_job

Read a native durable V3 build/deployment job by its ID. Only a terminal status is a result. Report the job ID, status, phase, and bounded sanitized reason or log excerpt. On completion, confirm both development containers are healthy through v3_build_status. Do not retry an uncertain deployment.
