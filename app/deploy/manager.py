"""Deploy orchestration — the glue between the API and a target provider.

Builds the panel's catalog (every drop-in target + its forms + whether its keys
are present + its last deployment), and runs the streaming deploy / destroy:
loads the saved config + the vault credentials, drives the provider's async
generator, stamps the deployment record on success, and — when the target's
"Forget keys after deploy" option is on — DELETES the cloud key from the vault so
the app holds no standing cloud access.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict

from app.deploy import credentials, store
from app.deploy.base import done, ev
from app.deploy.registry import get_provider, list_providers

logger = logging.getLogger(__name__)


def _field_defaults(fields) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in fields:
        if "default" in f:
            out[f["key"]] = f["default"]
    return out


def _mirror_keys(p) -> list:
    """The non-secret keys a target mirrors into the shared vault, so its cloud
    setup travels with a shared vault DB. Two sources, de-duplicated:
      • ``connect_config_keys`` — the sign-in 'id' fields (the Google project id).
      • ``shared_config_keys``  — extra settings worth carrying but NOT part of the
        sign-in form (e.g. the deploy Zone), so mirroring them never clutters the
        Cloud VMs sign-in panel or the 'connected' check (which read the connect
        keys directly). Empty for targets that declare neither."""
    keys = list(getattr(p, "connect_config_keys", []) or [])
    for k in (getattr(p, "shared_config_keys", []) or []):
        if k not in keys:
            keys.append(k)
    return keys


async def _load_config(p) -> Dict[str, Any]:
    """The effective non-secret config for a target, assembled in priority order:
    field defaults → the vault's shared keys (project id, zone) → the local
    deploy.json working copy on top. The vault layer is what lets a SECOND device
    signed into a shared vault DB inherit the cloud setup (project id + zone) even
    though deploy.json is a local file; a value typed locally still wins, so
    same-device behaviour is unchanged. Targets with no mirror keys just get
    defaults + local, as before."""
    base = _field_defaults(p.config_fields)
    mk = _mirror_keys(p)
    shared = await credentials.read_connect(p.id, mk) if mk else {}
    return {**base, **shared, **store.get_config(p.id)}


async def save_config(provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Save a target's non-secret settings to deploy.json AND mirror its sign-in
    'id' fields into the vault, so the cloud connection travels with a shared vault
    DB. The Deploy card's Save routes here (the reserved ``_repo`` slot has no
    provider, so it just writes the file — no mirror)."""
    saved = store.save_config(provider_id, config or {})
    p = get_provider(provider_id)
    mk = _mirror_keys(p) if p else []
    if mk:
        await credentials.save_connect(provider_id, mk, config or {})
    return saved


async def build_catalog() -> Dict[str, Any]:
    """Everything the panel needs to render, for every discovered target."""
    providers = []
    for p in list_providers():
        ok_avail, reason = p.available()
        # Layer the saved config (incl. the vault's shared connect id) over the
        # field defaults so the form shows sensible starting values on a fresh
        # install — and inherits the project id from a shared vault DB.
        merged = await _load_config(p)
        cred_view = await credentials.public_view(p.id, p.credential_fields, p.credential_required)
        entry = {
            "id": p.id,
            "display_name": p.display_name,
            "icon": p.icon,
            "summary": p.summary,
            "requires": p.requires,
            "manual": bool(getattr(p, "manual", False)),
            "creates_server": bool(getattr(p, "creates_server", True)),
            "available": ok_avail,
            "unavailable_reason": reason,
            "config_fields": p.config_fields,
            "credential_fields": [
                {k: v for k, v in f.items() if k != "secret"} | {"secret": bool(f.get("secret"))}
                for f in p.credential_fields
            ],
            "config": merged,
            "credentials_set": cred_view["secrets_set"],
            "configured": cred_view["configured"],
            "deployment": store.get_deployment(p.id),
            "saved_servers": bool(getattr(p, "saved_servers", False)),
            # Staged form reveal (project → key → rest); see deploy.js.
            "progressive": bool(getattr(p, "progressive", False)),
        }
        # A profile-aware target (the SSH one) also carries its list of saved
        # servers + which is currently loaded, so the panel can render the picker.
        if entry["saved_servers"]:
            entry["servers"] = await _server_views(p)
            entry["active_server"] = store.get_active_server(p.id)
        providers.append(entry)
    # The SHARED repo details (repo URL + public/private) the New-deployment panel
    # carries into every target, kept in the reserved "_repo" slot so the panel can
    # pre-fill the Repo-details bar. Non-secret; the token + admin password are never
    # persisted. When this device has no local "_repo" yet, fall back to the repo URL
    # stored in the shared vault (see credentials.save_github_repo_url) — so a device
    # signed into the same vault pre-fills the same repo without re-typing it.
    shared_repo = dict(store.get_config("_repo") or {})
    if not str(shared_repo.get("github_url") or "").strip():
        try:
            vurl = await credentials.get_github_repo_url()
            if vurl:
                shared_repo["github_url"] = vurl
        except Exception:  # noqa: BLE001
            pass
    return {
        "providers": providers,
        "active_provider": store.get_active_provider() or (providers[0]["id"] if providers else ""),
        "shared_repo": shared_repo,
    }


