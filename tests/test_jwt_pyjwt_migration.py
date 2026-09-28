import unittest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from plugins.auth_providers.apple import apple


class ApplePyJWTMigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_decodes_token_minted_by_python_jose(self):
        legacy_token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJsZWdhY3ktdXNlciIsInVzZXJfaWQiOiJsZWdhY3ktaWQiLCJleHAiOjQxMDI0NDQ4MDB9."
            "yekYcX4MyRfkcqYtXzf-CP7KEu1vaNTD2wtGYRc1vb8"
        )

        claims = jwt.decode(
            legacy_token,
            "legacy-test-secret-32-bytes-long!!",
            algorithms=["HS256"],
        )

        self.assertEqual(claims["user_id"], "legacy-id")

    def test_client_secret_is_a_valid_es256_token(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

        token = apple.build_client_secret(
            descriptor={},
            creds={
                "service_id": "com.example.web",
                "team_id": "TEAM123",
                "key_id": "KEY123",
                "private_key": private_pem,
            },
        )

        self.assertEqual(jwt.get_unverified_header(token)["kid"], "KEY123")
        claims = jwt.decode(
            token,
            private_key.public_key(),
            algorithms=["ES256"],
            audience="https://appleid.apple.com",
        )
        self.assertEqual(claims["iss"], "TEAM123")
        self.assertEqual(claims["sub"], "com.example.web")

    async def test_profile_extraction_preserves_unverified_id_token_read(self):
        id_token = jwt.encode(
            {"sub": "apple-user-1", "email": "person@example.com"},
            "test-only-key-that-is-at-least-32-bytes",
            algorithm="HS256",
        )

        profile = await apple.extract_profile(
            descriptor={},
            token_data={"id_token": id_token},
            access_token="",
            http=None,
            creds={},
        )

        self.assertEqual(profile["external_id"], "apple-user-1")
        self.assertEqual(profile["email"], "person@example.com")
        self.assertEqual(profile["name"], "person")


if __name__ == "__main__":
    unittest.main()
