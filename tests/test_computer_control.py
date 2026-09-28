from __future__ import annotations

import asyncio
import json

from app import abilities


def test_computer_control_is_a_discoverable_administrator_drop_in():
    abilities.reload()
    entry = abilities.all_raw()["computer_control"]

    assert entry["display_name"] == "Computer Control"
    assert entry["group"] == "administrator"
    assert entry["default_enabled"] is False
    assert entry["tools"] == [
        "computer_screenshot",
        "computer_move",
        "computer_click",
        "computer_scroll",
        "computer_drag",
        "computer_type",
        "computer_key",
    ]
    assert entry["has_runtime"] is True
    assert "computer_screenshot" in abilities.ability_feature_with_skill(
        "computer_control"
    )["skill"]


def test_phase_three_exposes_input_tools_and_marks_consequential_actions():
    abilities.reload()
    module = abilities.ability_module("computer_control")
    tools = module.build_tools(user_id="user", session_id="session")

    expected = {
        "computer_screenshot",
        "computer_move",
        "computer_click",
        "computer_scroll",
        "computer_drag",
        "computer_type",
        "computer_key",
    }
    assert set(tools) == expected
    assert set(module.TOOL_SCHEMAS) == expected
    assert module.TOOL_SCHEMAS["computer_screenshot"]["required"] == []
    assert module.DESTRUCTIVE == {
        "computer_click",
        "computer_drag",
        "computer_type",
        "computer_key",
    }


