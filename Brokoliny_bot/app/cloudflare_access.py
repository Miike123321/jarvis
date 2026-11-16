"""Fail-closed validation of Cloudflare Access JWT assertions at the origin."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx


_MAX_ASSERTION_BYTES = 16 * 1024
_MAX_JWKS_BYTES = 256 * 1024
_MAX_JWKS_KEYS = 32
_MAX_KID_LENGTH = 256
_HARD_CACHE_AGE_SECONDS = 1800
_MAX_TOKEN_LIFETIME_SECONDS = 24 * 60 * 60
_TEAM_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_EMAIL_RE = re.compile(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$")
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class AccessVerificationError(ValueError):
    """Safe, detail-free failure category for Access assertion checks."""


def _json_without_duplicate_names(data: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AccessVerificationError("duplicate JSON member")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AccessVerificationError("malformed JSON") from exc


def _decode_segment(segment: str) -> bytes:
    if not segment or not _BASE64URL_RE.fullmatch(segment) or len(segment) % 4 == 1:
        raise AccessVerificationError("malformed JWT segment")
    try:
        return base64.b64decode(
            segment + "=" * (-len(segment) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise AccessVerificationError("malformed JWT segment") from exc


def _numeric_date(value: object, claim: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AccessVerificationError(f"invalid {claim}")
    result = float(value)
    if not math.isfinite(result):
        raise AccessVerificationError(f"invalid {claim}")
    return result


def _normalize_email(value: object) -> str:
    if not isinstance(value, str):
        raise AccessVerificationError("invalid email")
    normalized = value.strip().casefold()
    if (
        len(normalized) > 254
        or not _EMAIL_RE.fullmatch(normalized)
        or normalized.startswith(".")
        or normalized.endswith(".")
        or ".." in normalized
    ):
        raise AccessVerificationError("invalid email")
    return normalized


def _fetch_jwks(url: str) -> bytes:
    """Fetch bounded JSON over verified TLS; redirects and proxy env are disabled."""
    timeout = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        verify=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        headers={"Accept": "application/json"},
    ) as client:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise AccessVerificationError("JWKS HTTP failure")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/json", "application/jwk-set+json"}:
                raise AccessVerificationError("invalid JWKS content type")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > _MAX_JWKS_BYTES:
                        raise AccessVerificationError("JWKS too large")
                except ValueError as exc:
                    raise AccessVerificationError("invalid JWKS length") from exc
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_JWKS_BYTES:
                    raise AccessVerificationError("JWKS too large")
                chunks.append(chunk)
            return b"".join(chunks)


class CloudflareAccessVerifier:
    """Verify one Access assertion against a pinned team's certificate endpoint.

    ``PyJWT`` performs the RS256 signature verification. Trust roots are never
    selected from a JWT header, request host, cookie, or forwarded header.
    """

    def __init__(
        self,
        team: str,
        issuer: str,
        audience: str,
        allowed_emails: tuple[str, ...] | list[str],
        *,
        clock_skew_seconds: int = 30,
        cache_ttl_seconds: int = 300,
        fetch_jwks: Callable[[str], bytes] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(team, str) or not _TEAM_RE.fullmatch(team):
            raise AccessVerificationError("invalid team configuration")
        expected_issuer = f"https://{team}.cloudflareaccess.com"
        if issuer != expected_issuer or audience == "" or len(audience) > 512:
            raise AccessVerificationError("invalid issuer or audience configuration")
        if not 0 <= clock_skew_seconds <= 60:
            raise AccessVerificationError("invalid clock skew configuration")
        if not 30 <= cache_ttl_seconds <= 900:
            raise AccessVerificationError("invalid JWKS cache configuration")
        if not allowed_emails or len(allowed_emails) > 100:
            raise AccessVerificationError("invalid allowlist configuration")
        normalized_emails = frozenset(_normalize_email(item) for item in allowed_emails)
        if len(normalized_emails) != len(allowed_emails):
            raise AccessVerificationError("duplicate allowlist entry")

        try:
            import jwt
        except ImportError as exc:
            raise AccessVerificationError("PyJWT is unavailable") from exc

        self._jwt = jwt
        self.team = team
        self.issuer = issuer
        self.audience = audience
        self._allowed_emails = normalized_emails
        self._clock_skew = clock_skew_seconds
        self._cache_ttl = cache_ttl_seconds
        self._fetch = fetch_jwks or _fetch_jwks
        self._clock = clock
        self._url = f"{expected_issuer}/cdn-cgi/access/certs"
        self._lock = threading.RLock()
        self._keys: dict[str, Any] = {}
        self._cache_fetched_at: float | None = None
        self._last_unknown_refresh_at: float | None = None

    def ensure_ready(self) -> None:
        """Load a verified JWKS before remote service startup accepts requests."""
        with self._lock:
            self._refresh_locked()

    def verify(self, assertion: str) -> bool:
        try:
            return self._verify(assertion)
        except Exception:
            # Do not log or propagate attacker-controlled parsing/crypto details.
            return False

    def _verify(self, assertion: str) -> bool:
        if (
            not isinstance(assertion, str)
            or not assertion
            or len(assertion.encode("utf-8", errors="ignore")) > _MAX_ASSERTION_BYTES
        ):
            return False
        parts = assertion.split(".")
        if len(parts) != 3:
            return False
        header_value = _json_without_duplicate_names(_decode_segment(parts[0]))
        claims = _json_without_duplicate_names(_decode_segment(parts[1]))
        if not isinstance(header_value, dict) or not isinstance(claims, dict):
            return False
        if (
            header_value.get("alg") != "RS256"
            or set(header_value) - {"alg", "kid", "typ"}
            or (
                "typ" in header_value
                and header_value["typ"] != "JWT"
            )
        ):
            return False
        kid = header_value.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > _MAX_KID_LENGTH:
            return False

        key = self._key_for(kid)
        if key is None:
            return False
        try:
            decoded = self._jwt.decode(
                assertion,
                key,
                algorithms=["RS256"],
                options={
                    "verify_signature": True,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "verify_iss": False,
                    "verify_aud": False,
                    "verify_sub": False,
                    "verify_jti": False,
                },
            )
        except Exception:
            return False
        if not isinstance(decoded, dict) or decoded != claims:
            return False

        if claims.get("iss") != self.issuer:
            return False
        # JWT permits a scalar audience or an array of intended recipients.
        # Match the configured AUD as an exact array element (never substring
        # or coercive matching), as standard JWT audience validators do.
        aud = claims.get("aud")
        if not (
            aud == self.audience
            or (
                isinstance(aud, list)
                and aud
                and all(isinstance(item, str) for item in aud)
                and len(aud) == len(set(aud))
                and self.audience in aud
            )
        ):
            return False
        try:
            now = self._clock()
            exp = _numeric_date(claims.get("exp"), "exp")
            nbf = (
                _numeric_date(claims["nbf"], "nbf")
                if "nbf" in claims
                else None
            )
            iat = (
                _numeric_date(claims["iat"], "iat")
                if "iat" in claims
                else None
            )
            if exp <= now - self._clock_skew:
                return False
            if nbf is not None and nbf > now + self._clock_skew:
                return False
            if nbf is not None and exp <= nbf:
                return False
            if iat is not None and exp <= iat:
                return False
            lifetime_start = iat if iat is not None else nbf
            if lifetime_start is not None:
                if lifetime_start > now + self._clock_skew:
                    return False
                if exp - lifetime_start > _MAX_TOKEN_LIFETIME_SECONDS:
                    return False
            elif exp - now > _MAX_TOKEN_LIFETIME_SECONDS:
                return False
            if iat is not None and iat > now + self._clock_skew:
                return False
            email = _normalize_email(claims.get("email"))
        except (AccessVerificationError, OverflowError, TypeError, ValueError):
            return False
        return email in self._allowed_emails

    def _key_for(self, kid: str) -> Any | None:
        now = self._clock()
        with self._lock:
            fresh = (
                self._cache_fetched_at is not None
                and 0 <= now - self._cache_fetched_at <= self._cache_ttl
            )
            if fresh and kid in self._keys:
                return self._keys[kid]

            # An unknown kid forces a refresh even when the previous set is
            # fresh, to support key rotation. Repeated unknown-kid requests are
            # bounded to one refresh per second; startup refreshes do not count.
            if kid not in self._keys:
                if (
                    self._last_unknown_refresh_at is not None
                    and 0 <= now - self._last_unknown_refresh_at < 1
                ):
                    return None
                self._last_unknown_refresh_at = now
            try:
                self._refresh_locked()
            except Exception:
                return None
            return self._keys.get(kid)

    def _refresh_locked(self) -> None:
        now = self._clock()
        if (
            self._cache_fetched_at is not None
            and now - self._cache_fetched_at > _HARD_CACHE_AGE_SECONDS
        ):
            self._keys = {}
            self._cache_fetched_at = None
        try:
            response = self._fetch(self._url)
            if not isinstance(response, bytes) or len(response) > _MAX_JWKS_BYTES:
                raise AccessVerificationError("invalid JWKS response")
            document = _json_without_duplicate_names(response)
            # Cloudflare also documents PEM metadata fields alongside keys.
            # They are ignored as trust material; signatures use only `keys`.
            if (
                not isinstance(document, dict)
                or "keys" not in document
                or set(document) - {"keys", "public_cert", "public_certs"}
                or not isinstance(document["keys"], list)
                or not 1 <= len(document["keys"]) <= _MAX_JWKS_KEYS
            ):
                raise AccessVerificationError("invalid JWKS structure")
            parsed: dict[str, Any] = {}
            for item in document["keys"]:
                if not isinstance(item, dict):
                    raise AccessVerificationError("invalid JWK")
                kid = item.get("kid")
                if (
                    not isinstance(kid, str)
                    or not kid
                    or len(kid) > _MAX_KID_LENGTH
                    or kid in parsed
                    or item.get("kty") != "RSA"
                    or item.get("use", "sig") != "sig"
                    or item.get("alg", "RS256") != "RS256"
                    or (
                        "key_ops" in item
                        and (
                            not isinstance(item["key_ops"], list)
                            or "verify" not in item["key_ops"]
                            or set(item["key_ops"]) - {"verify"}
                        )
                    )
                    or not isinstance(item.get("n"), str)
                    or not isinstance(item.get("e"), str)
                    or any(
                        name in item
                        for name in ("d", "p", "q", "dp", "dq", "qi", "oth", "x5u")
                    )
                ):
                    raise AccessVerificationError("unsupported JWK")
                parsed_key = self._jwt.algorithms.RSAAlgorithm.from_jwk(
                    json.dumps(item, separators=(",", ":"))
                )
                if (
                    not 2048 <= parsed_key.key_size <= 8192
                    or parsed_key.public_numbers().e < 3
                    or parsed_key.public_numbers().e % 2 == 0
                ):
                    raise AccessVerificationError("unsupported RSA key size")
                parsed[kid] = parsed_key
            self._keys = parsed
            self._cache_fetched_at = now
        except Exception as exc:
            # Keep a previously fresh set only for known keys; callers trying
            # an expired/unknown key still fail closed.
            raise AccessVerificationError("JWKS refresh failed") from exc


class CloudflareAccessMiddleware:
    """Apply the origin Access gate to every HTTP request and reject sockets."""

    def __init__(self, app: Any, verifier: CloudflareAccessVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        if scope_type != "http":
            await self._deny(send)
            return

        values = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"cf-access-jwt-assertion"
        ]
        if len(values) != 1:
            await self._deny(send)
            return
        try:
            assertion = values[0].decode("ascii")
        except UnicodeDecodeError:
            await self._deny(send)
            return
        if not assertion or not await asyncio.to_thread(self.verifier.verify, assertion):
            await self._deny(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send: Any) -> None:
        body = b'{"detail":"Access authentication required."}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

