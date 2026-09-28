"""Admin-only managed ChatGPT authorization for the local Codex runtime."""
import asyncio
import shutil
import subprocess
from fastapi import APIRouter, HTTPException, Request

from app.api.claude_auth import _require_admin

router = APIRouter(prefix="/api/v1/codex")


def _codex() -> str:
    path = shutil.which("codex")
    if not path:
        raise HTTPException(status_code=404, detail="Codex is not installed or not on PATH on this device.")
    return path


def _status() -> dict:
    """Use the CLI's own status command; never inspect auth.json/keychain data."""
    try:
        result = subprocess.run([_codex(), "login", "status"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10)
        detail = (result.stdout or result.stderr or "").strip()
        return {"installed": True, "signed_in": result.returncode == 0, "detail": detail[:500]}
    except subprocess.TimeoutExpired:
        return {"installed": True, "signed_in": False, "detail": "Codex did not return a login status in time."}
    except OSError as exc:
        return {"installed": False, "signed_in": False, "detail": str(exc)}


@router.get("/auth/status")
async def auth_status(request: Request):
    await _require_admin(request)
    try:
        from plugins.engines.codex.app_server import app_server

        state = await app_server.account(refresh=False)
        account = state.get("account") if isinstance(state, dict) else None
        limits = {}
        if isinstance(account, dict) and account.get("type") == "chatgpt":
            try:
                limits = await app_server.rate_limits()
            except Exception:
                limits = {}
        return {
            "installed": True,
            # Keep the legacy Codex-agent status meaningful for API-key logins,
            # while subscription controls can require the explicit ChatGPT flag.
            "signed_in": bool(isinstance(account, dict) and account.get("type")),
            "chatgpt_authorized": bool(
                isinstance(account, dict) and account.get("type") == "chatgpt"
            ),
            "detail": (
                f"ChatGPT {account.get('planType') or 'subscription'}"
                if isinstance(account, dict) and account.get("type") == "chatgpt"
                else f"Codex authorized via {account.get('type')}."
                if isinstance(account, dict) and account.get("type")
                else "Not authorized with a ChatGPT subscription."
            ),
            "account": account,
            "plan_type": account.get("planType") if isinstance(account, dict) else None,
            "rate_limits": limits,
        }
    except Exception as exc:
        fallback = await asyncio.to_thread(_status)
        fallback["chatgpt_authorized"] = False
        fallback["detail"] = fallback.get("detail") or str(exc)
        return fallback


@router.post("/login/start")
async def login_start(request: Request):
    await _require_admin(request)
    try:
        from plugins.engines.codex.app_server import app_server

        result = await app_server.start_chatgpt_login()
        verification_url = str(result.get("verificationUrl") or "")
        user_code = str(result.get("userCode") or "")
        if not verification_url or not user_code:
            raise RuntimeError("Codex did not return device authorization details.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start Codex sign-in: {exc}")
    return {
        "ok": True,
        "verification_url": verification_url,
        "user_code": user_code,
        "login_id": result.get("loginId"),
        "message": "Open the verification page and enter the one-time code.",
    }


@router.post("/auth/signout")
async def auth_signout(request: Request):
    await _require_admin(request)
    try:
        from plugins.engines.codex.app_server import app_server

        await app_server.logout()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not sign out of Codex: {exc}")
    return {"ok": True}


@router.get("/models")
async def subscription_models(request: Request):
    """Models available to the currently authorized ChatGPT subscription."""
    await _require_admin(request)
    try:
        from plugins.engines.codex.app_server import app_server

        account = await app_server.account(refresh=False)
        row = account.get("account") if isinstance(account, dict) else None
        if not isinstance(row, dict) or row.get("type") != "chatgpt":
            return {"models": [], "error": "Authorize a ChatGPT subscription first."}
        models = []
        for item in await app_server.models():
            model_id = str(item.get("id") or item.get("model") or "").strip()
            if not model_id:
                continue
            modalities = item.get("inputModalities") or ["text", "image"]
            efforts = item.get("supportedReasoningEfforts") or []
            models.append({
                "id": model_id,
                "name": item.get("displayName") or model_id,
                "description": item.get("description") or "",
                "text_capable": "text" in modalities,
                "image_capable": "image" in modalities,
                "image_out_capable": False,
                "voice_capable": "audio" in modalities,
                "reasoning_efforts": efforts,
                "default_reasoning_effort": item.get("defaultReasoningEffort"),
                "is_default": bool(item.get("isDefault")),
            })
        return {"models": models, "plan_type": row.get("planType")}
    except Exception as exc:
        return {"models": [], "error": str(exc)}