def test_screenshot_result_preserves_coordinate_contract(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    png = b"\x89PNG\r\n\x1a\n" + (b"x" * 256)

    monkeypatch.setattr(
        module,
        "_capture_desktop",
        lambda: module._DesktopCapture(
            png=png,
            width=3200,
            height=1080,
            virtual_left=-1280,
            virtual_top=0,
            native_width=3200,
            native_height=1080,
        ),
    )

    async def fake_store(**_kwargs):
        return {
            "id": "attachment-1",
            "filename": "desktop.png",
            "mime_type": "image/png",
            "size_bytes": len(png),
            "storage_path": "ignored",
            "storage_provider": "local",
        }

    async def fake_describe(**_kwargs):
        return {"vision_model": "vision-test", "description": "A desktop."}

    monkeypatch.setattr(module, "_store_screenshot", fake_store)
    monkeypatch.setattr(module, "_describe_screenshot", fake_describe)

    tool = module.build_tools(user_id="user", session_id="session")[
        "computer_screenshot"
    ]
    result = json.loads(asyncio.run(tool(question="What is open?")))

    assert result["status"] == "ok"
    assert result["phase"] == "keyboard_control"
    assert result["dimensions"] == {"width": 3200, "height": 1080}
    assert result["coordinate_space"] == {
        "units": "screenshot_pixels",
        "left": 0,
        "top": 0,
        "right": 3199,
        "bottom": 1079,
        "native_virtual_left": -1280,
        "native_virtual_top": 0,
        "native_virtual_width": 3200,
        "native_virtual_height": 1080,
    }
    assert result["attachment"]["id"] == "attachment-1"
    assert result["description"] == "A desktop."
    assert result["observation"]["ready_for_keyboard_action"] is True


def test_pointer_actions_require_observe_then_verify(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    tools = module.build_tools(
        user_id="user", session_id="session", agent_id="agent"
    )

    before_observation = json.loads(
        asyncio.run(tools["computer_move"](x=50, y=60))
    )
    assert before_observation["code"] == "observation_required"

    png = b"\x89PNG\r\n\x1a\n" + (b"x" * 256)
    monkeypatch.setattr(
        module,
        "_capture_desktop",
        lambda: module._DesktopCapture(
            png=png,
            width=1920,
            height=1080,
            virtual_left=0,
            virtual_top=0,
            native_width=1920,
            native_height=1080,
        ),
    )
    monkeypatch.setattr(
        module,
        "_windows_virtual_bounds",
        lambda: (0, 0, 1920, 1080),
    )

    async def fake_store(**_kwargs):
        return {
            "id": "attachment-2",
            "filename": "desktop.png",
            "mime_type": "image/png",
            "size_bytes": len(png),
            "storage_path": "ignored",
            "storage_provider": "local",
        }

    moved = []
    clicked = []
    monkeypatch.setattr(module, "_store_screenshot", fake_store)
    monkeypatch.setattr(
        module,
        "_move_pointer",
        lambda x, y, duration, state: moved.append((x, y, duration, state.width)),
    )
    monkeypatch.setattr(
        module,
        "_click_pointer",
        lambda x, y, button, clicks, interval, state: clicked.append(
            (x, y, button, clicks, interval, state.height)
        ),
    )

    screenshot = json.loads(
        asyncio.run(tools["computer_screenshot"](analyze=False))
    )
    assert screenshot["observation"]["ready_for_pointer_action"] is True

    move = json.loads(
        asyncio.run(tools["computer_move"](x=50, y=60, duration_ms=250))
    )
    assert move["status"] == "ok"
    assert move["requires_screenshot"] is True
    assert moved == [(50, 60, 250, 1920)]

    blind_click = json.loads(
        asyncio.run(tools["computer_click"](x=70, y=80))
    )
    assert blind_click["code"] == "verification_required"
    assert clicked == []

    asyncio.run(tools["computer_screenshot"](analyze=False))
    click = json.loads(
        asyncio.run(
            tools["computer_click"](
                x=70,
                y=80,
                button="right",
                clicks=2,
                interval_ms=120,
            )
        )
    )
    assert click["status"] == "ok"
    assert clicked == [(70, 80, "right", 2, 120, 1080)]


def test_pointer_coordinate_normalization_and_bounds(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")

    assert module._normalize_coordinate(0, 1920) == 0
    assert module._normalize_coordinate(1919, 1920) == 65535
    assert 32750 <= module._normalize_coordinate(960, 1920) <= 32800

    module._DESKTOP_STATES[("u", "s", "a")] = module._DesktopState(
        width=100,
        height=50,
        virtual_left=-100,
        virtual_top=0,
        native_width=100,
        native_height=50,
        observed_at=module.time.monotonic(),
        generation=module._HOST_ACTION_GENERATION,
    )
    monkeypatch.setattr(
        module, "_windows_virtual_bounds", lambda: (-100, 0, 100, 50)
    )
    with module._POINTER_LOCK:
        try:
            module._validate_action_state(("u", "s", "a"), ((100, 10),))
        except module._PointerControlError as exc:
            assert exc.code == "coordinates_out_of_bounds"
        else:
            raise AssertionError("Out-of-bounds action was accepted")


def test_scroll_uses_wheel_notches_and_drag_releases_button(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    state = module._DesktopState(
        width=100,
        height=100,
        virtual_left=0,
        virtual_top=0,
        native_width=100,
        native_height=100,
        observed_at=module.time.monotonic(),
        generation=0,
    )
    batches = []
    monkeypatch.setattr(
        module, "_send_mouse_events", lambda events: batches.append(events)
    )

    module._scroll_pointer(-3, 25, 30, state)
    assert batches[-1] == [(0, 0, -360, module._MOUSE_WHEEL)]

    batches.clear()
    module._drag_pointer(10, 10, 20, 20, "left", 100, state)
    assert batches[1] == [(0, 0, 0, module._MOUSE_LEFT_DOWN)]
    assert batches[-1] == [(0, 0, 0, module._MOUSE_LEFT_UP)]


def test_non_windows_pointer_backend_fails_explicitly(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    monkeypatch.setattr(module.sys, "platform", "darwin")
    tools = module.build_tools(user_id="user", session_id="session")

    result = json.loads(asyncio.run(tools["computer_move"](x=1, y=1)))

    assert result["status"] == "error"
    assert result["code"] == "unsupported_platform"


def test_unicode_text_and_key_chord_event_construction(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    batches = []
    monkeypatch.setattr(
        module, "_send_keyboard_events", lambda events: batches.append(events)
    )

    module._type_text("A✓😀", 0)
    flat = [event for batch in batches for event in batch]
    assert len(flat) == 8  # A, check mark, and a two-unit UTF-16 emoji.
    assert flat[0] == (0, ord("A"), module._KEY_UNICODE)
    assert flat[1] == (0, ord("A"), module._KEY_UNICODE | module._KEY_UP)

    normalized = module._normalize_key_chord(["l", "control", "shift"])
    assert normalized == ("CTRL", "SHIFT", "L")
    batches.clear()
    module._press_key_chord(normalized)
    assert len(batches) == 2
    assert len(batches[0]) == 3
    assert len(batches[1]) == 3
    assert batches[0][0][0] == 0x11  # CTRL down
    assert batches[1][-1] == (0x11, 0, module._KEY_UP)

    try:
        module._normalize_key_chord(["A", "B"])
    except module._PointerControlError as exc:
        assert exc.code == "invalid_chord"
    else:
        raise AssertionError("A chord with two ordinary keys was accepted")


def test_keyboard_actions_require_observe_then_verify_and_do_not_echo_text(
    monkeypatch,
):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    tools = module.build_tools(
        user_id="keyboard-user",
        session_id="keyboard-session",
        agent_id="keyboard-agent",
    )

    before_observation = json.loads(
        asyncio.run(tools["computer_key"](keys=["ENTER"]))
    )
    assert before_observation["code"] == "observation_required"

    png = b"\x89PNG\r\n\x1a\n" + (b"x" * 256)
    monkeypatch.setattr(
        module,
        "_capture_desktop",
        lambda: module._DesktopCapture(
            png=png,
            width=1920,
            height=1080,
            virtual_left=0,
            virtual_top=0,
            native_width=1920,
            native_height=1080,
        ),
    )
    monkeypatch.setattr(
        module, "_windows_virtual_bounds", lambda: (0, 0, 1920, 1080)
    )

    async def fake_store(**_kwargs):
        return {
            "id": "keyboard-attachment",
            "filename": "desktop.png",
            "mime_type": "image/png",
            "size_bytes": len(png),
            "storage_path": "ignored",
            "storage_provider": "local",
        }

    typed = []
    chords = []
    monkeypatch.setattr(module, "_store_screenshot", fake_store)
    monkeypatch.setattr(
        module, "_type_text", lambda text, interval: typed.append((text, interval))
    )
    monkeypatch.setattr(
        module, "_press_key_chord", lambda keys: chords.append(keys)
    )

    asyncio.run(tools["computer_screenshot"](analyze=False))
    text = "phase-three-private-value"
    typed_result_raw = asyncio.run(
        tools["computer_type"](text=text, interval_ms=25)
    )
    typed_result = json.loads(typed_result_raw)
    assert typed_result["status"] == "ok"
    assert typed_result["characters"] == len(text)
    assert text not in typed_result_raw
    assert typed == [(text, 25)]

    blind_key = json.loads(
        asyncio.run(tools["computer_key"](keys=["CTRL", "A"]))
    )
    assert blind_key["code"] == "verification_required"
    assert chords == []

    asyncio.run(tools["computer_screenshot"](analyze=False))
    key_result = json.loads(
        asyncio.run(tools["computer_key"](keys=["a", "control"]))
    )
    assert key_result["status"] == "ok"
    assert key_result["keys"] == ["CTRL", "A"]
    assert chords == [("CTRL", "A")]


def test_keyboard_text_rejects_controls_without_consuming_observation(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    key = ("u3", "s3", "a3")
    module._DESKTOP_STATES[key] = module._DesktopState(
        width=100,
        height=100,
        virtual_left=0,
        virtual_top=0,
        native_width=100,
        native_height=100,
        observed_at=module.time.monotonic(),
        generation=module._HOST_ACTION_GENERATION,
    )
    monkeypatch.setattr(
        module, "_windows_virtual_bounds", lambda: (0, 0, 100, 100)
    )
    tools = module.build_tools(user_id="u3", session_id="s3", agent_id="a3")

    invalid = json.loads(
        asyncio.run(tools["computer_type"](text="line one\nline two"))
    )
    assert invalid["code"] == "invalid_text"
    assert module._DESKTOP_STATES[key].awaiting_verification is False


def test_key_chord_attempts_release_when_key_down_fails(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    batches = []

    def fail_down_then_release(events):
        batches.append(events)
        if len(batches) == 1:
            raise OSError("simulated partial SendInput failure")

    monkeypatch.setattr(module, "_send_keyboard_events", fail_down_then_release)

    try:
        module._press_key_chord(("CTRL", "L"))
    except OSError:
        pass
    else:
        raise AssertionError("The simulated SendInput failure was swallowed")

    assert len(batches) == 2
    assert all(event[2] & module._KEY_UP for event in batches[1])


def test_desktop_stream_frame_keeps_native_coordinates_and_scales_transport(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    from PIL import Image
    import io

    source = Image.new("RGB", (3200, 1200), "navy")
    output = io.BytesIO()
    source.save(output, format="PNG")
    source.close()
    capture = module._DesktopCapture(
        png=output.getvalue(),
        width=3200,
        height=1200,
        virtual_left=-1280,
        virtual_top=0,
        native_width=3200,
        native_height=1200,
    )
    monkeypatch.setattr(module, "_capture_desktop", lambda: capture)

    observed, jpeg = module.capture_desktop_stream_frame(
        max_width=1600, max_height=900, quality=55
    )

    assert observed is capture
    with Image.open(io.BytesIO(jpeg)) as frame:
        assert frame.format == "JPEG"
        assert frame.size == (1600, 600)


def test_desktop_stream_input_uses_shared_native_lock_and_invalidates_agent_view(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    monkeypatch.setattr(module.sys, "platform", "win32")
    events = []
    monkeypatch.setattr(module, "_send_mouse_events", lambda batch: events.extend(batch))
    module._HOST_ACTION_GENERATION = 11
    capture = module._DesktopCapture(
        png=b"png",
        width=1920,
        height=1080,
        native_width=1920,
        native_height=1080,
    )

    module.dispatch_desktop_stream_input(
        capture, {"kind": "mousedown", "x": 960, "y": 540, "button": "left"}
    )

    assert len(events) == 2
    assert events[0][3] & module._MOUSE_ABSOLUTE
    assert events[1][3] == module._MOUSE_LEFT_DOWN
    assert module._HOST_ACTION_GENERATION == 12


def test_desktop_stream_input_rejects_coordinates_outside_latest_frame(monkeypatch):
    abilities.reload()
    module = abilities.ability_module("computer_control")
    monkeypatch.setattr(module.sys, "platform", "win32")
    capture = module._DesktopCapture(
        png=b"png", width=100, height=50, native_width=100, native_height=50
    )

    try:
        module.dispatch_desktop_stream_input(
            capture, {"kind": "click", "x": 100, "y": 10}
        )
    except module._PointerControlError as exc:
        assert exc.code == "coordinates_out_of_bounds"
    else:
        raise AssertionError("Out-of-bounds remote desktop input was accepted")
