"""HTTP Basic auth against a scrypt hash from .env, and CSRF tokens for POST actions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_basic_auth(header: str | None, username: str, password_hash: str) -> bool:
    if not header or not password_hash or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, password = decoded.partition(":")
    user_ok = hmac.compare_digest(user.encode(), username.encode())
    password_ok = verify_password(password, password_hash)
    return user_ok and password_ok


def csrf_token(password_hash: str, username: str) -> str:
    """Stable per installation; derived from the secret hash, never shown elsewhere."""
    key = hashlib.sha256(f"csrf:{password_hash}".encode()).digest()
    return hmac.new(key, username.encode(), hashlib.sha256).hexdigest()


def check_csrf(token: str | None, password_hash: str, username: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, csrf_token(password_hash, username))
