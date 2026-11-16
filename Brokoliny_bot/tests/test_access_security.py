"""Offline security contract: ephemeral RSA keys, synthetic claims and JWKS."""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.cloudflare_access import AccessVerificationError, CloudflareAccessVerifier, _fetch_jwks
from app.config import Settings
from app.database import Database
from app.security import SecretVault

NOW = 1_800_000_000
ISSUER = "https://example.cloudflareaccess.com"
AUD = "synthetic-audience"
EMAIL = "admin@example.test"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(key, kid):
    numbers = key.public_key().public_numbers()
    def integer(value):
        return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig",
            "n": integer(numbers.n), "e": integer(numbers.e)}


class AccessFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = jwk(cls.key, "one")
        cls.other_jwk = jwk(cls.other_key, "two")

    def setUp(self):
        self.now = NOW
        self.fetch = Mock(return_value=json.dumps({"keys": [self.jwk]}).encode())
        self.verifier = self.make_verifier()
        self.verifier.ensure_ready()

    def make_verifier(self, **kwargs):
        return CloudflareAccessVerifier(
            team="example", issuer=ISSUER, audience=AUD,
            allowed_emails=kwargs.pop("allowed_emails", [EMAIL]),
            fetch_jwks=kwargs.pop("fetch_jwks", self.fetch),
            clock=lambda: self.now, **kwargs,
        )

    def token(self, claims=None, *, key=None, kid="one", header=None):
        payload = {"iss": ISSUER, "aud": AUD, "email": EMAIL,
                   "exp": NOW + 300, "iat": NOW - 10}
        if claims:
            for name, value in claims.items():
                if value is ...:
                    payload.pop(name, None)
                else:
                    payload[name] = value
        headers = {"kid": kid}
        if header:
            headers.update(header)
        return jwt.encode(payload, key or self.key, algorithm="RS256", headers=headers)

    def test_pinned_endpoint_and_cached_valid_assertions(self):
        self.assertEqual(self.fetch.call_args.args[0], ISSUER + "/cdn-cgi/access/certs")
        self.assertTrue(self.verifier.verify(self.token()))
        self.assertTrue(self.verifier.verify(self.token({"aud": ["another", AUD]})))
        self.assertEqual(self.fetch.call_count, 1)

    def test_claim_validation_and_temporal_boundaries(self):
        bad = [
            {"iss": "https://other.cloudflareaccess.com"}, {"iss": ...},
            {"aud": "other"}, {"aud": [AUD + "-suffix"]}, {"aud": ...},
            {"exp": ...}, {"exp": NOW - 30}, {"exp": "later"}, {"exp": True},
            {"nbf": NOW + 31}, {"nbf": "later"}, {"nbf": NOW + 300},
            {"iat": NOW + 31}, {"exp": NOW + 90000},
            {"email": ...}, {"email": "other@example.test"},
            {"email": "broken"}, {"email": ["admin@example.test"]},
        ]
        for index, claims in enumerate(bad):
            with self.subTest(case=index):
                self.assertFalse(self.verifier.verify(self.token(claims)))
        self.assertTrue(self.verifier.verify(self.token({"exp": NOW - 29, "iat": NOW - 100})))
        self.assertTrue(self.verifier.verify(self.token({"nbf": NOW + 30})))
        self.assertTrue(self.verifier.verify(self.token({"iat": ..., "nbf": ...})))

    def test_forgery_algorithm_key_and_signature(self):
        valid = self.token()
        parts = valid.split(".")
        for changes in ({"alg": "none", "kid": "one"}, {"alg": "HS256", "kid": "one"},
                        {"alg": "RS256"}, {"alg": "RS256", "kid": "one", "jku": "https://evil.test"}):
            with self.subTest(header_type=changes.get("alg"), has_kid="kid" in changes):
                forged = ".".join([b64(json.dumps(changes).encode()), parts[1], parts[2]])
                self.assertFalse(self.verifier.verify(forged))
        self.assertFalse(self.verifier.verify(self.token(key=self.other_key)))
        self.assertFalse(self.verifier.verify(self.token(kid="unknown")))
        self.assertFalse(self.verifier.verify(".."))
        changed = bytearray(base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)))
        changed[0] ^= 1
        self.assertFalse(self.verifier.verify(".".join([parts[0], parts[1], b64(changed)])))

    def test_allowlist_is_required_and_valid(self):
        for emails in ([], [""], ["not-an-email"], [EMAIL, EMAIL.upper()]):
            with self.subTest(case=len(emails)):
                with self.assertRaises(AccessVerificationError):
                    self.make_verifier(allowed_emails=emails)
        self.assertTrue(self.make_verifier(allowed_emails=[EMAIL.upper()]).verify(self.token()))

    def test_rotation_unknown_kid_timeout_and_expired_cache(self):
        self.fetch.return_value = json.dumps({"keys": [self.other_jwk]}).encode()
        self.assertTrue(self.verifier.verify(self.token(key=self.other_key, kid="two")))
        self.assertEqual(self.fetch.call_count, 2)
        self.now += 2
        self.fetch.side_effect = httpx.ReadTimeout("synthetic timeout")
        self.assertFalse(self.verifier.verify(self.token(kid="missing")))
        self.assertTrue(self.verifier.verify(self.token(key=self.other_key, kid="two")))
        self.now += 301
        self.assertFalse(self.verifier.verify(self.token(key=self.other_key, kid="two")))
        self.now += 1801
        self.assertFalse(self.verifier.verify(self.token(key=self.other_key, kid="two")))

    def test_unknown_kid_refresh_is_rate_limited(self):
        self.assertFalse(self.verifier.verify(self.token(kid="unknown")))
        self.assertFalse(self.verifier.verify(self.token(kid="unknown")))
        self.assertEqual(self.fetch.call_count, 2)
        self.now += 1
        self.assertFalse(self.verifier.verify(self.token(kid="unknown")))
        self.assertEqual(self.fetch.call_count, 3)

    def test_bad_jwks_and_duplicate_json_are_rejected(self):
        bodies = [
            b"not-json", b'{"keys":[],"keys":[]}', b"x" * (256 * 1024 + 1),
            json.dumps({"keys": [self.jwk, self.jwk]}).encode(),
            json.dumps({"keys": [{**self.jwk, "kty": "EC"}]}).encode(),
            json.dumps({"keys": [{**self.jwk, "d": "private"}]}).encode(),
        ]
        for index, body in enumerate(bodies):
            with self.subTest(case=index):
                verifier = self.make_verifier(fetch_jwks=lambda _url, body=body: body)
                with self.assertRaises(AccessVerificationError):
                    verifier.ensure_ready()

    def test_fetch_timeout_blocks_startup(self):
        verifier = self.make_verifier(fetch_jwks=Mock(side_effect=httpx.ConnectTimeout("timeout")))
        with self.assertRaises(AccessVerificationError):
            verifier.ensure_ready()

    def test_jwks_http_transport_rejects_redirect_wrong_type_and_oversize(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_bytes.return_value = [b"{}"]
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream.return_value = response
        with patch("app.cloudflare_access.httpx.Client", return_value=client) as factory:
            for status, headers in ((302, {"content-type": "application/json"}),
                                    (200, {"content-type": "text/html"}),
                                    (200, {"content-type": "application/json", "content-length": "999999"})):
                response.status_code = status
                response.headers = headers
                with self.subTest(status=status, headers=list(headers)):
                    with self.assertRaises(AccessVerificationError):
                        _fetch_jwks(ISSUER + "/cdn-cgi/access/certs")
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertTrue(factory.call_args.kwargs["verify"])

    def test_duplicate_jwt_claims_and_header_names_are_rejected(self):
        parts = self.token().split(".")
        for index, value in ((0, b'{"alg":"RS256","kid":"one","kid":"one"}'),
                             (1, b'{"iss":"x","iss":"y"}')):
            forged = list(parts)
            forged[index] = b64(value)
            self.assertFalse(self.verifier.verify(".".join(forged)))


class AccessIntegrationTests(AccessFixture):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "test.sqlite3"
        key = Fernet.generate_key().decode()
        self.settings = Settings(
            telegram_token="synthetic", admin_token="synthetic-admin",
            data_encryption_key=key, database_path=path, default_delivery_time="09:00",
            timezone="UTC", daily_budget_usd=1, max_tokens=1000, app_mode="remote",
            cloudflare_access_team="example", cloudflare_access_audience=AUD,
            cloudflare_access_admin_emails=(EMAIL,),
        )
        with patch.dict(os.environ, {"DATABASE_PATH": str(path), "DATA_ENCRYPTION_KEY": key}):
            from app.admin import create_app
        db = Database(path)
        self.app = create_app(self.settings, db, SecretVault(key), object(), self.verifier)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_header_without_cookie_and_unrelated_cookie(self):
        for cookie in (None, "CF_Authorization=unrelated"):
            headers = {"Cf-Access-Jwt-Assertion": self.token()}
            if cookie:
                headers["Cookie"] = cookie
            with self.subTest(has_cookie=bool(cookie)):
                for path in ("/health", "/admin", "/static/admin.css"):
                    self.assertEqual(self.client.get(path, headers=headers).status_code, 200)

    def test_api_also_requires_admin_token(self):
        headers = {"Cf-Access-Jwt-Assertion": self.token()}
        for auth, expected in ((None, 401), ("Bearer incorrect", 401), ("Bearer synthetic-admin", 200)):
            with self.subTest(expected=expected):
                current = dict(headers)
                if auth:
                    current["Authorization"] = auth
                self.assertEqual(self.client.get("/admin/users", headers=current).status_code, expected)

    def test_all_paths_and_methods_reject_missing_and_invalid_access(self):
        paths = ("/health", "/static/admin.css", "/admin", "/admin/users", "/not-found")
        for path in paths:
            for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH",
                           "TRACE", "CONNECT", "CUSTOM"):
                for headers in ({}, {"Cookie": "CF_Authorization=anything"},
                                {"Cf-Access-Jwt-Assertion": self.token(key=self.other_key)},
                                {"Cf-Access-Authenticated-User-Email": EMAIL,
                                 "X-Forwarded-Host": "trusted.example", "Authorization": "Bearer synthetic-admin"}):
                    with self.subTest(path=path, method=method, case=len(headers)):
                        self.assertEqual(self.client.request(method, path, headers=headers).status_code, 401)

    def test_websocket_is_closed_even_with_valid_assertion(self):
        with self.assertRaises(WebSocketDisconnect) as error:
            with self.client.websocket_connect(
                "/health", headers={"Cf-Access-Jwt-Assertion": self.token()}
            ):
                pass
        self.assertEqual(error.exception.code, 4401)

    def test_duplicate_empty_and_malformed_headers(self):
        values = [[], [("Cf-Access-Jwt-Assertion", "")],
                  [("Cf-Access-Jwt-Assertion", "not-a-jwt")],
                  [("Cf-Access-Jwt-Assertion", self.token()), ("cf-access-jwt-assertion", self.token())]]
        for index, headers in enumerate(values):
            with self.subTest(case=index):
                self.assertEqual(self.client.get("/health", headers=headers).status_code, 401)

    def test_signed_email_cannot_be_replaced_by_forwarded_identity(self):
        headers = {"Cf-Access-Jwt-Assertion": self.token({"email": "other@example.test"}),
                   "Cf-Access-Authenticated-User-Email": EMAIL,
                   "Cookie": "CF_Authorization=irrelevant"}
        self.assertEqual(self.client.get("/health", headers=headers).status_code, 401)

    def test_local_mode_retains_public_health_and_api_token(self):
        from dataclasses import replace
        from app.admin import create_app
        settings = replace(self.settings, app_mode="local")
        app = create_app(settings, Database(settings.database_path), SecretVault(settings.data_encryption_key), object())
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/admin").status_code, 200)
            self.assertEqual(client.get("/static/admin.css").status_code, 200)
            self.assertEqual(client.get("/admin/users").status_code, 401)
            self.assertEqual(client.get("/admin/users", headers={"Authorization": "Bearer synthetic-admin"}).status_code, 200)

    def test_startup_fails_on_jwks_outage(self):
        self.fetch.side_effect = httpx.ReadTimeout("timeout")
        with self.assertRaises(AccessVerificationError):
            with TestClient(self.app):
                pass

    def test_access_denial_does_not_log_assertion(self):
        token = self.token(key=self.other_key)
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        with patch("app.cloudflare_access.httpx.Client") as transport:
            response = self.client.get("/health", headers={"Cf-Access-Jwt-Assertion": token})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(token in response.text)
        self.assertFalse(any(token in message for message in records))
        transport.assert_not_called()

    def test_invalid_remote_configuration_fails_before_opening_database(self):
        from dataclasses import replace
        from app.admin import create_app
        for values in ({"admin_token": ""}, {"cloudflare_access_team": "evil.test"},
                       {"cloudflare_access_audience": ""}, {"cloudflare_access_admin_emails": ()},
                       {"cloudflare_access_admin_emails": ("invalid",)}):
            with self.subTest(field=next(iter(values))):
                with patch("app.admin.Database") as database:
                    with self.assertRaises((RuntimeError, AccessVerificationError)):
                        create_app(replace(self.settings, **values))
                    database.assert_not_called()
