import io
import os
import tarfile
import unittest
from unittest.mock import patch

from app.devices import identity
from app.deploy.providers.google_vm_cloud_run import (
    GoogleCloudRunProvider,
    _flatten_github_archive,
    _github_repo,
    _revision_template_with_image,
)


class GoogleCloudRunProviderTests(unittest.TestCase):
    def test_github_repo_parses_https_and_ssh(self):
        self.assertEqual(
            _github_repo("https://github.com/acme/webagent.git"), ("acme", "webagent")
        )
        self.assertEqual(
            _github_repo("git@github.com:acme/webagent.git"), ("acme", "webagent")
        )

    def test_github_repo_rejects_non_github_hosts(self):
        with self.assertRaisesRegex(ValueError, "GitHub"):
            _github_repo("https://example.com/acme/webagent")

    def test_flatten_github_archive_removes_generated_root(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            body = b"FROM python:3.14\n"
            info = tarfile.TarInfo("acme-webagent-deadbeef/Dockerfile")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        flattened = _flatten_github_archive(raw.getvalue())
        with tarfile.open(fileobj=io.BytesIO(flattened), mode="r:gz") as archive:
            self.assertEqual(archive.getnames(), ["Dockerfile"])
            self.assertEqual(
                archive.extractfile("Dockerfile").read(), b"FROM python:3.14\n"
            )

    def test_cloud_run_provider_is_progressive_and_cloud_target(self):
        provider = GoogleCloudRunProvider()
        self.assertEqual(provider.id, "google_cloud_run")
        self.assertTrue(provider.progressive)
        self.assertFalse(provider.manual)
        self.assertEqual(provider.credential_required, ["service_account_json"])

    def test_image_update_preserves_live_revision_configuration(self):
        service = {
            "template": {
                "revision": "output-only",
                "scaling": {"minInstanceCount": 1, "maxInstanceCount": 7},
                "serviceAccount": "runner@example.iam.gserviceaccount.com",
                "containers": [{
                    "name": "webagent",
                    "image": "old/image:tag",
                    "env": [{"name": "DATABASE_URL", "value": "secret"}],
                    "resources": {"limits": {"cpu": "4", "memory": "8Gi"}},
                    "buildInfo": {"functionTarget": "output-only"},
                }],
            }
        }
        template = _revision_template_with_image(service, "new/image:abc123")
        self.assertEqual(template["containers"][0]["image"], "new/image:abc123")
        self.assertEqual(template["scaling"]["maxInstanceCount"], 7)
        self.assertEqual(template["containers"][0]["env"][0]["name"], "DATABASE_URL")
        self.assertEqual(template["containers"][0]["resources"]["limits"]["cpu"], "4")
        self.assertNotIn("revision", template)
        self.assertNotIn("buildInfo", template["containers"][0])

    def test_cloud_run_identity_is_published_from_environment(self):
        values = {
            "WEBAGENT_DEPLOY_PROVIDER": "google_cloud_run",
            "WEBAGENT_CLOUD_RUN_PROJECT": "demo-project",
            "WEBAGENT_CLOUD_RUN_REGION": "us-east1",
            "WEBAGENT_CLOUD_RUN_SERVICE": "webagent-prod",
            "WEBAGENT_DEPLOY_REPO": "https://github.com/acme/webagent",
            "WEBAGENT_DEPLOY_BRANCH": "release",
        }
        with patch.dict(os.environ, values, clear=False):
            caps = identity.capabilities()
        self.assertEqual(caps["deployment_provider"], "google_cloud_run")
        self.assertEqual(caps["repo"], values["WEBAGENT_DEPLOY_REPO"])
        self.assertEqual(caps["branch"], "release")
        self.assertEqual(caps["cloud_run"]["service"], "webagent-prod")
