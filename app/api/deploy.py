"""Deploy admin API: /admin/deploy/*

Backs the Deploy card in App Config → App Settings. Live one-click deploy of
WebAgent onto a cloud target. ALL endpoints are admin-only — deploying spends
money on the admin's cloud account and exposes the (admin-gated) tools, so it is
locked to admins, exactly like Remote Access.

Cloud keys ride in via these endpoints into the encrypted vault
(app/deploy/credentials.py) and are never returned to the browser. The deploy /
tear-down endpoints stream NDJSON progress (like the Commit ⭐ button) so the
panel shows a live log.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.deploy import credentials, manager, store
from app.deploy.registry import get_provider
from app.git_executor import run_git_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/deploy", tags=["admin-deploy"])


async def _require_admin(uid: str) -> None:
    # Honors open mode via the shared chokepoint (mirrors app/admin/remote_access).
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── Models ──
class UserBody(BaseModel):
    requesting_user_id: str = ""


class SelectBody(UserBody):
    provider: str


class ConfigBody(UserBody):
    provider: str
    config: dict = {}


class CredentialsBody(UserBody):
    provider: str
    values: dict = {}


# ── Saved servers (profile-aware targets, e.g. ssh_vm) ──
class ServerSaveBody(UserBody):
    provider: str
    server_id: str = ""            # blank = create a new saved server
    label: str = ""                # the nickname shown in the picker
    values: dict = {}              # config + secret fields from the form


class ServerSelectBody(UserBody):
    provider: str
    server_id: str = ""            # blank = "New server…" (clear the form)


class ServerIdBody(UserBody):
    provider: str
    server_id: str


class ManualCommandBody(UserBody):
    provider: str = "termux"       # any "manual" target: termux | windows | macos
    github_url: str = ""
    visibility: str = "public"     # "public" | "private"
    token: str = ""                # only for a private repo; never stored
    branch: str = ""               # git branch (blank = main); shared with cloud targets
    install_dir: str = ""          # optional folder to install into (blank = default)
    admin_password: str = ""       # optional; when set it is embedded in the command
                                   # (in plain text), never stored server-side
    persist: bool = False          # save the non-secret URL+visibility+folder (on
                                   # blur, NOT on every live-preview keystroke)
    embed_config: bool = False     # "Include this app's configuration": pack this
                                   # install's AI/vault/database into the command
    embed_sections: list = []      # which sections to include (llm/database/vault)


# ── Local deployments (this app + sibling checkouts) — see the instances section ──
class InstFolderBody(UserBody):
    folder: str = ""


class InstAddBody(UserBody):
    label: str = ""
    folder: str = ""
    port: int = 0


class InstUpdateBody(UserBody):
    id: str
    label: str = ""
    folder: str = ""
    port: int = 0


class InstIdBody(UserBody):
    id: str


class HubPortBody(UserBody):
    port: int = 0


# ── Catalog (status) ──
@router.get("/catalog")
async def catalog(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return await manager.build_catalog()


# ── Which target is selected ──
@router.post("/select")
async def select(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    store.set_active_provider(body.provider)
    return {"ok": True, "active_provider": body.provider}


# ── Save non-secret settings ──
@router.post("/config")
async def save_config(body: ConfigBody):
    await _require_admin(body.requesting_user_id)
    # "_repo" is a reserved, provider-less slot that holds the SHARED repo details
    # (repo URL + public/private) the New-deployment panel carries into every target
    # — a UI-prefill store only, never a deploy target. Non-secret, so it rides the
    # same deploy.json config store as the real targets.
    if body.provider != "_repo" and not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    # Routes through manager (not store) so a target's sign-in 'id' fields (the
    # project id) are also mirrored into the vault, letting the whole cloud
    # connection travel with a shared vault DB.
    await manager.save_config(body.provider, body.config or {})
    return {"ok": True}


# ── Clone the current repo (prefill the shared Repo-details bar) ──
# "Clone current repo" on the Repo-details bar fills the deploy repo fields from
# whatever repo the Git Control page is pointed at (git_repos.get_active_full):
# its origin URL, current branch, and — when it has a stored GitHub token — the
# token + Private visibility, so a private fork can be deployed without re-typing
# anything. The token is returned ONLY to prefill the (masked, never-persisted)
# token field the admin would otherwise type by hand; the endpoint is admin-gated.
def _normalize_clone_url(raw: str) -> str:
    """Best-effort turn a git origin URL into an https clone URL for the deploy
    command: SSH form (git@github.com:owner/repo.git) → https, drop any embedded
    credentials and a trailing ``.git``. Blank / unparseable → ''."""
    u = (raw or "").strip()
    if not u:
        return ""
    m = re.match(r"^[\w.+-]+@([^:]+):(.+)$", u)          # git@host:owner/repo(.git)
    if m:
        u = f"https://{m.group(1)}/{m.group(2)}"
    u = re.sub(r"^(https?://)[^/@]+@", r"\1", u)          # strip https://user:tok@host
    if u.endswith(".git"):
        u = u[:-4]
    return u


def _gh_owner_repo(url: str):
    """Pull (owner, repo) out of a GitHub URL, or None if it isn't one."""
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)/?$", _normalize_clone_url(url))
    return (m.group(1), m.group(2)) if m else None


