"""Standalone plugin lifecycle/asset contracts using isolated manifest roots."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from app import application_plugins as loader


def manifest(tmp_path, *, name="recorder", kind="application"):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.json").write_text(json.dumps({
        "id": name, "kind": kind, "entry": "toggle.js", "assets": "frontend"
    }))
    return folder


def test_absent_plugins_allow_app_start_stop_mount_and_genui(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    app = FastAPI()
    before = len(app.routes)
    asyncio.run(loader.mount(app))
    asyncio.run(loader.start())
    asyncio.run(loader.stop())
    assert len(app.routes) == before
    assert asyncio.run(loader.catalog({"user_id": "admin"})) == {"plugins": []}
    assert asyncio.run(loader.genui_pages("admin")) == []
    assert asyncio.run(loader.genui_html("admin", "journey-reports")) is None


def test_only_direct_application_manifests_are_discovered(tmp_path, monkeypatch):
    manifest(tmp_path)
    manifest(tmp_path, name="ability", kind="ability")
    nested = tmp_path / "abilities/Admin/hidden"
    nested.mkdir(parents=True)
    (nested / "plugin.json").write_text(json.dumps({"id": "hidden", "kind": "application"}))
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    assert [x["id"] for x in loader.manifests()] == ["recorder"]
    catalog = asyncio.run(loader.catalog({"user_id": "admin"}))
    assert catalog["plugins"][0]["entry_url"] == "/plugin-assets/recorder/"+loader.asset_version(catalog["plugins"][0])+"/toggle.js"


def test_broken_plugin_import_is_isolated(tmp_path, monkeypatch):
    manifest(tmp_path)
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    def missing(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr(loader.importlib, "import_module", missing)
    assert list(loader.modules()) == []
    assert asyncio.run(loader.genui_pages("admin")) == []


def test_only_frontend_assets_are_served_and_paths_cannot_escape(tmp_path, monkeypatch):
    plugin = manifest(tmp_path)
    frontend = plugin / "frontend"
    frontend.mkdir()
    (frontend / "toggle.js").write_text("export const ready = true;")
    (plugin / "service.py").write_text("SECRET_SOURCE = 1")
    (plugin / "private.js").write_text("private backend sibling")
    (frontend / "escape.js").symlink_to(plugin / "private.js")
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    response = asyncio.run(loader.asset("recorder", "toggle.js"))
    assert Path(response.path) == frontend / "toggle.js"
    for asset in ("../service.py", "../private.js", "escape.js", "unknown.js"):
        with pytest.raises(HTTPException) as err:
            asyncio.run(loader.asset("recorder", asset))
        assert err.value.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(loader.asset("missing", "toggle.js"))


def test_removal_disappears_from_catalog_and_genui_without_cached_import_dependency(tmp_path, monkeypatch):
    plugin = manifest(tmp_path)
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    async def pages(user):
        return [{"id": "journey-reports"}]
    monkeypatch.setattr(loader.importlib, "import_module", lambda name: SimpleNamespace(genui_pages=pages))
    assert asyncio.run(loader.genui_pages("admin")) == [{"id": "journey-reports"}]
    (plugin / "plugin.json").unlink()
    assert asyncio.run(loader.genui_pages("admin")) == []
    with pytest.raises(HTTPException):
        asyncio.run(loader.asset("recorder", "toggle.js"))


def test_recorder_is_not_an_agent_ability():
    from app import abilities
    ids = set(abilities.ability_ids("ability"))
    assert "recorder" not in ids
    assert "render_recorder" not in ids

def test_dormant_observer_does_not_materialize_stream_payload():
    from app.observation import subscribe, emit
    seen = []
    materialized = []
    def forbidden():
        materialized.append(True)
        return {}
    remove = subscribe("dormant-test", lambda *args: seen.append(args), lambda: False)
    try:
        emit("stream.emit", forbidden)
        assert seen == []
        assert materialized == []
    finally:
        remove()
    remove = subscribe("active-test", lambda topic, value: seen.append(value), lambda: True)
    try:
        emit("stream.emit", lambda: {"fact": 1})
        assert seen == [{"fact": 1}]
    finally:
        remove()

def test_versioned_assets_keep_dependencies_coherent_and_change_with_source(tmp_path, monkeypatch):
    plugin = manifest(tmp_path)
    frontend = plugin / "frontend"
    frontend.mkdir()
    (frontend / "toggle.js").write_text("import './capture.js';")
    dependency = frontend / "capture.js"
    dependency.write_text("export const version = 1;")
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    entry = asyncio.run(loader.catalog({"user_id":"admin"}))["plugins"][0]["entry_url"]
    version = entry.split("/")[-2]
    response = asyncio.run(loader.asset("recorder",version+"/capture.js"))
    assert "immutable" in response.headers["cache-control"]
    assert Path(response.path) == dependency
    dependency.write_text("export const version = 200;")
    changed = asyncio.run(loader.catalog({"user_id":"admin"}))["plugins"][0]["entry_url"]
    assert changed != entry
    with pytest.raises(HTTPException):
        asyncio.run(loader.asset("recorder",version+"/capture.js"))
    response = asyncio.run(loader.asset("recorder","capture.js"))
    assert response.headers["cache-control"] == "no-cache"
