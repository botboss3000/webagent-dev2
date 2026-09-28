"""
DB connection configuration: provider, host, port, db name, secret reference.

Persisted to `db_connection.json` (non-secret fields only). The password is
fetched on demand from the active SecretsBackend using `password_secret_key`.

Providers (case matches UI dropdown):
  - sqlite             → uses plane-routed LocalBackend authority files
  - postgres           → raw Postgres via asyncpg (PostgresBackend)
  - mysql              → MySQL (future PostgresBackend extension)
  - aws_rds            → managed Postgres on Amazon RDS / Aurora (postgres dialect)
  - gcp_cloud_sql      → managed Postgres in GCP (uses postgres dialect)
  - azure_postgres     → managed Postgres on Azure (Flexible Server, postgres dialect)
  - neon               → Neon serverless Postgres

All Postgres-family providers (postgres, aws_rds, gcp_cloud_sql, azure_postgres,
neon) share the same raw asyncpg backend and differ only in their UI label,
default-field hints, and connection notes. Use ``is_postgres_provider(name)`` as
the single source of truth — any provider whose ``PROVIDER_META`` entry has
``dialect: "postgres"`` is a Postgres endpoint with zero additional wiring.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from app.util.config_io import safe_write_json

logger = logging.getLogger(__name__)

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_AGENT_DIR, "db_connection.json")

PROVIDERS = (
    "sqlite",
    "postgres",
    "mysql",
    "aws_rds",
    "gcp_cloud_sql",
    "azure_postgres",
    "neon",
)

# Map provider → SQLAlchemy-style URL scheme + canonical dialect
PROVIDER_META = {
    "sqlite":         {"dialect": "sqlite",   "needs_host": False, "default_port": None},
    "postgres":       {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "mysql":          {"dialect": "mysql",    "needs_host": True,  "default_port": 3306},
    "aws_rds":        {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "gcp_cloud_sql":  {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "azure_postgres": {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "neon":           {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
}


def is_postgres_provider(provider: str) -> bool:
    """True when *provider* uses the raw Postgres backend (asyncpg / psycopg).

    This is the single source of truth for "is this a Postgres endpoint?" —
    every call site that currently hard-codes a provider tuple (``_PG_PROVIDERS``
    in ``app/db/__init__.py``, ``app/util/reset_boot.py``, ``app/api/tenant_db.py``)
    should call this instead. Adding a new Postgres-family provider requires only
    one line in ``PROVIDER_META`` above; no other wiring changes."""
    return PROVIDER_META.get(provider, {}).get("dialect") == "postgres"


def pprint_provider(provider: str) -> str:
    """Human-readable label for a provider id (e.g. ``aws_rds`` → ``AWS RDS``)."""
    labels = {
        "sqlite": "SQLite", "postgres": "PostgreSQL", "mysql": "MySQL",
        "aws_rds": "AWS RDS", "gcp_cloud_sql": "Google Cloud SQL",
        "azure_postgres": "Azure PostgreSQL", "neon": "Neon",
    }
    return labels.get(provider, provider)


@dataclass
class DBConnectionConfig:
    provider: str = "sqlite"
    host: Optional[str] = None
    port: Optional[int] = None
    database: str = ""
    username: Optional[str] = None
    password_secret_key: Optional[str] = None  # key in SecretsBackend (e.g. "db_password_postgres")
    ssl_mode: str = "require"      # postgres only: disable | require | verify-ca | verify-full
    schema: str = "public"         # postgres schema name
    # Free-form per-provider tweaks (sslrootcert path, GCP instance connection name, etc.)
    options: dict = field(default_factory=dict)

    @property
    def dialect(self) -> str:
        return PROVIDER_META.get(self.provider, {}).get("dialect", "sqlite")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DBConnectionConfig":
        # Tolerate unknown keys (future fields)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def build_url(self, password: Optional[str] = None) -> str:
        """
        Build a SQLAlchemy-compatible async URL.

        password is supplied by caller (resolved from SecretsBackend) so that
        this method has no IO side effects.
        """
        prov = self.provider
        if prov == "sqlite":
            # Use the app-plane database when no explicit SQLite path is set.
            from app.db.local import DEFAULT_DB_PATH
            db_path = self.database or DEFAULT_DB_PATH
            return f"sqlite+aiosqlite:///{db_path}"

        meta = PROVIDER_META.get(prov)
        if not meta:
            raise ValueError(f"Unknown provider: {prov}")

        port = self.port or meta["default_port"]
        host = self.host or ""
        db = self.database or "postgres"
        user = self.username or ""
        pw = password or ""

        if meta["dialect"] == "postgres":
            # asyncpg
            cred = f"{user}:{pw}@" if user else ""
            return f"postgresql+asyncpg://{cred}{host}:{port}/{db}"
        if meta["dialect"] == "mysql":
            cred = f"{user}:{pw}@" if user else ""
            return f"mysql+aiomysql://{cred}{host}:{port}/{db}"
        raise ValueError(f"Unsupported dialect for provider {prov}")

    def validate(self) -> list:
        """Return list of human-readable validation errors (empty if OK)."""
        errs = []
        meta = PROVIDER_META.get(self.provider)
        if not meta:
            errs.append(f"Unknown provider: {self.provider}")
            return errs
        if meta["needs_host"]:
            if not self.host:
                errs.append("host is required")
            if not self.database:
                errs.append("database name is required")
            if not self.username:
                errs.append("username is required")
        return errs


# ── Persistence ──────────────────────────────────────────────────────────────

_cached: Optional[DBConnectionConfig] = None


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


def load_config() -> DBConnectionConfig:
    """Load active DB connection config from disk (or env in env-locked mode)."""
    global _cached
    if _cached is not None:
        return _cached

    if _env_locked():
        # Build from env vars: WEBAGENT_DB_PROVIDER, WEBAGENT_DB_HOST, etc.
        cfg = DBConnectionConfig(
            provider=os.environ.get("WEBAGENT_DB_PROVIDER", "sqlite"),
            host=os.environ.get("WEBAGENT_DB_HOST") or None,
            port=int(os.environ["WEBAGENT_DB_PORT"]) if os.environ.get("WEBAGENT_DB_PORT") else None,
            database=os.environ.get("WEBAGENT_DB_NAME", ""),
            username=os.environ.get("WEBAGENT_DB_USER") or None,
            password_secret_key=os.environ.get("WEBAGENT_DB_PASSWORD_KEY") or None,
            ssl_mode=os.environ.get("WEBAGENT_DB_SSLMODE", "require"),
        )
        _cached = cfg
        return cfg

    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r") as f:
                data = json.load(f)
            # Self-heal: a config saved by an older version may carry
            # provider="supabase". SupabaseBackend has been removed — flip to
            # SQLite so the app boots cleanly and the admin can re-activate on
            # a Postgres-family provider from the Database page.
            if data.get("provider") == "supabase":
                logger.warning(
                    "db_connection.json has provider='supabase' which is no longer "
                    "supported — switching to sqlite. Open the Database page to "
                    "configure a Postgres connection."
                )
                data["provider"] = "sqlite"
                try:
                    from app.util.config_io import safe_write_json
                    safe_write_json(_CONFIG_FILE, data)
                except Exception:
                    pass
            # Strip legacy supabase fields so they don't accumulate in the config.
            data.pop("supabase_url", None)
            data.pop("supabase_service_key_secret", None)
            cfg = DBConnectionConfig.from_dict(data)
        except Exception as e:
            logger.warning("Failed to read db_connection.json: %s", e)
            cfg = DBConnectionConfig()
    else:
        cfg = DBConnectionConfig()
    _cached = cfg
    return cfg


def save_config(cfg: DBConnectionConfig) -> None:
    """Persist active DB connection config to disk (unless env-locked)."""
    global _cached
    _cached = cfg
    if _env_locked():
        logger.warning("Config is env-locked; not writing db_connection.json")
        return
    try:
        safe_write_json(_CONFIG_FILE, cfg.to_dict())
    except Exception as e:
        logger.error("Failed to save db_connection.json: %s", e)
        raise
