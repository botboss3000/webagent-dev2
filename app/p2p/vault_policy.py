"""P2P vault sync policy — decides which auth_elements rows leave the device.

Routes rows through _vault_for() and filters out local-only rows that must never
be synced (master encryption keys, per-machine secrets).
"""

from __future__ import annotations

import fnmatch
from typing import List


# Rows matching these (user_id, service, label) patterns are NEVER synced.
# Each tuple is (user_id_pattern, service_pattern, label_pattern) using fnmatch.
LOCAL_ONLY_PATTERNS: List[tuple] = [
    # Master encryption key — per-machine root of trust
    ("_vault", "_secrets_vault", "wa:kek:active"),
    # Retired KEK versions
    ("_vault", "_secrets_vault", "wa:kek:v*"),
    # Data encryption keys — per-machine
    ("_vault", "_secrets_vault", "wa:dek:*"),
    # JWT signing secret — per-machine, committing it lets anyone forge tokens
    ("_vault", "_secrets_vault", "jwt_signing_secret"),
    # Database password — per-machine
    ("_vault", "_secrets_vault", "db_password_postgres"),
    # New instances clone the canonical public repo. Deployment source-control
    # tokens are not required on peers and remain local to the source instance.
    ("admin", "deploy_github_token", "default"),
]


def should_sync_row(user_id: str, service: str, label: str) -> bool:
    """Return True if this auth_elements row should be included in P2P sync manifests.
    
    Filters out per-machine secrets (KEK, DEK, JWT secret, DB password).
    All other rows sync.
    """
    for uid_pat, svc_pat, lbl_pat in LOCAL_ONLY_PATTERNS:
        if (fnmatch.fnmatch(user_id, uid_pat)
                and fnmatch.fnmatch(service, svc_pat)
                and fnmatch.fnmatch(label, lbl_pat)):
            return False
    return True
