# git_tool

Use operation status first. Read operations: status, diff, log, show, branch_list, tag_list, stash_list. Mutations: branch_create, branch_delete, tag_create, tag_delete, checkout, stash_push, stash_pop, add, reset, pull, push. Mutations require expected_head. Provide exact paths for add/reset; optional repo defaults webagent-v3. name/ref are strict references, never flags. pull is FF-only; push never forces. Trusted configured remote transport is required.
