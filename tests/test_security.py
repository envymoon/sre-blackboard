"""Token verification and role checks (no database needed)."""

from __future__ import annotations

import time

import jwt
import pytest

from app.core.config import Settings
from app.core.security import AuthError, Principal, decode_token, mint_token, validate_secret

SECRET = "unit-test-secret-that-is-at-least-32-bytes"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, jwt_secret=SECRET)  # type: ignore[call-arg]


def raw_token(settings: Settings, **claims: object) -> str:
    now = int(time.time())
    base = {"sub": "u1", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": now, "exp": now + 60}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, SECRET, algorithm="HS256")


def test_round_trip(settings):
    token = mint_token(settings, subject="alice", tenant_id="t1", roles=["responder"])
    assert decode_token(settings, token) == Principal("alice", "t1", frozenset({"responder"}))


@pytest.mark.parametrize(
    "token_claims",
    [
        {"tenant": None, "roles": ["viewer"]},  # no tenant: fail closed (B6)
        {"tenant": "", "roles": ["viewer"]},
        {"tenant": "t1", "roles": None},
        {"tenant": "t1", "roles": ["viewer"], "exp": int(time.time()) - 3600},
        {"tenant": "t1", "roles": ["viewer"], "aud": "someone-else"},
        {"tenant": "t1", "roles": ["viewer"], "iss": "someone-else"},
    ],
)
def test_rejects_bad_claims(settings, token_claims):
    with pytest.raises(AuthError):
        decode_token(settings, raw_token(settings, **token_claims))


def test_rejects_wrong_key_and_unsigned_tokens(settings):
    forged = jwt.encode({"sub": "x", "tenant": "t1", "roles": ["admin"]}, "another-key-of-at-least-32-bytes!", "HS256")
    with pytest.raises(AuthError):
        decode_token(settings, forged)
    unsigned = jwt.encode({"sub": "x", "tenant": "t1", "roles": ["admin"]}, None, algorithm="none")
    with pytest.raises(AuthError):
        decode_token(settings, unsigned)


def test_unknown_roles_are_dropped(settings):
    principal = decode_token(settings, raw_token(settings, tenant="t1", roles=["viewer", "superuser"]))
    assert principal.roles == frozenset({"viewer"})


def test_role_hierarchy():
    approver = Principal("a", "t1", frozenset({"approver"}))
    assert approver.has("viewer") and approver.has("responder") and approver.has("approver")
    assert not approver.has("admin") and not approver.has("ingest")
    source = Principal("am", "t1", frozenset({"ingest"}))
    assert source.has("ingest") and not source.has("viewer")


@pytest.mark.parametrize("secret", [None, "too-short"])
def test_weak_or_missing_secret_refuses_to_start(secret):
    with pytest.raises(RuntimeError):
        validate_secret(Settings(_env_file=None, jwt_secret=secret))  # type: ignore[call-arg]