# ── Repository access auto-detection ──
# Backs the Repo-details bar's public/private auto-detect (the manual dropdown is
# gone). We quietly ask GitHub about the entered URL so the UI can show a status
# without the admin picking one by hand. GitHub deliberately answers 404 for BOTH a
# private repo and a wrong address when unauthenticated, so "not public" can only
# ever mean "unknown — add a token", never a confirmed "private". The token, when
# supplied, is used for this one check and never stored.
class ProbeRepoBody(UserBody):
    github_url: str = ""
    token: str = ""


@router.post("/probe-repo")
async def probe_repo(body: ProbeRepoBody):
    await _require_admin(body.requesting_user_id)
    parts = _gh_owner_repo(body.github_url)
    if not parts:
        return {"ok": True, "state": "unknown", "detail": "That doesn’t look like a GitHub URL."}
    owner, repo = parts
    # A typed token wins; when the field is blank we fall back to the reusable, vault-
    # stored GitHub key so a private repo the admin has already saved a key for is
    # confirmed without re-pasting. `used_saved` tells the UI the stored key did the
    # work (so it can show "using your saved key"); `has_saved` lets it offer to save
    # a freshly-typed key. The key itself is NEVER returned.
    typed = (body.token or "").strip()
    saved = await credentials.get_github_token()
    token = typed or saved
    used_saved = bool(not typed and saved)
    base = {"has_saved": bool(saved), "used_saved": used_saved}
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "webagent-deploy"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
    except Exception:  # noqa: BLE001
        return {"ok": True, "state": "error", "detail": "GitHub didn’t respond — couldn’t check.", **base}

    if r.status_code == 200:
        try:
            private = bool(r.json().get("private"))
        except Exception:  # noqa: BLE001
            private = False
        # A private repo can only return 200 when the token could read it.
        return {"ok": True, "state": "authenticated" if private else "public", "private": private, **base}
    if r.status_code == 401:
        return {"ok": True, "state": "denied", **base}    # the token itself is bad
    if r.status_code == 403:
        return {"ok": True, "state": "error", "detail": "GitHub rate limit reached — try again shortly.", **base}
    if r.status_code == 404:
        # 404 with a token → the token can't reach it; without → private-or-wrong-URL.
        return {"ok": True, "state": "denied" if token else "unknown", **base}
    return {"ok": True, "state": "error", "detail": f"GitHub returned HTTP {r.status_code}.", **base}


