"""Audit built-in/plugin tool code for raw database handle access.

The audit fails when a new file starts using ``_get_conn()``,
``get_raw_client()``, or ``sqlite3.connect()`` without an explicit storage-plane
classification. It also fails on module-level handle capture, which could bind a
tool permanently to the wrong authority before request context is installed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    ROOT / "app" / "tools",
    ROOT / "app" / "integrations",
    ROOT / "plugins" / "abilities",
)
RAW_PATTERN = re.compile(
    r"\._get_conn\s*\(|\.get_raw_client\s*\(|sqlite3\.connect\s*\("
    r"|getattr\s*\([^,\n]+,\s*['\"](?:_get_conn|get_raw_client)['\"]"
)

# Every raw-handle-bearing file must fit one reviewed authority plane.
CLASSIFICATIONS = {
    "app/tools/core_tools.py": "server-authority transcript/account operations; browser adapter fails closed",
    "app/tools/loader.py": "account/config tool catalog; resolved after request authority context",
    "app/tools/optimizer_tools.py": "isolated optimizer trial database, never a browser transcript",
    "app/tools/registry.py": "account tool catalog and server transcript analytics; browser adapter fails closed",
    "plugins/abilities/Administrator/code_index/code_index.py": "dedicated code-index database",
    "plugins/abilities/Administrator/git_control/git_control.py": "account/control-plane repository metadata",
    "plugins/abilities/Core/agent_management/agent_management.py": "account/control-plane agent configuration",
    "plugins/abilities/Core/agent_orchestration/agent_orchestration.py": "server-only session orchestration; browser adapter fails closed",
    "plugins/abilities/Core/session_titler/session_titler.py": "server transcript hook; not invoked for browser authority",
}


def _module_level_captures(tree: ast.AST) -> list[int]:
    captures: list[int] = []
    for node in getattr(tree, "body", []):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
            continue
        if RAW_PATTERN.search(ast.unparse(node)):
            captures.append(int(getattr(node, "lineno", 0)))
    return captures


def run_audit() -> dict:
    findings = []
    seen_files: set[str] = set()
    for scan_root in SCAN_ROOTS:
        for path in scan_root.rglob("*.py"):
            if "__pycache__" in path.parts or path.name.startswith("test_"):
                continue
            source = path.read_text(encoding="utf-8")
            matches = [
                index
                for index, line in enumerate(source.splitlines(), start=1)
                if RAW_PATTERN.search(line)
            ]
            if not matches:
                continue
            rel = path.relative_to(ROOT).as_posix()
            seen_files.add(rel)
            tree = ast.parse(source, filename=rel)
            findings.append(
                {
                    "path": rel,
                    "lines": matches,
                    "classification": CLASSIFICATIONS.get(rel),
                    "module_level_captures": _module_level_captures(tree),
                }
            )

    unclassified = sorted(seen_files - CLASSIFICATIONS.keys())
    stale_classifications = sorted(CLASSIFICATIONS.keys() - seen_files)
    module_captures = [
        {"path": item["path"], "lines": item["module_level_captures"]}
        for item in findings
        if item["module_level_captures"]
    ]
    return {
        "contract": {
            "authority_resolution": (
                "get_db() must be called inside request/turn execution so the "
                "context-local BrowserAuthorityDB override is observed"
            ),
            "browser_behavior": (
                "BrowserAuthorityDB exposes no raw handle; classified server "
                "transcript/control-plane paths therefore fail closed"
            ),
            "new_usage": "new raw-handle files require an explicit classification",
        },
        "findings": sorted(findings, key=lambda item: item["path"]),
        "unclassified": unclassified,
        "module_level_captures": module_captures,
        "stale_classifications": stale_classifications,
        "ok": not unclassified and not module_captures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()
    report = run_audit()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
