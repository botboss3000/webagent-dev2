"""Retired migration entry point.

Storage layout v2 replaced the intermediate local/global split. Keeping this
module as a CLI redirect avoids running the obsolete migration by accident.
"""

from __future__ import annotations

from app.db.migrate_storage_layout import main


if __name__ == "__main__":
    raise SystemExit(main())
