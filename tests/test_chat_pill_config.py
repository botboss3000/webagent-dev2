import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_chat_ui_v3_uses_two_row_pill_schema():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    pill = config["chat_common"]["active_footer"]["chat_pill"]

    assert config["version"] == 3
    assert pill["rows"] == [
        {"center": {"controls": ["textarea"]}},
        {
            "left": {
                "controls": ["attach"],
                "align": "bottom",
                "padding": "4px 2px 20px 20px",
            },
            "center": {
                "carousel": ["stats"],
                "align": "bottom",
                "padding": "0 0 28px 0",
            },
            "right": {
                "controls": ["stop", "continue", "mic_send"],
                "align": "bottom",
                "padding": "4px 20px 20px 0",
            },
        },
    ]
    assert {
        "textarea", "stats", "mic", "send", "mic_send", "attach",
        "stop", "continue",
    } <= set(
        pill["controls"]
    )
    above = config["chat_common"]["active_footer"]["above_pill"]
    assert above["enabled"] is True
    assert above["rows"][0] == {
        "left": [],
        "center": ["activity"],
        "right": ["scroll_bottom"],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_pill_normalizer_supports_legacy_and_distinct_buttons():
    script = textwrap.dedent(
        """
        const path = await import('node:path');
        const { pathToFileURL } = await import('node:url');
        const moduleUrl = pathToFileURL(
          path.resolve('ui/shared/js/chat-pill-config.js'),
        );
        const mod = await import(moduleUrl.href);
        const controls = value => mod.configuredRowControls(
          mod.normalizeChatPill(value),
        );
        const result = {
          legacyCombined: controls({
            layout: {
              textarea: '1,2',
              stats: '2,2',
              buttons: '1,3',
              attach: '2,3',
            },
            buttons: {
              voice: { enabled: true },
              send: { enabled: true },
            },
            attach: { enabled: true },
          }),
          legacySendOnly: controls({
            buttons: { voice: { enabled: false } },
          }),
          distinct: controls({
            rows: [
              { center: ['textarea'], right: ['mic', 'send'] },
              { center: ['stats'], right: ['attach'] },
            ],
            controls: {},
          }),
          exclusive: controls({
            rows: [{ right: ['mic_send', 'mic', 'send'] }, {}],
            controls: {},
          }),
          disabledCombined: controls({
            rows: [{ center: ['textarea'], right: ['mic_send'] }, {}],
            controls: { mic_send: { enabled: false } },
          }),
          configuredRuntimeActions: controls({
            rows: [
              { center: ['textarea'] },
              { center: ['stats', 'stop', 'continue'] },
            ],
            controls: {},
          }),
        };
        console.log(JSON.stringify(result));
        """
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)

    assert "mic_send" in result["legacyCombined"]
    assert "mic_send" not in result["legacySendOnly"]
    assert "send" in result["legacySendOnly"]
    assert {"mic", "send"} <= set(result["distinct"])
    assert result["exclusive"] == ["mic_send"]
    assert "mic_send" not in result["disabledCombined"]
    assert result["configuredRuntimeActions"] == [
        "textarea", "stats", "stop", "continue",
    ]


def test_unified_zone_renderer_resets_static_and_generated_controls():
    source = (ROOT / "ui/chat-controls/chat-controls-config.js").read_text(
        encoding="utf-8"
    )

    assert "'[data-header-control], [data-element-origin]'" in source


def test_static_chat_composer_is_visible_before_async_control_hydration():
    partial = (ROOT / "ui/chat/chat-side-panel.html").read_text(encoding="utf-8")
    marker = '<div id="chat-input-row"'
    composer_tag = partial[partial.index(marker):partial.index('>', partial.index(marker))]

    assert "display:none" not in composer_tag
    assert "chat-pill-boot" in composer_tag
    assert "chat-pill-row-layout" in composer_tag
    assert "chat-pill-send-only" in composer_tag

    css = (ROOT / "ui/shared/css/app1.css").read_text(encoding="utf-8")
    assert 'class="chat-pill-layout-row" data-pill-row="0"' in partial
    assert 'class="chat-pill-layout-row" data-pill-row="1"' in partial
    assert "padding:4px 2px 20px 20px" in partial
    assert "padding:4px 20px 20px 0" in partial
    assert partial.index('id="chat-attach-btn"') < partial.index('id="chat-send"')
    assert 'id="chat-continue-btn"' in partial
    continue_tag = partial[
        partial.index('<button id="chat-continue-btn"'):
        partial.index('>', partial.index('<button id="chat-continue-btn"'))
    ]
    assert "hidden" in continue_tag
    assert "#chat-input-row.chat-pill-boot .chat-pill-attach" in css
    assert "#chat-input-row.chat-pill-boot .chat-pill-voice" in css
    assert "width: 36px !important" in css
    assert "min-height: 88px" in css
    assert "padding: 28px 20px 13px 20px" in css
    assert "--chat-pill-font-size: 16px" in css
    assert "font-size: var(--chat-pill-font-size)" in css
    assert "font-family: inherit" in css
    assert "font-weight: 400" in css
    assert "border-radius: 41px" in css
    assert "color: var(--fg-4)" in css
    assert "opacity: 0.4" in css
    assert "pointer-events: none" in css
    assert '.chat-pill-layout-row[data-pill-row="1"]' in css
    assert "flex: 1 1 auto" in css


def test_continue_button_submits_a_normal_fresh_turn():
    source = (ROOT / "ui/chat/js/chat-ui.js").read_text(encoding="utf-8")
    start = source.index("// ── Continue button ──")
    end = source.index("// ── Stop button ──", start)
    handler = source[start:end]

    assert "app.chatInput.value = 'Continue'" in handler
    assert "sendMessage()" in handler
    assert "/api/v1/chat/resume" not in handler


def test_desktop_boot_composer_uses_configured_idle_width():
    css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")
    boot_rule = css[css.index("body.is-booting:not(.app-restricted) #chat-panel {") :]
    boot_rule = boot_rule[:boot_rule.index("\n  }")]

    assert "--chat-pill-current-width: var(--chat-pill-width) !important;" in boot_rule
    assert "content_max_width instead of" not in boot_rule


def test_common_composer_placeholder_and_signed_out_popover_copy():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    messages = config["chat_common"]["messages"]
    source = (ROOT / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")
    partial = (ROOT / "ui/chat/chat-side-panel.html").read_text(encoding="utf-8")

    assert messages["pill_placeholder"] == "Chat"
    assert "pill_locked_placeholder" not in messages
    assert 'placeholder="Chat"' in partial
    assert "app.chatInput.readOnly = true" in source
    assert "app.chatInput.placeholder = agentChatMsg('pill_placeholder')" in source
    assert "locked_message: 'Please sign in to use the app'" in source
    assert "locked_cta: 'Sign in'" in source


def test_chat_pill_rebuild_restores_focused_textarea_after_reparenting():
    source = (ROOT / "ui/shared/js/chat-pill-config.js").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "ui/shared/css/app1.css").read_text(encoding="utf-8")

    assert "document.activeElement === els.input" in source
    assert "pillEl.classList.add('chat-pill-caret-suppressed')" in source
    assert "void els.input.offsetHeight" in source
    assert "els.input.blur()" in source
    assert "els.input.focus({ preventScroll: true })" in source
    assert "els.input.setSelectionRange(" in source
    assert "els.input.addEventListener('pointerdown', revealCaret, true)" in source
    assert "els.input.addEventListener('beforeinput', revealCaret, true)" in source
    assert "#chat-input-row.chat-pill-caret-suppressed .chat-pill-input" in css
    assert "caret-color: transparent !important" in css


def test_initial_empty_composer_skips_the_hydrated_active_pill_frame():
    source = (
        ROOT / "ui/chat-controls/chat-controls-config.js"
    ).read_text(encoding="utf-8")

    assert "async function _buildActiveFooter({ initialIdle = false } = {})" in source
    assert "if (profile.chat_pill && !initialIdle)" in source
    initial_mode = source.index("const shouldStartIdle =")
    build = source.index("await _buildActiveFooter({ initialIdle: shouldStartIdle })")
    recheck = source.index("const commitIdle = shouldStartIdle", build)
    idle_commit = source.index("await switchFooterMode('idle')", build)
    assert initial_mode < build < recheck < idle_commit
    assert "await switchFooterMode('active')" in source[recheck:]


def test_idle_chat_composer_keeps_runtime_controls():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    row = config["chat_common"]["idle_footer"]["chat_pill"]["rows"][0]

    assert row["left"]["controls"] == ["attach"]
    assert row["center"]["controls"] == ["textarea"]
    assert row["right"]["controls"] == ["stop", "continue", "mic_send"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_composer_max_width_preserves_css_percentage_units():
    source = (ROOT / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")
    start = source.index("function _resolvePillMaxWidth(rawValue, availableW)")
    end = source.index("\n\n// Needed by chat-ui.js", start)
    helper = source[start:end]
    script = helper + textwrap.dedent(
        """

        console.log(JSON.stringify({
          percentage: _resolvePillMaxWidth('100%', 356),
          partialPercentage: _resolvePillMaxWidth('50%', 356),
          pixels: _resolvePillMaxWidth('1000px', 356),
          fallback: _resolvePillMaxWidth('', 356),
        }));
        """
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(completed.stdout) == {
        "percentage": 356,
        "partialPercentage": 178,
        "pixels": 1000,
        "fallback": 356,
    }
    assert "const configuredMaxW = _resolvePillMaxWidth(" in source


def test_active_footer_rebuild_reapplies_configured_resting_width_before_input():
    source = (ROOT / "ui/chat-controls/chat-controls-config.js").read_text(
        encoding="utf-8"
    )
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    active_width = config["chat_common"]["active_footer"]["chat_pill"]["pill_width"]

    assert active_width
    rebuild = source[source.index("applyChatPillLayout(pillEls(), activeProfile.chat_pill);"):]
    assert "app.autoResizeChatInput?.();" in rebuild.split("// Focus the textarea", 1)[0]


def test_boot_composer_waits_for_configured_idle_geometry_without_content_width_flash():
    css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")
    source = (ROOT / "ui/shared/js/app-prompts.js").read_text(encoding="utf-8")

    assert "data-chat-ui-ready" in css
    assert "--chat-pill-current-width: var(--chat-pill-width) !important;" in css
    assert "content_max_width instead of the" not in css
    assert "function _applyStaticComposerGeometry(ui)" in source
    assert "idle?.pill_width || active?.pill_width" in source
    assert "document.body.dataset.chatUiReady = 'true'" in source


def test_footer_actions_leave_before_idle_collapse_and_enter_after_active_layout():
    source = (ROOT / "ui/chat-controls/chat-controls-config.js").read_text(encoding="utf-8")
    css = (ROOT / "ui/shared/css/app1.css").read_text(encoding="utf-8")

    assert "footer-actions-exiting" in source
    assert "await _waitForFooterActions();" in source
    idle = source[source.index("if (mode === 'idle')"):source.index("  } else {", source.index("if (mode === 'idle')"))]
    assert idle.index("await _waitForFooterActions();") < idle.index("area?.classList.add('footer-mode-idle')")
    active = source[source.index("  } else {", source.index("if (mode === 'idle')")):]
    layout = active.index("applyChatPillLayout(pillEls(), activeProfile.chat_pill);")
    wait_after_layout = active.index("await _waitForFooterActions();", layout)
    assert active.index("fr?.classList.add('footer-actions-reserved');") < active.index("area.style.setProperty('bottom', activeProfile.margin_bottom, 'important');") < layout < wait_after_layout < active.index("fr?.classList.add('expanded');")
    assert ".chat-footer-row.footer-actions-exiting" in css
    assert "#chat-input-area.footer-mode-idle .chat-footer-row.footer-actions-reserved" in css
    assert "Reserve height immediately so expansion starts at its final baseline." in css


def test_active_textarea_keeps_configured_right_padding_inside_its_flex_width():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    css = (ROOT / "ui/shared/css/app1.css").read_text(encoding="utf-8")
    padding = config["chat_common"]["active_footer"]["chat_pill"]["controls"]["textarea"]["padding"].split()

    assert padding[1] == padding[3]
    input_rule = css[css.index(".chat-pill-input {"):css.index(".chat-pill-input::-webkit-scrollbar")]
    assert "box-sizing: border-box;" in input_rule


def test_composer_lower_fade_is_disabled_and_pull_cap_is_ten_pixels():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    transcript_bottoms = {"chat_common": 160, "chat_desktop": 200, "chat_mobile": 120, "chat_widget": 80}
    for surface, transcript_bottom in transcript_bottoms.items():
        assert config[surface]["fade"]["bottom"] == transcript_bottom
        assert config[surface]["pill_fade"]["bottom"] == 0
        assert config[surface]["transcript_overscroll"]["max_stretch_px"] == 10


def test_composer_dock_does_not_animate_before_active_layout_expands():
    css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")
    assert "transition: bottom" not in css


def test_idle_transition_clears_a_reversed_active_footer_reservation():
    source = (ROOT / "ui/chat-controls/chat-controls-config.js").read_text(encoding="utf-8")
    idle = source[source.index("if (mode === 'idle')"):source.index("  } else {", source.index("if (mode === 'idle')"))]
    assert "footerRow?.classList.remove('footer-actions-reserved');" in idle


def test_activity_progress_keeps_center_chip_without_a_transcript_mirror():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    source = (ROOT / "ui/chat-controls/chat-controls-config.js").read_text(encoding="utf-8")
    rows = config["chat_common"]["active_footer"]["above_pill"]["rows"]

    assert rows[0]["center"] == ["activity"]
    # The activity note lives in that footer chip. Activity notes are NOT
    # configured to render into the chat transcript, and the client's mock notes
    # ("Assessing…" / "Re-assessing…") follow the same rule — see
    # chat-activity.js MOCK_ACTIVITY_NOTES / mockActivityNote.
    assert config["chat_common"]["mirror_activity_in_transcript"] is False
    assert "function _dedupeActivityControl(containerEl)" in source
    assert "_dedupeActivityControl(containerEl);" in source


def test_activity_module_only_targets_a_configured_footer_row():
    source = (ROOT / "ui/shared/js/chat-activity.js").read_text(encoding="utf-8")

    assert "activityBars.find((el) => el.closest('[data-controls-row]')) || null" in source
    assert "if (textEl) textEl.textContent = txt;" in source
    assert "if (app.mirrorActivityNote)" in source
    css = (ROOT / "ui/shared/css/app1.css").read_text(encoding="utf-8")
    assert "#chat-above-pill > #chat-activity-bar," in css
    assert "#chat-above-pill > #chat-activity-panel," in css
    assert "display: none !important;" in css
    assert "panelEl = barEl?.closest('[data-element-origin=\"activity\"]')" in source


def test_react_composer_island_does_not_render_a_third_progress_surface():
    source = (ROOT / "frontend/composer/index.jsx").read_text(encoding="utf-8")

    assert "function ComposerIsland() {" in source
    assert "return null;" in source
    assert "chat-composer-react-status" not in source


def test_content_max_width_is_resolved_by_viewport_not_panel_density():
    """chat_common.content_max_width is a HOST rule — how wide chat content may
    run — so the chat-controls engine must resolve it from the viewport.

    Resolving it from _isNarrowPanel() handed chat_mobile's "100%" to the default
    desktop side panel (33%, i.e. under the 768px panel breakpoint), which dropped
    the 1000px cap the moment the panel was dragged wider than content_max_width.
    """
    source = (ROOT / "ui/chat-controls/chat-controls-config.js").read_text(encoding="utf-8")
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))

    assert config["chat_common"]["content_max_width"] == "1000px"
    assert config["chat_mobile"]["content_max_width"] == "100%"
    assert "content_max_width" not in config["chat_desktop"]

    # The width profile follows the viewport, never the panel-density helper.
    width_fn = source[source.index("function _contentWidthProfile(chatUi)"):]
    width_fn = width_fn[:width_fn.index("\n}")]
    assert "window.innerWidth <= MOBILE_VIEWPORT_MAX" in width_fn
    assert "_isNarrowPanel" not in width_fn
    assert "chatUi.chat_mobile" in width_fn
    assert "chatUi.chat_desktop" in width_fn

    # It is written to the surface variable, on every apply pass (agent switch
    # and panel-width crossing re-apply included), not only in the footer build.
    assert "surface.style.setProperty('--chat-surface-max-width', width)" in source
    resolved = source[source.index("const profile = mergeProfile(chatUi.chat_common, overrides);"):]
    assert "_applyContentMaxWidth(chatUi);" in resolved.split("await _buildActiveFooter", 1)[0]
