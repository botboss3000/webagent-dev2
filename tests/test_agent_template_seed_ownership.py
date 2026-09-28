import asyncio
import json

from app.db.local import LocalBackend


def test_manifest_reseed_supports_split_app_plane_without_agent_prompts(tmp_path):
    db = LocalBackend(str(tmp_path / "app.db"), plane="app")
    conn = db._get_conn()
    try:
        conn.execute("DROP TABLE IF EXISTS agent_prompts")
        result = db._seed_agent_templates_from_json_files(conn, force=False)
        row = conn.execute(
            "SELECT metadata FROM agent_templates WHERE id = 'default'"
        ).fetchone()
    finally:
        conn.close()

    assert result["templates"] > 0
    assert row is not None
    assert "execution_modes" in json.loads(row["metadata"])


def test_manifest_reseed_preserves_admin_claimed_template_config(tmp_path):
    db = LocalBackend(str(tmp_path / "app.db"), plane="app")
    conn = db._get_conn()
    try:
        db._seed_agent_templates_from_json_files(conn, force=False)
        conn.commit()
        row = conn.execute(
            "SELECT id, discoverable FROM agent_templates ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None

    expected = 0 if int(row["discoverable"] or 0) else 1
    updated = asyncio.run(db.update_agent_template_fields(
        row["id"], {"discoverable": expected}
    ))
    assert updated is not None
    assert json.loads(updated["metadata"])["source"] == "admin_edit"

    conn = db._get_conn()
    try:
        conn.execute("DELETE FROM app_meta WHERE key = 'last_agent_manifest_hash'")
        db._seed_agent_templates_from_json_files(conn, force=False)
        conn.commit()
        after = conn.execute(
            "SELECT discoverable, metadata FROM agent_templates WHERE id = ?",
            (row["id"],),
        ).fetchone()
    finally:
        conn.close()

    assert int(after["discoverable"]) == expected
    assert json.loads(after["metadata"])["source"] == "admin_edit"
