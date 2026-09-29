"""Fresh split-storage initialization, without historical migrations or source data."""
import json
import uuid
from app.db.schema import render_plane
from app.db.storage_layout import get_app_store, read_layout

def initialize():
    store = get_app_store()
    existing = read_layout()
    if existing:
        if existing.get("state") != "active" or not existing.get("manifest", {}).get("fresh_chat_v2"):
            raise RuntimeError("V2 refuses an existing non-V2 storage layout")
        return existing["manifest"]["instance_id"]
    store.ensure_schema()
    # Validate every ownership plane's fresh DDL before activating the instance.
    import sqlite3
    for plane in ("app", "user", "agent"):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(render_plane(plane, "sqlite"))
        finally:
            conn.close()
    instance_id = str(uuid.uuid4())
    manifest = {"verified":True,"fresh_chat_v2":True,"instance_id":instance_id,
                "plane_status":dict.fromkeys(("app","user","agent"),"verified")}
    with store.connection() as conn:
        conn.execute("""INSERT INTO storage_layout(id,layout_version,state,manifest_json,activated_at)
            VALUES (1,2,'active',?,datetime('now'))""",(json.dumps(manifest),))
        conn.commit()
    return instance_id
