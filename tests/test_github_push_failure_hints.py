import unittest
from unittest.mock import patch

from app.api import github


class GithubPushFailureHintTests(unittest.TestCase):
    def test_workflow_scope_rejection_has_specific_recovery_steps(self):
        raw = (
            "! [remote rejected] main -> main (refusing to allow a Personal "
            "Access Token to create or update workflow "
            "`.github/workflows/offline-cache.yml` without `workflow` scope)"
        )

        with (
            patch.object(github, "_TOKEN_CACHE", "configured-token"),
            patch.object(github, "_get_token", return_value="configured-token"),
        ):
            detail = github._push_failure_hint(raw, "")

        self.assertIn(raw, detail)
        self.assertIn("GitHub accepted the token", detail)
        self.assertIn("Workflows: Read and write", detail)
        self.assertIn("'workflow'", detail)
        self.assertIn("press Push", detail)
        self.assertIn("do not create another commit", detail)


if __name__ == "__main__":
    unittest.main()
