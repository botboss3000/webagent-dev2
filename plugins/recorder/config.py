"""Non-secret recorder settings; plugin presence replaces ability registration.

Legacy capture preferences are read without rewriting administrator state.
New settings live in data/config/recorder.json. All required diagnostic channels
are enabled by a capture regardless of ordinary diagnostic switches.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {"enabled": True, "auto_investigate": True, "retention_days": 30,
            "execution_agent_id": ""}

def settings():
    out = dict(DEFAULTS)
    path = ROOT / "data/config/recorder.json"
    if path.is_file():
        try:
            value = json.loads(path.read_text())
            for key in DEFAULTS:
                if key in value: out[key] = value[key]
        except (OSError,ValueError):
            pass
    return out

def client_config():
    from .legacy import get_render_recorder
    cfg = get_render_recorder().client_config()
    cfg.update(capture_duration_ms=0, capture_max_bytes=0, capture_max_operations=0,
               capture_snapshots=True, capture_lag=True, capture_js_errors=True,
               capture_console=True, capture_network=True)
    return cfg
