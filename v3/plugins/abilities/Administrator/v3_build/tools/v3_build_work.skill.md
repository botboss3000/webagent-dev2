# v3_build_work

Begin with mode `isolated`, a stable task ID, and a summary before source edits. The receipt provides a private workspace path and input digest. Read the current `workspace_inputs_sha256` from status after applying reviewed patches and focused checks. Finish with that digest to freeze this task and immediately queue its own serial build and automatic V3 development deployment. Other registered tasks may remain open or continue editing.

The broker compares the frozen candidate with the latest healthy release. A same-file conflict fails without deploying; fix it under a new task ID. Poll this task's returned job ID through `v3_worker_job` and confirm both containers healthy after a completed result. A lost response can be recovered by repeating the exact finish. An uncertain deployment requires reconciliation, never automatic retry. Abandon only an open task. Production is not a target.