# ── Saved servers (named reusable profiles for a profile-aware target) ───────
#
# The SSH target lets an admin keep several servers as pick-from-a-dropdown
# profiles. A profile is NON-secret settings (store.py ``servers``) + its secrets
# in the vault (credentials.py ``profile=<id>``). Selecting one COPIES it into the
# single "working" slot (store config + the bare ``deploy_cred:<id>`` vault row)
# that deploy/test/tear-down already read — so those paths need no change; they
# always act on "whatever is currently loaded".


def _label_for(p, cfg: Dict[str, Any]) -> str:
    return (str(cfg.get("label") or cfg.get("host") or "").strip()
            or cfg.get("display") or "Saved server")


async def _server_views(p) -> list:
    """The saved servers for one target, as the panel renders them: id + a short
    label + the key non-secret fields + whether a login is stored."""
    fields = p.credential_fields
    out = []
    for sid, rec in store.list_servers(p.id).items():
        rec = rec or {}
        view = await credentials.public_view(p.id, fields, [], profile=sid)
        has_login = any(bool(v) for v in (view.get("secrets_set") or {}).values())
        out.append({
            "id": sid,
            "label": _label_for(p, rec),
            "host": rec.get("host", ""),
            "ssh_user": rec.get("ssh_user", ""),
            "ssh_port": rec.get("ssh_port", ""),
            "source": rec.get("source", ""),          # "manual" | "google_vm" | …
            "configured": has_login,
        })
    out.sort(key=lambda s: str(s.get("label", "")).lower())
    return out


def _split_config_keys(p, values: Dict[str, Any]):
    """Partition a submitted form dict into this target's config keys vs cred keys
    (label is a saved-server-only key, kept with the config record)."""
    config_keys = {f["key"] for f in p.config_fields} | {"label"}
    cred_keys = {f["key"] for f in p.credential_fields}
    cfg = {k: v for k, v in (values or {}).items() if k in config_keys}
    creds = {k: v for k, v in (values or {}).items() if k in cred_keys}
    return cfg, creds


async def _load_into_working_slot(provider_id: str, p, cfg: Dict[str, Any], server_id: str) -> None:
    """Make one saved server the ACTIVE one: copy its non-secret config into the
    working config slot and its stored secrets into the working vault row, then
    mark it active. deploy/test/tear-down then run against it unchanged."""
    working_cfg = {k: v for k, v in cfg.items() if k != "label"}
    store.save_config(provider_id, working_cfg)
    if server_id:
        secrets = await credentials.read(provider_id, p.credential_fields, profile=server_id)
        if secrets:
            await credentials.save(provider_id, p.credential_fields, secrets)   # working slot
    store.set_active_server(provider_id, server_id or "")


async def save_server(provider_id: str, label: str, values: Dict[str, Any],
                      server_id: str = "", *, source: str = "manual") -> Dict[str, Any]:
    """Create or update a saved server from a submitted form (config + secrets),
    then make it the loaded/active one. Blank secrets keep whatever that server had
    stored. Returns ``{ok, server_id}``."""
    p = get_provider(provider_id)
    if not p or not getattr(p, "saved_servers", False):
        return {"ok": False, "detail": "This target has no saved servers."}
    cfg, creds = _split_config_keys(p, values)
    cfg["label"] = (label or cfg.get("label") or cfg.get("host") or "").strip()
    cfg["source"] = source
    sid = store.upsert_server(provider_id, server_id, cfg)
    # Persist the secrets to this server's own vault row (blank = keep existing).
    await credentials.save(provider_id, p.credential_fields, creds, profile=sid)
    # Load it into the working slot so a following Deploy targets it.
    await _load_into_working_slot(provider_id, p, cfg, sid)
    return {"ok": True, "server_id": sid}


