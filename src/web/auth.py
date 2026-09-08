"""Authentication and authorisation for the EpiAlert web application.

Passwords are hashed with bcrypt. Sessions are signed cookies (itsdangerous),
so the cookie carries the username but cannot be forged without SECRET_KEY.

Authorisation is enforced server-side by the `require_user` dependency. Hiding
a link in a template is not access control: every protected route and API
endpoint depends on `require_user`, so an unauthenticated request is rejected
by the server with 401/403 regardless of what the UI shows.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer

from src.database.db import get_connection

logger = logging.getLogger("web.auth")

SESSION_COOKIE = "epialert_session"

# bcrypt refuses inputs over 72 bytes rather than silently truncating them.
BCRYPT_MAX_BYTES = 72


def _secret_key() -> str:
    """Return the cookie signing key.

    Taken from EPIALERT_SECRET_KEY. When unset a random key is generated for
    the process, which is safe but invalidates sessions on restart -- set the
    variable in .env for a stable deployment. Never hardcode a key in source.
    """
    key = os.getenv("EPIALERT_SECRET_KEY", "")
    if not key:
        key = secrets.token_urlsafe(32)
        logger.warning(
            "EPIALERT_SECRET_KEY not set; generated an ephemeral key. "
            "Sessions will not survive a restart."
        )
    return key


_serializer = URLSafeSerializer(_secret_key(), salt="epialert-session")


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------

def _encode(password: str) -> bytes:
    """Encode a password for bcrypt, rejecting over-long input.

    bcrypt only considers the first 72 bytes. Truncating silently would mean
    two different passwords sharing a hash, so an over-long one is refused.
    """
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"password must be at most {BCRYPT_MAX_BYTES} bytes "
            f"(got {len(raw)})"
        )
    return raw


def hash_password(password: str) -> str:
    """Return a bcrypt hash of the password.

    bcrypt is deliberately slow, so a leaked users table cannot be
    brute-forced offline the way a plain SHA-256 table could.
    """
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """True if the password matches the stored hash."""
    try:
        return bcrypt.checkpw(_encode(password), password_hash.encode("utf-8"))
    except ValueError:
        # Over-long input or a malformed hash in the database: treat as a
        # failed login rather than letting it surface as a 500.
        logger.warning("Rejected password verification: bad input or hash")
        return False


# ---------------------------------------------------------------------------
# User records
# ---------------------------------------------------------------------------

def create_user(username: str, password: str) -> int:
    """Create a user and return its user_id.

    Raises ValueError if the username already exists.
    """
    if not username or not password:
        raise ValueError("username and password are both required")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE username = ?", (username,))
    if cur.fetchone() is not None:
        cur.close()
        conn.close()
        raise ValueError(f"user {username!r} already exists")

    cur.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (username, hash_password(password)),
    )
    user_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Created user %s (id=%s)", username, user_id)
    return user_id


def authenticate(username: str, password: str) -> Optional[str]:
    """Return the username if credentials are valid, else None."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT password_hash FROM users WHERE username = ?", (username,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row is None:
        return None
    if not verify_password(password, row[0]):
        return None
    return username


def user_count() -> int:
    """Number of registered users. Used to detect first-run setup."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Session cookies
# ---------------------------------------------------------------------------

def make_session(username: str) -> str:
    """Return a signed session value for this username."""
    return _serializer.dumps({"username": username})


def read_session(raw: Optional[str]) -> Optional[str]:
    """Return the username from a signed cookie, or None if absent/invalid."""
    if not raw:
        return None
    try:
        data = _serializer.loads(raw)
    except BadSignature:
        logger.info("Rejected a session cookie with a bad signature")
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("username")
    return username if isinstance(username, str) else None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def current_user(request: Request) -> Optional[str]:
    """Username of the signed-in user, or None. Never raises."""
    return read_session(request.cookies.get(SESSION_COOKIE))


def require_user(request: Request) -> str:
    """Dependency enforcing authentication. Raises 401 when not signed in.

    Every protected route and API endpoint must depend on this. Server-side
    enforcement is the point: omitting a link from a template hides a feature
    but does not protect the endpoint behind it.
    """
    username = current_user(request)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return username


CurrentUser = Depends(require_user)
