from unittest.mock import patch

import pytest
from fastapi import HTTPException


def test_missing_user_database_does_not_disclose_absolute_path(tmp_path):
    from app.api import db_viewer

    hidden = tmp_path / "private" / "anon-user.db"
    with (
        patch("app.db.local.get_db_user_context", return_value="anon-user"),
        patch("app.db.user_store._user_db_path", return_value=hidden),
        patch.object(db_viewer, "_pg_conninfo_for", return_value=None),
        pytest.raises(HTTPException) as error,
    ):
        db_viewer._get_db_path("user.db")

    assert error.value.status_code == 404
    assert error.value.detail == "Database 'user.db' not found"
    assert str(hidden) not in error.value.detail
