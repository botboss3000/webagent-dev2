"""Safe, create-on-demand JSON config I/O.

The app ships with **no ``data/`` folder** — config files materialize only when a
setting actually changes. Every JSON config writer routes through here so that:

  * **the parent directory is created on demand** — a save into a missing
    ``data/config/`` no longer raises ``FileNotFoundError`` (the old failure mode
    when ``data/`` was absent);
  * **writes are atomic** — content is written to a sibling temp file then
    ``os.replace``-d into place, so a crash mid-write never leaves a truncated or
    half-written config (``os.replace`` is atomic on the same volume, Windows
    included);
  * **only the changed key is touched** — :func:`set_config_key` read-modify-writes
    a single key, leaving every sibling key intact and creating the file if absent.

Byte-compatibility note: like the writers it replaces, :func:`safe_write_json`
serializes with ``json.dumps(..., indent=2)`` and the default ``ensure_ascii=True``
(non-ASCII escaped to ``\\uXXXX``), so existing committed config files are written
back identically.

Error contract: :func:`read_json` **never raises** (returns ``default`` on a
missing/corrupt file). :func:`safe_write_json` and :func:`set_config_key`
**raise** on write failure (after cleaning up the temp file) so each caller keeps
its own degrade-or-propagate behaviour — most callers wrap these in a
``try/except`` that logs and continues; a couple (e.g. the DB connection config)
deliberately re-raise.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any, Optional, Sequence, Union

__all__ = ["read_json", "safe_write_json", "set_config_key"]

# A key path for set_config_key: a single literal top-level key (str — NOT split
# on dots, so arbitrary ids/emails are safe), or a sequence of keys for a nested
# path (intermediate dicts are created as needed).
KeyPath = Union[str, Sequence[str]]


def read_json(path: Union[str, os.PathLike], default: Optional[Any] = None) -> Any:
    """Return the parsed JSON at ``path``.

    On a missing file, unreadable file, or invalid JSON, return a deep copy of
    ``default`` (so callers can safely pass and then mutate a literal like ``{}``).
    Never raises.
    """
    try:
        with open(os.fspath(path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return copy.deepcopy(default)
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError (corrupt/partial file).
        return copy.deepcopy(default)


def safe_write_json(path: Union[str, os.PathLike], data: Any, *, indent: int = 2) -> None:
    """Atomically write ``data`` as JSON to ``path``, creating parent dirs as needed.

    Writes to a temp file in the same directory, flushes + fsyncs it, then
    ``os.replace``-s it over the target — so a reader never sees a partial file and
    a crash mid-write can't corrupt an existing config. Raises on failure (the temp
    file is removed first).
    """
    target = os.fspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    text = json.dumps(data, indent=indent)

    # Temp file must share the target's directory so os.replace is a same-volume
    # (atomic) rename, not a cross-device copy.
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(target) + ".",
        suffix=".tmp",
        dir=parent or ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Best-effort cleanup of the orphaned temp file, then re-raise.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def set_config_key(
    path: Union[str, os.PathLike], key: KeyPath, value: Any, *, indent: int = 2
) -> dict:
    """Set **only** ``key`` to ``value`` in the JSON object at ``path``, preserving
    every other key, and write the result back atomically (creating the file and
    its parent directory if absent).

    ``key`` is either a single literal top-level key (a ``str``, never split on
    dots — safe for ids/emails containing dots) or a sequence of keys describing a
    nested path (missing intermediate dicts are created). Returns the merged dict.

    Raises on write failure (see :func:`safe_write_json`).
    """
    data = read_json(path, {})
    if not isinstance(data, dict):
        data = {}

    keys = [key] if isinstance(key, str) else list(key)
    if not keys:
        raise ValueError("set_config_key requires at least one key")

    node = data
    for k in keys[:-1]:
        child = node.get(k)
        if not isinstance(child, dict):
            child = {}
            node[k] = child
        node = child
    node[keys[-1]] = value

    safe_write_json(path, data, indent=indent)
    return data
