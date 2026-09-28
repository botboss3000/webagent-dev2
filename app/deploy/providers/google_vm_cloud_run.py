"""Deploy WebAgent directly to Google Cloud Run from GitHub source.

The provider downloads the selected revision without exposing the GitHub token
to Google, uploads a short-lived source archive to Cloud Storage, builds the
repository's Dockerfile with Cloud Build, stores the image in Artifact Registry,
and creates or updates a Cloud Run service.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import tarfile
import time
from typing import Any, AsyncIterator, Dict, Tuple
from urllib.parse import quote, urlparse

import httpx

from app.deploy.base import BaseDeployProvider, done, ev
from app.deploy.bootstrap import DEFAULT_BRANCH, DEFAULT_REPO_URL
from app.deploy.providers.google_vm import _access_token, _google_error

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "google_cloud_run",
    "display_name": "Google Cloud Run",
    "category": "deploy",
    "status": "beta",
    "summary": "Build the selected repository and deploy it as a Cloud Run service.",
    "requires": ["Cloud Run, Cloud Build, Artifact Registry, and Storage permissions"],
}

_RUN = "https://run.googleapis.com/v2"
_BUILD = "https://cloudbuild.googleapis.com/v1"
_AR = "https://artifactregistry.googleapis.com/v1"
_STORAGE = "https://storage.googleapis.com/storage/v1"
_UPLOAD = "https://storage.googleapis.com/upload/storage/v1"
_SERVICE_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_TEMPLATE_KEYS = {
    "labels", "annotations", "scaling", "vpcAccess", "timeout", "serviceAccount",
    "containers", "volumes", "executionEnvironment", "encryptionKey",
    "maxInstanceRequestConcurrency", "serviceMesh", "encryptionKeyRevocationAction",
    "encryptionKeyShutdownDuration", "sessionAffinity", "healthCheckDisabled",
    "nodeSelector", "client", "clientVersion", "gpuZonalRedundancyDisabled",
}
_CONTAINER_KEYS = {
    "name", "image", "sourceCode", "command", "args", "env", "resources", "ports",
    "volumeMounts", "workingDir", "livenessProbe", "startupProbe",
    "readinessProbe", "dependsOn", "baseImageUri",
}


def _headers(token: str, *, json_body: bool = True) -> Dict[str, str]:
    out = {"Authorization": f"Bearer {token}"}
    if json_body:
        out["Content-Type"] = "application/json"
    return out


def _github_repo(repo_url: str) -> Tuple[str, str]:
    """Return (owner, repo) for a GitHub URL from the shared repo form."""
    raw = (repo_url or "").strip()
    if raw.startswith("git@github.com:"):
        path = raw.split(":", 1)[1]
    else:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("Cloud Run source deploy currently supports GitHub repository URLs.")
        path = parsed.path
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Enter a GitHub repository URL such as https://github.com/owner/repo.")
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return parts[0], repo


def _flatten_github_archive(data: bytes) -> bytes:
    """Strip GitHub's generated top-level directory from a .tar.gz archive."""
    source = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    output = io.BytesIO()
    with source, tarfile.open(fileobj=output, mode="w:gz") as target:
        members = source.getmembers()
        roots = {m.name.split("/", 1)[0] for m in members if m.name}
        prefix = (next(iter(roots)) + "/") if len(roots) == 1 else ""
        for member in members:
            name = member.name[len(prefix):] if prefix and member.name.startswith(prefix) else member.name
            name = name.lstrip("/")
            if not name or name == ".." or name.startswith("../") or "/../" in name:
                continue
            member.name = name
            payload = source.extractfile(member) if member.isfile() else None
            target.addfile(member, payload)
    return output.getvalue()


