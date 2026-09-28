"""Embed this app's configuration into a deploy so the new server self-provisions.

THE FEATURE (in plain terms)
----------------------------
When an admin deploys a fresh copy of WebAgent, the Deploy panel's "Include this
app's configuration" toggle lets them carry this install's setup — its AI provider
+ keys, its secrets-vault choice, and (optionally) its database connection — onto
the new server, so it comes up already configured instead of dropping the first
visitor on the setup page. This is the deploy-time twin of the Data Settings
"Create setup code" button: it reuses the SAME gather-and-encrypt engine
(``app/admin/bootstrap_bundle``).

HOW IT TRAVELS
--------------
The gathered sections are packed into a single **keyless** ``WABOOT2`` code
(``export_code``). That code is written onto the new box as ``bootstrap.json`` —
for a cloud VM by the shared install script
(``app/deploy/bootstrap.build_install_script``), for a pasted-command install by
the platform setup script (which writes it from the ``WA_BOOTSTRAP_CODE`` the
command carries). On first boot ``bootstrap_bundle.apply_boot_file`` finds the
file and applies it, then scrubs the key.

LLM KEYS OMITTED WHEN THEY'RE ALREADY REACHABLE
-----------------------------------------------
The LLM section normally carries the API keys so a brand-new box comes up able to
talk to the model. But when the deploy ALSO shares this app's database (the
``database`` section) and this host runs the default ``inline_db`` vault, the new
server can already read the keys from that shared DB on boot — the encrypted key
lives in the DB and, for inline_db, so does the material that decrypts it. In
that case the keys are stripped from the bundle (see
``_llm_keys_resolvable_from_shared_infra``): the LLM section still carries the
provider / model / roster, just not the redundant plaintext keys. Every other
vault mode keeps the keys in the bundle, so those deploys still boot working.

NO PASSWORD NEEDED (decoupled from the admin password)
------------------------------------------------------
Including configuration used to require a pre-set admin password, because the
bundle was locked with it (the legacy ``WABOOT1`` format). That lock never bought
anything in a *deploy*: the bundle and its key always travel together to the same
trusted destination — on a manual install the admin password already rides the
very same copy-paste command in plaintext; on a cloud VM both land in the same
box. So the bundle is now a **keyless** ``WABOOT2`` code that the new box can
read on its own, and the admin password is back to doing ONLY its own job
(pre-setting the first admin, via ``BOOTSTRAP_ADMIN_PASSWORD``). The two are
independent: you can include configuration with or without a pre-set password.

ADMIN LOGIN COMES WITH THE SHARED DATABASE
------------------------------------------
User + admin logins live in the shared database's central ``user_accounts`` table
(see ``app/auth/users.py``), NOT in a per-box file. So when the ``database``
section is included the new box points at THIS install's database and therefore
already sees the exact same admin — same username, same password — with nothing to
carry, re-hash or re-type: once the database section activates on first boot,
``admin_exists()`` reads the shared table and the box boots straight past the setup
page. This is why the deploy panel greys out the "Admin password" field once
"Database (shared)" is ticked (the existing admin is reused, so there is nothing to
pre-set) and greys — but keeps ticked — the "AI model & key" box (its key is
likewise reachable from the shared DB). A fresh-DB deploy (database unticked) gets
its own empty ``user_accounts`` and still honours the optional pre-set admin
password to create that first admin.

WHY A SHARED HELPER (not per-provider)
--------------------------------------
Both the cloud path (``app/deploy/manager.run_deploy``) and the manual path
(``app/api/deploy`` building a copy-paste command) need the identical "should we
embed, and if so produce the code" decision. Keeping it here means one behaviour,
one set of guard rails, mirrored by neither.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# The sections the toggle can carry. Admin login is not one of them — it lives in
# the shared ``user_accounts`` DB table, so a shared-database deploy (``database``
# on) already sees the same admin, and a fresh-DB deploy creates its first admin
# from the optional pre-set password instead (see the module docstring).
ALLOWED_SECTIONS = ("llm", "database", "vault")
# Ticked by default in the panel: the AI keys + the vault. The database is opt-in
# (ticking it points the new server at THIS app's database — a shared, not a
# fresh, database), so it's left off by default.
DEFAULT_SECTIONS: List[str] = ["llm", "vault"]


def wants_embed(config: Dict[str, Any]) -> bool:
    """True when the admin turned on "Include this app's configuration"."""
    return bool((config or {}).get("embed_config"))


def _llm_keys_resolvable_from_shared_infra(sections: List[str]) -> bool:
    """True when the new server will be able to read the LLM API keys from shared
    infrastructure, so embedding the plaintext keys in the bundle is redundant.

    Where the pieces live (see app/encryption/vault_keys and app/admin/settings):
      * the ENCRYPTED LLM key sits in the DATABASE (``auth_elements.secret_ref``),
        alongside its key metadata (``tenant_key_meta``);
      * the material that DECRYPTS it — the KEK and the wrapped per-tenant DEK —
        sits in the VAULT.
    So a fresh box can resolve the key WITHOUT the bundle carrying it only when
    both reach it. That is exactly the case when:
      * the ``database`` section is embedded — the new server runs on THIS app's
        shared DB, so it already has the encrypted row + metadata; AND
      * this host's vault mode is ``inline_db`` — the one mode whose KEK/DEK ride
        *inside* that same shared DB, so sharing the DB shares the keys too, with
        nothing extra to re-enter.
    Every other vault mode keeps its key material off the shared DB (a remote
    vault's access token is deliberately NOT bundled; os_keyring / env keys are
    device-local), so we keep embedding the LLM key there and the deploy still
    comes up working.
    """
    if "database" not in sections:
        return False
    try:
        from app.secrets import get_mode
        return get_mode() == "inline_db"
    except Exception:
        return False


def _clean_sections(config: Dict[str, Any]) -> List[str]:
    raw = (config or {}).get("embed_sections")
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_SECTIONS)
    return [s for s in raw if s in ALLOWED_SECTIONS] or list(DEFAULT_SECTIONS)


async def build_embed_code(config: Dict[str, Any]) -> Tuple[str, str]:
    """Produce the config bundle for a deploy, or a reason it was skipped.

    Returns ``(code, warning)``:
      * embed OFF                 → ``("", "")`` — nothing to do.
      * embed ON, nothing to pack → ``("", "<why>")`` — the caller surfaces the
        warning and deploys BARE (never fatal: an empty include shouldn't sink
        the whole deploy).
      * embed ON, all good        → ``(code, "")`` — a keyless ``WABOOT2`` code to
        write as ``bootstrap.json`` on the new box.

    No admin password is required (or used): the bundle is keyless because it only
    ever travels to the trusted new install alongside the command that carries it
    (see the module docstring). The admin password, if the admin set one, is a
    separate thing that presets the first admin — it does not gate this.
    """
    if not wants_embed(config):
        return "", ""
    sections = _clean_sections(config)
    if not sections:
        return "", ""
    # When the new box will run on this app's shared DB + inline vault, the LLM
    # keys already resolve from that shared infrastructure on boot, so shipping
    # them in the bundle is redundant (and needlessly copies the keys around).
    strip_llm = _llm_keys_resolvable_from_shared_infra(sections)
    if strip_llm:
        logger.info("build_embed_code: LLM keys resolvable from shared DB+inline vault — "
                    "omitting them from the bundle.")
    # No admin section is carried: logins live in the shared ``user_accounts`` table,
    # so a shared-database deploy already sees the same admin once the database
    # section activates on first boot (see the module docstring).
    try:
        from app.admin.bootstrap_bundle import export_code, BundleError
        code = await export_code(sections, strip_llm_secrets=strip_llm)
    except Exception as e:  # noqa: BLE001 — BundleError (nothing to pack) or any gather failure
        # BundleError carries a friendly message ("none of the selected sections
        # had any data"); anything else is logged and reported generically.
        from app.admin.bootstrap_bundle import BundleError
        if isinstance(e, BundleError):
            return "", f"Configuration was not included — {e}"
        logger.exception("build_embed_code: could not package configuration")
        return "", "Configuration was not included — it could not be packaged."
    return code, ""
