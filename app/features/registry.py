"""Folder-scan discovery for the select-by-id subsystems.

The Tier-2 subsystems (secrets vaults, encryption methods, payment processors,
scheduler providers, data connectors) historically each kept a hand-maintained
``if/elif`` or registry dict mapping an id string to a backend class. This helper
lets them **auto-discover** any provider file dropped into their folder instead:
it imports each module, finds the concrete subclass of the subsystem's base
class, and keys it by id.

The id is taken from (in order):
  1. the module's ``FEATURE['id']`` header, if present;
  2. a class/module attribute (e.g. ``cls.name`` for secrets backends);
  3. the file stem.

This is **additive**: callers keep their hard-wired fallback so the irreducible
core (SQLite / inline-DB secrets / none-encryption) always works even if its
file is somehow unreadable. Fully guarded — a broken module is skipped, never
fatal.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Dict, Optional, Type

logger = logging.getLogger(__name__)


def _module_stems(folder: Path, skip) -> list:
    if not folder.is_dir():
        return []
    out = []
    for fpath in sorted(folder.iterdir()):
        if fpath.suffix != ".py" or fpath.name.startswith("_"):
            continue
        if fpath.stem in skip:
            continue
        out.append(fpath.stem)
    return out


def _concrete_subclass(mod, base_cls) -> Optional[type]:
    """Find the concrete `base_cls` subclass *defined in* this module."""
    for obj in vars(mod).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, base_cls)
            and obj is not base_cls
            and getattr(obj, "__module__", None) == mod.__name__
            and not getattr(obj, "__abstractmethods__", None)
        ):
            return obj
    return None


def discover_backends(
    package_name: str,
    base_cls: Type,
    *,
    id_attr: str = "name",
    skip=(),
) -> Dict[str, type]:
    """Return ``{id: cls}`` for every provider module under ``package_name``.

    ``package_name`` is an importable package (e.g. ``"app.secrets"``).
    ``base_cls`` is the subsystem's interface; we register the concrete subclass
    each module defines. ``id_attr`` is the class attribute used as the id when
    no ``FEATURE['id']`` is present.
    """
    try:
        pkg = importlib.import_module(package_name)
    except Exception as e:
        logger.warning("Provider discovery: cannot import package %s: %s", package_name, e)
        return {}
    folder = Path(pkg.__file__).resolve().parent
    skip = set(skip)

    out: Dict[str, type] = {}
    for stem in _module_stems(folder, skip):
        mod_name = f"{package_name}.{stem}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            logger.debug("Provider discovery: %s failed to import: %s", mod_name, e)
            continue
        cls = _concrete_subclass(mod, base_cls)
        if cls is None:
            continue
        feature = getattr(mod, "FEATURE", None)
        fid = None
        if isinstance(feature, dict) and feature.get("id"):
            fid = str(feature["id"])
        if not fid:
            fid = getattr(cls, id_attr, None) or stem
        out[str(fid)] = cls
    return out
