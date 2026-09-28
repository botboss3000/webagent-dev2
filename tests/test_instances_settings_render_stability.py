from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_settings_start_is_idempotent_while_view_is_active():
    source = _source("ui/main-panel/app/settings/settings.js")

    start = source.index("export async function startSettings")
    stop = source.index("export function stopSettings", start)
    body = source[start:stop]

    assert "if (_active) return;" in body
    assert body.index("if (_active) return;") < body.index("await initSettings()")
    assert body.index("_active = true;") < body.index("await initSettings()")


def test_instances_ends_settings_lifecycle_on_real_navigation_only():
    source = _source("ui/main-panel/app/instances.js")

    tab_handler = source[source.index("function _onClick"):]
    assert "if (S.tab === tabId) return;" in tab_handler
    assert tab_handler.index("if (S.tab === tabId) return;") < tab_handler.index(
        "window.stopSettings();"
    )

    stop_view = source[source.index("export function stopView"):]
    assert "if (window.stopSettings) window.stopSettings();" in stop_view


def test_instances_descriptor_delivers_the_stable_settings_lifecycle():
    descriptor = _source("ui/main-panel/app/page.json")
    tabs = _source("ui/shared/js/tabs.js")

    assert '"entry": "ui/main-panel/app/instances.js?v=262"' in descriptor
    assert "id === 'app'" not in tabs


def test_global_stop_explains_run_family_and_recovery_semantics():
    source = _source("ui/shared/js/kill-switch.js")
    shell = _source("index.html")

    assert "stop active run families" in source
    assert "automatic recovery is suppressed" in source
    assert "server restart clears only this momentary switch" in source
    assert "stopped runs remain stopped" in source
    assert "_btn.setAttribute('aria-busy', String(_busy))" in source
    assert 'class="kill-switch-live" aria-live="polite"' in shell


def test_agents_config_exposes_normalized_manager_loop_contract():
    source = _source("ui/main-panel/agents/js/tab-config.js")

    assert "_renderManagerLoopConfig(body, agent)" in source
    assert "_saveCfg(agent,{manager_loop:cfg},g)" in source
    assert "Starter / Run Scout" in source
    assert "Plan gate" in source
    assert "Edit gate" in source
    assert "Commit gate" in source
    assert "Watchdog" in source
    assert "Closer integration" in source
    assert "Raw tool output is never sent" in source
