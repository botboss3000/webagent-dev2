# v3_worker_submit

Submit a fixed build or deploy job to the dedicated worker. Use a unique request_id and reuse it on uncertain retries. Deploy requires the successful build job ID. This replaces only v3 development and may briefly interrupt it; never interpret accepted/queued as completed. Poll with v3_worker_job. No arbitrary commands, hosts, or images are accepted.
