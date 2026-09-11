"""Tests for organization auth helpers."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest
from fastapi import HTTPException

from core.auth import AuthContext, resolve_owner_id, get_owner_type, require_org_admin


class TestResolveOwnerId:
    def test_personal_context_requires_workspace(self):
        auth = AuthContext(user_id="user_123")
        with pytest.raises(HTTPException) as exc_info:
            resolve_owner_id(auth)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "workspace_required"
        assert exc_info.value.detail["message"] == "Choose or create a workspace to continue."

    def test_org_context_returns_org_id(self):
        auth = AuthContext(user_id="user_123", org_id="org_456", org_role="org:admin")
        assert resolve_owner_id(auth) == "org_456"

    def test_org_member_returns_org_id(self):
        auth = AuthContext(user_id="user_123", org_id="org_456", org_role="org:member")
        assert resolve_owner_id(auth) == "org_456"


class TestGetOwnerType:
    def test_personal_context_requires_workspace(self):
        auth = AuthContext(user_id="user_123")
        with pytest.raises(HTTPException) as exc_info:
            get_owner_type(auth)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "workspace_required"

    def test_org_returns_org(self):
        auth = AuthContext(user_id="user_123", org_id="org_456", org_role="org:admin")
        assert get_owner_type(auth) == "org"


class TestRequireOrgAdmin:
    def test_personal_context_requires_workspace(self):
        auth = AuthContext(user_id="user_123")
        with pytest.raises(HTTPException) as exc_info:
            require_org_admin(auth)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "workspace_required"

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
