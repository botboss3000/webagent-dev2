from __future__ import annotations

import time

from app.auth import connection_tickets


def test_connection_ticket_is_path_scoped_and_single_use(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(connection_tickets, "_DB_PATH", tmp_path / "tickets.sqlite")
    ticket = connection_tickets.issue("user-1", "/api/v1/browser/ws")

    assert connection_tickets.consume(ticket, "/api/v1/browser/screencast") == ""
    assert connection_tickets.consume(ticket, "/api/v1/browser/ws") == ""

    ticket = connection_tickets.issue("user-1", "/api/v1/browser/ws")
    assert connection_tickets.consume(ticket, "/api/v1/browser/ws") == "user-1"
    assert connection_tickets.consume(ticket, "/api/v1/browser/ws") == ""


def test_connection_ticket_expires(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(connection_tickets, "_DB_PATH", tmp_path / "tickets.sqlite")
    ticket = connection_tickets.issue("user-1", "/api/v1/terminal/ws", ttl_seconds=1)
    now = time.time()
    monkeypatch.setattr(connection_tickets.time, "time", lambda: now + 2)
    assert connection_tickets.consume(ticket, "/api/v1/terminal/ws") == ""

