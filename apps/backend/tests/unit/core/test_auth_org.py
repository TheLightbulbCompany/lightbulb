"""Tests for organization auth helpers."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest
from fastapi import HTTPException

from core.auth import AuthContext, resolve_owner_id, require_org_admin


class TestResolveOwnerId:
    def test_personal_context_is_its_own_tenant(self):
        assert resolve_owner_id(AuthContext(user_id="user_123")) == "user_123"

    def test_org_context_returns_org_id(self):
        auth = AuthContext(user_id="user_123", org_id="org_456", org_role="org:admin")
        assert resolve_owner_id(auth) == "org_456"

    def test_org_member_returns_org_id(self):
        auth = AuthContext(user_id="user_123", org_id="org_456", org_role="org:member")
        assert resolve_owner_id(auth) == "org_456"


class TestRequireOrgAdmin:
    def test_personal_context_is_its_own_admin(self):
        auth = AuthContext(user_id="user_123")
        assert require_org_admin(auth) == auth

    def test_org_admin_passes(self):
        auth = AuthContext(user_id="user_123", org_id="org_456", org_role="org:admin")
        result = require_org_admin(auth)
        assert result == auth

    def test_org_member_raises_403(self):
        auth = AuthContext(user_id="user_123", org_id="org_456", org_role="org:member")
        with pytest.raises(HTTPException) as exc_info:
            require_org_admin(auth)
        assert exc_info.value.status_code == 403

    def test_org_no_role_raises_403(self):
        auth = AuthContext(user_id="user_123", org_id="org_456")
        with pytest.raises(HTTPException) as exc_info:
            require_org_admin(auth)
        assert exc_info.value.status_code == 403
