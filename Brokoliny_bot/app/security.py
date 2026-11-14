from __future__ import annotations

import hmac

from cryptography.fernet import Fernet, InvalidToken


class SecretVault:
    def __init__(self, key: str) -> None:
        if not key:
            raise RuntimeError("DATA_ENCRYPTION_KEY is required for encrypted data.")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("DATA_ENCRYPTION_KEY must be a valid Fernet key.") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise RuntimeError("Stored encrypted value could not be decrypted.") from exc


def valid_admin_token(candidate: str | None, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)
