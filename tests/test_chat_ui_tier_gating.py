import json
from pathlib import Path

from app.auth import _chat_ui_deep_merge, _resolve_chat_ui_config


ROOT = Path(__file__).resolve().parents[1]


def _config():
    return json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))


def _capabilities(*, tier="anonymous", subject="anonymous", features=(), groups=(), admin=False):
    return {
        "subject": {"class": subject, "is_admin": admin},
        "tier": {"slug": tier},
        "features": {name: True for name in features},
        "pages": {},
        "ability_groups": list(groups),
    }


def test_chat_ui_is_resolved_server_side_for_anonymous_tier():
    resolved = _resolve_chat_ui_config(
        _config(),
        _capabilities(tier="anonymous", features=("chat",)),
    )

    assert "tier_gates" not in resolved
    assert resolved["_tier_enforced"]["tier"] == "anonymous"
    assert {
        "model_changer", "storage_mode", "sub_agent_tabs",
        "local_changes", "message_id", "agent_id", "session_id", "engine_thread_id",
    } <= set(
        resolved["_tier_enforced"]["controls"]
    )
    controls = resolved["chat_common"]["active_footer"]["chat_pill"]["controls"]
    assert controls["attach"]["enabled"] is True
    assert controls["attach"]["locked"] is True
    assert controls["attach"]["locked_cta"] == "Register"
    assert "attach" in resolved["chat_common"]["active_footer"]["chat_pill"]["rows"][1]["left"]["controls"]
    assert "model_changer" not in resolved["chat_common"]["active_footer"]["below_pill"]["rows"][0]["right"]
    assert resolved["chat_desktop"]["chat_header"]["controls"]["sub_agent_tabs"]["enabled"] is False
    dropdown = resolved["chat_common"]["chat_header"]["controls"]["session_dropdown"]
    assert dropdown["menu"]["content_search"] is False
    assert dropdown["menu"]["batch_delete"] is False
    assert dropdown["menu"]["grouping"] is False
    assert dropdown["menu"]["agent_id"] is False
    assert dropdown["menu"]["session_id"] is False
    assert dropdown["menu"]["engine_thread_id"] is False
    gutters = resolved["chat_common"]["message_gutters"]
    assert gutters["user"]["controls"]["undo"]["locked"] is True
    assert gutters["user"]["controls"]["undo"]["locked_feature"] == "Undo"
    assert gutters["agent"]["controls"]["fork"]["locked"] is True
    assert gutters["agent"]["controls"]["fork"]["locked_feature"] == "Fork"
    assert gutters["more_menu"]["controls"]["message_id"]["enabled"] is False
    assert "message_id" not in gutters["more_menu"]["order"]


def test_capability_grants_keep_matching_chat_controls_enabled():
    resolved = _resolve_chat_ui_config(
        _config(),
        _capabilities(
            tier="pro",
            subject="registered",
            features=("chat", "attachments", "model_picker", "user_byod"),
            groups=("agent_orchestration", "developer_write"),
        ),
    )

    assert resolved["_tier_enforced"]["controls"] == ["agent_id", "engine_thread_id", "message_id", "session_id"]
    controls = resolved["chat_common"]["active_footer"]["chat_pill"]["controls"]
    assert controls["attach"]["enabled"] is True
    dropdown = resolved["chat_common"]["chat_header"]["controls"]["session_dropdown"]
    assert dropdown["menu"]["content_search"] is True
    assert dropdown["menu"]["grouping"] is True
    assert dropdown["menu"]["agent_id"] is False
    assert dropdown["menu"]["session_id"] is False


def test_admin_is_the_only_subject_that_receives_message_ids():
    resolved = _resolve_chat_ui_config(
        _config(),
        _capabilities(
            tier="pro",
            subject="registered",
            features=("chat",),
            admin=True,
        ),
    )

    gutters = resolved["chat_common"]["message_gutters"]
    assert "message_id" not in resolved["_tier_enforced"]["controls"]
    assert gutters["more_menu"]["controls"]["message_id"]["enabled"] is True
    assert "message_id" in gutters["more_menu"]["order"]
    dropdown = resolved["chat_common"]["chat_header"]["controls"]["session_dropdown"]
    assert dropdown["menu"]["agent_id"] is True
    assert dropdown["menu"]["session_id"] is True


def test_tier_overlay_relocks_attach_and_blocks_denied_control_overrides():
    overridden = _chat_ui_deep_merge(
        _config(),
        {"chat_common": {
            "active_footer": {"chat_pill": {"controls": {
                "attach": {"enabled": True, "locked": False, "element_size": "99px"},
            }}},
            "chat_header": {"controls": {"session_dropdown": {"menu": {
                "agent_id": True,
                "session_id": True,
            }}}},
        }},
    )
    resolved = _resolve_chat_ui_config(
        overridden,
        _capabilities(tier="anonymous", features=("chat",)),
    )

    attach = resolved["chat_common"]["active_footer"]["chat_pill"]["controls"]["attach"]
    assert attach["enabled"] is True
    assert attach["locked"] is True
    assert attach["element_size"] == "99px"
    dropdown = resolved["chat_common"]["chat_header"]["controls"]["session_dropdown"]
    assert dropdown["menu"]["agent_id"] is False
    assert dropdown["menu"]["session_id"] is False


def test_session_dropdown_has_first_class_trigger_and_menu_schema():
    dropdown = _config()["chat_common"]["chat_header"]["controls"]["session_dropdown"]

    assert {"agent_icon", "status", "row_actions", "delete", "rename_on_hold"} <= set(dropdown["trigger"])
    assert {
        "search", "content_search", "recycle_bin", "hidden_sessions",
        "batch_delete", "grouping", "pinning", "rename", "auto_rename",
        "row_actions", "delete", "width", "max_height",
    } <= set(dropdown["menu"])


def test_message_gutters_have_ordered_configurable_controls():
    gutters = _config()["chat_common"]["message_gutters"]

    assert gutters["user"]["order"] == ["time", "copy", "undo", "delete", "more"]
    assert "fork" in gutters["agent"]["order"]
    assert gutters["user"]["controls"]["undo"]["locked_message"]
    assert gutters["agent"]["controls"]["fork"]["locked_message"]
    assert "message_id" in gutters["more_menu"]["order"]
