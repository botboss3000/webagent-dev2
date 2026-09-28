import sqlite3

from app.agent.session_inbox import claim_pending


class Db:
    def __init__(self, path):
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def test_claim_pending_delivers_normal_messages_once_in_order(tmp_path):
    db = Db(tmp_path / "inbox.db")
    conn = db._get_conn()
    conn.execute("CREATE TABLE interactions (id TEXT, content TEXT, metadata TEXT, session_id TEXT, role TEXT, status TEXT, created_at INTEGER)")
    conn.executemany("INSERT INTO interactions VALUES (?,?,?,?,?,?,?)", [
        ("m2", "second status", "{}", "s", "user", "queued", 2),
        ("m1", "first status", "{}", "s", "user", "queued", 1),
        ("compact", "later", '{"queued_for":"compact"}', "s", "user", "queued", 3),
    ])
    conn.commit()
    conn.close()

    assert [item["id"] for item in claim_pending(db, "s")] == ["m1", "m2"]
    assert claim_pending(db, "s") == []
    conn = db._get_conn()
    statuses = dict(conn.execute("SELECT id, status FROM interactions").fetchall())
    conn.close()
    assert statuses == {"m1": "complete", "m2": "complete", "compact": "queued"}
