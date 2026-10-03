"""get_human_session: only a first-party Clerk session token reaches the local-session doors that
put keystrokes or a screen on a person's Mac (/input, /viewing, /resume)."""

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import core.auth as auth
from core.auth import AuthContext

ALICE = AuthContext(user_id="user_alice", org_id="org_1")


def token(claims: dict, headers: dict | None = None) -> HTTPAuthorizationCredentials:
    # The signature was checked by get_current_user (stubbed here); only the shape matters.
    raw = jwt.encode({"sub": "user_alice", **claims}, "k" * 32, algorithm="HS256", headers=headers)
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)


@pytest.fixture(autouse=True)
def _verified(monkeypatch):
    async def verified(credentials):
        return ALICE

    monkeypatch.setattr(auth, "get_current_user", verified)


async def test_a_session_token_with_sid_passes():
    assert await auth.get_human_session(token({"sid": "sess_1", "azp": "https://x"})) is ALICE


@pytest.mark.parametrize(
    ("claims", "headers"),
    [
        ({}, None),  # a JWT-template token: no sid
        ({"sid": ""}, None),
        ({"sid": "sess_1"}, {"typ": "at+jwt"}),  # an OAuth access token issued as a JWT
        ({"sid": "sess_1", "client_id": "oauth_app"}, None),
    ],
)
async def test_anything_but_a_session_token_is_refused(claims, headers):
    with pytest.raises(HTTPException) as refused:
        await auth.get_human_session(token(claims, headers))
    assert refused.value.status_code == 403 and refused.value.detail == "human_session_required"


async def test_a_token_get_current_user_refuses_is_refused_the_same_way(monkeypatch):
    async def invalid(credentials):
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    monkeypatch.setattr(auth, "get_current_user", invalid)
    with pytest.raises(HTTPException) as refused:
        await auth.get_human_session(token({"sid": "sess_1"}))
    assert refused.value.status_code == 401
