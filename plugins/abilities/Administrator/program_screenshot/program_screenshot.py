"""Program Screenshot ability — SELF-CONTAINED drop-in.

One tool:

  • ``program_screenshot`` — finds (or starts) a program by name, screenshots its
    window via PowerShell/.NET, saves the screenshot as a DB attachment, and
    delegates a description to a vision-capable model. Returns a full text
    description of everything visible in the window.

Uses the same vision-delegation path as ``image_vision.py`` (ask_model →
pick_vision_model), so it works even when the Orchestration ability is off.

Drop-in contract: FEATURE descriptor + build_tools()/TOOL_SCHEMAS/DESTRUCTIVE,
discovered generically by app/tools/loader.py. See plugins/abilities/_TEMPLATE.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()  # read-only — it takes a screenshot and describes it.


# ── PowerShell helpers ──────────────────────────────────────────────────────

# P/Invoke-based Powershell that:
#   1. Finds a process by name, gets MainWindowHandle
#   2. If not found, tries Start-Process to launch it, waits, then re-finds
#   3. Uses GetWindowRect to get window bounds (handles DPI minimised windows)
#   4. Uses Graphics.CopyFromScreen to capture the window rectangle
#   5. Saves to the given output path
_SCREENSHOT_PS_BASE = r"""
Add-Type @"
using System;
using System.Drawing;
using System.Runtime.InteropServices;
public class WinScreen {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left,Top,Right,Bottom; }
}
"@

$procName = $args[0]
$outPath  = $args[1]

# ---- 1. Find the process ----
$proc = Get-Process -Name $procName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1

if (-not $proc) {{
    # Try to start it
    try {{
        Start-Process $procName -ErrorAction Stop
        Start-Sleep -Seconds 2
    }} catch {{
        # Try common aliases
        $aliases = @{{
            "chrome"  = "chrome";
            "firefox" = "firefox";
            "edge"    = "msedge";
            "notepad" = "notepad";
            "calc"    = "calc";
            "cmd"     = "cmd";
            "powershell" = "powershell";
            "explorer"= "explorer";
            "vscode"  = "code";
            "spotify" = "spotify";
            "slack"   = "slack";
            "discord" = "discord";
            "teams"   = "teams";
            "outlook" = "outlook";
            "excel"   = "excel";
            "word"    = "winword";
            "onenote" = "onenote";
            "settings"= "ms-settings:";
            "store"   = "ms-windows-store:";
            "terminal"= "wt";
        }}
        $launchName = $procName.ToLower()
        if ($aliases.ContainsKey($launchName)) {{
            $launchName = $aliases[$launchName]
        }}
        try {{
            Start-Process $launchName -ErrorAction Stop
            Start-Sleep -Seconds 2
        }} catch {{
            Write-Output ("ERROR:CANNOT_START:" + $procName)
            exit 1
        }}
    }}
    # Re-find
    Start-Sleep -Seconds 2
    $proc = Get-Process -Name $procName -ErrorAction SilentlyContinue | Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero }} | Select-Object -First 1
}}

if (-not $proc) {{
    Write-Output ("ERROR:NOT_FOUND:" + $procName)
    exit 1
}}

$hwnd = $proc.MainWindowHandle

# ---- 2. Restore if minimised, bring to foreground ----
if ([WinScreen]::IsIconic($hwnd)) {{
    [WinScreen]::ShowWindowAsync($hwnd, 9) | Out-Null  # SW_RESTORE
    Start-Sleep -Milliseconds 500
}}
[WinScreen]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 300

# ---- 3. Get window bounds ----
$rect = New-Object WinScreen+RECT
if (-not [WinScreen]::GetWindowRect($hwnd, [ref]$rect)) {{
    Write-Output ("ERROR:GET_RECT:" + $procName)
    exit 1
}}

