from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTANCES = ROOT / "ui" / "main-panel" / "instances"


def test_embedded_settings_composer_precedes_all_settings_content():
    source = (INSTANCES / "instances.js").read_text(encoding="utf-8")
    dock = source[source.index("function _dockConfigurationAssistant") : source.index("function _restoreConfigurationAssistant")]

    assert "container.prepend(chat)" in dock


def test_embedded_settings_composer_sticks_below_tabs_and_above_subnav():
    css = (INSTANCES / "instances.css").read_text(encoding="utf-8")
    subnav = css[css.index(".inst-settings-host #app-config-subnav:not(:empty) {") :]
    subnav = subnav[: subnav.index("}")]
    composer = css[css.index(".inst-settings-host #ac-unified-pa-bar {") :]
    composer = composer[: composer.index("}")]

    assert "top: var(--inst-tabs-sticky-h, 0px);" in composer
    assert "z-index: 5;" in composer
    assert "top: calc(var(--inst-tabs-sticky-h, 0px) + var(--inst-config-chat-height, 76px));" in subnav
    assert "z-index: 4;" in subnav
    assert "top: calc(var(--inst-config-grid-height, 100vh) - var(--inst-config-chat-height, 76px));" not in css
