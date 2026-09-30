"""Canonical chat tables; registered in the existing schema and ownership registry."""
TABLE_NAMES = (
    "chat_messages", "chat_runs", "chat_inbox", "chat_execution",
    "chat_candidates", "chat_publications", "chat_delivery", "chat_counters",
    "chat_acknowledgements", "chat_segments",
)

def definitions(C, T, I):
    def text(name, **kw):
        return C(name, "TEXT", **kw)
    def integer(name, **kw):
        return C(name, "INTEGER", **kw)
    tables = [
        T("chat_counters", [text("session_id", primary_key=True), integer("position", default="0", nullable=False)]),
        T("chat_messages", [
            text("id", primary_key=True), text("session_id", nullable=False),
            text("author_id", nullable=False), text("client_message_id", nullable=False),
            text("digest", nullable=False), text("payload", nullable=False),
            text("created_at", nullable=False),
        ], ["UNIQUE(session_id, author_id, client_message_id)"]),
        T("chat_runs", [
            text("id", primary_key=True), text("session_id", nullable=False),
            text("message_id", nullable=False), text("parent_run_id"),
            text("kind", nullable=False, default="'chat'"),
            integer("queue_seq", nullable=False, default="0"),
            text("state", nullable=False), integer("generation", nullable=False, default="0"),
            text("lease_owner"), C("lease_until", "REAL", nullable=False, default="0"),
            text("policy", nullable=False), text("candidate_id"), text("stop_reason"),
            text("created_at", nullable=False), text("updated_at", nullable=False),
        ], ["CHECK(state IN ('queued','running','finalizing','stopping','completed','cancelled','failed'))"]),
        T("chat_inbox", [
            text("message_id", primary_key=True), text("session_id", nullable=False),
            text("run_id", nullable=False), integer("consumed", nullable=False, default="0"),
            integer("ordinal", nullable=False),
        ]),
        T("chat_execution", [
            integer("seq", primary_key=True), text("event_id", nullable=False, unique=True),
            text("session_id", nullable=False), text("run_id", nullable=False),
            integer("generation", nullable=False), text("kind", nullable=False),
            text("payload", nullable=False), text("created_at", nullable=False),
        ]),
        T("chat_candidates", [
            text("id", primary_key=True), text("session_id", nullable=False),
            text("run_id", nullable=False), integer("generation", nullable=False),
            text("kind", nullable=False), text("payload", nullable=False),
            text("decision", nullable=False), text("reason", nullable=False),
            text("policy_digest", nullable=False), text("created_at", nullable=False),
        ]),
        T("chat_publications", [
            text("id", primary_key=True), text("session_id", nullable=False),
            text("run_id"), text("terminal_run_id", unique=True),
            integer("position", nullable=False), integer("revision", nullable=False, default="1"),
            integer("schema_version", nullable=False, default="1"), text("kind", nullable=False),
            text("payload", nullable=False), text("candidate_id"),
            integer("deleted", nullable=False, default="0"), text("created_at", nullable=False),
        ], ["UNIQUE(session_id, position)", "CHECK(kind IN ('user','answer','notice','failure','attachment','component'))"]),
        T("chat_delivery", [
            integer("cursor", primary_key=True), text("session_id", nullable=False),
            text("item_id", nullable=False), integer("revision", nullable=False),
            text("payload", nullable=False), text("created_at", nullable=False),
        ], ["UNIQUE(item_id, revision)"]),
        T("chat_acknowledgements", [
            text("session_id", primary_key=True), integer("cursor", nullable=False, default="0"),
            integer("manual_unread", nullable=False, default="0"),
        ]),
        T("chat_segments", [
            text("id", primary_key=True), text("session_id", nullable=False),
            text("run_id", nullable=False), text("boundary_item_id", nullable=False),
            integer("first_seq"), integer("last_seq"), integer("closed", nullable=False, default="0"),
        ]),
    ]
    indexes = [
        I("chat_delivery_session", "chat_delivery", "session_id, cursor"),
        I("chat_execution_run", "chat_execution", "session_id, run_id, seq"),
        I("chat_run_session", "chat_runs", "session_id, created_at"),
        I("chat_inbox_run", "chat_inbox", "run_id, consumed, ordinal"),
    ]
    return tables, indexes
