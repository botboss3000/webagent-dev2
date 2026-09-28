"""User Files ability — SELF-CONTAINED drop-in.

Gives the agent a SANCTIONED way to hand a file to the user instead of dumping
artifacts into the project root. Every file lands under the user's own home
(``data/user_data/<user_id>/<room>/`` — see app/user_workspace.py) and the tool
returns the ``/user_data/...`` URL the chat UI can render or link.

Tools:
  • save_file       — write text or (base64) binary into the user's home; returns the URL.
  • attach_file     — re-share an existing file from the user's home without rewriting it.
  • list_user_files — list what's in the user's home (optionally one room).
  • read_user_file  — read back a text file the agent saved earlier (own user only).

Discovered generically by core (app/tools/loader.py "Self-contained ability
tools"): build_tools() returns the handlers and the loader reads the module-level
TOOL_SCHEMAS / DESTRUCTIVE populated below AFTER the call. Nothing is wired in
app/ — drop the file in to add the ability, delete it to remove it.
"""

from __future__ import annotations

import base64
import json
from typing import Optional

# Populated inside build_tools(); the loader reads these AFTER the call. None of
# these tools is destructive to existing data — each writes only inside the
# user's own sandbox — so DESTRUCTIVE stays empty (no per-call confirm prompt).
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the User Files ability.

    Closes over ``user_id`` so every file is scoped to the caller's home. Heavy
    work is just filesystem I/O via app.user_workspace (imported lazily)."""
    from app import user_workspace as ws
    from app.encryption.workspace_files import read_encrypted_file, write_encrypted_file

    async def _save_file(filename: str, content: str, room: str = "files",
                         is_base64: bool = False) -> str:
        """Save a file into the user's home and return its URL.

        filename: desired name (sanitised to a safe basename).
        content:  text, or base64-encoded bytes when is_base64=True.
        room:     bucket under the home — 'files' (default), 'screenshots', etc.
        """
        if not filename or not str(filename).strip():
            return json.dumps({"status": "error", "message": "filename is required"})
        name = ws.safe_filename(filename)
        out_dir = ws.user_dir(user_id, room)
        dest = ws.unique_path(out_dir, name)
        try:
            if is_base64:
                raw = base64.b64decode(content or "", validate=False)
                size = len(raw)
            else:
                text = content or ""
                raw = text.encode("utf-8")
                size = len(raw)
            await write_encrypted_file(user_id, dest, raw)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"could not save file: {e}"})
        import mimetypes
        url = ws.public_url(user_id, room, dest.name)
        return json.dumps({
            # This discriminant lets the trusted transcript renderer recognize
            # save_file output without treating model-authored JSON as UI.
            "kind": "file_attachment",
            "status": "ok",
            "filename": dest.name,
            "room": ws.safe_segment(room, fallback="files"),
            "mime_type": mimetypes.guess_type(dest.name)[0] or "application/octet-stream",
            "url": url,
            "bytes": size,
        })

    async def _attach_file(path: str) -> str:
        """Re-share an existing file from the current user's workspace.

        path: path relative to the user's home, as returned by list_user_files
        (for example ``files/report.pdf`` or ``screenshots/snippet.jpg``).
        The file is never copied or rewritten; a fresh opaque content grant is
        returned in the same trusted descriptor shape as save_file.
        """
        target = ws.resolve_within_home(user_id, path)
        if target is None:
            return json.dumps({"status": "error", "message": "path is outside your home"})
        if not target.exists() or not target.is_file():
            return json.dumps({"status": "error", "message": f"not found: {path}"})
        try:
            home = ws.user_home(user_id).resolve()
            relative_path = target.resolve().relative_to(home).as_posix()
            raw = await read_encrypted_file(user_id, target)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"could not attach file: {e}"})

        import mimetypes
        from app.content_access import artifact_url_for_path

        filename = target.name
        storage_path = f"{ws.safe_segment(user_id)}/{relative_path}"
        return json.dumps({
            "kind": "file_attachment",
            "status": "ok",
            "filename": filename,
            "room": relative_path.split("/", 1)[0] if "/" in relative_path else "",
            "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "url": artifact_url_for_path(
                user_id=user_id,
                storage_path=storage_path,
                original_name=filename,
                mime_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            ),
            "bytes": len(raw),
        })

    async def _list_user_files(room: str = "") -> str:
        """List files in the user's home. Pass a room ('files', 'screenshots')
        to scope it; omit to list every room."""
        home = ws.user_home(user_id)
        scope = home / ws.safe_segment(room, fallback="files") if room else home
        items = []
        if scope.exists():
            for p in sorted(scope.rglob("*")):
                if p.is_file():
                    try:
                        rel = p.relative_to(home).as_posix()
                        room_name = rel.split("/", 1)[0] if "/" in rel else ""
                        items.append({
                            "path": rel,
                            "room": room_name,
                            "url": f"{ws.URL_PREFIX}/{ws.safe_segment(user_id)}/{rel}",
                            "bytes": p.stat().st_size,
                        })
                    except Exception:  # noqa: BLE001
                        continue
        return json.dumps({"status": "ok", "count": len(items), "files": items})

    async def _read_user_file(path: str) -> str:
        """Read back a text file the agent saved earlier (path relative to the
        user's home, e.g. 'files/report.md'). Only the caller's own home is
        reachable."""
        target = ws.resolve_within_home(user_id, path)
        if target is None:
            return json.dumps({"status": "error", "message": "path is outside your home"})
        if not target.exists() or not target.is_file():
            return json.dumps({"status": "error", "message": f"not found: {path}"})
        try:
            text = (await read_encrypted_file(user_id, target)).decode("utf-8")
        except UnicodeDecodeError:
            return json.dumps({"status": "error", "message": "file is not UTF-8 text (binary)"})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"could not read file: {e}"})
        return json.dumps({"status": "ok", "path": path, "content": text})

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "save_file": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Desired file name, e.g. 'summary.md' or 'chart.png'. Sanitised to a safe basename."},
                "content": {"type": "string", "description": "File content — plain text, or base64-encoded bytes when is_base64 is true."},
                "room": {"type": "string", "description": "Folder under the user's home: 'files' (default) for documents/reports, or another bucket like 'screenshots'.", "default": "files"},
                "is_base64": {"type": "boolean", "description": "Set true when 'content' is base64-encoded binary (images, PDFs, etc.).", "default": False},
            },
            "required": ["filename", "content"],
        },
        "attach_file": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Existing file path relative to the user's home, e.g. 'files/report.md' or 'screenshots/snippet.png'."},
            },
            "required": ["path"],
        },
        "list_user_files": {
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "Optional room to scope the listing ('files', 'screenshots'). Omit to list everything.", "default": ""},
            },
            "required": [],
        },
        "read_user_file": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the user's home, e.g. 'files/report.md'."},
            },
            "required": ["path"],
        },
    })
    DESTRUCTIVE.clear()  # none of these confirm-gate

    return {
        "save_file": _save_file,
        "attach_file": _attach_file,
        "list_user_files": _list_user_files,
        "read_user_file": _read_user_file,
    }
