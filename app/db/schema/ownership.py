"""Declarative storage ownership for every core WebAgent table.

The storage *plane* answers who owns a row and which lifecycle governs it.  It
is deliberately independent from the physical backend: SQLite uses separate
files, while Postgres deployments may use separate databases or schemas.

This module is the single source of truth for schema generation, migration,
export/erase audits, and routing checks.  A core table must appear exactly once
in ``TABLE_POLICIES``; tests fail when a new table is added without an explicit
ownership decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class StoragePlane(StrEnum):
    APP = "app"
    USER = "user"
    AGENT = "agent"
    SECRETS = "secrets"
    TELEMETRY = "telemetry"


class AuthorityKind(StrEnum):
    PRIMARY = "primary"
    PROJECTION = "projection"
    CACHE = "cache"


@dataclass(frozen=True)
class TablePolicy:
    plane: StoragePlane
    owner_key: str | None = None
    authority: AuthorityKind = AuthorityKind.PRIMARY
    sensitive: bool = False
    retention: str = "durable"
    note: str = ""


def _policies(
    plane: StoragePlane,
    names: Iterable[str],
    *,
    owner_key: str | None = None,
    authority: AuthorityKind = AuthorityKind.PRIMARY,
    sensitive: bool = False,
    retention: str = "durable",
    note: str = "",
) -> dict[str, TablePolicy]:
    return {
        name: TablePolicy(
            plane=plane,
            owner_key=owner_key,
            authority=authority,
            sensitive=sensitive,
            retention=retention,
            note=note,
        )
        for name in names
    }


TABLE_POLICIES: dict[str, TablePolicy] = {}

# Installation/control plane.  These records must be available before a user
# or agent database can be selected and include the financially authoritative
# ledger and cross-device coordination state.
TABLE_POLICIES.update(_policies(StoragePlane.APP, (
    "agent_templates",
    "agent_prompt_templates",
    "app_meta",
    "tools",
    "channel_identities",
    "anonymous_guest_credentials",
    "linking_codes",
    "user_profiles",
    "user_agent_sidebar_preferences",
    "user_agent_page_preferences",
    "user_my_agents_page_preferences",
    "user_accounts",
    "experience_tiers",
    "experience_tier_revisions",
    "model_rosters",
    "model_roster_revisions",
    "user_tier_assignments",
    "entitlement_audit_events",
    "tenant_key_meta",
    "billing_configs",
    "usage_events",
    "wallets",
    "wallet_transactions",
    "subscriptions",
    "trials",
    "payments",
    "billing_exemptions",
    "background_leader",
    "device_presence",
    "device_jobs",
    "instances",
    "agent_catalog",
    "storage_layout",
    "storage_migrations",
)))

# User-private interaction/content plane.  Cross-plane identifiers such as
# agent_id are intentionally plain IDs; SQLite cannot enforce cross-file FKs.
TABLE_POLICIES.update(_policies(StoragePlane.USER, (
    "sessions",
    "browser_sessions",
    "browser_sync_receipts",
    "interactions",
    "session_activity_summaries",
    "session_manifests",
    "session_runs",
    "session_summaries",
    "session_summary_segments",
    "soft_ability_runs",
    "memories",
    "memory_chunks",
    "skills",
    "skill_executions",
    "skill_feedback",
    "session_interrupts",
    "session_notifications",
    "run_contract_checks",
    "run_contract_state",
    "attachments",
    "workspace_artifacts",
    "webhook_registrations",
    "webhook_event_log",
    "data_sources",
    "doc_chunks",
    "agent_automations",
    "agent_event_subscriptions",
    "automation_runs",
    "event_deliveries",
    "genui",
), owner_key="user_id"))

# One authority database per agent.  ``agent_catalog`` above is the minimal app
# projection used for discovery and authorization; it is not a second copy of
# the full configuration.
TABLE_POLICIES.update(_policies(StoragePlane.AGENT, (
    "agents",
    "agent_prompts",
    "agent_connections",
    "agent_abilities",
    "agent_soft_abilities",
    "agent_data_sources",
), owner_key="agent_id"))

# Secret values never belong in an app/user/agent data file.  Domain rows keep
# opaque references to these records.
TABLE_POLICIES.update(_policies(
    StoragePlane.SECRETS,
    ("auth_elements", "agent_credentials"),
    owner_key="user_id",
    sensitive=True,
))

# High-volume, per-machine operational data with independent retention.
TABLE_POLICIES.update(_policies(
    StoragePlane.TELEMETRY,
    ("diagnostics", "render_recordings"),
    retention="rolling",
))


def policy_for(table_name: str) -> TablePolicy:
    try:
        return TABLE_POLICIES[table_name]
    except KeyError as exc:
        raise KeyError(f"Core table {table_name!r} has no storage ownership policy") from exc


def tables_for_plane(plane: StoragePlane | str) -> tuple[str, ...]:
    selected = StoragePlane(plane)
    return tuple(name for name, policy in TABLE_POLICIES.items() if policy.plane == selected)


def validate_table_policies(table_names: Iterable[str]) -> None:
    canonical = set(table_names)
    declared = set(TABLE_POLICIES)
    missing = canonical - declared
    unknown = declared - canonical
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing policies: {sorted(missing)}")
        if unknown:
            details.append(f"policies for unknown tables: {sorted(unknown)}")
        raise ValueError("Invalid storage ownership registry (" + "; ".join(details) + ")")
