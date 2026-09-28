import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from plugins.active_session_bonus import scanner
from plugins.active_session_bonus.qualification import iso, utcnow
from plugins.active_session_bonus.scanner import BonusWorker, _first_reply
from plugins.active_session_bonus.store import BonusStore


def test_continue_policy_accepts_reply_after_source_closer_before_activation():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE interactions (
        id TEXT, session_id TEXT, role TEXT, content TEXT, source TEXT,
        created_at TEXT, session_seq INTEGER
    )""")
    start = utcnow()
    conn.executemany("INSERT INTO interactions VALUES (?,?,?,?,?,?,?)", [
        ("closer", "session", "assistant", "Choose A or B", None, iso(start), 1),
        ("reply", "session", "user", "A", "user", iso(start + timedelta(seconds=2)), 2),
    ])
    offer = {
        "session_id": "session",
        "source_closer_interaction_id": "closer",
        "activated_at": iso(start + timedelta(seconds=4)),
    }

    # Legacy actions still require a reply after activation.
    assert _first_reply(conn, offer) is None
    # Continue resolves the source closer, even when discovery was delayed.
    assert _first_reply(conn, offer, from_source_closer=True)[0] == "reply"


def test_refresh_reactivates_an_offer_arriving_in_the_open_session():
    source = (Path(__file__).parents[1] / "frontend" / "active-session-bonus.js").read_text(encoding="utf-8")
    refresh_update = source.index("offers.clear(); next.forEach")
    refresh_activation = source.index("activate(activeSessionFromUrl())", refresh_update)
    assert refresh_activation > refresh_update
    assert "const activating = new Set();" in source
    assert source.rstrip().endswith("sessionRowObserver.disconnect();\n});")


def test_initial_offer_hydration_does_not_replay_a_historical_claim_toast():
    source = (Path(__file__).parents[1] / "frontend" / "active-session-bonus.js").read_text(encoding="utf-8")
    assert "let offersHydrated = false;" in source
    claim_check = "if (offersHydrated && offer.status === 'claimed' && previous?.status !== 'claimed')"
    assert claim_check in source
    assert source.index("offersHydrated = true;") > source.index(claim_check)
    # A later refresh can still detect an actual non-claimed -> claimed transition.
    assert "previous?.status !== 'claimed'" in claim_check


def test_api_serves_the_linear_countdown_contract(tmp_path, monkeypatch):
    import asyncio

    from plugins.active_session_bonus import api
    from plugins.active_session_bonus.config import DEFAULTS, BonusConfig
    from plugins.active_session_bonus.store import BonusStore

    class Request:
        pass

    for config, expected in ((DEFAULTS, {"mode": "linear", "floor": 10}),
                             (BonusConfig(minimum_offer=0), {"mode": "linear", "floor": 0})):
        store = BonusStore(tmp_path / f"bonus-{expected['floor']}.db", config)
        monkeypatch.setattr(api, "get_store", lambda store=store: store)
        monkeypatch.setattr(api, "_user", lambda _request: "user")
        served = asyncio.run(api.offers(Request()))
        assert served["decay"] == expected
        assert served["offers"] == []

    source = (Path(__file__).parents[1] / "frontend" / "active-session-bonus.js").read_text(encoding="utf-8")
    scheduler = source[source.index("function scheduleLiveRender()"):source.index("function render()")]
    assert "offer.status === 'offered'" in scheduler
    assert "nextAmountChangeAt(offer, now)" in scheduler
    assert "queueRender();\n    scheduleLiveRender();" in scheduler
    assert "timer = setInterval" not in source


def test_live_countdown_uses_each_offer_timer_as_a_linear_slope():
    source = (Path(__file__).parents[1] / "frontend" / "active-session-bonus.js").read_text(encoding="utf-8")
    countdown = source[source.index("function currentAmount("):source.index("function remainingMinutes(")]
    # Every repaint recomputes from issued start/end timestamps rather than a
    # global rate, and rounds only the displayed linear result.
    assert "const expiresAt = Date.parse(offer.expires_at);" in countdown
    assert "(expiresAt - now) / (expiresAt - offeredAt)" in countdown
    assert "Math.ceil(floor + (initial - floor) * remainingFraction)" in countdown
    assert "function nextAmountChangeAt(offer, now)" in source
    assert "decay.perSecond" not in source
    assert '"mode": "linear"' in (Path(__file__).parents[1] / "api.py").read_text(encoding="utf-8")
    assert "if (Number.isFinite(Number(data.decay?.floor))) decay.floor = Number(data.decay.floor);" in source


def test_continue_settles_reply_that_arrived_before_delayed_activation(tmp_path, monkeypatch):
    root = tmp_path / "data"
    user_db = root / "user_data" / "user" / "user.db"
    user_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(user_db)
    conn.executescript("""
        CREATE TABLE interactions (
            id TEXT, session_id TEXT, role TEXT, content TEXT, source TEXT,
            created_at TEXT, session_seq INTEGER
        );
        CREATE TABLE agent_task_state (session_id TEXT, state_json TEXT);
    """)
    start = utcnow()
    task = {"task_id": "task", "contract_revision": 1, "work_revision": 0,
            "steps": [], "evidence": [], "status": "running"}
    conn.execute("INSERT INTO agent_task_state VALUES (?,?)", ("session", json.dumps(task)))
    conn.executemany("INSERT INTO interactions VALUES (?,?,?,?,?,?,?)", [
        ("closer", "session", "assistant", "Choose A or B", None, iso(start), 1),
        ("reply", "session", "user", "A", "user", iso(start + timedelta(seconds=2)), 2),
    ])
    conn.commit()
    conn.close()

    store = BonusStore(root / "bonus.db")
    policy = {"flag": "needs_input", "max_bonus": 60,
              "resolution_seconds": 86400, "resolution_action": "continue"}
    offer = store.create_offer(user_id="user", session_id="session", closer_id="closer",
                               snapshot={"task_id": "task", "task_revision": 1,
                                         "work_revision": 0, "done_step_count": 0,
                                         "evidence_count": 0, "task_status": "running"},
                               policy=policy, now=start)
    store.activate(offer["id"], "user", now=start + timedelta(seconds=4))
    credits = []

    async def record_credit(user_id, offer_id, amount):
        credits.append((user_id, offer_id, amount))
        return f"active-session-bonus:{offer_id}"

    monkeypatch.setattr(scanner, "data_root", lambda: root)
    monkeypatch.setattr(scanner, "credit_bonus", record_credit)
    asyncio.run(BonusWorker(store)._settle_candidates())

    settled = store.offer(offer["id"], "user")
    assert settled["status"] == "claimed"
    assert settled["candidate_user_interaction_id"] == "reply"
    assert credits == [("user", offer["id"], 60)]
