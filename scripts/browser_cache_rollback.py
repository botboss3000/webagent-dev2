"""Activate, clear, or inspect the validated IndexedDB cache rollback marker."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.browser_canary import rollback_active, set_rollback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("activate", "clear", "status"))
    args = parser.parse_args()
    if args.action == "activate":
        set_rollback(True)
    elif args.action == "clear":
        set_rollback(False)
    print("active" if rollback_active() else "inactive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
