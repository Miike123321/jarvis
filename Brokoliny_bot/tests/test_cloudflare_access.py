"""Synthetic, offline tests for Cloudflare Access assertion verification."""

from __future__ import annotations

import base64
import json
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.cloudflare_access import AccessVerificationError, CloudflareAccessVerifier


_NOW = 1_800_000_000
_ISSUER = "https://example.cloudflareaccess.com"
_AUDIENCE = "synthetic-application-audience"
_EMAIL = "admin@example.test"
_KID = "synthetic-key-id"


def _b64url_integer(value: int) -> str:
    encoded = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


class CloudflareAccessJWKSCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_numbers = cls.private_key.public_key().public_numbers()
        cls.jwk = {
            "kid": _KID,
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "e": _b64url_integer(public_numbers.e),
            "n": _b64url_integer(public_numbers.n),
        }

    def _verifier(self, response: bytes) -> CloudflareAccessVerifier:
        verifier = CloudflareAccessVerifier(
            team="example",
            issuer=_ISSUER,
            audience=_AUDIENCE,
            allowed_emails=[_EMAIL],
            fetch_jwks=lambda _url: response,
            clock=lambda: _NOW,
        )
        verifier.ensure_ready()
        return verifier

    def _assertion(self, audience: object) -> str:
        return jwt.encode(
            {
                "iss": _ISSUER,
                "aud": audience,
                "exp": _NOW + 300,
                "iat": _NOW - 5,
                "email": _EMAIL,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": _KID},
        )

    def test_documented_certificate_metadata_is_accepted_but_not_trusted(self) -> None:
        # Cloudflare's documented response includes keys plus PEM certificate
        # metadata; only the validated JWK in keys is used for signatures.
        response = json.dumps(
            {
                "keys": [self.jwk],
                "public_cert": {"kid": _KID, "cert": "synthetic PEM"},
                "public_certs": [{"kid": _KID, "cert": "synthetic PEM"}],
            }
        ).encode()
        verifier = self._verifier(response)

        self.assertTrue(verifier.verify(self._assertion(_AUDIENCE)))
        self.assertTrue(verifier.verify(self._assertion([_AUDIENCE])))

    def test_jwks_still_requires_valid_nonempty_keys_and_unique_kids(self) -> None:
        cases = (
            {"public_cert": {"kid": _KID, "cert": "synthetic PEM"}},
            {"keys": []},
            {"keys": [self.jwk, self.jwk]},
            {"keys": [self.jwk], "unexpected": True},
        )
        for document in cases:
            with self.subTest(document=list(document)):
                verifier = CloudflareAccessVerifier(
                    team="example",
                    issuer=_ISSUER,
                    audience=_AUDIENCE,
                    allowed_emails=[_EMAIL],
                    fetch_jwks=lambda _url, body=json.dumps(document).encode(): body,
                    clock=lambda: _NOW,
                )
                with self.assertRaises(AccessVerificationError):
                    verifier.ensure_ready()

    def test_audience_uses_exact_jwt_string_or_array_membership(self) -> None:
        verifier = self._verifier(json.dumps({"keys": [self.jwk]}).encode())

        # A JWT audience array may list multiple intended recipients. The
        # configured audience must be an exact member, not a substring.
        self.assertTrue(verifier.verify(self._assertion([_AUDIENCE, "other-app"])))
        self.assertFalse(verifier.verify(self._assertion(["other-app"])))
        self.assertFalse(verifier.verify(self._assertion([_AUDIENCE, _AUDIENCE])))
        self.assertFalse(verifier.verify(self._assertion([_AUDIENCE, 123])))
        self.assertFalse(verifier.verify(self._assertion(123)))


if __name__ == "__main__":
    unittest.main()
