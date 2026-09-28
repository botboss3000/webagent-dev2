from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.remote_access import store


class _Table:
    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows
        self.ref = ""

    def select(self, *_args):
        return self

    def eq(self, _column, value):
        self.ref = value
        return self

    def execute(self):
        row = self.rows.get(self.ref)
        return SimpleNamespace(data=[dict(row)] if row else [])

    def upsert(self, row, **_kwargs):
        self.rows[row["ref"]] = dict(row)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[row]))


class _Raw:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def table(self, _name):
        return _Table(self.rows)


class TunnelLinkStoreTests(unittest.TestCase):
    def test_control_details_round_trip_through_app_database(self) -> None:
        raw = _Raw()
        db = SimpleNamespace(get_raw_client=lambda: raw)
        with (
            patch("app.db.get_app_db", return_value=db),
            patch("app.remote_access.store.save_config"),
            patch("app.remote_access.store.load_config", return_value={"slave_tokens": {}}),
        ):
            store.update_slave_state(
                8080,
                token="control-token",
                running=True,
                url="https://river.trycloudflare.com/",
                provider="cloudflare",
                state="running",
                slave_pid=101,
                tunnel_pid=202,
                started_at=123.5,
            )
            link = store.load_slave_link(8080)

        self.assertEqual(link["token"], "control-token")
        self.assertEqual(link["url"], "https://river.trycloudflare.com")
        self.assertEqual(link["slave_pid"], 101)
        self.assertEqual(link["tunnel_pid"], 202)
        row = raw.rows["runtime:tunnel:8080"]
        self.assertEqual(json.loads(row["metadata"])["tunnel_link"]["state"], "running")


if __name__ == "__main__":
    unittest.main()
