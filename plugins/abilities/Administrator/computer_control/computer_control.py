"""Computer Control ability.

Phase 3 exposes full-desktop screenshots plus native Windows pointer and
keyboard input. Public coordinates always refer to pixels in the most recent
``computer_screenshot`` PNG. The runtime rejects blind, stale, out-of-bounds,
and unverified follow-up actions.

Screenshot capture is platform-neutral through Pillow. Input actions currently
return an explicit unsupported-platform result on macOS and Linux so the public
tool contract can remain stable while native backends are added later.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import platform
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = {
    "computer_click",
    "computer_drag",
    "computer_type",
    "computer_key",
}

_MAX_OBSERVATION_AGE_SECONDS = 300.0
_STATE_LOCK = threading.Lock()
_POINTER_LOCK = threading.Lock()
_INPUT_LOCK = _POINTER_LOCK
_DESKTOP_STATES: dict[tuple[str, str, str], "_DesktopState"] = {}
_HOST_ACTION_GENERATION = 0
_DPI_AWARENESS_ATTEMPTED = False


@dataclass(frozen=True)
class _DesktopCapture:
    png: bytes
    width: int
    height: int
    virtual_left: int = 0
    virtual_top: int = 0
    native_width: int = 0
    native_height: int = 0


@dataclass
class _DesktopState:
    width: int
    height: int
    virtual_left: int
    virtual_top: int
    native_width: int
    native_height: int
    observed_at: float
    generation: int
    awaiting_verification: bool = False


class _PointerControlError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _enable_windows_dpi_awareness() -> None:
    """Opt into physical-pixel coordinates before capturing or sending input."""
    global _DPI_AWARENESS_ATTEMPTED
    if sys.platform != "win32" or _DPI_AWARENESS_ATTEMPTED:
        return
    _DPI_AWARENESS_ATTEMPTED = True
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        logger.debug("Could not enable Windows DPI awareness", exc_info=True)


def _windows_virtual_bounds() -> tuple[int, int, int, int]:
    """Return native (left, top, width, height) for the Windows virtual desktop."""
    if sys.platform != "win32":
        return 0, 0, 0, 0
    _enable_windows_dpi_awareness()
    import ctypes

    user32 = ctypes.windll.user32
    return (
        int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
        int(user32.GetSystemMetrics(77)),  # SM_YVIRTUALSCREEN
        int(user32.GetSystemMetrics(78)),  # SM_CXVIRTUALSCREEN
        int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
    )


def _windows_virtual_origin() -> tuple[int, int]:
    """Return the native origin of the Windows virtual screen.

    A monitor to the left or above the primary display produces a negative
    origin. Public action coordinates remain normalized to the PNG's top-left;
    pointer tools translate through these values.
    """
    if sys.platform != "win32":
        return 0, 0
    try:
        left, top, _width, _height = _windows_virtual_bounds()
        return left, top
    except Exception:
        logger.debug("Could not read the Windows virtual-screen origin", exc_info=True)
        return 0, 0


def _capture_desktop() -> _DesktopCapture:
    """Capture the current interactive desktop with Pillow's native backend."""
    from PIL import ImageGrab

    kwargs = {}
    native_left = native_top = 0
    native_width = native_height = 0
    if sys.platform == "win32":
        _enable_windows_dpi_awareness()
        native_left, native_top, native_width, native_height = (
            _windows_virtual_bounds()
        )
        kwargs = {"include_layered_windows": True, "all_screens": True}

    grabbed = ImageGrab.grab(**kwargs)
    image = grabbed
    try:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        with io.BytesIO() as output:
            image.save(output, format="PNG")
            png = output.getvalue()
        return _DesktopCapture(
            png=png,
            width=int(image.width),
            height=int(image.height),
            virtual_left=native_left,
            virtual_top=native_top,
            native_width=native_width or int(image.width),
            native_height=native_height or int(image.height),
        )
    finally:
        if image is not grabbed:
            image.close()
        grabbed.close()


async def _store_screenshot(
    *, png: bytes, user_id: str, session_id: str
) -> dict:
    """Persist a captured PNG in the owning conversation."""
    from app.db import get_db
    from app.db.attachments import store_file

    filename = f"computer_screenshot_{int(time.time() * 1000)}.png"
    stored = await store_file(
        user_id=user_id,
        session_id=session_id,
        file_bytes=png,
        filename=filename,
        mime_type="image/png",
    )
    db = get_db()
    attachment_id = await db.insert_attachment(
        user_id=user_id,
        session_id=session_id,
        original_name=filename,
        mime_type="image/png",
        size_bytes=len(png),
        storage_path=stored["storage_path"],
        storage_provider=stored.get("storage_provider", "local"),
    )
    return {
        "id": attachment_id,
        "filename": filename,
        "original_name": filename,
        "mime_type": "image/png",
        "size_bytes": len(png),
        "storage_path": stored["storage_path"],
        "storage_provider": stored.get("storage_provider", "local"),
    }


