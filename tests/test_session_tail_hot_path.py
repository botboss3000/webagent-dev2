from unittest.mock import patch
from pathlib import Path

from app.api import db_viewer


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row, opens):
        self._row = row
        self._opens = opens

    def execute(self, sql, params):
        self._opens.append((sql, params))
        return _Result(self._row)

    def close(self):
        return None


def _opener(row, opens):
    def open_read(_db):
        return _Connection(row, opens), "sqlite"

    return open_read


def test_session_db_resolution_coalesces_hot_tail_lookups():
    db_viewer._SESSION_DB_ROUTE_CACHE.clear()
    opens = []
    row = {"metadata": '{"temp_db_path":"C:/tmp/session-one.db"}'}

    with patch.object(db_viewer, "_open_read", side_effect=_opener(row, opens)):
        assert db_viewer._resolve_session_db("session-one") == "session-one.db"
        assert db_viewer._resolve_session_db("session-one") == "session-one.db"

    assert len(opens) == 1


def test_fallback_route_expires_quickly_for_new_temp_session():
    db_viewer._SESSION_DB_ROUTE_CACHE.clear()
    opens = []
    now = [100.0]

    with (
        patch.object(db_viewer.time, "monotonic", side_effect=lambda: now[0]),
        patch.object(db_viewer, "_open_read", side_effect=_opener(None, opens)),
    ):
        assert db_viewer._resolve_session_db("new-session") == "user.db"
        now[0] += db_viewer._SESSION_DB_ROUTE_NEGATIVE_TTL_S - 0.1
        assert db_viewer._resolve_session_db("new-session") == "user.db"
        now[0] += 0.2
        assert db_viewer._resolve_session_db("new-session") == "user.db"

    assert len(opens) == 2


def test_transient_resolution_failure_is_not_cached():
    db_viewer._SESSION_DB_ROUTE_CACHE.clear()

    with patch.object(db_viewer, "_open_read", side_effect=RuntimeError("busy")) as open_read:
        assert db_viewer._resolve_session_db("busy-session") == "user.db"
        assert db_viewer._resolve_session_db("busy-session") == "user.db"

    assert open_read.call_count == 2


class _StatusCursor:
    def __init__(self):
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)
        return self

    def fetchall(self):
        return [(0, "id"), (1, "status")]


def test_session_tail_status_schema_probe_is_cached():
    db_viewer._SESSION_TAIL_STATUS_CACHE.clear()
    cursor = _StatusCursor()

    assert db_viewer._session_tail_has_status(cursor, "user.db") is True
    assert db_viewer._session_tail_has_status(cursor, "user.db") is True
    assert cursor.queries == ["PRAGMA table_info(interactions)"]


def test_idle_tail_poll_is_visibility_gated_but_active_turns_still_poll():
    source = (
        Path(__file__).parents[1] / "ui" / "chat" / "js" / "chat-reconcile.js"
    ).read_text(encoding="utf-8")

    assert "document.visibilityState === 'hidden'" in source
    assert "window.__getChatVisible() === true" in source
    active_gate = source.index("if (app.isProcessing) {")
    idle_gate = source.index("if (!shouldPollIdleTail()) {")
    assert active_gate < idle_gate
    assert "_idleTicks = IDLE_POLL_EVERY - 1" in source


def test_session_header_polls_pause_offscreen_and_coalesce_slow_lists():
    source = (
        Path(__file__).parents[1] / "ui" / "chat" / "js" / "session-init.js"
    ).read_text(encoding="utf-8")

    assert "document.visibilityState === 'hidden'" in source
    assert "window.__getChatVisible() === true" in source
    assert source.count("if (!isChatPollingVisible()") == 2
    assert "if (!isChatPollingVisible() || _sessionListPollInFlight) return" in source
    assert "await Promise.allSettled([" in source
    assert "_fetchRelatedSessions owns a same-session single-flight" in source


def test_anonymous_session_metadata_denials_trip_client_circuit_breakers():
    root = Path(__file__).parents[1]
    session_list = (
        root / "ui" / "chat" / "elements" / "session-dropdown" / "list.js"
    ).read_text(encoding="utf-8")
    display_cache = (
        root / "ui" / "shared" / "js" / "agent-display-cache.js"
    ).read_text(encoding="utf-8")

    assert "let _sessionListAccessDenied = false" in session_list
    assert "let _relatedAccessDenied = false" in session_list
    assert "resp.status === 401 || resp.status === 403" in session_list
    assert "if (!_sessionListAccessDenied) _scheduleSessionFetchRetry()" in session_list
    assert "isDefaultScope && isAuthenticated() && !isAnonGuest()" in session_list
    assert "if (_accessDenied || !userId" in display_cache
    assert "_accessDenied = true" in display_cache
