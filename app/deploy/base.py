"""The deploy-target contract.

A deploy target (Google VM, AWS, a plain Linux box, Docker) is a DROP-IN: one
file under ``app/deploy/providers/`` exposing a module-level ``PROVIDER`` (an
instance of a ``BaseDeployProvider`` subclass) plus a ``FEATURE`` header. The
registry (``app/deploy/registry.py``) auto-discovers it — no central edit. This
mirrors how scheduler providers and remote-access methods already work.

Each target declares two field lists that drive the panel's forms:

  * ``config_fields``     — NON-secret settings (project, region, machine size,
                            repo URL, domain). Stored in ``data/config/deploy.json``.
  * ``credential_fields`` — the cloud keys. Secrets land in the encrypted vault
                            (``app/deploy/credentials.py``); blank-on-save keeps
                            the stored value; auto-discarded after a successful
                            deploy when the panel's "forget keys" option is on.

A field is ``{key, label, type, secret?, required?, placeholder?, options?,
default?, hint?}`` where ``type`` is one of ``text`` / ``password`` /
``textarea`` / ``select`` / ``checkbox`` / ``number``.

Three actions, all admin-gated by the router:

  * ``test(config, creds)``           — verify the keys work, create nothing.
  * ``deploy(config, creds, emit)``   — async generator streaming progress
                                        events; the FINAL event is
                                        ``{"phase": "done", "result": {...}}``
                                        carrying ``{ok, public_url, server, …}``.
  * ``destroy(config, creds, record)``— async generator; tear the server down.

Progress events are ``{"phase", "message", "level"}`` (level ∈ info/ok/warn/err),
the same vocabulary the Commit ⭐ stream uses, so the frontend reader is shared
in spirit.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple


def ev(message: str, *, phase: str = "step", level: str = "info") -> Dict[str, Any]:
    """Build one progress event."""
    return {"phase": phase, "message": message, "level": level}


def done(result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the terminal event that carries the final result dict."""
    return {"phase": "done", "result": result}


class BaseDeployProvider:
    # ── identity / display (override on the subclass) ──
    id: str = ""
    display_name: str = ""
    icon: str = "cloud"            # a lucide icon name
    summary: str = ""
    requires: List[str] = []       # human-readable prerequisites

    # A "manual" target creates nothing on a cloud account: it has no cloud key
    # and (typically) produces a copy-paste command the user runs themselves
    # (e.g. installing onto a phone via Termux). The panel uses this to drop the
    # cloud-key section + the "billable resource" confirm, and to show the
    # generated command in a copy box instead of a "last server" line.
    manual: bool = False

    # Whether Deploy CREATES a new machine (a billable cloud VM) vs installs onto a
    # server the admin already owns (e.g. the SSH target). The panel reads this to
    # word its confirm dialogs correctly: a target that creates nothing shouldn't
    # warn about a "billable server" on deploy or "permanently deletes it" on
    # tear-down. Cloud targets leave it True; ``ssh_vm`` sets it False.
    creates_server: bool = True

    config_fields: List[Dict[str, Any]] = []
    credential_fields: List[Dict[str, Any]] = []
    # Which credential keys must be present to count "ready to deploy". Defaults
    # to every secret field when None.
    credential_required: List[str] | None = None

    # ── capability ──
    def available(self) -> Tuple[bool, str]:
        """(True, "") when this target can run here, else (False, reason).

        Used for targets that need an optional Python library (AWS → boto3, a
        plain server → an SSH lib, Docker → the docker SDK). A target whose
        dependency is missing reports it here instead of crashing; the panel
        shows the reason and the other targets still work. Google VM needs no
        extra package (it drives the REST API with httpx), so it returns True.
        """
        return True, ""

    # ── actions (override) ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        """Validate credentials without creating anything. {ok, detail}."""
        return {"ok": False, "detail": "not implemented"}

    async def deploy(
        self, config: Dict[str, Any], creds: Dict[str, Any]
    ) -> AsyncIterator[Dict[str, Any]]:
        """Provision + install, streaming events. Yield a final ``done`` event."""
        raise NotImplementedError
        yield  # pragma: no cover  (makes this an async generator)

    async def destroy(
        self, config: Dict[str, Any], creds: Dict[str, Any], record: Dict[str, Any]
    ) -> AsyncIterator[Dict[str, Any]]:
        """Tear down the last-deployed server, streaming events."""
        yield done({"ok": False, "message": "This target has no tear-down."})

    # ── instance management (OPTIONAL — drives the cloud tiles on the Instances page) ──
    #
    # A target that can ENUMERATE and MANAGE the servers already living in the
    # admin's cloud account (not just the single one it last deployed) sets
    # ``supports_instances = True`` and implements the two methods below. The
    # Instances admin page (ui/admin-tools/instances/) lists every such target's
    # instances and offers per-server Start / Stop / Delete. A target that leaves
    # these at their defaults simply never appears on that page — the safe
    # "not supported" fallbacks mean adding the page broke nothing. This extends
    # THIS contract (the same kind of optional capability as test/deploy/destroy),
    # it is not a central registry edit.
    supports_instances: bool = False

    # Which ``config_fields`` keys must be filled in JUST to connect to the cloud
    # account and manage instances — the "id" half of the Cloud VMs page's
    # sign-in panel (e.g. a cloud project id). The "key" half is the secret
    # ``credential_fields``. Listing servers needs only these + the key (start /
    # stop / delete take the per-server zone from each row), so the sign-in panel
    # stays short. Leave empty when a target needs no such id. The page renders an
    # input for each, pre-filled with the saved value (never the secret).
    connect_config_keys: List[str] = []

    async def list_instances(
        self, config: Dict[str, Any], creds: Dict[str, Any]
    ) -> Dict[str, Any]:
        """List every server this target can see in the admin's cloud account.

        Returns ``{ok, instances, detail}`` where each instance is a dict the
        Cloud VMs page renders — at least ``{name, zone, status, machine_type,
        ip, created, is_webagent, is_this_app}``. ``zone`` + ``name`` are the
        handle ``instance_action`` needs to act on a server."""
        return {"ok": False, "instances": [], "detail": "This target has no instance list."}

    async def instance_action(
        self, config: Dict[str, Any], creds: Dict[str, Any],
        action: str, instance: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Start / Stop / Delete one server, streaming progress events.

        ``action`` ∈ {"start", "stop", "delete"}; ``instance`` carries at least
        ``{name, zone}``. Streams the same ``{phase, message, level}`` events as
        deploy/destroy and ends with a ``done`` event carrying ``{ok, message}``
        (plus ``{deleted: True}`` for a successful delete, so the manager can
        clear the deployment record). Default: not supported."""
        yield done({"ok": False, "message": "This target has no instance actions."})
