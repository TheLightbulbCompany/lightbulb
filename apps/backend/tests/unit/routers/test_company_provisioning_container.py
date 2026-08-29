import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.paperclip_admin_client import PaperclipApiError  # noqa: E402
from main import app  # noqa: E402
from routers import company_provisioning as mod  # noqa: E402

BASE = "/api/v1/company/provisioning/u/org_a"
HDRS = {"Authorization": "Bearer hook-token"}

_PAID_ROW = {"owner_id": "org_a", "subscription_status": "active"}
_TRIAL_ROW = {"owner_id": "org_a", "subscription_status": "trial_local"}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _hook(monkeypatch):
    monkeypatch.setattr(mod, "_require_hook_token", AsyncMock(return_value={"owner_id": "org_a"}))


@pytest.fixture(autouse=True)
def _paid(monkeypatch):
    """Autonomy is paid-only; every route reads the billing row first."""
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=_PAID_ROW))


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", writer)
    return writer


def test_pairing_files_the_card(client):
    with patch.object(mod.provisioning_approvals, "request_cli_pairing", AsyncMock(return_value="apr_1")) as ask:
        resp = client.post(
            f"{BASE}/pairing",
            headers=HDRS,
            json={
                "browser_url": "https://dashboard.stripe.com/stripecli/confirm?t=abc",
                "verification_code": "ABCD-EFGH",
                "issue_id": "iss_1",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"approval_id": "apr_1", "status": "filed"}
    assert ask.await_args.kwargs["verification_code"] == "ABCD-EFGH"
    assert ask.await_args.kwargs["issue_id"] == "iss_1"


def test_pairing_rejects_a_non_stripe_browser_url(client):
    resp = client.post(
        f"{BASE}/pairing",
        headers=HDRS,
        json={"browser_url": "https://evil.example.com/phish", "verification_code": "ABCD-EFGH"},
    )
    assert resp.status_code == 422


def test_no_company_is_reported_not_faked(client):
    with patch.object(mod.provisioning_approvals, "request_cli_pairing", AsyncMock(return_value=None)):
        resp = client.post(
            f"{BASE}/pairing",
            headers=HDRS,
            json={
                "browser_url": "https://dashboard.stripe.com/stripecli/confirm?t=abc",
                "verification_code": "ABCD-EFGH",
            },
        )
    assert resp.json()["status"] == "no_company"


def test_board_outage_is_503_and_rejection_is_502(client):
    with patch.object(
        mod.provisioning_approvals, "request_provider_consent", AsyncMock(side_effect=httpx.ConnectError("down"))
    ):
        resp = client.post(f"{BASE}/consent", headers=HDRS, json={"provider": "posthog", "service": "analytics"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "paperclip_unavailable"

    err = PaperclipApiError("bad request", 400, {"error": "invalid type"})
    with patch.object(mod.provisioning_approvals, "request_provider_consent", AsyncMock(side_effect=err)):
        resp = client.post(f"{BASE}/consent", headers=HDRS, json={"provider": "posthog", "service": "analytics"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "paperclip_rejected"

    retryable = PaperclipApiError("boom", 502, None)
    with patch.object(mod.provisioning_approvals, "request_provider_consent", AsyncMock(side_effect=retryable)):
        resp = client.post(f"{BASE}/consent", headers=HDRS, json={"provider": "posthog", "service": "analytics"})
    assert resp.status_code == 503


@pytest.mark.parametrize("bad", ["http://terms.example.com", "ftp://x", "javascript:alert(1)"])
def test_consent_rejects_a_non_https_terms_url(client, bad):
    with patch.object(mod.provisioning_approvals, "request_provider_consent", AsyncMock()) as ask:
        resp = client.post(
            f"{BASE}/consent",
            headers=HDRS,
            json={"provider": "posthog", "service": "analytics", "terms_url": bad},
        )
    assert resp.status_code == 422
    ask.assert_not_awaited()


def test_landed_stores_the_pointer_and_refreshes_the_facts(client, facts):
    with patch.object(mod.provisioning_landing, "land_credentials", AsyncMock()) as land:
        resp = client.post(
            f"{BASE}/landed",
            headers=HDRS,
            json={
                "provider": "posthog",
                "service": "analytics",
                "resource_id": "res_1",
                "resource_name": "Northwind analytics",
                "env": {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"},
                "dashboard_url": "https://us.posthog.com/project/1",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource_id"] == "res_1"
    assert body["level"] == "company"
    assert body["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]
    # The VALUE never comes back out of the API.
    assert "phc_live_abc" not in resp.text
    assert land.await_args.kwargs["env"] == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"}
    facts.assert_awaited_once_with("org_a")


def test_the_stored_pointer_carries_env_keys_and_no_values(client):
    """``env_keys`` is load-bearing: a later reprovision re-injects ONLY the
    names the pointer declares (``build_provisioned_env``). Values never land
    on the row — they live in Secrets Manager."""
    env = {"B_TOKEN": "secret-b", "A_TOKEN": "secret-a"}
    with patch.object(mod.provisioning_landing, "land_credentials", AsyncMock()) as land:
        resp = client.post(
            f"{BASE}/landed",
            headers=HDRS,
            json={"provider": "posthog", "service": "analytics", "resource_id": "res_1", "env": env},
        )
    assert resp.status_code == 200
    stored = land.await_args.kwargs["resource"]
    assert stored["env_keys"] == ["A_TOKEN", "B_TOKEN"]
    flat = repr(stored)
    assert "secret-a" not in flat and "secret-b" not in flat


@pytest.mark.parametrize(
    "patch_body",
    [
        {"provider": "../../etc"},  # path traversal into the secret name
        {"provider": "PostHog!"},  # charset
        {"provider": "p" * 80},  # length
        {"resource_id": "res/../1"},  # lands in a URL path
        {"env": {"lowercase_key": "v"}},  # not a shell env var name
        {"env": {"K" * 200: "v"}},  # absurd key
        {"env": {f"K{i}": "v" for i in range(40)}},  # too many
        {"env": {"K": "x" * 20000}},  # absurd value
        {"env": {"K": ""}},  # empty value
        {"dashboard_url": "http://us.posthog.com"},  # not https
    ],
)
def test_landed_rejects_hostile_bodies(client, patch_body):
    body = {"provider": "posthog", "service": "analytics", "resource_id": "res_1", "env": {"TOKEN": "v"}, **patch_body}
    with patch.object(mod.provisioning_landing, "land_credentials", AsyncMock()) as land:
        resp = client.post(f"{BASE}/landed", headers=HDRS, json=body)
    assert resp.status_code == 422
    land.assert_not_awaited()


_ROUTES = (
    ("pairing", {"browser_url": "https://dashboard.stripe.com/x", "verification_code": "Z"}),
    ("consent", {"provider": "posthog", "service": "analytics"}),
    ("landed", {"provider": "posthog", "service": "analytics", "resource_id": "r", "env": {}}),
)


def test_every_container_route_requires_the_hook_token(monkeypatch, client):
    from fastapi import HTTPException

    monkeypatch.setattr(
        mod, "_require_hook_token", AsyncMock(side_effect=HTTPException(status_code=401, detail="invalid hook token"))
    )
    for path, body in _ROUTES:
        assert client.post(f"{BASE}/{path}", headers=HDRS, json=body).status_code == 401


@pytest.mark.parametrize("path,body", _ROUTES)
def test_every_container_route_is_paid_only(monkeypatch, client, path, body):
    """A trial owner's container cannot provision: no card filed, nothing landed."""
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=_TRIAL_ROW))
    with (
        patch.object(mod.provisioning_approvals, "request_cli_pairing", AsyncMock()) as pair,
        patch.object(mod.provisioning_approvals, "request_provider_consent", AsyncMock()) as consent,
        patch.object(mod.provisioning_landing, "land_credentials", AsyncMock()) as land,
    ):
        resp = client.post(f"{BASE}/{path}", headers=HDRS, json=body)
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "routines_require_subscription"
    pair.assert_not_awaited()
    consent.assert_not_awaited()
    land.assert_not_awaited()
