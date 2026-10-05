import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "promote", pathlib.Path(__file__).parents[1] / "promote.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name) / "live-v3"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src/main.rs").write_text("old\n")
        self.lock = (pathlib.Path(self.temp.name) / "dispatch.lock").open("w+b")
        self.shutoff = (pathlib.Path(self.temp.name) / "shutoff").open("w+b")
        self.shutoff.write(b"0")
        self.shutoff.flush()

    def tearDown(self):
        self.lock.close()
        self.shutoff.close()
        self.temp.cleanup()

    def patch(self, path="src/main.rs", before="old", after="new"):
        return (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -1 +1 @@\n"
            f"-{before}\n"
            f"+{after}\n"
        )

    def apply(self, patch):
        return promote.apply_patch(
            self.root, patch, self.lock.fileno(), self.shutoff.fileno())

    def test_applies_checked_backend_patch(self):
        result = self.apply(self.patch())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["paths"], ["src/main.rs"])
        self.assertEqual((self.root / "src/main.rs").read_text(), "new\n")

    def test_coding_paths_are_promotable_without_runtime_paths(self):
        for path in ("ui/app.js", "plugins/abilities/example.py", "tests/test_example.py", "docs/example.md", "config/example.json", "sandbox.py"):
            target=self.root/path
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text("old\n")
            result=self.apply(self.patch(path=path))
            self.assertEqual(result["status"],"success",path)
            self.assertEqual(target.read_text(),"new\n")

    def test_preflight_rejects_invalid_second_hunk_without_partial_edit(self):
        patch = self.patch() + self.patch(
            path="src/other.rs", before="missing", after="new")
        result = self.apply(patch)
        self.assertEqual(result["status"], "denied")
        self.assertEqual((self.root / "src/main.rs").read_text(), "old\n")

    def test_rejects_metadata_runtime_and_escape_paths(self):
        for path in (".git/config", "data/key", "workspaces/other",
                     "runtime/control", "build-jobs/job", "../outside.txt"):
            patch = (
                f"diff --git a/{path} b/{path}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{path}\n"
                "@@ -0,0 +1 @@\n"
                "+unsafe\n"
            )
            result = self.apply(patch)
            self.assertEqual(result["status"], "denied", path)
        self.assertFalse((pathlib.Path(self.temp.name) / "outside.txt").exists())

    def test_rejects_symlink_ancestor_and_special_mode(self):
        (self.root / "src/link").symlink_to(self.temp.name, target_is_directory=True)
        patch = (
            "diff --git a/src/link/escape.rs b/src/link/escape.rs\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/link/escape.rs\n"
            "@@ -0,0 +1 @@\n"
            "+unsafe\n"
        )
        self.assertEqual(self.apply(patch)["status"], "denied")
        self.assertFalse((pathlib.Path(self.temp.name) / "escape.rs").exists())
        patch = patch.replace("src/link/escape.rs", "src/symlink").replace(
            "new file mode 100644", "new file mode 120000")
        self.assertEqual(self.apply(patch)["status"], "denied")

    def test_shutoff_denies_before_mutation(self):
        self.shutoff.seek(0)
        self.shutoff.write(b"1")
        self.shutoff.flush()
        self.assertEqual(self.apply(self.patch())["status"], "denied")
        self.assertEqual((self.root / "src/main.rs").read_text(), "old\n")

    def test_dispatch_error_reports_uncertain_outcome(self):
        original = promote.run_git

        def fail_apply(root, arguments, *args):
            if arguments[:2] == ["apply", "--whitespace=error"]:
                raise OSError("simulated failure during apply")
            return original(root, arguments, *args)

        with mock.patch.object(promote, "run_git", side_effect=fail_apply):
            result = self.apply(self.patch())
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual((self.root / "src/main.rs").read_text(), "old\n")


if __name__ == "__main__":
    unittest.main()
