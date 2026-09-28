import asyncio

import pytest

from app.tools import browser


def test_browser_resource_status_counts_server_owned_and_attached_sessions(monkeypatch):
    monkeypatch.setattr(browser, "_pages", {"headless": object(), "local": object()})
    monkeypatch.setattr(browser, "_attached", {"local"})
    monkeypatch.setattr(browser, "_active_refs", {"headless": 1})
    monkeypatch.setattr(browser, "_playwright_instance", object())
    monkeypatch.setattr(browser, "_session_policy", lambda: {
        "max_concurrent_sessions": 3,
        "idle_timeout_seconds": 300,
        "idle_cleanup_enabled": True,
    })

    assert browser.resource_status() == {
        "live_sessions": 2,
        "headless_sessions": 1,
        "attached_sessions": 1,
        "playwright_running": True,
        "active_sessions": 1,
        "policy": {
            "max_concurrent_sessions": 3,
            "idle_timeout_seconds": 300,
            "idle_cleanup_enabled": True,
        },
    }


def test_browser_emergency_stop_reports_resources_before_closing(monkeypatch):
    async def fake_close_all():
        return None

    monkeypatch.setattr(browser, "close_all", fake_close_all)
    monkeypatch.setattr(browser, "resource_status", lambda: {"live_sessions": 3})

    assert asyncio.run(browser.emergency_stop()) == {"live_sessions": 3}


class _Closable:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _seed_sessions(monkeypatch, ids):
    pages = {key: _Closable() for key in ids}
    browsers = {key: _Closable() for key in ids}
    monkeypatch.setattr(browser, "_pages", pages)
    monkeypatch.setattr(browser, "_contexts", {})
    monkeypatch.setattr(browser, "_browsers", browsers)
    monkeypatch.setattr(browser, "_attached", set())
    monkeypatch.setattr(browser, "_backend", {})
    monkeypatch.setattr(browser, "_visited_domains", {})
    monkeypatch.setattr(browser, "_active_refs", {})
    monkeypatch.setattr(browser, "_last_activity", {})
    monkeypatch.setattr(browser, "_set_session_status", lambda *_args: None)
    return pages


def test_idle_reaper_closes_only_unprotected_headless_sessions(monkeypatch):
    pages = _seed_sessions(monkeypatch, ["old", "active", "local", "fresh"])
    old_page = pages["old"]
    statuses = []
    monkeypatch.setattr(browser, "_set_session_status", lambda *args: statuses.append(args))
    monkeypatch.setattr(browser, "_attached", {"local"})
    monkeypatch.setattr(browser, "_active_refs", {"active": 1})
    monkeypatch.setattr(browser, "_last_activity", {
        "old": 100.0, "active": 100.0, "local": 100.0, "fresh": 390.0,
    })
    monkeypatch.setattr(browser, "_session_policy", lambda: {
        "max_concurrent_sessions": 3,
        "idle_timeout_seconds": 300,
        "idle_cleanup_enabled": True,
    })

    closed = asyncio.run(browser.reap_idle_sessions(now=400.0))

    assert closed == ["old"]
    assert old_page.closed is True
    assert statuses == [("old", "idle")]
    assert set(browser._pages) == {"active", "local", "fresh"}


def test_cap_reclaims_lru_inactive_session_but_protects_active(monkeypatch):
    _seed_sessions(monkeypatch, ["active-oldest", "lru", "newer", "newest"])
    monkeypatch.setattr(browser, "_active_refs", {"active-oldest": 1})
    monkeypatch.setattr(browser, "_last_activity", {
        "active-oldest": 1.0, "lru": 2.0, "newer": 3.0, "newest": 4.0,
    })
    monkeypatch.setattr(browser, "_session_policy", lambda: {
        "max_concurrent_sessions": 3,
        "idle_timeout_seconds": 300,
        "idle_cleanup_enabled": False,
    })

    result = asyncio.run(browser.enforce_policy())

    assert result == {"idle_closed": [], "cap_closed": ["lru"]}
    assert set(browser._pages) == {"active-oldest", "newer", "newest"}


def test_agent_browser_resolution_is_isolated_per_chat(monkeypatch):
    from app.db import browser_sessions_store as store

    rows = {}

    def fake_create(user_id, **fields):
        row = {
            "id": f"browser-{len(rows) + 1}", "user_id": user_id,
            "shared": True, **fields,
        }
        rows[row["id"]] = row
        return row

    def fake_list(user_id, agent_id, chat_session_id):
        return [row for row in rows.values() if
                row["user_id"] == user_id and row["agent_id"] == agent_id and
                row["chat_session_id"] == chat_session_id and row["shared"]]

    monkeypatch.setattr(store, "create", fake_create)
    monkeypatch.setattr(store, "list_shared_for_agent_session", fake_list)

    first = browser.resolve_agent_session("u1", "a1", chat_session_id="chat-1")
    same = browser.resolve_agent_session("u1", "a1", chat_session_id="chat-1")
    second = browser.resolve_agent_session("u1", "a1", chat_session_id="chat-2")

    assert first == same
    assert first != second
    assert rows[first]["chat_session_id"] == "chat-1"
    assert rows[second]["chat_session_id"] == "chat-2"

    monkeypatch.setattr(store, "get", lambda bs_id: rows.get(bs_id))
    with pytest.raises(PermissionError, match="different chat"):
        browser.resolve_agent_session(
            "u1", "a1", browser_session_id=first, chat_session_id="chat-2",
        )


def test_browser_view_websockets_do_not_pin_idle_lifecycle():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app/api/browser_stream.py").read_text(
        encoding="utf-8"
    )
    assert "retain_session" not in source
    assert "release_session" not in source


def test_stale_active_reaper_adopts_missing_start_time(monkeypatch):
    pages = _seed_sessions(monkeypatch, ["active"])
    active_page = pages["active"]
    statuses = []
    monkeypatch.setattr(browser, "_active_refs", {"active": 1})
    monkeypatch.setattr(browser, "_active_since", {})
    assert asyncio.run(browser.reap_stale_active_sessions(now=100.0)) == []
    assert browser._active_since == {"active": 100.0}
    monkeypatch.setattr(browser, "_set_session_status", lambda *args: statuses.append(args))
    assert asyncio.run(browser.reap_stale_active_sessions(now=1900.0)) == ["active"]
    assert active_page.closed is True
    assert statuses == [("active", "idle")]


def test_navigation_loop_breaker_trips_after_ten_same_path_reloads(monkeypatch):
    monkeypatch.setattr(browser, "_navigation_history", {})
    monkeypatch.setattr(browser, "_navigation_loop_closing", set())
    url = "https://dev.webagent.live/app?session_id=sensitive-value"
    for now in range(10):
        assert browser._record_navigation("browser-1", url, now=float(now)) is False
    assert browser._record_navigation("browser-1", url, now=10.0) is True
    assert browser._navigation_key(url) == "https://dev.webagent.live/app"
    assert browser._navigation_loop_closing == {"browser-1"}