async def _describe_screenshot(
    *, attachment: dict, question: str, dimensions: str, user_id: str
) -> dict:
    """Return a vision description or a non-fatal availability warning."""
    from app.admin.settings import load_llm_capabilities_for_user, pick_vision_model
    from app.agent.provider_completion import ask_model

    try:
        from app import model_catalog

        await model_catalog.ensure_fresh()
    except Exception:
        logger.debug("Model catalog refresh failed before desktop analysis", exc_info=True)

    capabilities = await load_llm_capabilities_for_user(user_id)
    vision = pick_vision_model(capabilities)
    if not vision:
        return {
            "warning": (
                "Screenshot captured, but no image-input model is configured. "
                "Enable one in App Config -> Models to receive a description."
            )
        }

    prompt = (question or "").strip()
    if prompt:
        user_prompt = (
            f"This is a {dimensions} screenshot of the user's complete desktop. "
            f"Answer only this visual question: {prompt}"
        )
    else:
        user_prompt = (
            f"This is a {dimensions} screenshot of the user's complete desktop. "
            "Describe the visible applications, windows, dialogs, controls, text, "
            "and notable UI state precisely. Do not infer anything not visible. "
            "Do not repeat secrets, passwords, authentication codes, API keys, or "
            "payment details; identify those only as sensitive content."
        )

    system_prompt = (
        "Inspect desktop screenshots precisely and factually. Distinguish visible "
        "facts from uncertainty. Describe positions in screen-relative terms. "
        "Never reproduce visible credentials or other authentication secrets."
    )
    answer = await ask_model(
        vision,
        system_prompt,
        user_prompt,
        attachments=[attachment],
        max_tokens=1200,
    )
    if not answer:
        return {
            "vision_model": vision.get("model", ""),
            "warning": "Screenshot captured, but the vision model returned no description.",
        }
    return {"vision_model": vision.get("model", ""), "description": answer}


def _state_key(user_id: str, session_id: str, agent_id: str) -> tuple[str, str, str]:
    return user_id or "", session_id or "", agent_id or ""


def _record_observation(
    key: tuple[str, str, str], capture: _DesktopCapture
) -> _DesktopState:
    """Make a successfully saved screenshot the only valid action reference."""
    with _STATE_LOCK:
        state = _DesktopState(
            width=capture.width,
            height=capture.height,
            virtual_left=capture.virtual_left,
            virtual_top=capture.virtual_top,
            native_width=capture.native_width or capture.width,
            native_height=capture.native_height or capture.height,
            observed_at=time.monotonic(),
            generation=_HOST_ACTION_GENERATION,
        )
        _DESKTOP_STATES[key] = state
        return state


def _validate_action_state(
    key: tuple[str, str, str], points: tuple[tuple[int, int], ...]
) -> _DesktopState:
    """Reject action calls not grounded in a fresh, still-current screenshot."""
    if sys.platform != "win32":
        raise _PointerControlError(
            "unsupported_platform",
            "Computer input control is implemented on Windows only in Phase 3. "
            f"The current platform is {platform.system()}.",
        )

    with _STATE_LOCK:
        state = _DESKTOP_STATES.get(key)
        current_generation = _HOST_ACTION_GENERATION
        if state is None:
            raise _PointerControlError(
                "observation_required",
                "Call computer_screenshot before using computer input controls.",
            )
        if state.awaiting_verification or state.generation != current_generation:
            raise _PointerControlError(
                "verification_required",
                "The desktop may have changed since the last action. Call "
                "computer_screenshot, inspect it, then choose the next action.",
            )
        age = time.monotonic() - state.observed_at
        if age > _MAX_OBSERVATION_AGE_SECONDS:
            raise _PointerControlError(
                "stale_screenshot",
                f"The last screenshot is {int(age)} seconds old. Capture a fresh "
                "screenshot before acting.",
            )

    left, top, width, height = _windows_virtual_bounds()
    if (
        left != state.virtual_left
        or top != state.virtual_top
        or width != state.native_width
        or height != state.native_height
    ):
        raise _PointerControlError(
            "display_layout_changed",
            "The monitor layout or display scale changed after the screenshot. "
            "Call computer_screenshot again before acting.",
        )

    for x, y in points:
        if not (0 <= x < state.width and 0 <= y < state.height):
            raise _PointerControlError(
                "coordinates_out_of_bounds",
                f"Coordinate ({x}, {y}) is outside the latest screenshot "
                f"({state.width}x{state.height}).",
            )
    return state


def _normalize_coordinate(value: int, extent: int) -> int:
    """Map a zero-based screenshot coordinate to SendInput's 0..65535 range."""
    if extent <= 1:
        return 0
    return max(0, min(65535, round(int(value) * 65535 / (int(extent) - 1))))


