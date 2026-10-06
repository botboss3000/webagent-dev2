# v3_source_receipt

Read the durable receipt for one task and source-apply request ID. Success confirms the checked patch was applied; dispatching or uncertain requires source review before another mutation. Reading a receipt never applies source or schedules a build.