async def select_server(provider_id: str, server_id: str) -> Dict[str, Any]:
    """Load a saved server (or, with a blank id, clear the form to enter a new
    one). Returns ``{ok, config}`` with the non-secret settings so the form fills."""
    p = get_provider(provider_id)
    if not p or not getattr(p, "saved_servers", False):
        return {"ok": False, "detail": "This target has no saved servers."}
    if not server_id:
        # "New server…": blank the working slot + its working secrets.
        store.save_config(provider_id, {})
        await credentials.forget(provider_id)
        store.set_active_server(provider_id, "")
        return {"ok": True, "config": {}}
    cfg = store.get_server(provider_id, server_id)
    if not cfg:
        return {"ok": False, "detail": "That saved server no longer exists."}
    await _load_into_working_slot(provider_id, p, cfg, server_id)
    return {"ok": True, "config": {k: v for k, v in cfg.items() if k not in ("source",)}}


async def delete_server(provider_id: str, server_id: str) -> Dict[str, Any]:
    """Remove a saved server and its stored login."""
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown target."}
    store.delete_server(provider_id, server_id)
    await credentials.forget(provider_id, profile=server_id)
    return {"ok": True}


async def list_ssh_servers() -> list:
    """Public read-only view of the saved SSH servers, for the admin Terminal
    page's "Saved servers" launcher. Same shape the Deploy panel renders (id +
    label + host/user/port + whether a login is stored) — NEVER any secret.
    Returns [] if the SSH target isn't available here. The Terminal page reads
    this to offer one-click SSH into a server the app already knows how to reach;
    it opens an interactive shell over the SAME stored login the deploy runtime
    uses (see app/api/terminal.py TerminalSession.spawn_ssh)."""
    p = get_provider("ssh_vm")
    if not p or not getattr(p, "saved_servers", False):
        return []
    return await _server_views(p)


async def link_ssh_server(*, host: str, ssh_user: str, ssh_port: int, private_key: str,
                          label: str, repo_url: str = "", branch: str = "",
                          domain: str = "", source: str = "google_vm") -> str:
    """Create a saved SSH server from a freshly-provisioned VM (e.g. a Google VM),
    so it appears in the SSH target's picker ready to manage. Writes the profile
    WITHOUT touching the working slot (the admin may be mid-edit). Returns the id,
    or "" if the SSH target isn't available."""
    p = get_provider("ssh_vm")
    if not p or not getattr(p, "saved_servers", False):
        return ""
    cfg = {"label": (label or host).strip(), "host": host, "ssh_user": ssh_user,
           "ssh_port": ssh_port, "repo_url": repo_url, "branch": branch,
           "domain": domain, "source": source}
    sid = store.upsert_server("ssh_vm", "", cfg)
    await credentials.save("ssh_vm", p.credential_fields, {"private_key": private_key}, profile=sid)
    return sid


async def run_deploy(provider_id: str) -> AsyncIterator[Dict[str, Any]]:
    p = get_provider(provider_id)
    if not p:
        yield done({"ok": False, "message": "Unknown deploy target."})
        return
    ok_avail, reason = p.available()
    if not ok_avail:
        yield done({"ok": False, "message": reason or "This target is unavailable."})
        return

    config = await _load_config(p)
    creds = await credentials.read(provider_id, p.credential_fields)
    required = p.credential_required if p.credential_required is not None else \
        [f["key"] for f in p.credential_fields if f.get("secret")]
    if required and not all(str(creds.get(k, "")).strip() for k in required):
        yield done({"ok": False, "message": "Cloud key is missing. Fill in the credentials first."})
        return

    # Optionally gather THIS app's config (AI keys / vault / database) into a
    # keyless bundle to pre-load on the new server — the Deploy panel's "Include
    # this app's configuration" toggle. We inject it into config["_bootstrap_code"]
    # so the shared install script writes it as bootstrap.json; first boot applies
    # it (no admin password needed — decoupled, see config_embed). An empty include
    # only warns (build_embed_code returns a reason) — the deploy still proceeds bare.
    from app.deploy import config_embed
    if config_embed.wants_embed(config):
        code, warn = await config_embed.build_embed_code(config)
        if warn:
            yield ev(warn, phase="embed", level="warn")
        if code:
            config["_bootstrap_code"] = code
            yield ev("Including this app's configuration on the new server.", phase="embed", level="ok")

    final: Dict[str, Any] = {"ok": False, "message": "Deploy produced no result."}
    try:
        async for event in p.deploy(config, creds):
            if event.get("phase") == "done":
                final = event.get("result", final)
            yield event
    except Exception as e:
        logger.exception("deploy run crashed for %s", provider_id)
        yield done({"ok": False, "message": str(e) or "Deploy crashed."})
        return

    if final.get("ok"):
        store.set_deployment(provider_id, {
            "server": final.get("server", ""),
            "ip": final.get("ip", ""),
            "zone": final.get("zone", ""),
            "region": final.get("region", ""),
            "project": final.get("project", ""),
            "public_url": final.get("public_url", ""),
            "state": final.get("state", "running"),
            # Manual targets (e.g. Termux) return a copy-paste command + how-to
            # instructions instead of a created server; persist them so the panel
            # can re-show the command box on reload. Cloud targets leave these blank.
            "command": final.get("command", ""),
            "instructions": final.get("instructions", ""),
        })
        # Auto-forget the cloud key after a successful deploy — but only for
        # targets that actually HAVE a cloud key. A manual target (no credentials)
        # would otherwise print a misleading "key discarded" line.
        if config.get("forget_keys", True) and p.credential_fields:
            await credentials.forget(provider_id)
            yield ev("Cloud key discarded from the vault.", phase="forgot", level="ok")


