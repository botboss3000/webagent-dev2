"""Google Cloud Scheduler provider.

Authenticates with a service account JSON via JWT bearer tokens (RFC 7523)
exchanged at https://oauth2.googleapis.com/token for an OAuth 2.0 access
token, then calls the Cloud Scheduler REST API:

  POST/PATCH/DELETE  /v1/projects/{project_id}/locations/{location}/jobs[/{job_id}]

Each ``agent_automations`` row maps to one HTTP-target job whose URL is our
public fire webhook. The webhook validates the per-row ``fire_token`` query
param before running the executor.

This implementation uses only ``httpx`` + the standard library ``base64`` +
RSA signing via the ``cryptography`` package (declared through
``PyJWT[crypto]`` and also used by ``twilio``).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Dict, Optional

import httpx

from app.scheduler.remote_base import BaseRemoteScheduler, _build_fire_url

FEATURE = {
    "id": "google_cloud",
    "display_name": "Google Cloud Scheduler",
    "category": "scheduler",
    "status": "beta",
    "summary": "Push scheduled jobs to Google Cloud Scheduler.",
    "requires": ["a GCP service-account JSON with cloudscheduler.admin"],
}

logger = logging.getLogger(__name__)

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_JWT_ALG = "RS256"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256(private_key_pem: str, message: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8") if isinstance(private_key_pem, str) else private_key_pem,
        password=None,
    )
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def _sanitize_job_id(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "-", raw or "")[:500]
    if not s:
        s = "automation"
    if not s[0].isalpha():
        s = "j-" + s
    return s


class GoogleCloudScheduler(BaseRemoteScheduler):
    name = "google_cloud"

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(settings)
        self._access_token: Optional[str] = None
        self._access_token_expires: float = 0.0

    # ---- helpers -----------------------------------------------------

    def _load_service_account(self) -> Dict[str, str]:
        raw = self.settings.get("service_account_json") or ""
        if not raw:
            raise RuntimeError("Google Cloud Scheduler: service_account_json is empty.")
        if isinstance(raw, dict):
            data = raw
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"service_account_json is not valid JSON: {e}")
        if "client_email" not in data or "private_key" not in data:
            raise RuntimeError("service_account_json is missing client_email or private_key.")
        return data

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._access_token_expires - 60:
            return self._access_token
        sa = self._load_service_account()
        now = int(time.time())
        header = {"alg": _JWT_ALG, "typ": "JWT", "kid": sa.get("private_key_id", "")}
        claims = {
            "iss": sa["client_email"],
            "scope": _GOOGLE_SCOPE,
            "aud": _GOOGLE_TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        claims_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
        signature = _sign_rs256(sa["private_key"], signing_input)
        assertion = f"{header_b64}.{claims_b64}.{_b64url(signature)}"

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Google token exchange failed: {resp.status_code} {resp.text}")
        body = resp.json()
        self._access_token = body["access_token"]
        self._access_token_expires = time.time() + int(body.get("expires_in", 3600))
        return self._access_token

    def _project(self) -> str:
        p = (self.settings.get("project_id") or "").strip()
        if not p:
            raise RuntimeError("Missing project_id.")
        return p

    def _location(self) -> str:
        loc = (self.settings.get("location") or "").strip()
        if not loc:
            raise RuntimeError("Missing location.")
        return loc

    def _job_name(self, automation_id: str) -> str:
        prefix = (self.settings.get("job_prefix") or "webagent-").strip() or "webagent-"
        return f"projects/{self._project()}/locations/{self._location()}/jobs/{_sanitize_job_id(prefix + automation_id)}"

    def _fire_url(self, automation_id: str, token: str) -> str:
        override = (self.settings.get("fire_base_url") or "").strip().rstrip("/")
        if override:
            return f"{override}/api/v1/automations/fire/{automation_id}?token={token}"
        return _build_fire_url(automation_id, token)

    def _build_job_body(self, row: Dict[str, Any], fire_url: str) -> Dict[str, Any]:
        return {
            "name": self._job_name(row["id"]),
            "description": (row.get("task_label") or "WebAgent automation")[:500],
            "schedule": row["schedule_cron"],
            "timeZone": row.get("timezone") or "UTC",
            "httpTarget": {
                "uri": fire_url,
                "httpMethod": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(b"{}").decode("ascii"),
            },
        }

    # ---- abstract impl ----------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        try:
            await self._get_access_token()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # Hit a cheap endpoint: list jobs (limit 1).
        url = f"https://cloudscheduler.googleapis.com/v1/projects/{self._project()}/locations/{self._location()}/jobs?pageSize=1"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {self._access_token}"})
            if resp.status_code in (200, 404):
                return {"ok": True, "message": f"Reached Cloud Scheduler ({self._location()}) for project {self._project()}."}
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _push_job(self, row: Dict[str, Any], fire_url: str, fire_token: str) -> str:
        token = await self._get_access_token()
        body = self._build_job_body(row, fire_url)
        job_name = body["name"]
        parent = f"projects/{self._project()}/locations/{self._location()}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Try GET to see if the job exists.
            get_url = f"https://cloudscheduler.googleapis.com/v1/{job_name}"
            get_resp = await client.get(get_url, headers={"Authorization": f"Bearer {token}"})
            if get_resp.status_code == 200:
                # Patch existing job.
                patch_url = (
                    f"https://cloudscheduler.googleapis.com/v1/{job_name}"
                    "?updateMask=schedule,timeZone,httpTarget,description"
                )
                patch_resp = await client.patch(
                    patch_url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=body,
                )
                if patch_resp.status_code >= 300:
                    raise RuntimeError(f"PATCH job failed: {patch_resp.status_code} {patch_resp.text}")
            elif get_resp.status_code == 404:
                # Create job.
                create_url = f"https://cloudscheduler.googleapis.com/v1/{parent}/jobs"
                create_resp = await client.post(
                    create_url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=body,
                )
                if create_resp.status_code >= 300:
                    raise RuntimeError(f"CREATE job failed: {create_resp.status_code} {create_resp.text}")
            else:
                raise RuntimeError(f"GET job probe failed: {get_resp.status_code} {get_resp.text}")
        return job_name

    async def _cancel_job(self, row: Dict[str, Any]) -> None:
        ext = row.get("external_job_id")
        if not ext:
            return
        token = await self._get_access_token()
        async with httpx.AsyncClient(timeout=20.0) as client:
            url = f"https://cloudscheduler.googleapis.com/v1/{ext}"
            resp = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code not in (200, 204, 404):
                raise RuntimeError(f"DELETE job failed: {resp.status_code} {resp.text}")
