"""Optional observation bus — RECORDER-HOOK shared removable integration.

Producers in chat/diagnostics publish facts, never commands. The recorder service
registers a cheap activation predicate in each application worker. With no subscriber there is no allocation
of lazy payloads, IO, or plugin import. Remove producer RECORDER-HOOK blocks, then
this module and its lifecycle subscriptions; do not remove surrounding behavior.
"""
from __future__ import annotations
from typing import Callable, Any
import threading

_lock = threading.RLock()
_subscribers: dict[str, tuple[Callable, Callable | None]] = {}

def subscribe(name: str, callback: Callable, enabled: Callable | None = None) -> Callable:
    with _lock:
        _subscribers[name] = (callback, enabled)
    def unsubscribe():
        with _lock:
            if _subscribers.get(name, (None,))[0] is callback:
                _subscribers.pop(name, None)
    return unsubscribe

def observed() -> bool:
    return bool(_subscribers)

def emit(topic: str, payload: Any) -> None:
    if not _subscribers:
        return
    try:
        with _lock:
            candidates = tuple(_subscribers.values())
        callbacks = []
        for callback, enabled in candidates:
            try:
                if enabled is None or enabled(): callbacks.append(callback)
            except Exception:
                pass
        if not callbacks: return
        value = payload() if callable(payload) else payload
        for callback in callbacks:
            try:
                callback(topic, value)
            except Exception:
                pass  # Diagnostic failures must never affect the observed run.
    except Exception:
        pass