$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
if ($w -le 0 -or $h -le 0) {{
    Write-Output ("ERROR:ZERO_SIZE:" + $procName)
    exit 1
}}

# ---- 4. Screenshot that rectangle ----
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($rect.Left, $rect.Top, 0, 0, (New-Object System.Drawing.Size $w, $h))
$bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()

Write-Output ("OK:" + $outPath + "|" + $w + "x" + $h + "|" + $proc.ProcessName)
"""


async def _run_powershell(script: str, timeout: float = 30.0) -> tuple[str, int]:
    """Run a powershell script and return (stdout, returncode)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        if err_text:
            logger.debug("PowerShell stderr: %s", err_text[:500])
        return out_text, proc.returncode
    except asyncio.TimeoutError:
        logger.warning("PowerShell timed out after %.1fs", timeout)
        return "ERROR:TIMEOUT", -1


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the program_screenshot ability."""

    async def program_screenshot(program_name: str, question: str = "") -> str:
        """Take a screenshot of a program window and return a detailed text
        description of everything visible on screen.

        If the program isn't running, it will be started first. Use this when
        the user asks you to screenshot or describe what's showing in any
        application — e.g. 'screenshot Chrome' or 'what's in my Notepad?'.

        Args:
            program_name: The process name of the program (e.g. 'chrome',
                          'notepad', 'calculator', 'spotify'). Case-insensitive.
            question: Optional. A specific question about what's on screen —
                      e.g. 'read the error message' or 'what tab is open?'.
                      Leave empty for a full description.
        """
        import base64
        import time

        if not (program_name or "").strip():
            return json.dumps({"status": "error", "message": "program_name is required — e.g. 'chrome' or 'notepad'."})

        name = program_name.strip()

        # ---- 1. Screenshot the program window ----
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="progshot_")
        os.close(fd)

        safe_name = name.replace("'", "''")
        safe_path = tmp_path.replace("'", "''")

        ps_script = _SCREENSHOT_PS_BASE.replace(
            "$procName = $args[0]", f"$procName = '{safe_name}'"
        ).replace(
            "$outPath  = $args[1]", f"$outPath  = '{safe_path}'"
        )

        out_text, rc = await _run_powershell(ps_script, timeout=45.0)

        # ---- 2. Parse the result ----
        if not out_text or out_text.startswith("ERROR:"):
            err_part = out_text.split(":", 2) if ":" in out_text else ["", "", "Unknown error"]
            err_code = err_part[1] if len(err_part) > 1 else "UNKNOWN"
            err_detail = err_part[2] if len(err_part) > 2 else (out_text or "PowerShell returned nothing")

            if err_code == "NOT_FOUND":
                return json.dumps({
                    "status": "error",
                    "code": "not_found",
                    "message": f"No running window found for '{name}' and couldn't start it. Check the program name and try again, or start the program manually.",
                })
            elif err_code == "CANNOT_START":
                return json.dumps({
                    "status": "error",
                    "code": "cannot_start",
                    "message": f"Could not start '{name}'. It may not be installed or isn't on your PATH. Try starting it manually.",
                })
            else:
                return json.dumps({
                    "status": "error",
                    "code": err_code.lower(),
                    "message": f"Failed to screenshot '{name}': {err_detail}",
                })

        # Expected: "OK:<path>|<WxH>|<actual_process_name>"
        if not out_text.startswith("OK:"):
            return json.dumps({"status": "error", "message": f"Unexpected PowerShell output: {out_text[:200]}"})

        parts = out_text[3:].split("|")
        screenshot_path = parts[0] if len(parts) > 0 else tmp_path
        dimensions = parts[1] if len(parts) > 1 else "?"
        actual_name = parts[2] if len(parts) > 2 else name

        # ---- 3. Read the screenshot bytes ----
        try:
            with open(screenshot_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Failed to read screenshot: {e}"})
        finally:
            try:
                os.unlink(screenshot_path)
            except Exception:
                pass

        if not img_bytes or len(img_bytes) < 100:
            return json.dumps({"status": "error", "message": "Screenshot was empty or corrupt."})

        # ---- 4. Save as a DB attachment so the vision model can read it ----
        try:
            from app.db import get_db
            from app.db.attachments import store_file

            db = get_db()
            filename = f"screenshot_{actual_name}_{int(time.time())}.png"
            store_result = await store_file(
                user_id=user_id,
                session_id=session_id,
                file_bytes=img_bytes,
                filename=filename,
                mime_type="image/png",
            )
            att_id = await db.insert_attachment(
                user_id=user_id,
                session_id=session_id,
                original_name=filename,
                mime_type="image/png",
                size_bytes=len(img_bytes),
                storage_path=store_result["storage_path"],
                storage_provider=store_result.get("storage_provider", "local"),
            )
            att = {"id": att_id, "original_name": filename,
                   "mime_type": "image/png", "size_bytes": len(img_bytes),
                   "storage_path": store_result["storage_path"],
                   "storage_provider": store_result.get("storage_provider", "local")}
        except Exception as e:
            logger.exception("Failed to store screenshot as attachment")
            return json.dumps({"status": "error", "message": f"Cannot store screenshot: {e}"})

        # ---- 5. Delegate to vision model ----
        try:
            from app.admin.settings import load_llm_capabilities_for_user, pick_vision_model
            from app.agent.provider_completion import ask_model

            try:
                from app import model_catalog
                await model_catalog.ensure_fresh()
            except Exception:
                pass

            caps = await load_llm_capabilities_for_user(user_id)
            vision = pick_vision_model(caps)
            if not vision:
                return json.dumps({
                    "status": "error",
                    "code": "no_vision_model",
                    "message": "No image-input model is configured. Ask the admin to open App Config → Models, save a vision-capable model and tick its In box.",
                })

            prompt = (question or "").strip()
            if prompt:
                user_line = (
                    f"This is a screenshot of the program '{actual_name}' ({dimensions}). "
                    f"Answer this specific question about it: {prompt}"
                )
            else:
                user_line = (
                    f"This is a screenshot of the program '{actual_name}' ({dimensions}). "
                    f"Give a detailed, organised text description of everything visible — "
                    f"include all visible text verbatim (menus, buttons, labels, content), "
                    f"the layout, colours, open tabs/windows/files, and any notable UI state. "
                    f"Be thorough and factual. Do not speculate beyond what is visible."
                )

            sys_line = (
                "You are a vision assistant. Look at the screenshot and answer precisely "
                "and factually. Include all visible text verbatim and concrete details "
                "(UI elements, layout, colours, positions, content). Do not speculate "
                "beyond what is visible."
            )

            answer = await ask_model(vision, sys_line, user_line, attachments=[att], max_tokens=1200)

            if not answer:
                return json.dumps({
                    "status": "error",
                    "message": f"The vision model ({vision.get('model','')}) could not describe the screenshot.",
                })

            return json.dumps({
                "status": "ok",
                "program": actual_name,
                "dimensions": dimensions,
                "model": vision.get("model", ""),
                "description": answer,
            })

        except Exception as e:
            logger.exception("Vision delegation failed")
            return json.dumps({"status": "error", "message": f"Vision model error: {e}"})

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "program_screenshot": {
            "type": "object",
            "properties": {
                "program_name": {
                    "type": "string",
                    "description": "The process name of the program to screenshot (e.g. 'chrome', 'notepad', 'spotify', 'calculator', 'vscode', 'slack'). Case-insensitive.",
                },
                "question": {
                    "type": "string",
                    "description": "Optional. A specific question about what's visible — e.g. 'read the error message', 'what tabs are open?', 'what song is playing?'. Leave empty for a full description.",
                },
            },
            "required": ["program_name"],
        },
    })
    return {"program_screenshot": program_screenshot}
