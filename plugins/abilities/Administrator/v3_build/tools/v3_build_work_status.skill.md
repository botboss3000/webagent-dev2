# v3_build_work_status

Read this caller's durable task receipts, including each open isolated workspace's path and current `workspace_inputs_sha256`. Use that digest for checked source apply and final finish. A finished task has its own job ID immediately; inspect it with `v3_worker_job`. This tool never registers work or starts a build.
