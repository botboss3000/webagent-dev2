"""Cut a pruned edition artifact.

Copies the repo into an output directory, then **physically removes the drop-in
plugin files that the target edition does not include** — so a `production`
build literally does not ship experimental code. The runtime edition layer
already gates loading; this script is the optional "code-absent" packaging step
described in docs/claude/production-editions.md.

Usage (from the repo root):

    python -m scripts.build_edition production ../webagent-production
    python -m scripts.build_edition beta ../webagent-beta

Only **drop-in** features (integrations, event sources, channels — the
folder-scan subsystems where deleting a file cleanly removes the capability) are
pruned. Registry-based subsystems (secrets/encryption/payments/scheduler/
connectors/storage) keep all their files — their factories need the fallback —
and are instead gated at runtime by the edition. Abilities are runtime-gated too.

What it does NOT copy: .git, .venv, __pycache__, node_modules, local.db, logs/,
temp/, visuals/, and the various runtime *.json config/state files.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Never copy these into an edition artifact.
_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", "*.pyc", "node_modules",
    "local.db", "*.db", "logs", "temp", "visuals", ".pytest_cache",
    "db_mode.json", "db_connection.json", "secrets_mode.json",
    "encryption_mode.json",
)

# Only these categories are pruned by file deletion (true drop-in folders).
_PRUNABLE = {"integration", "event_source", "channel"}


def _module_to_path(module: str) -> Path:
    return REPO_ROOT / (module.replace(".", "/") + ".py")


def _edition_exclude_paths(edition: str) -> list:
    """Whole files/folders the edition strips outright (an optional tier that must
    leave no trace). Declared as the edition's ``exclude_paths`` in editions.json."""
    try:
        import json
        data = json.loads((REPO_ROOT / "app" / "features" / "editions.json").read_text(encoding="utf-8"))
        spec = data.get("editions", {}).get(edition, {})
        return [Path(p) for p in (spec.get("exclude_paths") or [])]
    except Exception as e:
        print(f"  (warning) could not read exclude_paths: {e}")
        return []


def build(edition: str, out_dir: Path) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from app.features.catalog import build_catalog
    from app.features.editions import list_editions, edition_includes

    editions = list_editions()
    if edition not in editions:
        print(f"Unknown edition '{edition}'. Defined: {', '.join(editions)}")
        return 2

    catalog = build_catalog()
    features = catalog["features"]

    # Decide which drop-in files to drop (relative to repo root).
    to_remove: list[Path] = []
    for f in features:
        if f["category"] not in _PRUNABLE:
            continue
        if edition_includes(f["status"], f["id"], edition):
            continue
        mod = f.get("module")
        if not mod:
            continue
        rel = _module_to_path(mod).relative_to(REPO_ROOT)
        to_remove.append(rel)

    if out_dir.exists():
        print(f"Output dir {out_dir} already exists — refusing to overwrite.")
        return 2

    print(f"Copying repo → {out_dir} …")
    shutil.copytree(REPO_ROOT, out_dir, ignore=_IGNORE)

    # Pin the artifact's active edition, and SANITIZE the manifest so the
    # artifact never reveals what was stripped: drop every edition's build-only
    # `exclude_paths` and neutralize the active edition's description.
    ed_file = out_dir / "app" / "features" / "editions.json"
    try:
        import json
        data = json.loads(ed_file.read_text(encoding="utf-8"))
        data["active"] = edition
        for _name, _spec in (data.get("editions") or {}).items():
            if isinstance(_spec, dict):
                _spec.pop("exclude_paths", None)
        active_spec = (data.get("editions") or {}).get(edition)
        if isinstance(active_spec, dict):
            active_spec["description"] = f"The {active_spec.get('label', edition)} edition."
        ed_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  (warning) could not pin active edition: {e}")

    removed = 0
    for rel in to_remove:
        target = out_dir / rel
        if target.exists():
            target.unlink()
            removed += 1
            print(f"  pruned: {rel}")

    # Edition-level path strips: whole folders/files removed outright so an
    # optional tier leaves no trace (code + UI + schema). The platform billing
    # tier is stripped this way for the 'public' edition.
    path_removed = 0
    for rel in _edition_exclude_paths(edition):
        target = out_dir / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            path_removed += 1
            print(f"  pruned dir:  {rel}")
        elif target.exists():
            target.unlink()
            path_removed += 1
            print(f"  pruned file: {rel}")
    if path_removed:
        print(f"  edition path-strips removed: {path_removed}")

    kept = sum(1 for f in features if f["category"] in _PRUNABLE) - removed
    print(
        f"\nEdition '{edition}' built at {out_dir}\n"
        f"  drop-in plugins kept: {kept}, pruned: {removed}\n"
        f"  (registry subsystems + abilities are runtime-gated by the edition)"
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m scripts.build_edition <edition> <output_dir>")
        return 2
    return build(argv[1], Path(argv[2]).resolve())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
