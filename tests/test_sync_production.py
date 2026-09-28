from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from scripts.sync_production import (
    SyncError,
    apply_plan,
    build_plan,
    load_config,
)


@contextmanager
def raises(error_type: type[Exception], match: str):
    try:
        yield
    except error_type as exc:
        if not re.search(match, str(exc)):
            raise AssertionError(
                f"{exc!r} does not match expected pattern {match!r}"
            ) from exc
    except Exception as exc:
        raise AssertionError(
            f"expected {error_type.__name__}, got {type(exc).__name__}"
        ) from exc
    else:
        raise AssertionError(f"expected {error_type.__name__} to be raised")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def initialize_repo(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Sync Test")
    git(repo, "config", "user.email", "sync-test@example.invalid")
    for relative, content in files.items():
        write(repo, relative, content)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")


def make_manifest(
    path: Path,
    production: Path,
    *,
    dev_only_paths: list[str] | None = None,
    state_paths: list[str] | None = None,
) -> Path:
    data = {
        "version": 1,
        "production_path": str(production),
        "dev_only_roots": [
            "TUI",
            "plugins/engines",
            "plugins/abilities/Administrator",
            "ui/admin-tools",
        ],
        "dev_only_paths": dev_only_paths
        if dev_only_paths is not None
        else ["TUI", "ui/admin-tools/terminal"],
        "state_paths": state_paths
        if state_paths is not None
        else [
            ".tmp",
            "app/db/global.db",
            "data/agent_data",
            "data/agents",
            "data/db",
            "data/user_data",
            "plugins/data/db/index.db",
            "uploads",
        ],
        "tooling_paths": ["dev-sync.json", "scripts/sync_production.py"],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_filtered_sync_preserves_git_and_excluded_production_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "development"
    production = tmp_path / "production"
    initialize_repo(
        source,
        {
            ".gitignore": "data/config/jwt_secret.txt\n",
            "app/main.py": "development\n",
            "app/db/global.db": "development runtime\n",
            "data/config/default.json": '{"mode": "baseline"}\n',
            "data/agent_data/session.db": "development runtime\n",
            "data/db/runtime.db": "development runtime\n",
            "data/user_data/alice/profile.json": "development user\n",
            "plugins/data/db/index.db": "development runtime\n",
            "TUI/dev.txt": "development only\n",
            "ui/admin-tools/terminal/view.js": "development only\n",
        },
    )
    write(source, "data/config/jwt_secret.txt", "not tracked\n")
    initialize_repo(
        production,
        {
            "app/main.py": "production\n",
            "obsolete.py": "remove me\n",
            "TUI/production.txt": "keep me\n",
            "ui/admin-tools/terminal/production.js": "keep me\n",
        },
    )
    manifest = make_manifest(tmp_path / "dev-sync.json", production)
    config = load_config(manifest, production_override=production)

    plan = build_plan(source, config)

    assert "data/config/default.json" in plan.added
    assert "app/main.py" in plan.updated
    assert "obsolete.py" in plan.removed
    assert "TUI/dev.txt" in plan.excluded
    assert "app/db/global.db" in plan.excluded
    assert "data/agent_data/session.db" in plan.excluded
    assert "data/db/runtime.db" in plan.excluded
    assert "data/user_data/alice/profile.json" in plan.excluded
    assert "plugins/data/db/index.db" in plan.excluded
    assert "ui/admin-tools/terminal/view.js" in plan.excluded

    apply_plan(source, config, plan)

    assert (production / ".git").is_dir()
    assert (production / "app/main.py").read_text() == "development\n"
    assert (production / "data/config/default.json").is_file()
    assert not (production / "obsolete.py").exists()
    assert not (production / "data/config/jwt_secret.txt").exists()
    assert not (production / "app/db/global.db").exists()
    assert not (production / "data/agent_data/session.db").exists()
    assert not (production / "data/db/runtime.db").exists()
    assert not (production / "data/user_data/alice/profile.json").exists()
    assert not (production / "plugins/data/db/index.db").exists()
    assert (production / "TUI/production.txt").is_file()
    assert (production / "ui/admin-tools/terminal/production.js").is_file()
    assert build_plan(source, config).total_changes == 0


def test_apply_refuses_dirty_production(tmp_path: Path) -> None:
    source = tmp_path / "development"
    production = tmp_path / "production"
    initialize_repo(source, {"app/main.py": "development\n"})
    initialize_repo(production, {"app/main.py": "production\n"})
    manifest = make_manifest(tmp_path / "dev-sync.json", production)
    config = load_config(manifest, production_override=production)
    write(production, "local.txt", "do not overwrite\n")

    with raises(SyncError, match="production destination is dirty"):
        apply_plan(source, config, build_plan(source, config))

    assert (production / "app/main.py").read_text() == "production\n"


def test_apply_requires_explicit_dirty_source_override(tmp_path: Path) -> None:
    source = tmp_path / "development"
    production = tmp_path / "production"
    initialize_repo(source, {"app/main.py": "committed\n"})
    initialize_repo(production, {"app/main.py": "production\n"})
    manifest = make_manifest(tmp_path / "dev-sync.json", production)
    config = load_config(manifest, production_override=production)
    write(source, "app/main.py", "uncommitted\n")
    plan = build_plan(source, config)

    with raises(SyncError, match="development source is dirty"):
        apply_plan(source, config, plan)

    apply_plan(source, config, plan, allow_dirty_source=True)
    assert (production / "app/main.py").read_text() == "uncommitted\n"


def test_manifest_rejects_unsafe_paths(tmp_path: Path) -> None:
    production = tmp_path / "production"
    for unsafe in ["../outside", "/absolute", ".git/config", "C:/windows"]:
        manifest = make_manifest(
            tmp_path / "dev-sync.json",
            production,
            state_paths=[unsafe],
        )

        with raises(SyncError, match=""):
            load_config(manifest, production_override=production)


def test_dev_only_path_must_be_inside_declared_root(tmp_path: Path) -> None:
    production = tmp_path / "production"
    manifest = make_manifest(
        tmp_path / "dev-sync.json",
        production,
        dev_only_paths=["app/private"],
    )

    with raises(SyncError, match="outside dev_only_roots"):
        load_config(manifest, production_override=production)


class SyncProductionTests(unittest.TestCase):
    def run_with_temp_path(self, test_function) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_function(Path(directory))

    def test_filtered_sync(self) -> None:
        self.run_with_temp_path(
            test_filtered_sync_preserves_git_and_excluded_production_files
        )

    def test_dirty_production_refusal(self) -> None:
        self.run_with_temp_path(test_apply_refuses_dirty_production)

    def test_dirty_source_refusal(self) -> None:
        self.run_with_temp_path(
            test_apply_requires_explicit_dirty_source_override
        )

    def test_unsafe_manifest_paths(self) -> None:
        self.run_with_temp_path(test_manifest_rejects_unsafe_paths)

    def test_dev_only_root_validation(self) -> None:
        self.run_with_temp_path(
            test_dev_only_path_must_be_inside_declared_root
        )


if __name__ == "__main__":
    unittest.main()
