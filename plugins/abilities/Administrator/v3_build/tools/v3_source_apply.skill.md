# v3_source_apply

Apply a reviewed text diff only to the registered task's isolated V3 source snapshot, using that task's current `workspace_inputs_sha256`. A V3 Workspace proposal remains an artifact until this call succeeds. Use a stable request ID; on uncertainty inspect `v3_source_receipt` before any further mutation. The broker rejects stale inputs, paths outside V3 source, links, binary patches, and unsafe modes.
