"""
render_visual tool -- writes HTML to a per-user named genui and returns the path for the frontend iframe.
"""
import json
from app.visualizer.genui import save_genui_html, ensure_home_genui


def _looks_complete(html: str) -> bool:
    """True when the string looks like a whole HTML document, not a truncated stream.

    The recurring failure this guards against: the model streams a large genui in
    one tool call, the call is cut short (user interrupt, length cap, a dropped
    chunk), and a half-written document arrives here. Saving it would let the agent
    'succeed' on a broken genui and then claim it shipped. A complete document
    always ends with a closing </html>; a truncated one almost never does.
    """
    low = html.lower()
    return ("<html" in low or "<!doctype" in low) and "</html>" in low


async def render_visual(
    html: str,
    title: str = "",
    slug: str = "home",
    user_id: str = "default",
    genui_name: str = None,
    agent_id: str = "",
    session_target_name: str = "",
    session_mode: str = "",
    session_id: str = "",
) -> str:
    """
    Save HTML to the named genui for this user and return metadata.

    Args:
        html:     Full HTML document string
        title:    Human-readable title for the genui
        slug:     Gen UI slug to write to (e.g. "home", "dashboard")
        user_id:  User identifier for per-user genui isolation
        genui_name: Alias for ``slug`` — a model that names the genui with
                   ``genui_name`` instead of ``slug`` still writes to the right
                   genui. If given and ``slug`` is the default, it wins, so a
                   stray name never silently dumps onto ``home``.
        agent_id: The agent doing the render — recorded as the genui's owning
                   agent so the Gen UI footer can name it and route its chat.
        session_target_name: REQUIRED when this render CREATES a new genui —
                   the title of the deployed agent session the page's actions
                   dispatch into. Re-rendering an existing page keeps its config.
        session_mode: REQUIRED when creating — "new_reuse" | "new_each" |
                   "existing" (see the genui API session contract).
        session_id: Required iff session_mode == "existing".

    Returns:
        JSON string with path, title, slug, size_bytes, complete
    """
    # Accept ``genui_name`` as an alias for ``slug`` so a call that names the
    # genui the other way still lands on the intended genui rather than
    # defaulting to home.
    if genui_name and (not slug or slug == "home"):
        slug = genui_name

    if not html or not html.strip():
        return json.dumps({
            "status": "error",
            "message": "Empty HTML rejected. Put the COMPLETE HTML document string in the `html` parameter. The genui was NOT updated.",
        })

    if not _looks_complete(html):
        return json.dumps({
            "status": "error",
            "message": (
                "The HTML looks truncated — it has no closing </html>, so it is "
                "almost certainly a partial/cut-off document. NOTHING was saved; "
                "the genui is unchanged. Re-send the ENTIRE document from "
                "<!DOCTYPE html> through </html> in a single render_visual call."
            ),
            "saved": False,
            "slug": slug,
        })

    await ensure_home_genui(user_id)

    # ── Session contract on CREATE ───────────────────────────────────────────
    # Creating a brand-new genui REQUIRES the session target name + new-session
    # mode (the anti-confusing-names gate — every page must declare which
    # session its actions/chat deploy into). Re-rendering an EXISTING page is
    # fine without them: the page keeps the config it was created with.
    from app.visualizer.genui import list_genui as _list_genui
    session_config = None
    try:
        existing = await _list_genui(user_id)
        exists = any(p.get("slug") == slug for p in (existing or []))
    except Exception:
        exists = False  # fail closed → require the fields when unsure
    if not exists:
        name = (session_target_name or "").strip()
        mode = session_mode or ""
        if not name:
            return json.dumps({
                "status": "error",
                "message": (
                    "This render would CREATE a new genui, so the session "
                    "contract is required: pass session_target_name (the title "
                    "of the deployed agent session) and session_mode "
                    "('new_reuse', 'new_each', or 'existing'). Nothing was saved."
                ),
                "saved": False,
                "slug": slug,
            })
        if mode not in ("new_reuse", "new_each", "existing"):
            return json.dumps({
                "status": "error",
                "message": (
                    "session_mode must be one of: new_reuse, new_each, existing. "
                    "Nothing was saved."
                ),
                "saved": False,
                "slug": slug,
            })
        cfg: dict = {"target_name": name, "mode": mode}
        if mode == "existing" and not (session_id or "").strip():
            return json.dumps({
                "status": "error",
                "message": (
                    "session_mode is 'existing', so session_id is required. "
                    "Nothing was saved."
                ),
                "saved": False,
                "slug": slug,
            })
        if mode == "existing":
            cfg["session_id"] = (session_id or "").strip()
        session_config = cfg

    url_path = await save_genui_html(user_id, slug, html, title=title,
                                     agent_id=agent_id, session_config=session_config)
    size = len(html.encode("utf-8"))
    result = {
        "status": "ok",
        "saved": True,
        "complete": True,
        "path": url_path,
        "title": title or slug.capitalize(),
        "slug": slug,
        "size_bytes": size,
        "note": (
            "Saved to genui '{}'. The genui is updated ONLY because this call "
            "returned status ok — never tell the user it is done unless you saw "
            "this result. To double-check, read it back with get_genui('{}')."
        ).format(slug, slug),
    }
    # A finished dashboard/page is rarely this small; flag likely stubs so the
    # agent doesn't mistake a placeholder for a delivered design.
    if size < 900:
        result["warning"] = (
            "This document is very small ({} bytes) — that is a stub, not a "
            "finished design. If you meant to ship a full genui, it was likely "
            "truncated; re-render the complete document."
        ).format(size)
    return json.dumps(result)
