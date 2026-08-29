import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import company_provisioning as mod  # noqa: E402

RES = {
    "resource_id": "res_1",
    "provider": "posthog",
    "service": "analytics",
    "status": "active",
    "resource_name": "Northwind analytics",
    "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    "dashboard_url": "https://us.posthog.com/project/1",
    "level": "company",
}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_admin", org_id="org_a", org_role="org:admin", email="founder@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def member_client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_member", org_id="org_a", org_role="org:member", email="m@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _paid(monkeypatch):
    monkeypatch.setattr(mod, "require_always_on_entitlement", AsyncMock())


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", writer)
    return writer


def test_resources_returns_pointers_and_the_pairing_state(client):
    with patch.object(mod.provisioning_landing, "provisioning_summary", AsyncMock(return_value=("paired", [RES]))):
        resp = client.get("/api/v1/company/provisioning/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stripe_cli"] == "paired"
    assert body["resources"][0]["level"] == "company"
    assert body["resources"][0]["resource_name"] == "Northwind analytics"


def test_revoke_delegates_to_the_service_and_writes_facts(client, facts):
    """The router is a thin gate + delegate — the shared-blob-safe merge logic
    lives in provisioning_landing.revoke_credentials (see its own tests)."""
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=True)) as revoke:
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True}
    revoke.assert_awaited_once_with(owner_id="org_a", provider="posthog", resource_id="res_1")
    facts.assert_awaited_once_with("org_a")


def test_revoke_is_paid_only_with_the_provisioning_specific_message(client, monkeypatch):
    entitlement = AsyncMock()
    monkeypatch.setattr(mod, "require_always_on_entitlement", entitlement)
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=True)):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    entitlement.assert_awaited_once_with("org_a", message=mod.PAID_MESSAGE)


def test_revoke_returns_404_when_no_such_pointer_exists(client, facts):
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock(return_value=False)):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_missing/revoke")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "resource_not_found"
    facts.assert_not_awaited()


def test_revoke_is_org_admin_only(member_client):
    resp = member_client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 403


def test_revoke_is_blocked_on_the_trial_plan(client, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        mod,
        "require_always_on_entitlement",
        AsyncMock(
            side_effect=HTTPException(
                status_code=402, detail={"blocked": True, "code": "routines_require_subscription"}
            )
        ),
    )
    resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "routines_require_subscription"


def test_revoke_rejects_a_hostile_provider_segment(client):
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock()) as revoke:
        resp = client.post("/api/v1/company/provisioning/resources/PostHog!/res_1/revoke")
    assert resp.status_code == 422
    revoke.assert_not_awaited()


def test_revoke_rejects_a_hostile_resource_id_segment(client):
    # Leading underscore fails _RESOURCE_ID_RE (must start [A-Za-z0-9]).
    with patch.object(mod.provisioning_landing, "revoke_credentials", AsyncMock()) as revoke:
        resp = client.post("/api/v1/company/provisioning/resources/posthog/_res1/revoke")
    assert resp.status_code == 422
    revoke.assert_not_awaited()
