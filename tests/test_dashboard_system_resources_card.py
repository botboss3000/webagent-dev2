import asyncio
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CARD_DIR = ROOT / "ui" / "main-panel" / "instances" / "dashboard" / "cards" / "system_resources"


def _load_backend():
    spec = importlib.util.spec_from_file_location("dashboard_system_resources_test", CARD_DIR / "server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_system_resources_card_manifest_matches_folder():
    manifest = json.loads((CARD_DIR / "card.json").read_text(encoding="utf-8"))
    assert manifest["id"] == CARD_DIR.name
    assert manifest["section"] == "system_resources"
    assert manifest["sections"] == ["system_resources"]
    assert manifest["server"] == "server.py"


def test_system_resources_card_is_in_factory_dashboard():
    dashboard = json.loads((ROOT / "app" / "defaults" / "dashboard.json").read_text(encoding="utf-8"))
    cards = dashboard["cards"]
    system_cards = [card for card in cards if card.get("type") == "system_resources"]
    assert len(system_cards) == 1
    assert system_cards[0]["w"] >= 6
    assert system_cards[0]["h"] >= 5


def test_system_resources_backend_returns_safe_snapshot():
    backend = _load_backend()
    snapshot = asyncio.run(backend.build_section({"project_root": ROOT}))

    assert snapshot["platform"]
    assert snapshot["cpu"]["logical_cores"] >= 1
    assert set(snapshot["cpu"]["load"]) == {"one", "five", "fifteen"}
    assert snapshot["disk"]["total_bytes"] > 0
    assert snapshot["disk"]["free_bytes"] >= 0
    assert snapshot["network"]["scope"] in {"host", "webagent-container", "unavailable"}
    if snapshot["memory"]:
        assert snapshot["memory"]["total_bytes"] > 0
        assert 0 <= snapshot["memory"]["percent"] <= 100
