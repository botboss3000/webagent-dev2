"""
edit_genui — surgical edits to a saved genui, WITHOUT re-rendering the whole page.

``render_visual`` always replaces the entire document: every fix means re-emitting
the full ~30KB page, which is slow, risks truncation, and lets old bugs ride along
into each rewrite. ``edit_genui`` is the alternative for *changing* an existing
genui: read it (``get_genui``), then apply one or more exact find/replace edits to
just the parts that change. It routes through the SAME ``save_genui_html`` path as
``render_visual`` (so the manifest + the live Gen UI refresh behave identically) and
returns the same ``path``/``slug``/``title`` shape the frontend reload handler reads.

Design choices that keep it safe:
  • Edits are applied to an in-memory copy and only saved if EVERY edit succeeds —
    a miss leaves the genui untouched (atomic), never half-edited.
  • Each ``find`` must match the current text and be unique (unless ``replace_all``),
    mirroring a normal code-edit tool — so an ambiguous match can't silently hit the
    wrong spot.
  • The result is re-checked for a complete ``</html>`` document before saving, so a
    bad edit can't leave a truncated page.

Lives in the Visualizer ability (registered alongside render_visual/get_genui); no
core file is touched. Same trust boundary as the rest of the genui tools: access
follows the Gen UI page's VISIBILITY setting, enforced server-side (registration
required by default; admins + open single-user mode always pass — see app/auth
`genui_first_class` → user_may_access_page, and screenshot_genui).
"""
import json
from typing import List, Optional

from app.visualizer.genui import get_genui_html, save_genui_html
from app.visualizer.tool import _looks_complete


def _normalize_edits(edits, find, replace, replace_all) -> Optional[List[dict]]:
    """Accept either an ``edits`` list of {find, replace, replace_all} OR a single
    flat find/replace (a lenient alias so a model that passes them at the top level
    still works). Returns a list of edit dicts, or None if nothing usable was given."""
    out: List[dict] = []
    if edits:
        if not isinstance(edits, list):
            return None
        for e in edits:
            if not isinstance(e, dict):
                return None
            out.append({
                "find": e.get("find"),
                "replace": e.get("replace", "") or "",
                "replace_all": bool(e.get("replace_all", False)),
            })
    if find is not None:
        out.append({"find": find, "replace": replace or "", "replace_all": bool(replace_all)})
    return out or None


async def edit_genui(
    slug: str,
    edits=None,
    find: str = None,
    replace: str = None,
    replace_all: bool = False,
    user_id: str = "default",
    agent_id: str = "",
) -> str:
    """Apply surgical find/replace edits to a saved genui and save the result.

    Returns a JSON string. On any failure NOTHING is saved (the genui is unchanged),
    and the message says why so the agent can re-read and retry.
    """
    norm = _normalize_edits(edits, find, replace, replace_all)
    if not norm:
        return json.dumps({
            "status": "error",
            "message": ("No edits given. Pass `edits`: a list of {find, replace} objects "
                        "(or a single find/replace). `find` must be exact text from the "
                        "current genui — read it first with get_genui."),
        })

    html = await get_genui_html(user_id=user_id, slug=slug)
    if html is None:
        return json.dumps({
            "status": "error",
            "message": "Gen UI '{}' not found. Use list_genui / create_genui first.".format(slug),
        })

    original = html
    applied = []
    for i, e in enumerate(norm):
        f = e["find"]
        if not f:
            return json.dumps({
                "status": "error",
                "message": "Edit #{} has an empty `find`. Nothing was saved.".format(i + 1),
            })
        count = html.count(f)
        if count == 0:
            return json.dumps({
                "status": "error",
                "message": ("Edit #{}: the `find` text was not found in genui '{}' (nothing "
                            "saved). Re-read it with get_genui('{}') and copy the exact current "
                            "text — whitespace and tags must match. If you still can't match it, "
                            "FALL BACK to render_visual with the full updated HTML — a full render "
                            "always applies, so a stubborn edit should never block the change."
                            ).format(i + 1, slug, slug),
                "saved": False, "slug": slug,
                "fallback": "render_visual",
            })
        if count > 1 and not e["replace_all"]:
            return json.dumps({
                "status": "error",
                "message": ("Edit #{}: the `find` text appears {} times — it is not unique, so the "
                            "edit was rejected (nothing saved). Add more surrounding context to "
                            "target one spot, or set replace_all:true to change them all. If "
                            "scoping it is too fiddly, FALL BACK to render_visual with the full "
                            "updated HTML.").format(i + 1, count),
                "saved": False, "slug": slug,
                "fallback": "render_visual",
            })
        n = count if e["replace_all"] else 1
        html = html.replace(f, e["replace"], -1 if e["replace_all"] else 1)
        applied.append({"edit": i + 1, "replacements": n})

    if html == original:
        return json.dumps({
            "status": "ok", "saved": False, "slug": slug,
            "warning": ("The edits produced no change (every `find` already equals its `replace`). "
                        "Nothing was saved."),
        })

    if not _looks_complete(html):
        return json.dumps({
            "status": "error",
            "message": ("After applying the edits the document no longer looks complete (no "
                        "closing </html>) — the edit was rejected and NOTHING was saved. Check "
                        "that your replacement didn't delete structural markup."),
            "saved": False, "slug": slug,
        })

    url_path = await save_genui_html(user_id, slug, html, title="", agent_id=agent_id)
    size = len(html.encode("utf-8"))
    total_repl = sum(a["replacements"] for a in applied)
    return json.dumps({
        "status": "ok",
        "saved": True,
        "complete": True,
        "path": url_path,          # frontend reload handler reads this (same as render_visual)
        "slug": slug,
        "edits_applied": len(applied),
        "replacements": total_repl,
        "size_bytes": size,
        "note": ("Applied {} edit(s) ({} replacement(s)) to genui '{}' and saved. ALWAYS verify "
                 "now with screenshot_genui('{}', 'both') — a surgical edit can still break "
                 "layout or script scope; don't tell the user it's done until the shot is "
                 "clean.").format(len(applied), total_repl, slug, slug),
    })