def _send_mouse_events(events: list[tuple[int, int, int, int]]) -> None:
    """Send ``(dx, dy, mouseData, flags)`` events through Win32 SendInput."""
    if sys.platform != "win32":
        raise _PointerControlError(
            "unsupported_platform", "Windows SendInput is unavailable."
        )
    if not events:
        return

    import ctypes
    from ctypes import wintypes

    class _MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _InputUnion(ctypes.Union):
        _fields_ = [("mi", _MouseInput)]

    class _Input(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]

    inputs = (_Input * len(events))(
        *[
            _Input(
                type=0,  # INPUT_MOUSE
                union=_InputUnion(
                    mi=_MouseInput(
                        dx=dx,
                        dy=dy,
                        mouseData=data & 0xFFFFFFFF,
                        dwFlags=flags,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            )
            for dx, dy, data, flags in events
        ]
    )
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(_Input),
        ctypes.c_int,
    )
    user32.SendInput.restype = wintypes.UINT
    sent = int(user32.SendInput(len(inputs), inputs, ctypes.sizeof(_Input)))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        raise OSError(
            error,
            f"Windows SendInput accepted {sent} of {len(inputs)} mouse events.",
        )


_MOUSE_MOVE = 0x0001
_MOUSE_LEFT_DOWN = 0x0002
_MOUSE_LEFT_UP = 0x0004
_MOUSE_RIGHT_DOWN = 0x0008
_MOUSE_RIGHT_UP = 0x0010
_MOUSE_MIDDLE_DOWN = 0x0020
_MOUSE_MIDDLE_UP = 0x0040
_MOUSE_WHEEL = 0x0800
_MOUSE_VIRTUAL_DESKTOP = 0x4000
_MOUSE_ABSOLUTE = 0x8000
_ABSOLUTE_MOVE_FLAGS = _MOUSE_MOVE | _MOUSE_VIRTUAL_DESKTOP | _MOUSE_ABSOLUTE

_KEY_EXTENDED = 0x0001
_KEY_UP = 0x0002
_KEY_UNICODE = 0x0004

_KEY_ALIASES = {
    "CONTROL": "CTRL",
    "RETURN": "ENTER",
    "ESC": "ESCAPE",
    "DEL": "DELETE",
    "INS": "INSERT",
    "PGUP": "PAGEUP",
    "PAGE_UP": "PAGEUP",
    "PGDN": "PAGEDOWN",
    "PAGE_DOWN": "PAGEDOWN",
    "ARROWLEFT": "LEFT",
    "ARROW_LEFT": "LEFT",
    "ARROWRIGHT": "RIGHT",
    "ARROW_RIGHT": "RIGHT",
    "ARROWUP": "UP",
    "ARROW_UP": "UP",
    "ARROWDOWN": "DOWN",
    "ARROW_DOWN": "DOWN",
    "WINDOWS": "WIN",
    "META": "WIN",
}

_NAMED_VIRTUAL_KEYS = {
    "BACKSPACE": (0x08, False),
    "TAB": (0x09, False),
    "ENTER": (0x0D, False),
    "SHIFT": (0x10, False),
    "CTRL": (0x11, False),
    "ALT": (0x12, False),
    "PAUSE": (0x13, False),
    "CAPSLOCK": (0x14, False),
    "ESCAPE": (0x1B, False),
    "SPACE": (0x20, False),
    "PAGEUP": (0x21, True),
    "PAGEDOWN": (0x22, True),
    "END": (0x23, True),
    "HOME": (0x24, True),
    "LEFT": (0x25, True),
    "UP": (0x26, True),
    "RIGHT": (0x27, True),
    "DOWN": (0x28, True),
    "PRINTSCREEN": (0x2C, True),
    "INSERT": (0x2D, True),
    "DELETE": (0x2E, True),
    "WIN": (0x5B, True),
    "NUMLOCK": (0x90, True),
    "SCROLLLOCK": (0x91, False),
    "SEMICOLON": (0xBA, False),
    "EQUALS": (0xBB, False),
    "COMMA": (0xBC, False),
    "MINUS": (0xBD, False),
    "PERIOD": (0xBE, False),
    "SLASH": (0xBF, False),
    "BACKTICK": (0xC0, False),
    "BRACKET_LEFT": (0xDB, False),
    "BACKSLASH": (0xDC, False),
    "BRACKET_RIGHT": (0xDD, False),
    "QUOTE": (0xDE, False),
}
_MODIFIER_KEYS = {"CTRL", "SHIFT", "ALT", "WIN"}


def _absolute_event(x: int, y: int, state: _DesktopState) -> tuple[int, int, int, int]:
    return (
        _normalize_coordinate(x, state.width),
        _normalize_coordinate(y, state.height),
        0,
        _ABSOLUTE_MOVE_FLAGS,
    )


def _button_events(button: str) -> tuple[int, int]:
    buttons = {
        "left": (_MOUSE_LEFT_DOWN, _MOUSE_LEFT_UP),
        "right": (_MOUSE_RIGHT_DOWN, _MOUSE_RIGHT_UP),
        "middle": (_MOUSE_MIDDLE_DOWN, _MOUSE_MIDDLE_UP),
    }
    try:
        return buttons[button]
    except KeyError as exc:
        raise _PointerControlError(
            "invalid_button", "button must be left, right, or middle."
        ) from exc


def _move_pointer(x: int, y: int, duration_ms: int, state: _DesktopState) -> None:
    target_x = _normalize_coordinate(x, state.width)
    target_y = _normalize_coordinate(y, state.height)
    duration_ms = int(duration_ms)
    if duration_ms <= 0:
        _send_mouse_events([(target_x, target_y, 0, _ABSOLUTE_MOVE_FLAGS)])
        return

    import ctypes
    from ctypes import wintypes

    point = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise OSError(ctypes.get_last_error(), "GetCursorPos failed.")
    current_x = _normalize_coordinate(
        round(
            (point.x - state.virtual_left)
            * (state.width - 1)
            / max(1, state.native_width - 1)
        ),
        state.width,
    )
    current_y = _normalize_coordinate(
        round(
            (point.y - state.virtual_top)
            * (state.height - 1)
            / max(1, state.native_height - 1)
        ),
        state.height,
    )
    steps = max(2, min(120, round(duration_ms / 16)))
    pause = duration_ms / steps / 1000
    for step in range(1, steps + 1):
        ratio = step / steps
        nx = round(current_x + (target_x - current_x) * ratio)
        ny = round(current_y + (target_y - current_y) * ratio)
        _send_mouse_events([(nx, ny, 0, _ABSOLUTE_MOVE_FLAGS)])
        if step != steps:
            time.sleep(pause)


def _click_pointer(
    x: int,
    y: int,
    button: str,
    clicks: int,
    interval_ms: int,
    state: _DesktopState,
) -> None:
    down, up = _button_events(button)
    _send_mouse_events([_absolute_event(x, y, state)])
    for index in range(int(clicks)):
        _send_mouse_events([(0, 0, 0, down), (0, 0, 0, up)])
        if index + 1 < clicks:
            time.sleep(int(interval_ms) / 1000)


def _scroll_pointer(
    delta: int,
    x: Optional[int],
    y: Optional[int],
    state: _DesktopState,
) -> None:
    if x is not None and y is not None:
        _send_mouse_events([_absolute_event(x, y, state)])
    _send_mouse_events([(0, 0, int(delta) * 120, _MOUSE_WHEEL)])


def _drag_pointer(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str,
    duration_ms: int,
    state: _DesktopState,
) -> None:
    down, up = _button_events(button)
    _send_mouse_events([_absolute_event(start_x, start_y, state)])
    _send_mouse_events([(0, 0, 0, down)])
    pressed = True
    try:
        steps = max(2, min(180, round(int(duration_ms) / 16)))
        pause = int(duration_ms) / steps / 1000
        for step in range(1, steps + 1):
            ratio = step / steps
            x = round(start_x + (end_x - start_x) * ratio)
            y = round(start_y + (end_y - start_y) * ratio)
            _send_mouse_events([_absolute_event(x, y, state)])
            if step != steps:
                time.sleep(pause)
    finally:
        if pressed:
            _send_mouse_events([(0, 0, 0, up)])


def _send_keyboard_events(events: list[tuple[int, int, int]]) -> None:
    """Send ``(virtual_key, scan_code, flags)`` through Win32 SendInput."""
    if sys.platform != "win32":
        raise _PointerControlError(
            "unsupported_platform", "Windows SendInput is unavailable."
        )
    if not events:
        return

    import ctypes
    from ctypes import wintypes

    class _KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _InputUnion(ctypes.Union):
        # INPUT's native size is determined by its largest union member. Keep
        # all three members even though this helper emits keyboard input only.
        _fields_ = [
            ("mi", _MouseInput),
            ("ki", _KeyboardInput),
            ("hi", _HardwareInput),
        ]

    class _Input(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]

    inputs = (_Input * len(events))(
        *[
            _Input(
                type=1,  # INPUT_KEYBOARD
                union=_InputUnion(
                    ki=_KeyboardInput(
                        wVk=virtual_key,
                        wScan=scan_code,
                        dwFlags=flags,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            )
            for virtual_key, scan_code, flags in events
        ]
    )
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(_Input),
        ctypes.c_int,
    )
    user32.SendInput.restype = wintypes.UINT
    ctypes.set_last_error(0)
    sent = int(user32.SendInput(len(inputs), inputs, ctypes.sizeof(_Input)))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        raise OSError(
            error,
            f"Windows SendInput accepted {sent} of {len(inputs)} keyboard events.",
        )


def _unicode_events(text: str) -> list[tuple[int, int, int]]:
    """Encode literal text as KEYEVENTF_UNICODE UTF-16 code-unit events."""
    units = text.encode("utf-16-le")
    events: list[tuple[int, int, int]] = []
    for offset in range(0, len(units), 2):
        unit = int.from_bytes(units[offset : offset + 2], "little")
        events.append((0, unit, _KEY_UNICODE))
        events.append((0, unit, _KEY_UNICODE | _KEY_UP))
    return events


def _type_text(text: str, interval_ms: int) -> None:
    """Type validated literal Unicode text without using the clipboard."""
    interval_ms = int(interval_ms)
    if interval_ms <= 0:
        events = _unicode_events(text)
        # Keep each native call bounded while preserving UTF-16 event order.
        for offset in range(0, len(events), 512):
            _send_keyboard_events(events[offset : offset + 512])
        return

    pause = interval_ms / 1000
    for index, character in enumerate(text):
        _send_keyboard_events(_unicode_events(character))
        if index + 1 < len(text):
            time.sleep(pause)


def _normalize_key_name(raw: str) -> str:
    name = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
    return _KEY_ALIASES.get(name, name)


def _virtual_key(name: str) -> tuple[int, bool]:
    if len(name) == 1 and ("A" <= name <= "Z" or "0" <= name <= "9"):
        return ord(name), False
    if name.startswith("F") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return 0x6F + number, False
    try:
        return _NAMED_VIRTUAL_KEYS[name]
    except KeyError as exc:
        raise _PointerControlError(
            "invalid_key",
            f"Unsupported key '{name}'. Use a named key, A-Z, 0-9, or F1-F24.",
        ) from exc


def _normalize_key_chord(keys: list[str]) -> tuple[str, ...]:
    if isinstance(keys, (str, bytes)) or not isinstance(keys, (list, tuple)):
        raise _PointerControlError(
            "invalid_keys", "keys must be an array such as ['CTRL', 'L']."
        )
    if not 1 <= len(keys) <= 4:
        raise _PointerControlError(
            "invalid_keys", "keys must contain between 1 and 4 key names."
        )

    normalized = tuple(_normalize_key_name(key) for key in keys)
    if any(not key for key in normalized):
        raise _PointerControlError("invalid_key", "Key names cannot be empty.")
    if len(set(normalized)) != len(normalized):
        raise _PointerControlError(
            "duplicate_key", "A key chord cannot contain the same key twice."
        )

    for key in normalized:
        _virtual_key(key)
    modifiers = tuple(key for key in normalized if key in _MODIFIER_KEYS)
    ordinary = tuple(key for key in normalized if key not in _MODIFIER_KEYS)
    if len(ordinary) > 1:
        raise _PointerControlError(
            "invalid_chord",
            "A chord may contain modifiers plus one ordinary key only.",
        )
    return (*modifiers, *ordinary)


def _press_key_chord(keys: tuple[str, ...]) -> None:
    """Press a validated chord in order and release it in reverse order."""
    resolved = [(key, *_virtual_key(key)) for key in keys]
    down_events: list[tuple[int, int, int]] = []
    for _key, virtual_key, extended in resolved:
        down_events.append((virtual_key, 0, _KEY_EXTENDED if extended else 0))
    up_events: list[tuple[int, int, int]] = []
    for _key, virtual_key, extended in reversed(resolved):
        flags = _KEY_UP | (_KEY_EXTENDED if extended else 0)
        up_events.append((virtual_key, 0, flags))

    pressed = False
    try:
        _send_keyboard_events(down_events)
        pressed = True
    finally:
        # Release every member even if a down call was only partially accepted.
        # Sending an up for a key that was not pressed is harmless and avoids
        # leaving CTRL/ALT/WIN logically held after an input failure.
        if pressed or down_events:
            _send_keyboard_events(up_events)


def capture_desktop_stream_frame(
    *, max_width: int = 1920, max_height: int = 1080, quality: int = 62
) -> tuple[_DesktopCapture, bytes]:
    """Capture the host desktop and return a bandwidth-friendly JPEG frame.

    The full-resolution ``_DesktopCapture`` remains the coordinate authority;
    the JPEG may be scaled down for transport.  This lets the Control page map
    pointer input back to the server's real virtual desktop without making the
    public agent tools depend on a browser client's display size.
    """
    capture = _capture_desktop()
    from PIL import Image

    with Image.open(io.BytesIO(capture.png)) as source:
        frame = source.convert("RGB")
        try:
            frame.thumbnail(
                (max(1, int(max_width)), max(1, int(max_height))),
                Image.Resampling.LANCZOS,
            )
            with io.BytesIO() as output:
                frame.save(
                    output,
                    format="JPEG",
                    quality=max(30, min(90, int(quality))),
                    optimize=True,
                )
                jpeg = output.getvalue()
        finally:
            frame.close()
    return capture, jpeg


def dispatch_desktop_stream_input(capture: _DesktopCapture, message: dict) -> None:
    """Apply trusted, interactive Control-page input to the captured desktop.

    Unlike the agent tools, an interactive stream cannot require a new persisted
    screenshot between mouse-move/down/up events.  It is still constrained to
    the latest streamed coordinate space, serialized with agent input, and every
    accepted human action invalidates any agent screenshot observation.
    """
    global _HOST_ACTION_GENERATION
    if sys.platform != "win32":
        raise _PointerControlError(
            "unsupported_platform", "Interactive desktop input is available on Windows only."
        )

    state = _DesktopState(
        width=capture.width,
        height=capture.height,
        virtual_left=capture.virtual_left,
        virtual_top=capture.virtual_top,
        native_width=capture.native_width or capture.width,
        native_height=capture.native_height or capture.height,
        observed_at=time.monotonic(),
        generation=_HOST_ACTION_GENERATION,
    )
    kind = str(message.get("kind") or "").lower()
    x = int(round(float(message.get("x", 0))))
    y = int(round(float(message.get("y", 0))))
    pointer_kinds = {"mousemove", "mousedown", "mouseup", "click", "wheel"}
    if kind in pointer_kinds and not (0 <= x < state.width and 0 <= y < state.height):
        raise _PointerControlError(
            "coordinates_out_of_bounds",
            f"Coordinate ({x}, {y}) is outside the streamed desktop ({state.width}x{state.height}).",
        )

    with _INPUT_LOCK:
        if kind == "mousemove":
            _move_pointer(x, y, 0, state)
        elif kind in {"mousedown", "mouseup"}:
            button = str(message.get("button") or "left").lower()
            down, up = _button_events(button)
            _send_mouse_events([_absolute_event(x, y, state), (0, 0, 0, down if kind == "mousedown" else up)])
        elif kind == "click":
            _click_pointer(
                x,
                y,
                str(message.get("button") or "left").lower(),
                max(1, min(2, int(message.get("clickCount", 1)))),
                100,
                state,
            )
        elif kind == "wheel":
            raw_delta = float(message.get("deltaY", 0))
            if raw_delta:
                notches = max(1, min(5, round(abs(raw_delta) / 100)))
                _scroll_pointer(-notches if raw_delta > 0 else notches, x, y, state)
        elif kind == "text":
            text = str(message.get("text") or "")
            if text:
                _type_text(text[:4000], 0)
        elif kind == "key":
            raw_keys = message.get("keys")
            if not isinstance(raw_keys, list):
                raw_keys = [message.get("key")]
            keys = [str(key) for key in raw_keys if key]
            if keys:
                _press_key_chord(_normalize_key_chord(keys))
        else:
            raise _PointerControlError("invalid_input", f"Unsupported desktop input kind: {kind or 'empty'}")

        _HOST_ACTION_GENERATION += 1


def _perform_pointer_action(
    *,
    key: tuple[str, str, str],
    points: tuple[tuple[int, int], ...],
    operation: Callable[[_DesktopState], None],
) -> _DesktopState:
    """Serialize host input and invalidate every pre-action screenshot."""
    global _HOST_ACTION_GENERATION
    with _INPUT_LOCK:
        state = _validate_action_state(key, points)
        operation(state)
        with _STATE_LOCK:
            _HOST_ACTION_GENERATION += 1
            state.awaiting_verification = True
        return state


def _action_error(tool: str, exc: Exception) -> str:
    if isinstance(exc, _PointerControlError):
        code = exc.code
        message = str(exc)
    else:
        logger.exception("%s failed", tool)
        code = "input_failed"
        message = f"Windows could not perform the computer input action: {exc}"
    return json.dumps(
        {
            "status": "error",
            "tool": tool,
            "code": code,
            "platform": platform.system(),
            "message": message,
        }
    )


def _action_success(tool: str, **details) -> str:
    return json.dumps(
        {
            "status": "ok",
            "tool": tool,
            "platform": platform.system(),
            **details,
            "requires_screenshot": True,
            "message": (
                "Action sent. Call computer_screenshot now and verify the "
                "result before any further computer input action."
            ),
        }
    )


def build_tools(
    *,
    user_id: str = "",
    session_id: str = "",
    agent_id: str = "",
    agent_template_id: Optional[str] = None,
    enabled_providers=None,
    **_ctx,
):
    """Build screenshot plus Phase 3 Windows pointer and keyboard tools."""
    key = _state_key(user_id, session_id, agent_id)

    async def computer_screenshot(question: str = "", analyze: bool = True) -> str:
        """Capture the full desktop and optionally inspect it with vision."""
        try:
            capture = await asyncio.to_thread(_capture_desktop)
        except Exception as exc:
            logger.exception("Desktop screenshot failed")
            return json.dumps(
                {
                    "status": "error",
                    "code": "capture_failed",
                    "platform": platform.system(),
                    "message": (
                        "Could not capture the interactive desktop. The session may "
                        "be locked/headless, screen-capture permission may be denied, "
                        f"or the platform backend may be unavailable: {exc}"
                    ),
                }
            )

        if len(capture.png) < 100:
            return json.dumps(
                {
                    "status": "error",
                    "code": "empty_capture",
                    "platform": platform.system(),
                    "message": "Desktop capture returned an empty or corrupt PNG.",
                }
            )

        try:
            attachment = await _store_screenshot(
                png=capture.png, user_id=user_id, session_id=session_id
            )
        except Exception as exc:
            logger.exception("Could not store desktop screenshot")
            return json.dumps(
                {
                    "status": "error",
                    "code": "attachment_failed",
                    "platform": platform.system(),
                    "message": f"Desktop was captured but could not be saved: {exc}",
                }
            )

        state = _record_observation(key, capture)
        result = {
            "status": "ok",
            "phase": "keyboard_control",
            "platform": platform.system(),
            "capture": "virtual_desktop",
            "dimensions": {
                "width": capture.width,
                "height": capture.height,
            },
            "coordinate_space": {
                "units": "screenshot_pixels",
                "left": 0,
                "top": 0,
                "right": max(0, capture.width - 1),
                "bottom": max(0, capture.height - 1),
                "native_virtual_left": capture.virtual_left,
                "native_virtual_top": capture.virtual_top,
                "native_virtual_width": capture.native_width or capture.width,
                "native_virtual_height": capture.native_height or capture.height,
            },
            "observation": {
                "generation": state.generation,
                "valid_for_seconds": int(_MAX_OBSERVATION_AGE_SECONDS),
                "ready_for_pointer_action": sys.platform == "win32",
                "ready_for_keyboard_action": sys.platform == "win32",
            },
            "attachment": {
                "id": attachment["id"],
                "filename": attachment["filename"],
                "mime_type": attachment["mime_type"],
                "size_bytes": attachment["size_bytes"],
            },
        }

        if analyze:
            try:
                result.update(
                    await _describe_screenshot(
                        attachment=attachment,
                        question=question,
                        dimensions=f"{capture.width}x{capture.height}",
                        user_id=user_id,
                    )
                )
            except Exception as exc:
                logger.exception("Desktop screenshot vision analysis failed")
                result["warning"] = (
                    "Screenshot captured, but visual analysis failed: "
                    f"{exc}"
                )
        else:
            result["analysis"] = "skipped"

        return json.dumps(result)

    async def computer_move(x: int, y: int, duration_ms: int = 0) -> str:
        """Move the Windows pointer to fresh-screenshot pixel coordinates."""
        if not (0 <= int(duration_ms) <= 10000):
            return _action_error(
                "computer_move",
                _PointerControlError(
                    "invalid_duration", "duration_ms must be between 0 and 10000."
                ),
            )
        try:
            await asyncio.to_thread(
                _perform_pointer_action,
                key=key,
                points=((int(x), int(y)),),
                operation=lambda state: _move_pointer(
                    int(x), int(y), int(duration_ms), state
                ),
            )
            return _action_success(
                "computer_move", x=int(x), y=int(y), duration_ms=int(duration_ms)
            )
        except Exception as exc:
            return _action_error("computer_move", exc)

    async def computer_click(
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
        interval_ms: int = 100,
    ) -> str:
        """Click once or twice at fresh-screenshot pixel coordinates."""
        button = str(button).strip().lower()
        if int(clicks) not in (1, 2):
            return _action_error(
                "computer_click",
                _PointerControlError("invalid_clicks", "clicks must be 1 or 2."),
            )
        if not (40 <= int(interval_ms) <= 1000):
            return _action_error(
                "computer_click",
                _PointerControlError(
                    "invalid_interval", "interval_ms must be between 40 and 1000."
                ),
            )
        try:
            await asyncio.to_thread(
                _perform_pointer_action,
                key=key,
                points=((int(x), int(y)),),
                operation=lambda state: _click_pointer(
                    int(x),
                    int(y),
                    button,
                    int(clicks),
                    int(interval_ms),
                    state,
                ),
            )
            return _action_success(
                "computer_click",
                x=int(x),
                y=int(y),
                button=button,
                clicks=int(clicks),
            )
        except Exception as exc:
            return _action_error("computer_click", exc)

    async def computer_scroll(
        delta: int,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> str:
        """Scroll wheel notches; positive is up and negative is down."""
        if int(delta) == 0 or not (-20 <= int(delta) <= 20):
            return _action_error(
                "computer_scroll",
                _PointerControlError(
                    "invalid_delta",
                    "delta must be a non-zero integer from -20 to 20; positive "
                    "scrolls up and negative scrolls down.",
                ),
            )
        if (x is None) != (y is None):
            return _action_error(
                "computer_scroll",
                _PointerControlError(
                    "incomplete_coordinates",
                    "Provide both x and y to position the pointer before scrolling, "
                    "or omit both to scroll at its current position.",
                ),
            )
        points = () if x is None else ((int(x), int(y)),)
        try:
            await asyncio.to_thread(
                _perform_pointer_action,
                key=key,
                points=points,
                operation=lambda state: _scroll_pointer(
                    int(delta),
                    None if x is None else int(x),
                    None if y is None else int(y),
                    state,
                ),
            )
            return _action_success(
                "computer_scroll",
                delta=int(delta),
                x=None if x is None else int(x),
                y=None if y is None else int(y),
            )
        except Exception as exc:
            return _action_error("computer_scroll", exc)

    async def computer_drag(
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 600,
        button: str = "left",
    ) -> str:
        """Hold a pointer button while moving between screenshot coordinates."""
        button = str(button).strip().lower()
        if not (100 <= int(duration_ms) <= 10000):
            return _action_error(
                "computer_drag",
                _PointerControlError(
                    "invalid_duration",
                    "duration_ms must be between 100 and 10000.",
                ),
            )
        try:
            await asyncio.to_thread(
                _perform_pointer_action,
                key=key,
                points=(
                    (int(start_x), int(start_y)),
                    (int(end_x), int(end_y)),
                ),
                operation=lambda state: _drag_pointer(
                    int(start_x),
                    int(start_y),
                    int(end_x),
                    int(end_y),
                    button,
                    int(duration_ms),
                    state,
                ),
            )
            return _action_success(
                "computer_drag",
                start={"x": int(start_x), "y": int(start_y)},
                end={"x": int(end_x), "y": int(end_y)},
                button=button,
                duration_ms=int(duration_ms),
            )
        except Exception as exc:
            return _action_error("computer_drag", exc)

    async def computer_type(text: str, interval_ms: int = 0) -> str:
        """Type literal Unicode text into the verified focused control."""
        if not isinstance(text, str) or not text:
            return _action_error(
                "computer_type",
                _PointerControlError(
                    "invalid_text", "text must be a non-empty string."
                ),
            )
        if len(text) > 4000:
            return _action_error(
                "computer_type",
                _PointerControlError(
                    "text_too_long", "text cannot exceed 4000 Unicode characters."
                ),
            )
        if any(
            ord(character) < 32
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in text
        ):
            return _action_error(
                "computer_type",
                _PointerControlError(
                    "invalid_text",
                    "text cannot contain control characters or unpaired Unicode "
                    "surrogates. Use computer_key for ENTER, TAB, or other keys.",
                ),
            )
        try:
            interval = int(interval_ms)
        except (TypeError, ValueError):
            return _action_error(
                "computer_type",
                _PointerControlError(
                    "invalid_interval", "interval_ms must be an integer."
                ),
            )
        if not 0 <= interval <= 1000:
            return _action_error(
                "computer_type",
                _PointerControlError(
                    "invalid_interval", "interval_ms must be between 0 and 1000."
                ),
            )

        try:
            await asyncio.to_thread(
                _perform_pointer_action,
                key=key,
                points=(),
                operation=lambda _state: _type_text(text, interval),
            )
            return _action_success(
                "computer_type",
                characters=len(text),
                utf16_code_units=len(text.encode("utf-16-le")) // 2,
                interval_ms=interval,
            )
        except Exception as exc:
            return _action_error("computer_type", exc)

    async def computer_key(keys: list[str]) -> str:
        """Send one key or modifier chord to the verified focused control."""
        try:
            normalized = _normalize_key_chord(keys)
        except Exception as exc:
            return _action_error("computer_key", exc)

        try:
            await asyncio.to_thread(
                _perform_pointer_action,
                key=key,
                points=(),
                operation=lambda _state: _press_key_chord(normalized),
            )
            return _action_success("computer_key", keys=list(normalized))
        except Exception as exc:
            return _action_error("computer_key", exc)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(
        {
            "computer_screenshot": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "Optional focused question about what is visible. "
                            "Leave empty for a complete desktop description."
                        ),
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": (
                            "Ask the configured vision model to inspect the "
                            "screenshot. The PNG is saved either way."
                        ),
                        "default": True,
                    },
                },
                "required": [],
            },
            "computer_move": {
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Horizontal pixel in the latest screenshot.",
                    },
                    "y": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Vertical pixel in the latest screenshot.",
                    },
                    "duration_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10000,
                        "default": 0,
                        "description": "Smooth movement duration; 0 moves immediately.",
                    },
                },
                "required": ["x", "y"],
            },
            "computer_click": {
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Horizontal pixel in the latest screenshot.",
                    },
                    "y": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Vertical pixel in the latest screenshot.",
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "default": "left",
                    },
                    "clicks": {
                        "type": "integer",
                        "enum": [1, 2],
                        "default": 1,
                        "description": "Single-click or OS-recognized double-click.",
                    },
                    "interval_ms": {
                        "type": "integer",
                        "minimum": 40,
                        "maximum": 1000,
                        "default": 100,
                        "description": "Delay between clicks when clicks=2.",
                    },
                },
                "required": ["x", "y"],
            },
            "computer_scroll": {
                "type": "object",
                "properties": {
                    "delta": {
                        "type": "integer",
                        "minimum": -20,
                        "maximum": 20,
                        "description": (
                            "Non-zero wheel notches. Positive scrolls up; "
                            "negative scrolls down."
                        ),
                    },
                    "x": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Optional horizontal screenshot pixel. Supply with y."
                        ),
                    },
                    "y": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Optional vertical screenshot pixel. Supply with x."
                        ),
                    },
                },
                "required": ["delta"],
            },
            "computer_drag": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer", "minimum": 0},
                    "start_y": {"type": "integer", "minimum": 0},
                    "end_x": {"type": "integer", "minimum": 0},
                    "end_y": {"type": "integer", "minimum": 0},
                    "duration_ms": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 10000,
                        "default": 600,
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "default": "left",
                    },
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
            "computer_type": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4000,
                        "description": (
                            "Literal Unicode text for the currently focused "
                            "control. Control characters are rejected; use "
                            "computer_key for ENTER, TAB, and shortcuts."
                        ),
                    },
                    "interval_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1000,
                        "default": 0,
                        "description": "Delay between Unicode characters.",
                    },
                },
                "required": ["text"],
            },
            "computer_key": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 4,
                        "uniqueItems": True,
                        "description": (
                            "One key or one chord, for example ['ENTER'] or "
                            "['CTRL', 'L']. A chord may contain modifiers plus "
                            "one ordinary key."
                        ),
                    }
                },
                "required": ["keys"],
            },
        }
    )
    return {
        "computer_screenshot": computer_screenshot,
        "computer_move": computer_move,
        "computer_click": computer_click,
        "computer_scroll": computer_scroll,
        "computer_drag": computer_drag,
        "computer_type": computer_type,
        "computer_key": computer_key,
    }
