"""Social sign-in providers — core manager (discovery + credential store + flow).

Providers are drop-in folders under `plugins/auth_providers/`. This package is
the core that discovers them (`registry`) and runs the OAuth login flow
(`flow`); the HTTP endpoints live in `app/api/social_auth.py`.
"""

from app.auth_providers import registry, flow  # noqa: F401

__all__ = ["registry", "flow"]
