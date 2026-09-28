"""
Visualizer module -- Gen UI workspace.

Injects the following tools into the agent's tool registry:
  render_visual   -- write HTML to a named genui
  list_genui   -- list all genui for the current user
  create_genui   -- create a new named genui
  delete_genui   -- delete a genui (home is protected)
  get_genui      -- read the current HTML content of a genui
  rename_genui   -- rename a genui's display title
  get_genui_data -- read a genui's data bag (content kept separate from markup)
  set_genui_data -- update a genui's data bag WITHOUT rewriting its page markup
  get_genui_logs -- read a genui's OWN console output (page-scoped; no logs.db needed)
  check_credential -- is an ability connected (vault) + what a login form needs (no secrets)
  request_credential -- ask the user for a NEW secret (secure card → vault), get a key id back
  list_vault_keys -- list the user's vault keys (id/name/service/filled) — never any value
  refresh_genui   -- push a live reload of a genui to the browser (call after set_genui_data)

Call register_tools(tools, user_id) from app/tools/loader.py.
"""
import json
from typing import Dict
from app.tools.loader import ToolInfo


def register_tools(tools: Dict[str, ToolInfo], user_id: str, agent_id: str = "") -> None:
    """Inject visualizer tools into the tools dict.

    ``agent_id`` is the agent these tools are being built for; render_visual /
    create_genui record it as the genui's owning agent (so the Gen UI footer
    can name + chat to the agent that made each genui)."""

    from app.visualizer.tool import render_visual as _render_visual

    # present_chat_component / present_plan → RELOCATED to the always-on core:
    # app/tools/core_tools.py, wired by app/tools/loader.py and listed under the
    # virtual Core ▸ Base row. Neither is a Gen UI tool (both are plain chat-
    # component/harness presenters), and present_plan is REQUIRED by Plan mode's
    # mode contract — so it must never depend on this optional ability being on.

    from app.visualizer.edit import edit_genui as _edit_genui
    from app.visualizer.genui import (
        list_genui as _list_genui,
        create_genui as _create_genui,
        delete_genui as _delete_genui,
        get_genui_html as _get_genui_html,
        rename_genui as _rename_genui,
        get_genui_data as _get_genui_data,
        save_genui_data as _save_genui_data,
        read_genui_logs as _read_genui_logs,
    )

    # -- render_visual ---------------------------------------------------------
    # render_visual is the always-available FALLBACK (a full-page make) that weaker
    # models can do even when surgical edit_genui matching is beyond them — so it
    # must never hard-crash on the arg quirks those models produce. Several hidden
    # aliases map to ``slug`` (``genui_name``/``page_name``/``name``/``genui``),
    # and ``**_extra`` swallows any other stray keyword a model invents instead of
    # raising "unexpected keyword argument" (which is exactly what aborted a real
    # v6 build). None of these are advertised in the schema, so the model is still
    # steered to ``html`` + ``slug``; this is purely a crash-proofing net.
    async def _render_visual_wrapper(html: str, title: str = "", slug: str = "home",
                                     genui_name: str = None, page_name: str = None,
                                     name: str = None, genui: str = None,
                                     session_target_name: str = "", session_mode: str = "",
                                     session_id: str = "", **_extra):
        alias = genui_name or page_name or name or genui
        return await _render_visual(
            html=html,
            title=title,
            slug=slug,
            genui_name=alias,
            user_id=user_id,
            agent_id=agent_id,
            session_target_name=session_target_name,
            session_mode=session_mode,
            session_id=session_id,
        )

    tools["render_visual"] = ToolInfo(
        name="render_visual",
        handler=_render_visual_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": (
                        "Full HTML document string to render. "
                        "For p5.js sketches, include the p5.js CDN script tag. "
                        "For regular genui, standard HTML/CSS/JS is fine."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title for the genui (shown in the UI).",
                },
                "slug": {
                    "type": "string",
                    "description": (
                        "Slug of the genui to write to (e.g. 'home', 'dashboard', 'notes'). "
                        "Pass the slug from the `Gen UI: \"<slug>\"` hand-off tag — it MUST "
                        "match the genui the user is viewing, or your work lands on the wrong "
                        "genui. Only omit it for the 'home' genui (the default)."
                    ),
                },
                "session_target_name": {
                    "type": "string",
                    "description": (
                        "REQUIRED when this render CREATES a brand-new genui (the slug "
                        "doesn't exist yet): the title of the deployed agent session that "
                        "this page's actions/chat dispatch into (e.g. 'Code Index — scan'). "
                        "For an existing genui, omit — the page keeps its saved config."
                    ),
                },
                "session_mode": {
                    "type": "string",
                    "enum": ["new_reuse", "new_each", "existing"],
                    "description": (
                        "REQUIRED when creating a new genui: new_reuse (first action "
                        "creates a session, follow-ups reuse it — the default behaviour), "
                        "new_each (every action starts a fresh session), or existing "
                        "(always dispatch into session_id)."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": "Required iff session_mode is 'existing': the session id to dispatch into.",
                },
            },
            "required": ["html"],
        },
    )

    # -- edit_genui -----------------------------------------------------------
    # Surgical find/replace edits to an existing genui — the alternative to
    # re-rendering the whole page for a small change. Routes through the same
    # save_genui_html path as render_visual (so the live refresh fires) and
    # returns the same path/slug shape the frontend reload handler reads.
    async def _edit_genui_wrapper(slug: str, edits=None, find: str = None,
                                   replace: str = None, replace_all: bool = False):
        return await _edit_genui(
            slug=slug, edits=edits, find=find, replace=replace,
            replace_all=replace_all, user_id=user_id, agent_id=agent_id,
        )

    tools["edit_genui"] = ToolInfo(
        name="edit_genui",
        handler=_edit_genui_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui to edit. Read it first with get_genui('<slug>').",
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "One or more find/replace edits, applied in order. PREFER this over "
                        "re-rendering the whole page when changing an existing genui. Each "
                        "`find` must be copied EXACTLY from the current genui (read it with "
                        "get_genui first — whitespace and tags must match) and must be unique "
                        "unless replace_all is true. If any edit's `find` is missing or "
                        "ambiguous, NOTHING is saved and the genui is left unchanged."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {
                                "type": "string",
                                "description": "Exact existing text to locate. Include enough surrounding context to be unique.",
                            },
                            "replace": {
                                "type": "string",
                                "description": "Text to put in its place. Use an empty string to delete the found text.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace every occurrence instead of requiring a single unique match. Default false.",
                            },
                        },
                        "required": ["find", "replace"],
                    },
                },
            },
            "required": ["slug", "edits"],
        },
    )

    # -- list_genui ---------------------------------------------------------
    async def _list_genui_wrapper():
        genui = await _list_genui(user_id)
        return json.dumps({"status": "ok", "genui": genui, "count": len(genui)})

    tools["list_genui"] = ToolInfo(
        name="list_genui",
        handler=_list_genui_wrapper,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    # -- get_genui ------------------------------------------------------------
    async def _get_genui_wrapper(slug: str):
        html = await _get_genui_html(user_id=user_id, slug=slug)
        if html is None:
            return json.dumps({"status": "error", "message": "Gen UI '{}' not found.".format(slug)})
        return json.dumps({
            "status": "ok",
            "slug": slug,
            "html": html,
            "size_bytes": len(html),
        })

    tools["get_genui"] = ToolInfo(
        name="get_genui",
        handler=_get_genui_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui to read (e.g. 'home', 'dashboard', 'notes').",
                },
            },
            "required": ["slug"],
        },
    )

    # -- create_genui ---------------------------------------------------------
    async def _create_genui_wrapper(
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
        session_target_name: str = "",
        session_mode: str = "",
        session_id: str = "",
    ):
        # REQUIRED session contract (mirrors the genui REST API): every genui
        # must declare the deployed session's title + new-session behaviour.
        name = (session_target_name or "").strip()
        if not name:
            return json.dumps({
                "status": "error",
                "message": (
                    "session_target_name is required — the deployed agent "
                    "session's title (e.g. 'Code Index — scan'). No genui created."
                ),
            })
        if session_mode not in ("new_reuse", "new_each", "existing"):
            return json.dumps({
                "status": "error",
                "message": (
                    "session_mode is required and must be one of: new_reuse, "
                    "new_each, existing. No genui created."
                ),
            })
        cfg: dict = {"target_name": name, "mode": session_mode}
        if session_mode == "existing":
            if not (session_id or "").strip():
                return json.dumps({
                    "status": "error",
                    "message": (
                        "session_mode is 'existing', so session_id is required. "
                        "No genui created."
                    ),
                })
            cfg["session_id"] = (session_id or "").strip()
        try:
            entry = await _create_genui(
                user_id=user_id,
                slug=slug,
                title=title,
                agent_context=agent_context,
                initial_html=initial_html,
                agent_id=agent_id,
                session_config=cfg,
            )
            return json.dumps({"status": "ok", "genui": entry})
        except ValueError as e:
            return json.dumps({"status": "error", "message": str(e)})

    tools["create_genui"] = ToolInfo(
        name="create_genui",
        handler=_create_genui_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "URL-safe identifier for the genui (e.g. 'dashboard', 'notes'). Lowercase, no spaces.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable display name for the genui (e.g. 'My Dashboard').",
                },
                "agent_context": {
                    "type": "string",
                    "description": (
                        "Optional system prompt / persona for this genui's agent. "
                        "Describes the agent's role and the genui's purpose. "
                        "If omitted, a default context is generated from the title."
                    ),
                },
                "initial_html": {
                    "type": "string",
                    "description": "Optional initial HTML to seed the genui with. If omitted, a blank placeholder is used.",
                },
                "session_target_name": {
                    "type": "string",
                    "description": (
                        "REQUIRED — the title of the deployed agent session that this "
                        "genui's actions/chat dispatch into (e.g. 'Code Index — scan'). "
                        "This is the name that appears in the session sidebar, so make "
                        "it descriptive and specific."
                    ),
                },
                "session_mode": {
                    "type": "string",
                    "enum": ["new_reuse", "new_each", "existing"],
                    "description": (
                        "REQUIRED — new_reuse (default): first action creates a session, "
                        "follow-ups reuse it; new_each: every action starts a fresh "
                        "session; existing: always dispatch into the given session_id."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": "Required iff session_mode is 'existing': the session id to dispatch into.",
                },
            },
            "required": ["slug", "title", "session_target_name", "session_mode"],
        },
    )

    # -- delete_genui ---------------------------------------------------------
    async def _delete_genui_wrapper(slug: str):
        ok = await _delete_genui(user_id=user_id, slug=slug)
        if ok:
            return json.dumps({"status": "ok", "message": "Gen UI '{}' deleted.".format(slug)})
        if slug == "home":
            return json.dumps({"status": "error", "message": "The home genui cannot be deleted."})
        return json.dumps({"status": "error", "message": "Gen UI '{}' not found.".format(slug)})

    tools["delete_genui"] = ToolInfo(
        name="delete_genui",
        handler=_delete_genui_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui to delete. The 'home' genui cannot be deleted.",
                },
            },
            "required": ["slug"],
        },
    )

    # -- rename_genui ---------------------------------------------------------
    async def _rename_genui_wrapper(slug: str, title: str,
                                    session_target_name: str = "",
                                    session_mode: str = "",
                                    session_id: str = ""):
        # Optional session-contract update: PATCH may change title, config, or both.
        session_config = None
        if session_target_name or session_mode or session_id:
            name = (session_target_name or "").strip()
            if not name:
                return json.dumps({"status": "error", "message": "session_target_name cannot be empty."})
            if session_mode not in ("new_reuse", "new_each", "existing"):
                return json.dumps({"status": "error", "message": "session_mode must be one of: new_reuse, new_each, existing."})
            cfg: dict = {"target_name": name, "mode": session_mode}
            if session_mode == "existing":
                if not (session_id or "").strip():
                    return json.dumps({"status": "error", "message": "session_id is required when session_mode is 'existing'."})
                cfg["session_id"] = (session_id or "").strip()
            session_config = cfg
        ok = await _rename_genui(user_id=user_id, slug=slug, new_title=title,
                                 session_config=session_config)
        if ok:
            return json.dumps({"status": "ok", "message": "Gen UI '{}' renamed to '{}'.".format(slug, title),
                               "session_config": session_config})
        return json.dumps({"status": "error", "message": "Gen UI '{}' not found.".format(slug)})

    tools["rename_genui"] = ToolInfo(
        name="rename_genui",
        handler=_rename_genui_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui to rename (e.g. 'dashboard', 'notes').",
                },
                "title": {
                    "type": "string",
                    "description": "New human-readable display title for the genui.",
                },
                "session_target_name": {
                    "type": "string",
                    "description": "Optional new session target name (the deployed session's title).",
                },
                "session_mode": {
                    "type": "string",
                    "enum": ["new_reuse", "new_each", "existing"],
                    "description": "Optional new session mode: new_reuse, new_each, existing.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Required iff session_mode is 'existing': the session id to dispatch into.",
                },
            },
            "required": ["slug", "title"],
        },
    )

    # -- get_genui_data -------------------------------------------------------
    # Read a genui's DATA bag (its content — the records the page renders),
    # which lives in data.json separately from the page markup. Read this before
    # set_genui_data when you only want to change part of the content.
    async def _get_genui_data_wrapper(slug: str):
        data = await _get_genui_data(user_id=user_id, slug=slug)
        return json.dumps({"status": "ok", "slug": slug, "data": data or {}})

    tools["get_genui_data"] = ToolInfo(
        name="get_genui_data",
        handler=_get_genui_data_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui whose data bag to read (e.g. 'home', 'dashboard').",
                },
            },
            "required": ["slug"],
        },
    )

    # -- set_genui_data -------------------------------------------------------
    # Write a genui's DATA bag WITHOUT touching its page markup — this is how you
    # update a dashboard's content (add a student, move a lesson, change a row).
    # The page reads it via api.getData(); the change shows on next load/refresh.
    # `merge=true` shallow-merges your top-level keys into the existing data
    # (change one section, leave the rest); `merge=false` (default) replaces the
    # whole bag. PREFER this over render_visual/edit_genui for data-only changes.
    async def _set_genui_data_wrapper(slug: str, data=None, merge: bool = False, **_extra):
        if not isinstance(data, dict):
            return json.dumps({"status": "error", "message": "`data` must be a JSON object."})
        if merge:
            existing = await _get_genui_data(user_id=user_id, slug=slug)
            base = dict(existing) if isinstance(existing, dict) else {}
            base.update(data)
            data = base
        # ── Chat-input session contract gate ──────────────────────────────────
        # Adding a chat input to a genui (chatConfig.button or chatConfig.toolbar)
        # REQUIRES that input to declare its session target name + new-session
        # mode — the same anti-confusing-names rule as genui creation. An input
        # without a session config is refused with a clear error.
        cc = data.get("chatConfig") if isinstance(data, dict) else None
        if isinstance(cc, dict):
            for _kind in ("button", "toolbar"):
                _input = cc.get(_kind)
                if not isinstance(_input, dict):
                    continue
                _sc = _input.get("session")
                if not isinstance(_sc, dict) or not (_sc.get("target_name") or "").strip():
                    return json.dumps({
                        "status": "error",
                        "message": (
                            f"chatConfig.{_kind} requires a session config — every "
                            "chat input must declare how its messages deploy into "
                            "agent sessions. Add `\"session\": {{\"target_name\": "
                            "\"<deployed session title>\", \"mode\": \"new_reuse\"}}` "
                            "(mode: new_reuse | new_each | existing; session_id "
                            "required for existing). No data saved."
                        ),
                    })
                if _sc.get("mode") not in ("new_reuse", "new_each", "existing"):
                    return json.dumps({
                        "status": "error",
                        "message": (
                            f"chatConfig.{_kind}.session.mode must be one of: "
                            "new_reuse, new_each, existing. No data saved."
                        ),
                    })
                if _sc.get("mode") == "existing" and not (_sc.get("session_id") or "").strip():
                    return json.dumps({
                        "status": "error",
                        "message": (
                            f"chatConfig.{_kind}.session.mode is 'existing', so "
                            "session.session_id is required. No data saved."
                        ),
                    })
        await _save_genui_data(user_id=user_id, slug=slug, data=data)
        return json.dumps({"status": "ok", "slug": slug, "merged": bool(merge), "keys": list(data.keys())})

    tools["set_genui_data"] = ToolInfo(
        name="set_genui_data",
        handler=_set_genui_data_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui whose data to write (e.g. 'home', 'dashboard').",
                },
                "data": {
                    "type": "object",
                    "description": (
                        "The genui's content as a JSON object (e.g. "
                        "{\"students\": [...], \"lessons\": {...}}). The page reads "
                        "these via api.getData(). Use the SAME key names the page expects. "
                        "NOTE: any chatConfig.button / chatConfig.toolbar entry MUST "
                        "include a session config: {\"session\": {\"target_name\": "
                        "\"<deployed session title>\", \"mode\": \"new_reuse\"}} "
                        "(mode: new_reuse | new_each | existing; session_id required "
                        "for existing)."
                    ),
                },
                "merge": {
                    "type": "boolean",
                    "description": (
                        "If true, shallow-merge these top-level keys into the existing "
                        "data (change one section, keep the rest). If false (default), "
                        "replace the entire data bag."
                    ),
                },
            },
            "required": ["slug", "data"],
        },
    )

    # -- get_genui_logs -------------------------------------------------------
    # Read the page's OWN console output — the console.log/info/warn/error and
    # uncaught script errors the genui produced while running, captured per-page in a
    # file beside the genui (NOT the global logs.db). A page-scoped debug log the agent
    # reads with no codebase-admin access: build a genui, then read its errors and fix
    # them. Auto-cleared on every re-render, so it reflects the version now running.
    # TWO sources fill it: (a) a LIVE user session in the Gen UI tab (entries arrive a
    # moment after the page mounts; includes errors from the user's own clicks), and
    # (b) screenshot_genui's HEADLESS render (tagged source:'headless') — so calling
    # screenshot_genui after a build populates these logs immediately, no live session
    # needed. Each entry carries `source` ('headless' or absent = live).
    async def _get_genui_logs_wrapper(slug: str, level: str = None, limit: int = 100):
        try:
            lim = int(limit) if limit else 100
        except (TypeError, ValueError):
            lim = 100
        lim = max(1, min(lim, 500))
        lvl = (level or "").strip().lower() or None
        logs = _read_genui_logs(user_id=user_id, slug=slug, limit=lim, level=lvl)
        n_err = sum(1 for r in logs if str(r.get("level")) == "error")
        n_warn = sum(1 for r in logs if str(r.get("level")) == "warn")
        note = (
            "Empty means nothing has run+logged for this version yet. Either render it "
            "with screenshot_genui (populates these logs headlessly, source:'headless') "
            "or have the user open it in the Gen UI tab, then read again." if not logs else
            "Newest entries last. Each: {ts, level, text, stack?, source?}. source:'headless' "
            "is from a screenshot_genui render; no source = a live user session. A 'Cannot "
            "read properties of null' is almost always the shadow-scope bug — query through "
            "the mount `root`, not document.*."
        )
        return json.dumps({
            "status": "ok",
            "slug": slug,
            "count": len(logs),
            "errors": n_err,
            "warnings": n_warn,
            "logs": logs,
            "note": note,
        })

    tools["get_genui_logs"] = ToolInfo(
        name="get_genui_logs",
        handler=_get_genui_logs_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui whose console output to read (e.g. 'home', 'dashboard').",
                },
                "level": {
                    "type": "string",
                    "description": (
                        "Optional. Filter to one level: 'error', 'warn', 'log', 'info', or "
                        "'debug'. Omit to get all levels. Use 'error' to see only failures."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of most-recent entries to return (default 100, max 500).",
                },
            },
            "required": ["slug"],
        },
    )

    # -- refresh_genui ---------------------------------------------------------
    # Push a live reload of a genui to the browser — the frontend sees the tool
    # result (same shape as render_visual) and re-fetches + remounts the page,
    # which bakes the latest data bag into the HTML automatically.  Call this
    # AFTER set_genui_data to show the user the updated content immediately.
    async def _refresh_genui_wrapper(slug: str, **_extra):
        from app.genui_store import get_genui_store
        from app.genui_store.common import genui_url
        store = get_genui_store()
        pages = await store.list_genui(user_id)
        page = next((p for p in pages if p.get("slug") == slug), None)
        if page is None:
            return json.dumps({"status": "error", "message": f"Gen UI '{slug}' not found."})
        url_path = page.get("url", "")
        if not url_path:
            url_path = genui_url(user_id, slug)
        return json.dumps({
            "status": "ok",
            "path": url_path,
            "slug": slug,
            "title": page.get("title", slug),
            "refreshed": True,
            "note": (
                "The genui '{}' is being reloaded in the browser. Its data bag is "
                "baked in at serve time, so any set_genui_data call before this "
                "tool is now visible to the user."
            ).format(slug),
        })

    tools["refresh_genui"] = ToolInfo(
        name="refresh_genui",
        handler=_refresh_genui_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the genui to refresh in the browser (e.g. 'home', 'dashboard', 'production-readiness').",
                },
            },
            "required": ["slug"],
        },
    )

    # -- check_credential ------------------------------------------------------
    # Lets a genui be "linked to the vault": the design agent asks whether the
    # user has already connected an ability (e.g. browser_control / a login) and
    # what fields a connect form should collect — so it can render "Connected ✓"
    # vs a login form, WITHOUT ever seeing any secret value. Reads the same
    # encrypted vault the Abilities → Credentials panel writes to.
    async def _check_credential_wrapper(ability: str = "browser_control"):
        from app.abilities import credentials as _creds
        view = await _creds.public_view(ability, user_id=user_id)
        if view is None:
            return json.dumps({
                "status": "error",
                "message": "Ability '{}' has no credentials block to link to.".format(ability),
            })
        # public_view already excludes secret VALUES — pass through only the
        # safe shape (configured flag, field schema, which secrets are set).
        return json.dumps({
            "status": "ok",
            "ability": ability,
            "configured": view.get("configured", False),
            "fields": [
                {
                    "key": f.get("key"),
                    "label": f.get("label", ""),
                    "type": f.get("type", "text"),
                    "secret": bool(f.get("secret")),
                    "placeholder": f.get("placeholder", ""),
                }
                for f in (view.get("fields") or [])
            ],
            "secrets_set": view.get("secrets_set", {}),
        })

    tools["check_credential"] = ToolInfo(
        name="check_credential",
        handler=_check_credential_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "ability": {
                    "type": "string",
                    "description": (
                        "Ability whose vault credential the genui links to "
                        "(e.g. 'browser_control'). Returns whether the user has "
                        "connected it and the fields a login/connect form should "
                        "collect — never any secret value."
                    ),
                },
            },
            "required": [],
        },
    )

    # -- request_credential ----------------------------------------------------
    # Ask the USER for a secret (an API key, a token) WITHOUT ever seeing it. This
    # reserves a vault slot under the signed-in user and surfaces a secure card in
    # the chat: the user types the value there and it saves STRAIGHT to the vault —
    # it never comes back to you. You only get a stable `key_id` to wire into the
    # dashboard you're building. The dashboard then uses the secret by id through
    # the server-side proxy (api.callWithKey), so the plaintext never reaches the
    # page either. Re-calling with the same key_id just re-describes/re-prompts;
    # it never wipes an already-saved secret.
    async def _request_credential_wrapper(name: str, service_url: str = "",
                                          attach: str = "bearer", fields=None,
                                          key_id: str = None, **_extra):
        from app.abilities import vault_store
        meta = await vault_store.reserve_key(
            user_id,
            name=name,
            binding={"base_url": service_url, "attach": attach},
            fields=fields,
            key_id=key_id,
        )
        if not meta:
            return json.dumps({
                "status": "error",
                "message": "Could not reserve the vault key (is a user signed in?).",
            })
        bound = bool(str(meta.get("service") or "").strip())
        message = (
            "A secure entry card is now showing in the chat. The user types the "
            "secret there and it saves straight to the vault — you will NEVER see "
            "the value. Use key id '{kid}' in the dashboard; make its calls with "
            "api.callWithKey('{kid}', {{ path, method, ... }}) so the secret is added "
            "server-side. Re-check whether it's filled with list_vault_keys."
        ).format(kid=meta["key_id"])
        out = {
            "status": "ok",
            "ui": "vault_credential_form",   # the chat panel renders a secure entry card
            "key_id": meta["key_id"],
            "name": meta["name"],
            "fields": meta["fields"],
            "service": meta["service"],
            "filled": meta["filled"],
        }
        if not bound:
            # No service_url means the key is NOT usable yet: api.callWithKey fails
            # closed ("no service URL bound") until you bind it. This is the #1 way a
            # built dashboard ends up dead. Make it loud and tell the agent the fix.
            out["warning"] = (
                "This key has NO service_url, so the dashboard CANNOT use it yet — "
                "api.callWithKey will fail until you bind it. Once you know the exact "
                "service (e.g. Gmail → 'https://gmail.googleapis.com'), call "
                "request_credential AGAIN with the same name/key_id PLUS service_url. "
                "Re-calling keeps any value the user already typed; it only adds the "
                "binding. Do NOT wire api.callWithKey to an unbound key."
            )
            message = (
                "Card is showing — the user's secret saves straight to the vault (you "
                "never see it). BUT this key has no service_url yet, so it can't be "
                "used. " + out["warning"]
            )
        out["message"] = message
        return json.dumps(out)

    tools["request_credential"] = ToolInfo(
        name="request_credential",
        handler=_request_credential_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Human label for the secret, shown to the user on the card "
                        "(e.g. 'Gmail API Key', 'Stripe Secret Key'). Also the basis "
                        "for the key id if you don't pass one."
                    ),
                },
                "service_url": {
                    "type": "string",
                    "description": (
                        "The base URL of the service this key calls (e.g. "
                        "'https://www.googleapis.com'). The key is LOCKED to this "
                        "destination — the dashboard can only ever use it to call "
                        "URLs under this base, so the secret can't leak elsewhere. "
                        "Required for the dashboard to actually USE the key via the "
                        "proxy."
                    ),
                },
                "attach": {
                    "type": "string",
                    "description": (
                        "How the secret attaches to outbound calls: 'bearer' "
                        "(Authorization: Bearer <secret>, the default), 'basic' "
                        "(Authorization: Basic <secret>), 'header:<Name>' (e.g. "
                        "'header:X-Api-Key'), or 'query:<param>' (e.g. 'query:key')."
                    ),
                },
                "fields": {
                    "type": "array",
                    "description": (
                        "Optional. The field(s) the card collects. Default is a "
                        "single secret field. Use several for multi-part credentials "
                        "(e.g. a username + password). Each: {key, label, secret}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Field id (e.g. 'api_key')."},
                            "label": {"type": "string", "description": "Label shown on the card."},
                            "secret": {"type": "boolean", "description": "True = masked + never returned (default true)."},
                        },
                        "required": ["key"],
                    },
                },
                "key_id": {
                    "type": "string",
                    "description": (
                        "Optional explicit key id (slug). Omit to derive one from the "
                        "name. Pass an existing id to re-prompt for / update that key "
                        "without wiping the stored secret."
                    ),
                },
            },
            "required": ["name"],
        },
    )

    # -- list_vault_keys -------------------------------------------------------
    # See what the user has in the vault — id, name, bound service, and whether
    # it's been filled in yet — so you can REUSE an existing key instead of asking
    # again, or confirm a requested key is now filled before relying on it. Never
    # returns any secret value.
    async def _list_vault_keys_wrapper():
        from app.abilities import vault_store
        keys = await vault_store.list_keys(user_id)
        return json.dumps({"status": "ok", "keys": keys, "count": len(keys)})

    tools["list_vault_keys"] = ToolInfo(
        name="list_vault_keys",
        handler=_list_vault_keys_wrapper,
        parameters={"type": "object", "properties": {}, "required": []},
    )
