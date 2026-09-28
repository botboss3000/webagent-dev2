from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_roster_replaces_skeleton_without_waiting_for_auxiliary_metadata():
    source = (
        ROOT / "ui/main-panel/agents/js/app.js"
    ).read_text(encoding="utf-8")

    roster_wait = source.index("await _loadAgents();")
    first_live_render = source.index("_markAgentsPaint('network')")
    auxiliary_wait = source.index("await auxiliaryLoads;")

    assert "Promise.allSettled([_loadProfile(), _loadAppSettings()])" in source
    assert roster_wait < first_live_render < auxiliary_wait


def test_agent_startup_uses_the_active_panel_not_the_lagging_select_value():
    source = (
        ROOT / "ui/main-panel/agents/js/app.js"
    ).read_text(encoding="utf-8")

    init_start = source.index("async function initAgents()")
    init_end = source.index("export function startAgents()")
    init_source = source[init_start:init_end]

    assert "function _agentsPageIsActive()" in source
    assert (
        "document.getElementById('tab-agents')?.classList.contains('active')"
        in source
    )
    assert "if (!_agentsPageIsActive()) return;" in init_source
    assert "main-tab-select" not in init_source


def test_bare_app_always_defaults_to_agents_regardless_of_role():
    source = (ROOT / "ui/shared/js/tabs.js").read_text(encoding="utf-8")
    start = source.index("function _computeDefaultTab()")
    end = source.index("\n}\n\n// Mirror the active main tab", start) + 2
    default_tab_source = source[start:end]

    assert "const preferred = 'agents';" in default_tab_source
    assert "isAdmin()" not in default_tab_source
    assert "localStorage" not in default_tab_source
