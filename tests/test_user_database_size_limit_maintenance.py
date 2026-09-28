import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from plugins.app_functions.user_database_size_limit import user_database_size_limit as maintenance
class _Accounts:
    def __init__(self, rows): self.rows = rows
    async def list_user_accounts(self): return self.rows
class UserDatabaseQuotaMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self): await maintenance.stop_background()
    async def test_sweep_without_recycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user.db"; path.touch(); enforcer = AsyncMock(return_value={"purged_sessions": 1})
            with patch.object(maintenance, "_enabled", return_value=True), patch("app.db.get_db", return_value=_Accounts([{ "user_id": "user-1" }])), patch.object(maintenance, "_user_db_path", return_value=str(path)), patch.object(maintenance, "_enforce_user_database_size_limit", enforcer), patch.object(maintenance, "_hybrid_backend_for", return_value=None): result = await maintenance._sweep_once()
        self.assertEqual(result["cleaned"], 1); enforcer.assert_awaited_once_with("user.db", path, "user-1", [], None, None)
    def test_interval_is_clamped(self):
        with patch("app.admin.ability_config.get_ability_config", return_value={"maintenance_interval_hours": "999"}): self.assertEqual(maintenance._interval_seconds(), 168 * 3600)
    async def test_disabled_sweep_does_not_enumerate_users(self):
        with patch.object(maintenance, "_enabled", return_value=False), patch("app.db.get_db") as db: result = await maintenance._sweep_once()
        self.assertEqual(result["checked"], 0); db.assert_not_called()
