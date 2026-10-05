# v3_worker_submit

Provide `operation` (`build` or `deploy`), stable `request_id`, and `expected_inputs_sha256`. Deploy additionally requires the completed build `job_id` from this session. Build must omit job_id. Deployment is a separately authorized installation mutation and briefly interrupts V3. It preserves private settings and rollback material. Identical request retries return the original receipt; changed bodies fail. Do not retry uncertain effects with a new ID before reconciliation.
