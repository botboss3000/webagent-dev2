import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.agent import file_checkout as checkout
from app.db.local import LocalBackend


@pytest.fixture
def checkout_env(tmp_path, monkeypatch):
    backend = LocalBackend(str(tmp_path / "checkout-state.db"), seed=False)
    monkeypatch.setattr(checkout, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(checkout, "get_db", lambda: backend)
    return tmp_path, backend


async def _stale_case(root):
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_text("alpha")
    second.write_text("beta")
    observed = checkout.fingerprint_path("first.txt")
    assert observed["size"] == 5
    assert observed["sha256"] == "8ed3f6ad685b959ead7022518e1af76cd816f8e8ec7ccdda1ed4018e8f2223f8"
    assert observed["mtime_ns"] == first.stat().st_mtime_ns
    assert observed["mode"] == (first.stat().st_mode & 0o7777)
    await checkout.observe_files("s1", ["first.txt", "second.txt"])
    first.write_text("changed")
    with pytest.raises(checkout.CheckoutError) as error:
        await checkout.checkout_files("s1", "u1", "a1", ["first.txt", "second.txt"])
    assert error.value.files[0]["status"] == "stale"
    assert not await checkout.active_checkout_paths("s1")


def test_fingerprint_is_exact_and_stale_observation_blocks_atomic_checkout(checkout_env):
    asyncio.run(_stale_case(checkout_env[0]))


async def _creation_case(root):
    acquired = await checkout.checkout_files("s1", "u1", "a1", ["created.txt"])
    assert acquired[0]["fingerprint"]["exists"] is False
    await checkout.preflight_write("s1", ["created.txt"])
    (root / "created.txt").write_text("created")
    await checkout.approve_write_result("s1", ["created.txt"])
    renewed = await checkout.checkout_files("s1", "u1", "a1", ["created.txt"])
    assert renewed[0]["status"] == "checked_out"
    await checkout.mark_validated("s1", ["created.txt"], "unit test")
    status = await checkout.file_checkout_status("s1")
    assert status[0]["status"] == "validated"
    assert status[0]["validated"] is True
    checked_in = await checkout.checkin_files("s1", ["created.txt"])
    assert checked_in[0]["status"] == "checked_in"
    assert not await checkout.active_checkout_paths("s1")


def test_missing_creation_checkout_write_and_checkin(checkout_env):
    asyncio.run(_creation_case(checkout_env[0]))


async def _exclusive_case(root, backend):
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    await checkout.checkout_files("s1", "u1", "a1", ["a.txt"])
    renewed = await checkout.checkout_files("s1", "u1", "a1", ["a.txt"])
    assert renewed[0]["status"] == "checked_out"
    with pytest.raises(checkout.CheckoutError) as error:
        await checkout.checkout_files("s2", "u2", "a2", ["a.txt", "b.txt"])
    assert error.value.files[0]["status"] == "conflict"
    assert not await checkout.active_checkout_paths("s2")
    (root / "a.txt").write_text("out of band")
    with pytest.raises(checkout.CheckoutError):
        await checkout.checkin_files("s1", ["a.txt"])
    assert (await checkout.file_checkout_status("s1"))[0]["status"] == "violation"
    await checkout.observe_files("s1", ["a.txt"])
    await checkout.checkout_files("s1", "u1", "a1", ["a.txt"])
    await checkout.abandon_file_checkouts("s1", ["a.txt"], "test cleanup")
    await checkout.checkout_files("s1", "u1", "a1", ["b.txt"])
    conn = backend._get_conn()
    conn.execute(
        "UPDATE file_checkout_leases SET expires_at=? WHERE path='b.txt'",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="microseconds"),),
    )
    conn.commit()
    conn.close()
    assert not await checkout.active_checkout_paths("s1")


def test_atomic_exclusive_renew_expiry_abandon_and_violation(checkout_env):
    asyncio.run(_exclusive_case(*checkout_env))

async def _manual_checkin_case(root):
    (root / "recover.txt").write_text("before recovery")
    await checkout.checkout_files("dead-session", "u1", "a1", ["recover.txt"])
    visible = await checkout.file_checkout_status(
        "active-session", include_user_leases=True, user_id="u1"
    )
    assert len(visible) == 1
    assert visible[0]["status"] == "checked_out_elsewhere"
    assert visible[0]["owner"] == "dead-session"
    assert visible[0]["manual_checkin_allowed"] is True
    released = await checkout.manual_checkin_files(
        "active-session", "u1", ["recover.txt"]
    )
    assert released[0]["owner"] == "dead-session"
    with pytest.raises(checkout.CheckoutError, match="Reread"):
        await checkout.checkout_files("dead-session", "u1", "a1", ["recover.txt"])
    await checkout.observe_files("dead-session", ["recover.txt"])
    assert (await checkout.checkout_files(
        "dead-session", "u1", "a1", ["recover.txt"]
    ))[0]["status"] == "checked_out"


def test_manual_checkin_releases_another_same_user_session_and_requires_reread(checkout_env):
    asyncio.run(_manual_checkin_case(checkout_env[0]))


def test_path_normalization_rejects_unsafe_targets(checkout_env):
    root, _ = checkout_env
    (root / "folder").mkdir()
    (root / "target.txt").write_text("x")
    os.symlink(root / "target.txt", root / "link.txt")
    for value in ("folder", "link.txt", "../outside.txt", "data/db/runtime.sqlite"):
        with pytest.raises(checkout.CheckoutError):
            checkout.normalize_path(value)


async def _heartbeat_case(root, backend):
    (root / "heartbeat.txt").write_text("alive")
    await checkout.checkout_files("s1", "u1", "a1", ["heartbeat.txt"])
    conn = backend._get_conn()
    old_expiry = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(
        timespec="microseconds"
    )
    conn.execute(
        "UPDATE file_checkout_leases SET expires_at=? WHERE path='heartbeat.txt'",
        (old_expiry,),
    )
    conn.commit()
    conn.close()
    assert await checkout.heartbeat_file_checkouts("s1") == 1
    status = await checkout.file_checkout_status("s1", ["heartbeat.txt"])
    assert status[0]["active"] is True
    assert status[0]["expires_at"] > old_expiry


def test_heartbeat_renews_active_lease(checkout_env):
    asyncio.run(_heartbeat_case(*checkout_env))


async def _terminal_cleanup_case(root, backend, monkeypatch):
    (root / "terminal.txt").write_text("active")
    monkeypatch.setattr("app.db.local._emit_agent_run_status", lambda *args, **kwargs: _noop())
    conn = backend._get_conn()
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
        ("s1", "u1", "checkout cleanup", "{}"),
    )
    conn.commit()
    conn.close()
    await backend.run_state_begin("s1", "u1", None, "turn-1")
    await checkout.checkout_files("s1", "u1", "a1", ["terminal.txt"])
    assert await checkout.active_checkout_paths("s1") == ["terminal.txt"]
    assert await backend.run_state_finish("s1", status="interrupted") is True
    assert await checkout.active_checkout_paths("s1") == []


async def _noop():
    return None


def test_terminal_run_cleanup_releases_leases(checkout_env, monkeypatch):
    asyncio.run(_terminal_cleanup_case(*checkout_env, monkeypatch))
