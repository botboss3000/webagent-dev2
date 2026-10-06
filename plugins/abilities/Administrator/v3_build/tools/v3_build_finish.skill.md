# v3_build_finish

Close one isolated registered task with its settled `workspace_inputs_sha256` from `v3_build_work_status`. This immediately queues a validated serial V3 development build and automatic deployment, even while other tasks remain open. Obtain the required approval for that effect. Poll the returned job ID; on timeout or disconnect resume from the same task. Never use a new task ID to retry an uncertain finish.
