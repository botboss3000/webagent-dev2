"""
Export agent_templates rows from the local SQLite DB back to JSON seed files.

Reverse of the startup seed: reads the DB and writes one .json file per
template into the bundled app/defaults/agents/ (or data/agents/ if that override
exists), matching the format consumed by scan_agent_json_files() in
app/context/md_seeder.py.

Usage:
    python scripts/export_agent_templates.py            # write files
    python scripts/export_agent_templates.py --dry-run   # preview only
"""

import argparse
import json
import os
import sqlite3
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(PROJECT_ROOT, "data", "db", "app.db")
# Write back to the same seed dir the app reads — reuse its resolver so the two
# never drift (data/agents/ override if present, else bundled app/defaults/agents/).
sys.path.insert(0, PROJECT_ROOT)
from app.util.paths import agents_seed_dir
AGENTS_DIR = agents_seed_dir()

BOOL_COLUMNS = {"can_be_default", "is_system", "is_pipeline", "discoverable"}

COLUMNS = [
    "id", "name", "description", "icon",
    "can_be_default", "is_system", "is_pipeline", "access_level", "discoverable",
    "system_prompt", "max_turn_count", "max_wall_seconds",
    "max_identical_tool_calls", "max_stall_strikes", "model", "provider",
    "temperature", "max_tokens", "metadata",
    "agent_prompt", "user_prompt", "skills_prompt", "tasks_prompt", "misc_prompt",
    "bootstrap_tools", "trigger_type", "trigger_key", "loop_logic",
]


def row_to_dict(row: sqlite3.Row) -> dict:
    d = {}
    for col in COLUMNS:
        val = row[col]

        if col in BOOL_COLUMNS:
            val = bool(val)
        elif col == "metadata":
            try:
                val = json.loads(val) if val else {}
            except (json.JSONDecodeError, TypeError):
                val = {}
        elif col == "loop_logic":
            try:
                val = json.loads(val) if val else []
            except (json.JSONDecodeError, TypeError):
                val = []

        d[col] = val
    return d


def main():
    parser = argparse.ArgumentParser(description="Export agent_templates to JSON seed files")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    args = parser.parse_args()

    if not os.path.isfile(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM agent_templates ORDER BY id").fetchall()
    conn.close()

    if not rows:
        print("No agent templates found in database.")
        return

    os.makedirs(AGENTS_DIR, exist_ok=True)

    for row in rows:
        template = row_to_dict(row)
        tid = template["id"]
        fpath = os.path.join(AGENTS_DIR, f"{tid}.json")

        if args.dry_run:
            print(f"  [dry-run] {tid} -> {os.path.relpath(fpath, PROJECT_ROOT)}")
            continue

        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  Exported: {tid} -> {os.path.relpath(fpath, PROJECT_ROOT)}")

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Total: {len(rows)} template(s)")


if __name__ == "__main__":
    main()
