import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Union

import jwt
from app.core.config import settings
from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()
# Verified against when there is no real hash, so "unknown email" and "wrong password"
# take about the same time (stops account enumeration by timing).
_DUMMY_HASH = password_hash.hash("not-a-real-password")


def create_access_token(
    subject: Union[str, Any],
    expires_delta: Optional[timedelta] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    now = datetime.now(timezone.utc)
    expire = now + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode: Dict[str, Any] = {
        "exp": expire,
        "iat": now,
        "sub": str(subject),  # the User id (login), not a Member id
        "type": "access",
    }
    if extra_claims:
        to_encode.update(extra_claims)
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Raises jwt.PyJWTError (expired, bad signature, ...) on any problem."""
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],  # always pin the algorithm
        options={"require": ["exp", "sub"]},
    )
    if payload.get("type", "access") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return payload


def create_refresh_token() -> str:
    """Opaque random token returned to the client. Store only hash_token(token)."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """SHA-256 hex digest (64 chars). Save this in UserSession.refresh_token and look
    sessions up with hash_token(incoming_token), so a database leak can't be replayed.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    # Users without a password (invited, social login) have hashed_password = None.
    if not hashed_password:
        password_hash.verify(plain_password, _DUMMY_HASH)
        return False
    return password_hash.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return password_hash.hash(password)
