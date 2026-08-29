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


def test_revoke_drops_only_the_landed_env_keys_from_the_shared_blob(client, facts):
    """The (owner, provider) Secrets Manager blob is SHARED with the OAuth
    integrations rail, so revoke must drop only the keys THIS resource
    landed, never the whole blob — an unrelated OAuth credential (e.g.
    ``access_token``) must survive."""
    blob = {"access_token": "t", "NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "p"}
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[RES])),
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=dict(blob))),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
    ):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True}
    drop.assert_awaited_once_with("org_a", "posthog", "res_1")
    sm_put.assert_awaited_once_with("org_a", "posthog", {"access_token": "t"})
    sm_del.assert_not_awaited()
    facts.assert_awaited_once_with("org_a")


def test_revoke_deletes_the_secret_when_nothing_is_left(client):
    """A blob holding only this resource's keys goes away entirely once they
    are dropped."""
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[RES])),
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])),
        patch.object(
            mod.integration_secret_store,
            "get",
            AsyncMock(return_value={"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "p"}),
        ),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
    ):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True}
    sm_put.assert_not_awaited()
    sm_del.assert_awaited_once_with("org_a", "posthog")


def test_revoke_is_org_admin_only(member_client):
    resp = member_client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 403


def test_revoke_is_paid_only(client, monkeypatch):
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
    with patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock()) as drop:
        resp = client.post("/api/v1/company/provisioning/resources/PostHog!/res_1/revoke")
    assert resp.status_code == 422
    drop.assert_not_awaited()
