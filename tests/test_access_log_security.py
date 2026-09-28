from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_launchers_disable_raw_uvicorn_access_log() -> None:
    for relative in ("run.py", "app/main.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "Config")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "run")
            )
        ]
        access_values = [
            keyword.value for call in calls for keyword in call.keywords
            if keyword.arg == "access_log"
        ]
        assert access_values, f"{relative} did not configure access_log"
        assert all(isinstance(value, ast.Constant) and value.value is False for value in access_values)


def test_deployed_uvicorn_service_disables_raw_access_log() -> None:
    source = (ROOT / "app" / "deploy" / "bootstrap.py").read_text(encoding="utf-8")
    exec_line = next(line for line in source.splitlines() if line.startswith("ExecStart=") and "uvicorn" in line)
    assert "--no-access-log" in exec_line