async def run_cloud_run_rebuild(target: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
    """Build and deploy the repo recorded by a Cloud Run device heartbeat.

    This runs from the admin/control instance, not inside the ephemeral target.
    The provider's rebuild path updates only the service image, so the existing
    Cloud Run template settings remain intact.
    """
    provider_id = "google_cloud_run"
    p = get_provider(provider_id)
    if not p or not callable(getattr(p, "rebuild", None)):
        yield done({"ok": False, "message": "Google Cloud Run deployment is unavailable."})
        return

    config = await _load_config(p)
    config.update({
        "project_id": str(target.get("project") or "").strip(),
        "region": str(target.get("region") or "").strip(),
        "service_name": str(target.get("service") or "").strip(),
        "repo_url": str(target.get("repo") or "").strip(),
        "branch": str(target.get("branch") or "").strip(),
    })
    creds = await credentials.read(provider_id, p.credential_fields)
    if not str(creds.get("service_account_json") or "").strip():
        yield done({
            "ok": False,
            "message": (
                "The Google Cloud key is not stored. Re-enter it in New Deployment "
                "and turn off “Forget my Google Cloud key after deploy” to use Build & Deploy."
            ),
        })
        return

    try:
        async for event in p.rebuild(config, creds):
            yield event
    except Exception as exc:
        logger.exception("Cloud Run rebuild crashed")
        yield done({"ok": False, "message": str(exc) or "Cloud Run rebuild crashed."})


async def run_destroy(provider_id: str) -> AsyncIterator[Dict[str, Any]]:
    p = get_provider(provider_id)
    if not p:
        yield done({"ok": False, "message": "Unknown deploy target."})
        return
    config = await _load_config(p)
    creds = await credentials.read(provider_id, p.credential_fields)
    required = p.credential_required if p.credential_required is not None else \
        [f["key"] for f in p.credential_fields if f.get("secret")]
    if required and not all(str(creds.get(k, "")).strip() for k in required):
        yield done({"ok": False, "message": "The key was discarded after deploy — re-enter it to tear down."})
        return
    record = store.get_deployment(provider_id)

    final: Dict[str, Any] = {"ok": False, "message": "Tear-down produced no result."}
    try:
        async for event in p.destroy(config, creds, record):
            if event.get("phase") == "done":
                final = event.get("result", final)
            yield event
    except Exception as e:
        logger.exception("destroy run crashed for %s", provider_id)
        yield done({"ok": False, "message": str(e) or "Tear-down crashed."})
        return

    if final.get("ok") and final.get("deleted"):
        store.clear_deployment(provider_id)


# ── Instance management (the Cloud VMs admin page) ──────────────────────────
#
# A SEPARATE surface from deploy/destroy: the Cloud VMs page manages EVERY server
# in the admin's cloud account, not just the one this app last created. It reuses
# the same vault credentials + saved config a deploy target already has; a target
# only takes part if it sets ``supports_instances`` and a cloud key is currently
# stored (the Deploy card auto-forgets the key after a deploy, so the page must
# degrade gracefully when none is present).


async def _has_key(p) -> bool:
    creds = await credentials.read(p.id, p.credential_fields)
    required = p.credential_required if p.credential_required is not None else \
        [f["key"] for f in p.credential_fields if f.get("secret")]
    return bool(required) and all(str(creds.get(k, "")).strip() for k in required)


def _connect_field_views(p, cfg: Dict[str, Any]) -> list:
    """The descriptors for the 'id' inputs the Cloud VMs sign-in panel shows
    (the provider's ``connect_config_keys``), each pre-filled with the saved
    value so the panel can also act as a 'change account' form. Non-secret, so
    echoing the value back is safe."""
    keys = list(getattr(p, "connect_config_keys", []) or [])
    by_key = {f["key"]: f for f in p.config_fields}
    views = []
    for k in keys:
        f = by_key.get(k, {"key": k, "label": k, "type": "text"})
        views.append({
            "key": k,
            "label": f.get("label", k),
            "type": f.get("type", "text"),
            "placeholder": f.get("placeholder", ""),
            "tip": f.get("tip", ""),
            "required": bool(f.get("required")),
            "value": cfg.get(k, ""),
        })
    return views


def _connect_ready(p, cfg: Dict[str, Any]) -> bool:
    """True when every ``connect_config_keys`` value the target needs is set."""
    return all(str(cfg.get(k, "")).strip() for k in (getattr(p, "connect_config_keys", []) or []))


async def manageable_providers() -> Dict[str, Any]:
    """The targets the Cloud VMs page can offer: every discovered provider that
    declares ``supports_instances``, with whether its cloud key is stored, the
    saved project/zone, and the sign-in panel's 'id' fields (so the page can show
    a clear connect form per target, and a picker only when more than one
    qualifies)."""
    out = []
    for p in list_providers():
        if not getattr(p, "supports_instances", False):
            continue
        ok_avail, reason = p.available()
        cfg = await _load_config(p)
        has_key = await _has_key(p)
        connect_ready = _connect_ready(p, cfg)
        out.append({
            "id": p.id,
            "display_name": p.display_name,
            "icon": p.icon,
            "available": ok_avail,
            "unavailable_reason": reason,
            "has_key": has_key,
            # Fully signed in: the cloud key is stored AND the id fields are set.
            "connected": bool(has_key and connect_ready),
            "credential_fields": [
                {k: v for k, v in f.items() if k != "secret"} | {"secret": bool(f.get("secret"))}
                for f in p.credential_fields
            ],
            # The 'id' inputs the sign-in panel renders (pre-filled, non-secret).
            "connect_fields": _connect_field_views(p, cfg),
            "project": cfg.get("project_id") or cfg.get("project") or "",
            "zone": cfg.get("zone") or "",
        })
    return {"providers": out}


async def list_instances(provider_id: str) -> Dict[str, Any]:
    """List the servers one target can see, or a clear reason it can't.

    When the target isn't fully signed in — no cloud key, or a missing 'id' such
    as the project — it returns ``needs_connect: True`` so the page shows its
    sign-in panel instead of a raw error."""
    p = get_provider(provider_id)
    if not p or not getattr(p, "supports_instances", False):
        return {"ok": False, "instances": [], "detail": "This target cannot list servers."}
    ok_avail, reason = p.available()
    if not ok_avail:
        return {"ok": False, "instances": [], "detail": reason or "This target is unavailable."}
    config = await _load_config(p)
    if not await _has_key(p):
        return {"ok": False, "instances": [], "needs_key": True, "needs_connect": True,
                "detail": "Connect your cloud account to manage your servers — enter its id and key."}
    if not _connect_ready(p, config):
        return {"ok": False, "instances": [], "needs_connect": True,
                "detail": "Almost there — finish connecting by entering your cloud account id."}
    creds = await credentials.read(provider_id, p.credential_fields)
    try:
        return await p.list_instances(config, creds)
    except Exception as e:
        logger.exception("list_instances crashed for %s", provider_id)
        return {"ok": False, "instances": [], "detail": str(e) or "Listing servers failed."}


async def save_connection(provider_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Save what the Cloud VMs sign-in panel collected: the cloud 'id' fields
    (e.g. project) into deploy.json, and the secret 'key' into the SAME encrypted
    vault entry the Deploy card uses. A BLANK secret leaves the stored key
    untouched (so 'change account' can change just the id). Non-secret/unknown
    keys are ignored. Returns ``{ok, detail}``.

    A Google service-account JSON already contains the project id, so when the
    form submits ONLY the key (no project field), the project is extracted here
    rather than asking the user to type it twice."""
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown cloud target."}
    values = dict(values or {})

    # Google-style JSON keys embed the project id — pull it out when the form
    # didn't submit one (the HTTPS / device-connect panel asks for the key only).
    raw_json = str(values.get("service_account_json") or "").strip()
    if raw_json and not str(values.get("project_id") or "").strip():
        try:
            parsed = json.loads(raw_json)
            pid = str((parsed or {}).get("project_id") or "").strip()
            if pid:
                values["project_id"] = pid
        except Exception:
            pass  # leave the invalid JSON for the credential store to report

    # Split the submitted values: config-field keys → deploy.json (merged over
    # whatever the Deploy card already saved, so machine size / repo survive);
    # credential-field keys → the vault.
    config_keys = {f["key"] for f in p.config_fields}
    cred_keys = {f["key"] for f in p.credential_fields}

    cfg_updates = {k: v for k, v in values.items() if k in config_keys}
    if cfg_updates:
        merged = {**store.get_config(provider_id), **cfg_updates}
        store.save_config(provider_id, merged)
    # Mirror the non-secret shared keys (project id, zone) into the vault too, so
    # the whole cloud connection travels with a shared vault DB — a second device on
    # the same vault inherits the project id + zone, not just the key. (The sign-in
    # panel only submits the connect id, so blank keys keep whatever's stored.)
    mk = _mirror_keys(p)
    if mk:
        await credentials.save_connect(provider_id, mk, values)

    cred_updates = {k: v for k, v in values.items() if k in cred_keys}
    if cred_updates:
        ok = await credentials.save(provider_id, p.credential_fields, cred_updates)
        if not ok:
            return {"ok": False, "detail": "Could not save the cloud key."}

    return {"ok": True, "detail": "Cloud account connected."}


async def disconnect(provider_id: str, forget_config: bool = False) -> Dict[str, Any]:
    """Sign out / remove an account from the Cloud VMs page.

    Always deletes the stored cloud key from the vault. By default the non-secret
    'id' (project) is KEPT so reconnecting only needs the key again ("Sign out").
    With ``forget_config`` the account is fully removed: the connect 'id' keys are
    also cleared from deploy.json so it drops out of the page's account list
    ("Remove account"). Only the connect-config keys are cleared — other Deploy
    settings (machine size, repo) are left intact."""
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown cloud target."}
    await credentials.forget(provider_id)
    if forget_config:
        # Locally, clear only the sign-in id (leave harmless settings like zone/repo
        # in deploy.json). The vault's mirror row holds the whole shared set (id +
        # zone) and is dropped entirely, so the account fully leaves a shared vault DB.
        keys = list(getattr(p, "connect_config_keys", []) or [])
        if keys:
            cfg = dict(store.get_config(provider_id))
            for k in keys:
                cfg.pop(k, None)
            store.save_config(provider_id, cfg)
        if _mirror_keys(p):
            await credentials.forget_connect(provider_id)
        return {"ok": True, "detail": "Account removed — its key and id were cleared."}
    return {"ok": True, "detail": "Signed out — the cloud key was removed from the vault."}


async def run_instance_action(provider_id: str, action: str, zone: str,
                              name: str) -> AsyncIterator[Dict[str, Any]]:
    """Start / Stop / Delete one server, streaming progress. When the acted-on
    server is the one the Deploy card recorded as 'this app', the deployment
    record is updated (stop/start → new state) or cleared (delete) so the Deploy
    card stays in sync."""
    p = get_provider(provider_id)
    if not p or not getattr(p, "supports_instances", False):
        yield done({"ok": False, "message": "This target cannot manage servers."})
        return
    if not await _has_key(p):
        yield done({"ok": False, "message": "No cloud key is stored — add your cloud key to manage servers."})
        return
    config = await _load_config(p)
    creds = await credentials.read(provider_id, p.credential_fields)
    instance = {"name": name, "zone": zone}

    final: Dict[str, Any] = {"ok": False, "message": "No result."}
    try:
        async for event in p.instance_action(config, creds, action, instance):
            if event.get("phase") == "done":
                final = event.get("result", final)
            yield event
    except Exception as e:
        logger.exception("instance action crashed for %s", provider_id)
        yield done({"ok": False, "message": str(e) or "Action failed."})
        return

    # Keep the Deploy card's "last server" line honest when we touched it.
    if final.get("ok"):
        rec = store.get_deployment(provider_id)
        if rec.get("server") and rec.get("server") == name:
            if final.get("deleted") or (action or "").lower() == "delete":
                store.clear_deployment(provider_id)
            elif final.get("new_state"):
                rec["state"] = final["new_state"]
                store.set_deployment(provider_id, rec)
