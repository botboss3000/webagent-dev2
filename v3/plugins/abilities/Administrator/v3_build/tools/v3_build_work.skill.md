# v3_build_work

Use `status` to inspect registered work and the current open count. Call `begin` with a stable task ID and concise summary before touching V3 build inputs. Keep the claim open while editing and verifying. After review and focused checks, obtain the current digest from `v3_build_status` and call `finish` with that exact `expected_inputs_sha256`.

The last registered open task to finish atomically submits one job that builds and tests both V3 images, then replaces only the V3 development API and loop containers if inputs remain unchanged. Poll the returned `job_id` with `v3_worker_job`. A queued job, successful build, and healthy deployment are different states. An interrupted or uncertain deployment is never retried automatically.

A stable task ID is single use. Use `abandon` for canceled work; it closes only your open task and does not deploy. Repeat an identical finish after a lost response to retrieve its existing job ID. Batch participants may poll the same job even when another participant submitted it. This registry cannot detect editors that did not register; coordinate other writers before finishing. Do not use this tool for production.
