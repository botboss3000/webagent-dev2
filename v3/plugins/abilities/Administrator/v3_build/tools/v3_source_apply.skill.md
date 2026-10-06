# v3_source_apply

Apply one reviewed text diff to an open isolated task workspace using its current `workspace_inputs_sha256` and a stable request ID. The broker checks paths, modes, links, the digest, and `git apply --check`. This does not edit the live release. After an uncertain response, inspect `v3_source_receipt` before any retry.
