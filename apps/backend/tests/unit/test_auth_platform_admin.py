"""Tests for `require_platform_admin` — the per-env Clerk user-id allowlist.

Policy: a caller is a platform admin iff their Clerk user id is in
`PLATFORM_ADMIN_USER_IDS`. The email claim grants nothing: a self-registered
account on a team domain (dev's `+clerk_test` addresses) must be refused.
"""

import pytest
from fastapi import HTTPException

from core.auth import AuthContext, is_internal_email, require_platform_admin
from core.config import settings


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_ADMIN_USER_IDS", "user_listed, user_other")


def test_listed_user_id_passes():
    auth = AuthContext(user_id="user_listed", email=None)
    assert require_platform_admin(auth) is auth


@pytest.mark.parametrize(
    "email",
    ["x+clerk_test@thelightbulb.company", "someone@isol8.co", "prasiddha@gmail.com", None],
)
def test_team_email_without_listed_id_is_refused(email):
    with pytest.raises(HTTPException) as exc:
        require_platform_admin(AuthContext(user_id="user_self_registered", email=email))
    assert exc.value.status_code == 403


def test_empty_allowlist_admits_nobody(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_ADMIN_USER_IDS", "")
    with pytest.raises(HTTPException):
        require_platform_admin(AuthContext(user_id="user_listed", email="admin@thelightbulb.company"))


def test_internal_email_tagging():
    """Analytics-only tagging; the retired @isol8.co domain no longer counts."""
    assert is_internal_email("SOMEONE@THELIGHTBULB.COMPANY")
    assert is_internal_email("prasiddha@gmail.com")
    assert not is_internal_email("someone@isol8.co")
    assert not is_internal_email("attacker@evil-thelightbulb.company.example")
    assert not is_internal_email(None)
