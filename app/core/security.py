"""Single credential verifier + two token strategies (ADR-002).

Web clients get a signed, expiring session cookie; the iOS client gets a
short-lived JWT access token plus a refresh token. Both paths authenticate
through the same verify_credentials().
"""

import hashlib
import hmac
import secrets
import time

import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import Settings

_PBKDF2_ITERATIONS = 600_000
_ALGO = "HS256"


def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def verify_credentials(username: str, password: str, settings: Settings) -> bool:
    """The single verifier both token strategies sit behind."""
    if not settings.admin_password_hash:
        return False
    user_ok = hmac.compare_digest(username, settings.admin_username)
    pass_ok = verify_password(password, settings.admin_password_hash)
    return user_ok and pass_ok


# --- Web: signed session cookie ---------------------------------------------

def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.jwt_secret, salt="web-session")


def issue_session_token(username: str, settings: Settings) -> str:
    return _serializer(settings).dumps({"sub": username})


def read_session_token(token: str, settings: Settings) -> str | None:
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_ttl_seconds)
        return data["sub"]
    except (BadSignature, SignatureExpired, KeyError):
        return None


# --- iOS: JWT access + refresh ----------------------------------------------

def issue_jwt_pair(username: str, settings: Settings) -> dict:
    now = int(time.time())
    access = jwt.encode(
        {"sub": username, "type": "access", "iat": now,
         "exp": now + settings.access_token_ttl_seconds},
        settings.jwt_secret, algorithm=_ALGO,
    )
    refresh = jwt.encode(
        {"sub": username, "type": "refresh", "iat": now,
         "exp": now + settings.refresh_token_ttl_seconds,
         "jti": secrets.token_hex(8)},
        settings.jwt_secret, algorithm=_ALGO,
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": settings.access_token_ttl_seconds,
    }


def read_jwt(token: str, settings: Settings, *, expected_type: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGO])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload.get("sub")
