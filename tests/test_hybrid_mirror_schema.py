"""Hybrid mirroring must tolerate legacy remote-only interaction columns."""

import ast
import unittest
from pathlib import Path


class HybridMirrorSchemaTests(unittest.TestCase):
    def test_interaction_mirror_whitelists_canonical_columns(self):
        source = Path("app/db/hybrid.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        ensure = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "_ensure_local_session"
        )
        text = ast.get_source_segment(source, ensure) or ""
        self.assertIn("(*self._ICOLS, \"created_at\")", text)
        self.assertNotIn("cols = list(irows[0].keys())", text)

    def test_db_viewer_reconcile_whitelists_local_columns(self):
        source = Path("app/api/db_viewer.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        reconcile = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_reconcile_session_from_remote"
        )
        text = ast.get_source_segment(source, reconcile) or ""
        self.assertIn("PRAGMA table_info(interactions)", text)
        self.assertIn("if c in _local_cols", text)


if __name__ == "__main__":
    unittest.main()