def _revision_template_with_image(service: Dict[str, Any], image: str) -> Dict[str, Any]:
    """Copy the mutable revision template while changing only its primary image.

    Cloud Run returns output-only fields that cannot be sent back on PATCH. Keep
    the supported template/container fields so scaling, env, probes, resources,
    volumes, service account, and networking survive a routine repo deployment.
    """
    current = service.get("template") if isinstance(service.get("template"), dict) else {}
    template = {key: value for key, value in current.items() if key in _TEMPLATE_KEYS}
    containers = current.get("containers") if isinstance(current.get("containers"), list) else []
    if not containers:
        raise RuntimeError("The Cloud Run service has no container to update.")
    clean = []
    for item in containers:
        source = item if isinstance(item, dict) else {}
        clean.append({key: value for key, value in source.items() if key in _CONTAINER_KEYS})
    clean[0]["image"] = image
    template["containers"] = clean
    return template


async def _wait_operation(
    client: httpx.AsyncClient, url: str, token: str, *, tries: int = 180, delay: float = 2.0
) -> Dict[str, Any]:
    for _ in range(tries):
        response = await client.get(url, headers=_headers(token, json_body=False))
        if response.status_code >= 300:
            raise RuntimeError(_google_error(response))
        body = response.json()
        if body.get("done"):
            if body.get("error"):
                raise RuntimeError(body["error"].get("message") or "Google operation failed.")
            return body.get("response") or body
        await asyncio.sleep(delay)
    raise RuntimeError("Timed out waiting for Google Cloud to finish the operation.")


