from __future__ import annotations

import asyncio
import threading

from app.agent.diagnostics import DiagnosticRecorder
from app.api.http_diag import _safe_query


def _recorder_without_environment() -> DiagnosticRecorder:
    recorder = object.__new__(DiagnosticRecorder)
    recorder.enabled = True
    recorder.file_logging = True
    recorder.persist_level = 20
    recorder._ring = __import__("collections").deque(maxlen=8)
    recorder._pending = __import__("collections").deque(maxlen=32)
    recorder._pending_files = __import__("collections").deque(maxlen=32)
    recorder._pending_tools = __import__("collections").deque(maxlen=32)
    recorder._open_tools = {}
    recorder._counts = {"debug": 0, "info": 0, "warning": 0, "error": 0, "critical": 0}
    recorder._task = None
    recorder._instance_id = lambda: (_ for _ in ()).throw(
        AssertionError("request path initialized the log store")
    )
    recorder._ensure_task = lambda: None
    return recorder


def test_record_queues_file_io_instead_of_writing_on_caller(monkeypatch) -> None:
    recorder = _recorder_without_environment()

    def fail_if_called(_record):
        raise AssertionError("request path performed synchronous file I/O")

    monkeypatch.setattr(recorder, "_write_file", fail_if_called)
    result = recorder.record("warning", "http", "401 GET /api/test")

    assert result is not None
    assert list(recorder._pending_files) == [result]


def test_file_batch_is_offloaded_from_event_loop(monkeypatch) -> None:
    recorder = _recorder_without_environment()
    recorder.record("info", "access", "200 GET /health", persist=False)
    calls = []
    caller_thread = threading.get_ident()
    monkeypatch.setattr(recorder, "_write_file", lambda record: calls.append((record, threading.get_ident())))
    monkeypatch.setattr(recorder, "_db", lambda: None)

    asyncio.run(recorder._flush_once())

    assert len(calls) == 1
    assert calls[0][1] != caller_thread
    assert not recorder._pending_files


def test_http_query_redacts_credentials_but_keeps_safe_context() -> None:
    query = _safe_query("token=secret-token&session_id=s-1&api-key=private&view=active")

    assert "secret-token" not in query
    assert "private" not in query
    assert "session_id=s-1" in query
    assert "view=active" in query
    assert query.count("%5Bredacted%5D") == 2
