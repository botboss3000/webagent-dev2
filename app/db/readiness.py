"""Side-effect-free readiness checks for split storage authorities."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _rows(result: Any) -> list[dict[str, Any]]:
    data = getattr(result, "data", None)
    return list(data) if data else []


def check_storage_authorities() -> None:
    """Prove that app and user authorities can serve their owned core tables.

    The check deliberately resolves a real account from the app authority before
    opening its user authority. Checking the path first prevents a readiness
    request from creating an empty per-user database and masking a missing
    migration.
    """
    from app.db import get_app_db, get_user_db
    from app.db.storage_layout import is_layout_active
    from app.db.user_store import _user_db_path

    app_db = get_app_db()
    accounts = _rows(
        app_db.get_raw_client()
        .table("user_accounts")
        .select("user_id")
        .limit(1)
        .execute()
    )

    if not is_layout_active():
        # A remote single-database authority owns both tables.
        app_db.get_raw_client().table("sessions").select("id").limit(1).execute()
        return

    if not accounts:
        raise RuntimeError("No user account is available for user-plane readiness")

    user_id = str(accounts[0].get("user_id") or "").strip()
    if not user_id:
        raise RuntimeError("App authority returned an invalid user account")

    user_path = Path(_user_db_path(user_id))
    if not user_path.is_file():
        raise RuntimeError(f"User authority is missing for account {user_id!r}")

    get_user_db(user_id).get_raw_client().table("sessions").select("id").limit(1).execute()
