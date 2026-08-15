"""Operator auth: local accounts + roles. PLAN.md §11A.

Passwords are hashed with argon2. Bearer tokens (session or long-lived API
tokens) are random and stored only as a sha256 hash, so a DB leak doesn't
hand out usable credentials.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from folia_mgmt.db import get_session
from folia_mgmt.models import ApiToken, JoinToken, User, UserRole, utcnow

_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)

# Roles ordered least → most privileged; a required role of X is satisfied
# by any role at or above X in this list.
_ROLE_ORDER = [UserRole.viewer, UserRole.operator, UserRole.admin]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def new_token() -> tuple[str, str]:
    """Returns (plaintext_token, sha256_hash) — only the hash is stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session_token(session: Session, user: User, ttl: timedelta = timedelta(hours=12)) -> str:
    token, token_hash = new_token()
    session.add(
        ApiToken(
            token_hash=token_hash,
            user_id=user.id,
            kind="session",
            expires_at=utcnow() + ttl,
        )
    )
    session.commit()
    return token


def create_api_token(session: Session, user: User) -> str:
    token, token_hash = new_token()
    session.add(ApiToken(token_hash=token_hash, user_id=user.id, kind="api", expires_at=None))
    session.commit()
    return token


def _resolve_bearer_token(
    session: Session,
    credentials: HTTPAuthorizationCredentials | None,
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    token_hash = hash_token(credentials.credentials)
    record = session.exec(select(ApiToken).where(ApiToken.token_hash == token_hash)).first()
    if record is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    if record.expires_at is not None and utcnow() > record.expires_at:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")

    user = session.get(User, record.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> User:
    return _resolve_bearer_token(session, credentials)


def require_role(minimum: UserRole):
    """FastAPI dependency factory: 403s unless the caller's role >= minimum."""

    def _dependency(user: User = Depends(get_current_user)) -> User:
        if _ROLE_ORDER.index(user.role) < _ROLE_ORDER.index(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires role '{minimum.value}' or higher, caller has '{user.role.value}'",
            )
        return user

    return _dependency


require_viewer = require_role(UserRole.viewer)
require_operator = require_role(UserRole.operator)
require_admin = require_role(UserRole.admin)


# -- host join tokens (PLAN.md §4) -------------------------------------------


def create_join_token(session: Session, user: User, ttl: timedelta) -> str:
    token, token_hash = new_token()
    session.add(
        JoinToken(token_hash=token_hash, created_by=user.id, expires_at=utcnow() + ttl)
    )
    session.commit()
    return token


def consume_join_token(session: Session, token: str) -> JoinToken:
    """Validate a join token and mark it used. Raises 401 if invalid/expired/used."""
    token_hash = hash_token(token)
    record = session.exec(select(JoinToken).where(JoinToken.token_hash == token_hash)).first()
    if record is None or not record.is_valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid, expired, or already-used join token")
    return record


def get_bearer_token_value(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return credentials.credentials
