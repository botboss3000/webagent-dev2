"""Optional billing-extension seam.

The billing engine in this package handles the whole story on its own: an agent
admin sets a price, the end user pays it, the agent admin retains all of it.

A deployment may install an **optional billing extension** that participates in a
few well-defined moments — adjusting the effective config, allocating part of a
charge elsewhere, recording extra accounting, validating an agent's config, or
supplying subscription parameters. With **no extension registered** (the default)
every hook is a no-op: nothing is deducted, no extra config is applied, and the
agent admin retains the full charge.

Why a registry + folder-scan rather than a direct import: the engine must never
name an extension module, so that a deployment which ships none has no dangling
reference to one. At startup ``load_billing_extensions()`` imports any
``plugins/<group>/billing/`` package it finds — a generic convention — and each
such package registers itself on import. This package is ``plugins/billing/`` (one
level up), so the ``*/billing/`` glob never matches it; a deployment with no such
package simply finds nothing and the seam stays dormant.

An extension is any object exposing the methods in ``BillingExtension`` below
(duck-typed — importing that class is not required). All methods are optional;
a missing one falls back to the no-extension default.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class BillingExtension(Protocol):
    """The hooks an optional billing extension may implement. All optional."""

    async def augment_config(self, db: Any, agent_id: str, agent_cfg: dict) -> dict:
        """Return the agent config adjusted with any extension-supplied
        defaults/limits. Default: the agent config is returned unchanged."""

    async def split_charge(
        self, db: Any, agent: dict, end_user_charge_cents: int
    ) -> Tuple[int, int]:
        """Given what the end user pays, return (deducted_cents, retained_cents)
        where deducted + retained == the charge. Default: (0, full charge) — the
        agent admin retains all of it."""

    async def record_charge(
        self,
        db: Any,
        *,
        event_id: str,
        agent_id: str,
        user_id: str,
        end_user_charge_cents: int,
        deducted_cents: int,
        retained_cents: int,
        interaction_id: Optional[str] = None,
    ) -> None:
        """Persist any extension-side accounting for one charge. Default: no-op."""

    async def validate_agent_config(self, db: Any, agent_id: str, fields: dict) -> None:
        """Raise if an agent-admin config save violates an extension-imposed
        limit (e.g. an allowed-strategy/processor list). Default: anything goes."""

    async def subscription_params(
        self, db: Any, agent_id: str, processor: str
    ) -> Tuple[Optional[str], float]:
        """Return (payee_account_id, fee_pct) for a subscription. Default:
        (None, 0.0) — the subscription bills the operator's own account."""


# ── Registry ────────────────────────────────────────────────────────────────

_EXTENSION: Optional[Any] = None


def register_billing_extension(ext: Any) -> None:
    """Called by an extension package on import. Last registration wins."""
    global _EXTENSION
    _EXTENSION = ext
    logger.info("billing extension registered: %s", type(ext).__name__)


def get_billing_extension() -> Optional[Any]:
    return _EXTENSION


def has_billing_extension() -> bool:
    return _EXTENSION is not None


def load_billing_extensions() -> list:
    """Import any ``plugins/<group>/billing/`` package (generic convention) so it
    can register itself. Returns the imported modules so the caller can mount any
    ``router`` they expose. Safe to call repeatedly. Never raises."""
    mods: list = []
    try:
        plugins_dir = Path(__file__).resolve().parents[1]  # the plugins/ root
        for init in sorted(plugins_dir.glob("*/billing/__init__.py")):
            group = init.parent.parent.name  # plugins/<group>/billing -> <group>
            mod_name = f"plugins.{group}.billing"
            try:
                mods.append(importlib.import_module(mod_name))
            except Exception as e:  # a broken extension must never break boot
                logger.info("billing extension %s not loaded: %s", mod_name, e)
    except Exception as e:
        logger.info("billing extension scan skipped: %s", e)
    return mods


# ── Convenience wrappers (apply the extension, or the no-extension default) ──


async def apply_split(db: Any, agent: dict, end_user_charge_cents: int) -> Tuple[int, int]:
    """(deducted_cents, retained_cents) for a charge — (0, full) with no extension."""
    ext = _EXTENSION
    if ext is not None and hasattr(ext, "split_charge"):
        try:
            return await ext.split_charge(db, agent, end_user_charge_cents)
        except Exception as e:
            logger.debug("billing extension split_charge failed: %s", e)
    return 0, max(0, int(end_user_charge_cents or 0))


async def apply_record(db: Any, **kwargs) -> None:
    ext = _EXTENSION
    if ext is not None and hasattr(ext, "record_charge"):
        try:
            await ext.record_charge(db, **kwargs)
        except Exception as e:
            logger.debug("billing extension record_charge failed: %s", e)


async def apply_config_augment(db: Any, agent_id: str, agent_cfg: dict) -> dict:
    ext = _EXTENSION
    if ext is not None and hasattr(ext, "augment_config"):
        try:
            return await ext.augment_config(db, agent_id, agent_cfg)
        except Exception as e:
            logger.debug("billing extension augment_config failed: %s", e)
    return agent_cfg


async def apply_agent_config_validation(db: Any, agent_id: str, fields: dict) -> None:
    ext = _EXTENSION
    if ext is not None and hasattr(ext, "validate_agent_config"):
        await ext.validate_agent_config(db, agent_id, fields)


async def apply_subscription_params(
    db: Any, agent_id: str, processor: str
) -> Tuple[Optional[str], float]:
    """(payee_account_id, fee_pct) — (None, 0.0) with no extension, so
    subscriptions bill directly to the operator's own processor account."""
    ext = _EXTENSION
    if ext is not None and hasattr(ext, "subscription_params"):
        try:
            return await ext.subscription_params(db, agent_id, processor)
        except Exception as e:
            logger.debug("billing extension subscription_params failed: %s", e)
    return None, 0.0
