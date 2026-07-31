"""Tester-account auth helpers: password hashing, validation, session secret.

Passwords use stdlib pbkdf2 (no bcrypt/passlib dependency — avoids a compiled
wheel on the Pi). Sessions are signed cookies via Starlette's SessionMiddleware;
the signing key is persisted in app_settings so logins survive a web restart.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

_PBKDF2_ITERS = 240_000
_USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,64}$")

# A password_hash that verify_password always rejects — used for the operator
# account created by the backfill when no password has been set yet.
UNUSABLE_PASSWORD = "!"


def hash_password(pw: str) -> str:
    """Return a self-describing pbkdf2 hash: ``pbkdf2_sha256$iters$salt$hash``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    """Constant-time check of ``pw`` against a stored pbkdf2 hash. False on any
    malformed/empty/unusable hash."""
    if not stored or stored == UNUSABLE_PASSWORD:
        return False
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def validate_password_rules(pw: str) -> str | None:
    """Return an error message if the password is unacceptable, else None."""
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if len(pw) > 128:
        return "Password must be at most 128 characters."
    return None


def constant_time_eq(a: str, b: str) -> bool:
    """Timing-safe string compare (for the invite code)."""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def normalize_username(raw: str) -> str:
    """Case-fold + trim so usernames are compared/stored consistently."""
    return (raw or "").strip().casefold()


def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.match(name))


def get_or_create_secret_key(db) -> str:
    """Stable session-signing key: reuse the persisted one, else generate and
    persist (so cookie sessions survive a web-process restart). Callers should
    prefer an explicit ``BIRDBRAIN_SECRET_KEY`` from config when set."""
    key = db.get_setting("session_secret_key")
    if key:
        return key
    key = secrets.token_hex(32)
    db.set_setting("session_secret_key", key)
    return key
