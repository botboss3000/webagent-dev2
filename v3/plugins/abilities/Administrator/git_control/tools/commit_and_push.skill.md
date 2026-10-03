# commit_and_push

Supply paths, message, expected_head and expected_review_sha256 from fresh status. Unselected staged changes or possible secrets are rejected. Existing remote defaults origin. A committed-but-unconfirmed push returns git_effect_uncertain; reconcile local HEAD and remote before retrying.