# ── Reusable GitHub key (save / status / clear) ──
# The one encrypted GitHub token the whole deploy section falls back to for cloning
# private repos (see credentials.get_github_token + probe-repo / _build_manual_command
# / the credentials.read github_fallback). Stored in the vault like every other
# secret; the key itself is never returned — only whether one is set + a masked hint.
class GithubTokenBody(UserBody):
    token: str = ""


@router.get("/github-token")
async def github_token_get(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return {"ok": True, **(await credentials.github_token_status())}


@router.post("/github-token")
async def github_token_save(body: GithubTokenBody):
    await _require_admin(body.requesting_user_id)
    if not (body.token or "").strip():
        raise HTTPException(status_code=400, detail="Enter a token to save.")
    if not await credentials.save_github_token(body.token):
        raise HTTPException(status_code=500, detail="Could not save the GitHub key.")
    return {"ok": True, **(await credentials.github_token_status())}


@router.post("/github-token/clear")
async def github_token_clear(body: UserBody):
    await _require_admin(body.requesting_user_id)
    await credentials.clear_github_token()
    return {"ok": True, "configured": False, "masked": ""}


@router.get("/current-repo")
async def current_repo(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    from pathlib import Path

    from app import git_repos
    def _repo_snapshot():
        entry = git_repos.get_active_full()
        branch = ""
        try:
            out, _, rc = git_repos._git(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=Path(entry.get("folder") or "."),
            )
            if rc == 0 and out.strip() and out.strip() != "HEAD":
                branch = out.strip()
        except Exception:  # noqa: BLE001
            pass
        return entry, branch

    entry, branch = await run_git_job(_repo_snapshot)
    url = _normalize_clone_url(entry.get("remote_url") or "")
    # The token pre-filled by "Clone current repo" must BELONG to the active repo:
    # a registered repo carries its own key; the built-in (this-app) repo uses the
    # shared Git-page key in provider.json. We deliberately do NOT fall back to the
    # production-mirror vault key here — that key is scoped to the *production* repo,
    # so pasting it for a different active repo would fail its own access check.
    token = entry.get("token") or ""
    return {
        "ok": bool(url),
        "label": entry.get("label") or "",
        "github_url": url,
        "branch": branch,
        # A stored token implies a private repo (public clones need none), so default
        # the visibility switch accordingly; the admin can still flip it.
        "visibility": "private" if token else "public",
        "has_token": bool(token),
        "token": token,
    }


# ── Save / clear cloud keys (vault) ──
@router.post("/credentials")
async def save_credentials(body: CredentialsBody):
    await _require_admin(body.requesting_user_id)
    p = get_provider(body.provider)
    if not p:
        raise HTTPException(status_code=400, detail="Unknown target")
    ok = await credentials.save(body.provider, p.credential_fields, body.values or {})
    if not ok:
        raise HTTPException(status_code=500, detail="Could not save credentials")
    view = await credentials.public_view(body.provider, p.credential_fields, p.credential_required)
    return {"ok": True, "configured": view["configured"], "credentials_set": view["secrets_set"]}


@router.post("/credentials/clear")
async def clear_credentials(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    p = get_provider(body.provider)
    if not p:
        raise HTTPException(status_code=400, detail="Unknown target")
    await credentials.forget(body.provider)
    return {"ok": True}


# ── Test connection ──
@router.post("/test")
async def test(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    p = get_provider(body.provider)
    if not p:
        raise HTTPException(status_code=400, detail="Unknown target")
    ok_avail, reason = p.available()
    if not ok_avail:
        return {"ok": False, "detail": reason or "This target is unavailable here."}
    config = {**{f["key"]: f.get("default") for f in p.config_fields if "default" in f},
              **store.get_config(body.provider)}
    creds = await credentials.read(body.provider, p.credential_fields)
    try:
        return await p.test(config, creds)
    except Exception as e:
        logger.exception("deploy test failed for %s", body.provider)
        return {"ok": False, "detail": str(e)}


# ── Saved servers (profile-aware targets: the SSH one) ──
# Let an admin keep several servers as pick-from-a-dropdown profiles. Saving/
# selecting/deleting a server manages the profile store + its vault row; selecting
# also loads it into the working slot so a following Deploy / Test / Tear-down acts
# on it (those endpoints are unchanged — they always use "whatever is loaded").
@router.post("/servers/save")
async def servers_save(body: ServerSaveBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.save_server(body.provider, body.label, body.values or {}, body.server_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("detail", "Could not save the server."))
    return res


@router.post("/servers/select")
async def servers_select(body: ServerSelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.select_server(body.provider, body.server_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("detail", "Could not load the server."))
    return res


@router.post("/servers/delete")
async def servers_delete(body: ServerIdBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return await manager.delete_server(body.provider, body.server_id)


# ── Manual install (Linux/Termux, Windows, macOS) — build the command + QR ──
# Each "manual" target gets its OWN bespoke row in the Deploy card (not the
# cloud-provider dropdown): the admin gives a GitHub URL + public/private (+ a
# token for private), and we hand back the one command to paste into a terminal /
# PowerShell plus a QR of that command. The row shows the command LIVE as they
# type and calls this on every (debounced) change, so it must never error — a
# blank URL / token comes back as a placeholder command. The token is used only
# to build the command (never stored); the non-secret github_url + visibility
# persist only when ``persist`` is set (on blur), so keystrokes don't churn config.
#
# This is generic over ANY manual provider (it dispatches on ``body.provider``):
# a new platform = a new drop-in provider with a ``build_command``, no edit here.
async def _build_manual_command(body: ManualCommandBody):
    await _require_admin(body.requesting_user_id)
    provider_id = body.provider or "termux"
    p = get_provider(provider_id)
    if not p or not hasattr(p, "build_command") or not getattr(p, "manual", False):
        raise HTTPException(status_code=400, detail="Unknown install target")

    # "Include this app's configuration": gather THIS install's AI/vault/database
    # into a keyless WABOOT2 code and splice it into the command (as
    # WA_BOOTSTRAP_CODE) so the new device self-provisions on first boot. The
    # SERVER holds those secrets — the browser cannot build this, so the row fetches
    # it from here whenever the toggle is on. No admin password is needed (it's
    # decoupled now — see app/deploy/config_embed); an empty include only warns
    # (the code is skipped and the command falls back to a bare install).
    bootstrap_code = ""
    embed_warning = ""
    if body.embed_config:
        from app.deploy import config_embed
        bootstrap_code, embed_warning = await config_embed.build_embed_code(
            {"embed_config": True, "embed_sections": body.embed_sections},
        )

    # A private install needs a token to clone with. Prefer the one typed into the
    # bar; when it's blank, borrow the reusable, vault-stored GitHub key so the built
    # command carries a working token without re-pasting it every time.
    token = (body.token or "").strip()
    if not token and (body.visibility or "").strip() == "private":
        token = await credentials.get_github_token()
    plan = p.build_command(body.github_url, body.visibility, token,
                           branch=body.branch, install_dir=body.install_dir,
                           admin_password=body.admin_password,
                           bootstrap_code=bootstrap_code)
    if body.persist:
        # Persist only the NON-secret choices so the row pre-fills next time. The
        # admin PASSWORD is never stored (it only ever lives in the shown command).
        store.save_config(provider_id, {"github_url": (body.github_url or "").strip(),
                                        "visibility": body.visibility or "public",
                                        "install_dir": (body.install_dir or "").strip()})
    embedded = bool(bootstrap_code)
    # A QR can't hold an embedded configuration bundle (it's kilobytes), so skip it
    # when one is present — the row tells the admin to use Copy instead.
    qr_svg = None
    if not embedded:
        try:
            from app.remote_access import netinfo
            qr_svg = netinfo.qr_svg(plan["command"])
        except Exception:
            qr_svg = None
    # Surface any embed skip-reason alongside the command's own warning line.
    warning = " ".join(w for w in (plan.get("warning", ""), embed_warning) if w)
    return {
        "ok": True,
        "command": plan["command"],
        "qr_svg": qr_svg,
        "steps": plan.get("steps", []),
        "instructions": plan.get("instructions", ""),
        "reach_note": plan.get("reach_note", ""),
        "run_command": plan.get("run_command", ""),
        "install_dir": plan.get("install_dir", ""),
        "private": plan.get("private", False),
        "default_repo": plan.get("default_repo", False),
        "placeholder_token": plan.get("placeholder_token", False),
        "prewire": plan.get("prewire", False),
        "embedded": embedded,
        "warning": warning,
    }


@router.post("/command")
async def manual_command(body: ManualCommandBody):
    return await _build_manual_command(body)


# Back-compat alias for the original Termux-only route (older cached clients).
@router.post("/termux/command")
async def termux_command(body: ManualCommandBody):
    body.provider = body.provider or "termux"
    return await _build_manual_command(body)


# ── Deploy / tear down (streaming NDJSON) ──
def _stream(agen):
    async def _gen():
        try:
            async for evt in agen:
                yield json.dumps(evt) + "\n"
        except Exception as e:  # never leave the client's reader hanging
            logger.exception("deploy stream crashed")
            yield json.dumps({"phase": "done", "result": {"ok": False, "message": str(e) or "failed"}}) + "\n"
    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/deploy")
async def deploy(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return _stream(manager.run_deploy(body.provider))


@router.post("/destroy")
async def destroy(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return _stream(manager.run_destroy(body.provider))


# ── Local deployments (this app + sibling checkouts on this machine) ──────────
# The Deploy card's "Current deployment" list. Backed by app/local_instances.py:
# this app (the hub) plus any OTHER WebAgent repo folders registered to run as
# background servers on their own ports (8081, 8082, …). Same registry + engine the
# removed Dashboard instance-switcher header used; these are the Deploy card's own thin,
# admin-gated wrappers over it (self-contained, so the card doesn't depend on any
# other page's router). Start/Stop stream NDJSON like Deploy / Tear-down.
@router.get("/instances")
async def instances_list(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    from app import local_instances as li
    return {"instances": await li.list_instances(), "hub_port": li.hub_port()}


@router.post("/instances/validate")
async def instances_validate(body: InstFolderBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return li.validate_folder(body.folder)


@router.post("/instances/add")
async def instances_add(body: InstAddBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        return {"ok": True, "instance": li.add_instance(body.label, body.folder, body.port)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/update")
async def instances_update(body: InstUpdateBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        return {"ok": True, "instance": li.update_instance(
            body.id, label=body.label, folder=body.folder, port=body.port)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/remove")
async def instances_remove(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        li.remove_instance(body.id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/start")
async def instances_start(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return _stream(li.start_instance(body.id))


@router.post("/instances/stop")
async def instances_stop(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return _stream(li.stop_instance(body.id))


# Change the CURRENT app's own port. Persists the new port (run.py reads it on the
# next start) and relaunches this process so it rebinds — the browser is on the OLD
# port, so the response tells the caller where to reopen. The relauncher waits for
# us to exit, then brings us back on the new port (or a supervisor does — run.py
# reads the same saved port either way).
@router.post("/instances/set-hub-port")
async def instances_set_hub_port(body: HubPortBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        new_port = li.set_hub_port(body.port)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result = {"ok": True, "port": new_port, "url": f"http://localhost:{new_port}/"}
    try:
        from app import relauncher
        restart = relauncher.trigger_restart(port=new_port)
        result["restart"] = restart
        result["auto_restart"] = bool(restart.get("auto_restart"))
    except Exception as e:  # noqa: BLE001  — saved anyway; just couldn't self-relaunch
        logger.exception("hub-port relaunch failed")
        result["auto_restart"] = False
        result["restart"] = {"status": "error", "reason": str(e)}
    return result
