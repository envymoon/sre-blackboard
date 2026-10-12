"""Authentication and role checks.

Callers present an HS256 JWT. The tenant and roles come only from the verified token,
never from the request body. A token without a tenant is rejected (fail closed).

Human roles are ranked: viewer < responder < approver < admin, and a higher role
includes the lower ones. ``ingest`` is a separate service role for alert sources
such as Alertmanager; it grants nothing else.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

ROLE_RANK = {"viewer": 1, "responder": 2, "approver": 3, "admin": 4}
SERVICE_ROLES = frozenset({"ingest"})
ALL_ROLES = frozenset(ROLE_RANK) | SERVICE_ROLES
_ALGORITHM = "HS256"
_LEEWAY_S = 30


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str]

    def has(self, role: str) -> bool:
        if role in SERVICE_ROLES:
            return role in self.roles
        needed = ROLE_RANK[role]
        return any(ROLE_RANK.get(r, 0) >= needed for r in self.roles)


def mint_token(settings: Settings, *, subject: str, tenant_id: str, roles: list[str], ttl_s: int = 3600) -> str:
    unknown = set(roles) - ALL_ROLES
    if unknown:
        raise ValueError(f"unknown roles: {sorted(unknown)}")
    now = int(time.time())
    claims = {
        "sub": subject,
        "tenant": tenant_id,
        "roles": roles,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + ttl_s,
    }
    return jwt.encode(claims, _secret(settings), algorithm=_ALGORITHM)


def decode_token(settings: Settings, token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            _secret(settings),
            algorithms=[_ALGORITHM],  # pinned: never trust the token's own "alg" header
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=_LEEWAY_S,
            options={"require": ["sub", "exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
    tenant = claims.get("tenant")
    if not isinstance(tenant, str) or not tenant:
        raise AuthError("token has no tenant")
    roles = claims.get("roles")
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise AuthError("token has no valid roles claim")
    return Principal(subject=str(claims["sub"]), tenant_id=tenant, roles=frozenset(roles) & ALL_ROLES)


def validate_secret(settings: Settings) -> None:
    """Called at startup: refuse to run with a missing or weak signing key."""
    _secret(settings)


def _secret(settings: Settings) -> str:
    if settings.jwt_secret is None:
        raise RuntimeError("SRE_JWT_SECRET is not set")
    secret = settings.jwt_secret.get_secret_value()
    if len(secret) < 32:
        raise RuntimeError("SRE_JWT_SECRET must be at least 32 characters")
    return secret


_bearer = HTTPBearer(auto_error=False)


def _settings_from(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token", {"WWW-Authenticate": "Bearer"})
    try:
        return decode_token(_settings_from(request), credentials.credentials)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token", {"WWW-Authenticate": "Bearer"}) from exc


def require(role: str) -> Callable[[Principal], Principal]:
    """FastAPI dependency: an authenticated principal holding ``role`` (or a higher one)."""
    if role not in ALL_ROLES:
        raise ValueError(f"unknown role {role!r}")

    def dependency(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if not principal.has(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role}")
        return principal

    return dependency


def main() -> None:
    """``python -m app.core.security mint ...``: issue a token for local use and demos."""
    parser = argparse.ArgumentParser(prog="python -m app.core.security")
    sub = parser.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("mint", help="print a signed access token")
    mint.add_argument("--sub", required=True)
    mint.add_argument("--tenant", required=True)
    mint.add_argument("--roles", required=True, help="comma-separated, e.g. viewer or ingest")
    mint.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    args = parser.parse_args()
    print(
        mint_token(get_settings(), subject=args.sub, tenant_id=args.tenant, roles=args.roles.split(","), ttl_s=args.ttl)
    )


if __name__ == "__main__":
    main()
