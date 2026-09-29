"""Private activity snapshots. Never part of the public publication cache."""
async def activity_page(repo, session_id, *, after=0, limit=100):
    # Events and segment bounds come from the same read snapshot. A publication
    # closes its predecessor segment atomically in ChatRepository._publish.
    with repo.transaction(write=False) as conn:
        repo._session(conn, session_id)
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM chat_execution WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
            (session_id, after, min(max(limit, 1), 200)))]
        high_water = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM chat_execution WHERE session_id=?",
            (session_id,)).fetchone()[0]
        segments = [dict(row) for row in conn.execute(
            """SELECT s.id,s.run_id,s.boundary_item_id,s.first_seq,s.last_seq,s.closed,s.rowid AS order_id,
                      p.position FROM chat_segments s
               JOIN chat_publications p ON p.id=s.boundary_item_id
               WHERE s.session_id=? ORDER BY p.position,s.rowid""", (session_id,))]
        # Draft-only retries have no new user publication/segment. Anchor their
        # private activity after the parent's terminal item, with a stable key.
        retries = [dict(row) for row in conn.execute("""SELECT r.id,r.state,
            COALESCE((SELECT p.id FROM chat_publications p
                      WHERE p.terminal_run_id=r.parent_run_id),r.message_id) AS boundary
            FROM chat_runs r WHERE r.session_id=? AND NOT EXISTS
            (SELECT 1 FROM chat_segments s WHERE s.run_id=r.id)""", (session_id,))]
        for run in retries:
            boundary = conn.execute("SELECT position FROM chat_publications WHERE id=? AND session_id=?",
                                    (run["boundary"],session_id)).fetchone()
            if boundary is None:
                continue
            bounds = conn.execute("SELECT MIN(seq),MAX(seq) FROM chat_execution WHERE session_id=? AND run_id=?",
                                  (session_id,run["id"])).fetchone()
            segments.append({"id":"diagnostic:"+run["id"],"run_id":run["id"],
                "boundary_item_id":run["boundary"],"first_seq":bounds[0],"last_seq":bounds[1],
                "closed":int(run["state"] in {"completed","failed","cancelled"}),
                "position":boundary[0],"order_id":2**63-1})
        segments.sort(key=lambda row:(row["position"],row["order_id"],row["id"]))
    events = [{**row, "payload": await repo.decode(row["payload"])} for row in rows]
    cursor = rows[-1]["seq"] if rows else after
    return {"events": events, "segments": segments, "cursor": cursor,
            "has_more": cursor < high_water}