class GoogleCloudRunProvider(BaseDeployProvider):
    id = "google_cloud_run"
    display_name = "Google Cloud Run"
    icon = "cloud-cog"
    summary = "Build WebAgent from GitHub and deploy it directly to managed Cloud Run."
    requires = [
        "A Google Cloud project with billing enabled",
        "A deployment service account with Cloud Run Admin, Cloud Build Editor, "
        "Artifact Registry Admin, Storage Admin, Service Account User, and Logs Writer",
    ]
    progressive = True
    connect_config_keys = ["project_id"]
    shared_config_keys = ["region"]

    config_fields = [
        {"key": "project_id", "label": "Google Cloud project ID", "type": "text",
         "required": True, "placeholder": "my-project-123456",
         "link": {"url": "https://console.cloud.google.com/",
                  "label": "Open the Google Cloud console ↗"},
         "tip": "The project ID, not its display name. Billing must be enabled."},
        {"key": "region", "label": "Region", "type": "select", "default": "us-central1",
         "custom": True, "custom_label": "Custom…", "custom_placeholder": "e.g. us-east4",
         "tip": "The Cloud Run region closest to most users.",
         "options": [
             {"value": "us-central1", "label": "US Central — Iowa"},
             {"value": "us-east1", "label": "US East — South Carolina"},
             {"value": "us-west1", "label": "US West — Oregon"},
             {"value": "northamerica-northeast1", "label": "Canada — Montréal"},
             {"value": "europe-west1", "label": "Europe — Belgium"},
             {"value": "europe-west2", "label": "Europe — London"},
             {"value": "europe-west3", "label": "Europe — Frankfurt"},
             {"value": "asia-southeast1", "label": "Asia — Singapore"},
             {"value": "asia-northeast1", "label": "Asia — Tokyo"},
             {"value": "australia-southeast1", "label": "Australia — Sydney"},
         ]},
        {"key": "service_name", "label": "Cloud Run service name", "type": "text",
         "default": "webagent", "placeholder": "webagent",
         "tip": "Deploying the same name updates that service."},
        {"key": "cpu", "label": "CPU", "type": "select", "default": "2",
         "options": [
             {"value": "1", "label": "1 vCPU"},
             {"value": "2", "label": "2 vCPU — recommended"},
             {"value": "4", "label": "4 vCPU"},
         ]},
        {"key": "memory", "label": "Memory", "type": "select", "default": "2Gi",
         "options": [
             {"value": "1Gi", "label": "1 GiB"},
             {"value": "2Gi", "label": "2 GiB — recommended"},
             {"value": "4Gi", "label": "4 GiB"},
             {"value": "8Gi", "label": "8 GiB"},
         ]},
        {"key": "min_instances", "label": "Minimum instances", "type": "number", "default": 1,
         "tip": "Keep 1 for background agents, event renewals, and automation. Use 0 to scale to zero."},
        {"key": "max_instances", "label": "Maximum instances", "type": "number", "default": 3},
        {"key": "always_allocate_cpu", "label": "Allocate CPU outside requests",
         "type": "checkbox", "default": True,
         "tip": "Recommended for background agent work and schedulers."},
        {"key": "public_access", "label": "Allow public web access", "type": "checkbox",
         "default": True},
        {"key": "repo_url", "label": "Code to install (git repository)", "type": "text",
         "default": DEFAULT_REPO_URL},
        {"key": "visibility", "label": "Repository access", "type": "select",
         "default": "public", "options": [
             {"value": "public", "label": "Public"},
             {"value": "private", "label": "Private"},
         ]},
        {"key": "branch", "label": "Version (git branch)", "type": "text",
         "default": DEFAULT_BRANCH},
        {"key": "forget_keys", "label": "Forget my Google Cloud key after deploy",
         "type": "checkbox", "default": False,
         "tip": "Build & Deploy needs this key later. Turn this on only if you prefer "
                "to re-enter the key before every deployment."},
    ]
    credential_fields = [
        {"key": "service_account_json", "label": "Service-account key (JSON)",
         "type": "textarea", "secret": True, "required": True, "dropzone": True,
         "placeholder": "Paste the whole JSON key file here",
         "link": {"dynamic": True,
                  "url": "https://console.cloud.google.com/iam-admin/serviceaccounts"
                         "?project={project_id}",
                  "label": "Open Service Accounts for “{project_id}” ↗"},
         "tip": "Use a deployment service account with Cloud Run, Cloud Build, "
                "Artifact Registry, Storage, Service Account User, and Logs Writer permissions."},
        {"key": "github_token", "label": "GitHub access token (private repo)",
         "type": "password", "secret": True, "required": False},
        {"key": "admin_password", "label": "Admin password (optional)",
         "type": "password", "secret": True, "required": False},
    ]
    credential_required = ["service_account_json"]

    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        project = str(config.get("project_id") or "").strip()
        region = str(config.get("region") or "us-central1").strip()
        if not project:
            return {"ok": False, "detail": "Enter your Google Cloud project ID first."}
        try:
            token = await _access_token(creds)
            url = f"{_RUN}/projects/{quote(project)}/locations/{quote(region)}/services?pageSize=1"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=_headers(token, json_body=False))
            if response.status_code == 200:
                return {"ok": True, "detail": f"Connected to Cloud Run in {project} / {region}."}
            return {"ok": False, "detail": _google_error(response)}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    async def _github_source(
        self, repo: str, branch: str, visibility: str, github_token: str
    ) -> Tuple[bytes, str]:
        owner, name = _github_repo(repo)
        if visibility == "private" and not github_token:
            raise RuntimeError("That repository is private — add a GitHub access token in Repo details.")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "WebAgent-Cloud-Run"}
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            commit = await client.get(
                f"https://api.github.com/repos/{quote(owner)}/{quote(name)}"
                f"/commits/{quote(branch, safe='')}",
                headers=headers,
            )
            if commit.status_code >= 300:
                raise RuntimeError(
                    f"Could not resolve the GitHub branch: HTTP {commit.status_code}."
                )
            sha = str(commit.json().get("sha") or "").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                raise RuntimeError("GitHub did not return a valid commit for that branch.")
            response = await client.get(
                f"https://api.github.com/repos/{quote(owner)}/{quote(name)}"
                f"/tarball/{quote(sha, safe='')}",
                headers=headers,
            )
        if response.status_code >= 300:
            raise RuntimeError(f"Could not download the GitHub source: HTTP {response.status_code}.")
        return _flatten_github_archive(response.content), sha.lower()

    async def _source_archive(
        self, repo: str, branch: str, visibility: str, github_token: str
    ) -> bytes:
        archive, _sha = await self._github_source(repo, branch, visibility, github_token)
        return archive

    async def _ensure_bucket(
        self, client: httpx.AsyncClient, project: str, region: str, bucket: str, token: str
    ) -> None:
        response = await client.get(
            f"{_STORAGE}/b/{quote(bucket)}", headers=_headers(token, json_body=False)
        )
        if response.status_code == 200:
            return
        if response.status_code != 404:
            raise RuntimeError(f"Could not inspect the source bucket: {_google_error(response)}")
        response = await client.post(
            f"{_STORAGE}/b", params={"project": project}, headers=_headers(token),
            json={"name": bucket, "location": region, "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True}}},
        )
        if response.status_code >= 300 and response.status_code != 409:
            raise RuntimeError(f"Could not create the source bucket: {_google_error(response)}")

    async def _ensure_registry(
        self, client: httpx.AsyncClient, project: str, region: str, token: str
    ) -> None:
        name = f"projects/{project}/locations/{region}/repositories/webagent"
        response = await client.get(f"{_AR}/{name}", headers=_headers(token, json_body=False))
        if response.status_code == 200:
            return
        if response.status_code != 404:
            raise RuntimeError(f"Could not inspect Artifact Registry: {_google_error(response)}")
        response = await client.post(
            f"{_AR}/projects/{quote(project)}/locations/{quote(region)}/repositories",
            params={"repositoryId": "webagent"}, headers=_headers(token),
            json={"format": "DOCKER", "description": "WebAgent Cloud Run images"},
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Could not create Artifact Registry: {_google_error(response)}")
        operation = response.json().get("name", "")
        if operation:
            await _wait_operation(client, f"{_AR}/{operation}", token)

    async def _build(
        self, client: httpx.AsyncClient, project: str, bucket: str, obj: str,
        image: str, service_account: str, token: str,
    ) -> None:
        body = {
            "source": {"storageSource": {"bucket": bucket, "object": obj}},
            "steps": [{"name": "gcr.io/cloud-builders/docker",
                       "args": ["build", "-t", image, "."]}],
            "images": [image],
            "timeout": "1800s",
            "options": {"logging": "CLOUD_LOGGING_ONLY"},
            "serviceAccount": f"projects/{project}/serviceAccounts/{service_account}",
        }
        response = await client.post(
            f"{_BUILD}/projects/{quote(project)}/locations/global/builds",
            headers=_headers(token), json=body,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Cloud Build could not start: {_google_error(response)}")
        operation = response.json().get("name", "")
        if not operation:
            raise RuntimeError("Cloud Build did not return an operation.")
        result = await _wait_operation(
            client, f"{_BUILD}/{operation}", token, tries=240, delay=5.0
        )
        status = str(result.get("status") or "SUCCESS")
        if status != "SUCCESS":
            raise RuntimeError(result.get("statusDetail") or f"Cloud Build ended with {status}.")

    async def _deploy_service(
        self, client: httpx.AsyncClient, *, project: str, region: str, service: str,
        image: str, token: str, config: Dict[str, Any], creds: Dict[str, Any],
    ) -> Dict[str, Any]:
        env = []
        admin_password = str(creds.get("admin_password") or "").strip()
        if admin_password:
            if len(admin_password) < 6:
                raise RuntimeError("The admin password must be at least 6 characters.")
            env.append({"name": "BOOTSTRAP_ADMIN_PASSWORD", "value": admin_password})
        bootstrap_code = str(config.get("_bootstrap_code") or "").strip()
        if bootstrap_code:
            env.append({"name": "WA_BOOTSTRAP_CODE", "value": bootstrap_code})
        env.extend([
            {"name": "WEBAGENT_DEPLOY_PROVIDER", "value": self.id},
            {"name": "WEBAGENT_CLOUD_RUN_PROJECT", "value": project},
            {"name": "WEBAGENT_CLOUD_RUN_REGION", "value": region},
            {"name": "WEBAGENT_CLOUD_RUN_SERVICE", "value": service},
            {"name": "WEBAGENT_DEPLOY_REPO", "value": str(config.get("repo_url") or "").strip()},
            {"name": "WEBAGENT_DEPLOY_BRANCH", "value": str(config.get("branch") or "").strip()},
        ])
        minimum = max(0, int(config.get("min_instances") or 0))
        maximum = max(minimum or 1, int(config.get("max_instances") or 3))
        body = {
            "description": "WebAgent deployment managed from the New Deployment page",
            "ingress": "INGRESS_TRAFFIC_ALL",
            "invokerIamDisabled": bool(config.get("public_access", True)),
            "template": {
                "timeout": "3600s",
                "maxInstanceRequestConcurrency": 20,
                "containers": [{
                    "image": image,
                    "env": env,
                    "ports": [{"name": "http1", "containerPort": 8080}],
                    "resources": {
                        "limits": {
                            "cpu": str(config.get("cpu") or "2"),
                            "memory": str(config.get("memory") or "2Gi"),
                        },
                        "cpuIdle": not bool(config.get("always_allocate_cpu", True)),
                        "startupCpuBoost": True,
                    },
                }],
                "scaling": {"minInstanceCount": minimum, "maxInstanceCount": maximum},
            },
        }
        name = f"projects/{project}/locations/{region}/services/{service}"
        current = await client.get(f"{_RUN}/{name}", headers=_headers(token, json_body=False))
        if current.status_code == 404:
            response = await client.post(
                f"{_RUN}/projects/{quote(project)}/locations/{quote(region)}/services",
                params={"serviceId": service}, headers=_headers(token), json=body,
            )
        elif current.status_code == 200:
            body["name"] = name
            response = await client.patch(
                f"{_RUN}/{name}",
                params={"updateMask": "description,ingress,invokerIamDisabled,template"},
                headers=_headers(token), json=body,
            )
        else:
            raise RuntimeError(f"Could not inspect the Cloud Run service: {_google_error(current)}")
        if response.status_code >= 300:
            raise RuntimeError(f"Cloud Run deploy failed: {_google_error(response)}")
        operation = response.json().get("name", "")
        if not operation:
            raise RuntimeError("Cloud Run did not return an operation.")
        return await _wait_operation(
            client, f"{_RUN}/{operation}", token, tries=240, delay=3.0
        )

    async def _update_service_image(
        self, client: httpx.AsyncClient, *, project: str, region: str,
        service: str, image: str, token: str,
    ) -> Dict[str, Any]:
        """Create a new revision without rewriting the service configuration."""
        name = f"projects/{project}/locations/{region}/services/{service}"
        current = await client.get(f"{_RUN}/{name}", headers=_headers(token, json_body=False))
        if current.status_code == 404:
            raise RuntimeError(
                "That Cloud Run service no longer exists. Create it again from New Deployment."
            )
        if current.status_code >= 300:
            raise RuntimeError(f"Could not inspect the Cloud Run service: {_google_error(current)}")
        body = {
            "name": name,
            "template": _revision_template_with_image(current.json(), image),
        }
        response = await client.patch(
            f"{_RUN}/{name}",
            params={"updateMask": "template", "forceNewRevision": "true"},
            headers=_headers(token),
            json=body,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Cloud Run deploy failed: {_google_error(response)}")
        operation = response.json().get("name", "")
        if not operation:
            raise RuntimeError("Cloud Run did not return an operation.")
        return await _wait_operation(
            client, f"{_RUN}/{operation}", token, tries=240, delay=3.0
        )

    async def rebuild(
        self, config: Dict[str, Any], creds: Dict[str, Any]
    ) -> AsyncIterator[Dict[str, Any]]:
        """Build the configured GitHub revision and roll only the service image."""
        project = str(config.get("project_id") or "").strip()
        region = str(config.get("region") or "us-central1").strip()
        service = str(config.get("service_name") or "").strip().lower()
        repo = str(config.get("repo_url") or "").strip()
        branch = str(config.get("branch") or DEFAULT_BRANCH).strip()
        visibility = str(config.get("visibility") or "public").strip().lower()
        if not all((project, region, service, repo, branch)):
            yield done({
                "ok": False,
                "message": "This Cloud Run device is missing its deployment metadata.",
            })
            return
        if not _SERVICE_RE.fullmatch(service):
            yield done({"ok": False, "message": "The recorded Cloud Run service name is invalid."})
            return

        yield ev(f"Resolving {branch} on GitHub…", phase="source")
        try:
            archive, sha = await self._github_source(
                repo, branch, visibility, str(creds.get("github_token") or "").strip()
            )
            token = await _access_token(creds)
            account = (
                json.loads(creds["service_account_json"])
                if isinstance(creds.get("service_account_json"), str)
                else creds["service_account_json"]
            )
            service_account = account["client_email"]
        except Exception as exc:
            yield done({"ok": False, "message": str(exc)})
            return

        tag = sha[:12]
        bucket = f"{project}-webagent-run-source"
        obj = f"deploys/{service}-{tag}.tar.gz"
        image = f"{region}-docker.pkg.dev/{project}/webagent/{service}:{tag}"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                yield ev("Preparing source storage and Artifact Registry…", phase="prepare")
                await self._ensure_bucket(client, project, region, bucket, token)
                await self._ensure_registry(client, project, region, token)
                upload = await client.post(
                    f"{_UPLOAD}/b/{quote(bucket)}/o",
                    params={"uploadType": "media", "name": obj},
                    headers={
                        **_headers(token, json_body=False),
                        "Content-Type": "application/gzip",
                    },
                    content=archive,
                )
                if upload.status_code >= 300:
                    raise RuntimeError(
                        f"Could not upload the source archive: {_google_error(upload)}"
                    )
                yield ev(
                    f"Building commit {tag} (this can take several minutes)…",
                    phase="build",
                )
                await self._build(client, project, bucket, obj, image, service_account, token)
                yield ev(
                    f"Deploying commit {tag} to Cloud Run service '{service}'…",
                    phase="deploy",
                )
                deployed = await self._update_service_image(
                    client, project=project, region=region, service=service,
                    image=image, token=token,
                )
                await client.delete(
                    f"{_STORAGE}/b/{quote(bucket)}/o/{quote(obj, safe='')}",
                    headers=_headers(token, json_body=False),
                )
        except Exception as exc:
            logger.exception("google_cloud_run rebuild failed")
            yield done({"ok": False, "message": str(exc)})
            return

        yield ev("Cloud Run is serving the new revision.", phase="live", level="ok")
        yield done({
            "ok": True,
            "message": f"Deployed commit {tag} to {service}.",
            "commit": sha,
            "image": image,
            "public_url": str(deployed.get("uri") or "").strip(),
        })

    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        project = str(config.get("project_id") or "").strip()
        region = str(config.get("region") or "us-central1").strip()
        service = str(config.get("service_name") or "webagent").strip().lower()
        repo = str(config.get("repo_url") or DEFAULT_REPO_URL).strip()
        branch = str(config.get("branch") or DEFAULT_BRANCH).strip()
        visibility = str(config.get("visibility") or "public").strip().lower()
        if not project:
            yield done({"ok": False, "message": "No Google Cloud project ID set."})
            return
        if not _SERVICE_RE.fullmatch(service):
            yield done({"ok": False, "message": "Service name must use lowercase letters, numbers, and hyphens, start with a letter, and end with a letter or number."})
            return
        yield ev("Downloading the selected GitHub source…", phase="source")
        try:
            archive = await self._source_archive(
                repo, branch, visibility, str(creds.get("github_token") or "").strip()
            )
            token = await _access_token(creds)
            account = (
                json.loads(creds["service_account_json"])
                if isinstance(creds.get("service_account_json"), str)
                else creds["service_account_json"]
            )
            service_account = account["client_email"]
        except Exception as exc:
            yield done({"ok": False, "message": str(exc)})
            return

        stamp = f"{int(time.time())}-{secrets.token_hex(3)}"
        bucket = f"{project}-webagent-run-source"
        obj = f"deploys/{service}-{stamp}.tar.gz"
        image = f"{region}-docker.pkg.dev/{project}/webagent/{service}:{stamp}"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                yield ev("Preparing source storage and Artifact Registry…", phase="prepare")
                await self._ensure_bucket(client, project, region, bucket, token)
                await self._ensure_registry(client, project, region, token)
                upload = await client.post(
                    f"{_UPLOAD}/b/{quote(bucket)}/o",
                    params={"uploadType": "media", "name": obj},
                    headers={**_headers(token, json_body=False),
                             "Content-Type": "application/gzip"},
                    content=archive,
                )
                if upload.status_code >= 300:
                    raise RuntimeError(f"Could not upload the source archive: {_google_error(upload)}")
                yield ev("Building the WebAgent container (this can take several minutes)…", phase="build")
                await self._build(client, project, bucket, obj, image, service_account, token)
                yield ev(f"Deploying Cloud Run service '{service}' in {region}…", phase="deploy")
                deployed = await self._deploy_service(
                    client, project=project, region=region, service=service,
                    image=image, token=token, config=config, creds=creds,
                )
                public_url = str(deployed.get("uri") or "").strip()
                if not public_url:
                    check = await client.get(
                        f"{_RUN}/projects/{quote(project)}/locations/{quote(region)}"
                        f"/services/{quote(service)}",
                        headers=_headers(token, json_body=False),
                    )
                    if check.status_code == 200:
                        public_url = str(check.json().get("uri") or "").strip()
                await client.delete(
                    f"{_STORAGE}/b/{quote(bucket)}/o/{quote(obj, safe='')}",
                    headers=_headers(token, json_body=False),
                )
        except Exception as exc:
            logger.exception("google_cloud_run deploy failed")
            yield done({"ok": False, "message": str(exc)})
            return

        if int(config.get("min_instances") or 0) == 0:
            yield ev("This service can scale to zero. Background schedulers and agent helpers pause while no instance is warm.", phase="scaling", level="warn")
        yield ev("Cloud Run is serving the new revision.", phase="live", level="ok")
        yield done({
            "ok": True, "server": service, "project": project, "region": region,
            "zone": region, "public_url": public_url, "state": "running",
            "message": "WebAgent is live on Google Cloud Run.",
        })

    async def destroy(
        self, config: Dict[str, Any], creds: Dict[str, Any], record: Dict[str, Any]
    ) -> AsyncIterator[Dict[str, Any]]:
        project = str(record.get("project") or config.get("project_id") or "").strip()
        region = str(
            record.get("region") or record.get("zone") or config.get("region") or ""
        ).strip()
        service = str(record.get("server") or config.get("service_name") or "").strip()
        if not (project and region and service):
            yield done({"ok": False, "message": "No recorded Cloud Run service to tear down."})
            return
        yield ev("Authenticating with Google Cloud…", phase="auth")
        try:
            token = await _access_token(creds)
            name = f"projects/{project}/locations/{region}/services/{service}"
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.delete(
                    f"{_RUN}/{name}", headers=_headers(token, json_body=False)
                )
                if response.status_code == 404:
                    yield done({"ok": True, "deleted": True,
                                "message": "The Cloud Run service is already gone."})
                    return
                if response.status_code >= 300:
                    raise RuntimeError(_google_error(response))
                operation = response.json().get("name", "")
                if operation:
                    await _wait_operation(
                        client, f"{_RUN}/{operation}", token, tries=180, delay=2.0
                    )
        except Exception as exc:
            yield done({"ok": False, "message": f"Could not delete the Cloud Run service: {exc}"})
            return
        yield done({"ok": True, "deleted": True,
                    "message": f"Deleted Cloud Run service '{service}'."})


PROVIDER = GoogleCloudRunProvider()
