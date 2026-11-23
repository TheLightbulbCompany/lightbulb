"""Integration tests for /api/v1/oauth/chatgpt/* endpoints."""

import json
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from core.auth import AuthContext, get_current_user
from core.services.oauth_service import (
    DeviceCodeResponse,
    DevicePollPending,
    DevicePollResult,
    OAuthAlreadyActiveError,
    OAuthBoundElsewhereError,
)


@pytest.fixture(autouse=True)
def workspace_oauth_dependencies():
    """The shared client is an org admin; keep router tests off real billing."""
    with (
        patch("routers.oauth.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
    ):
        yield


@pytest.fixture(autouse=True)
def paid_plan():
    """ChatGPT sign-in needs a paid plan (/start checks it first); the router tests run as a subscriber."""
    with patch("core.services.provision_gate.is_paid_subscription", return_value=True) as m:
        yield m


@pytest.fixture(autouse=True)
def provider_revoke_mock():
    """Keep unrelated router tests off the real container-row/runtime cleanup path."""
    with patch("routers.oauth.revoke_provider_auth_in_container", new=AsyncMock(return_value=True)) as m:
        yield m


@pytest.fixture
def oauth_table(monkeypatch):
    """A real (moto) oauth-tokens table so the binding logic runs end-to-end
    through the router for the P1 cross-owner-reuse scenario, instead of
    mocking the service helper and losing the actual rejection."""
    from core.config import settings as _settings

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-oauth-tokens",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", "test-oauth-tokens")
        monkeypatch.setattr(_settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        yield


def _seed_active_row(*, user_id: str, bound_owner_id: str | None, account_id: str = "acc_x") -> None:
    from core.encryption import get_fernet

    plain = json.dumps({"access_token": "a", "refresh_token": "r", "account_id": account_id}).encode()
    item = {
        "user_id": user_id,
        "state": "active",
        "encrypted_tokens": get_fernet().encrypt(plain),
        "account_id": account_id,
    }
    if bound_owner_id is not None:
        item["bound_owner_id"] = bound_owner_id
    boto3.resource("dynamodb", region_name="us-east-1").Table("test-oauth-tokens").put_item(Item=item)


@pytest.mark.asyncio
async def test_start_returns_user_code_and_verification_uri(async_client):
    fake_resp = DeviceCodeResponse(
        user_code="TEST-1234",
        verification_uri="https://chatgpt.com/codex",
        expires_in=900,
        interval=5,
    )
    with patch(
        "routers.oauth.request_device_code",
        new=AsyncMock(return_value=fake_resp),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_code"] == "TEST-1234"
    assert body["verification_uri"] == "https://chatgpt.com/codex"
    assert body["interval"] == 5
    assert body["expires_in"] == 900


@pytest.mark.asyncio
async def test_start_refuses_402_without_a_paid_plan_before_any_device_code(async_client, paid_plan):
    """A free workspace never starts the OpenAI sign-in: the plan is checked first."""
    paid_plan.return_value = False
    with patch("routers.oauth.request_device_code", new=AsyncMock()) as request:
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "plan_required"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_returns_409_when_already_active(async_client):
    """When user already has an active OAuth session, /start returns 409."""
    with patch(
        "routers.oauth.request_device_code",
        new=AsyncMock(side_effect=OAuthAlreadyActiveError("already active")),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_start_returns_403_when_bound_elsewhere(async_client):
    """When the active row is bound to a DIFFERENT workspace, /start must NOT
    return 409 — the wizard treats a /start 409 as completion and would advance
    with no usable tokens for THIS workspace. Map to 403 with the human-readable
    detail so the wizard surfaces it as an error (its error branch shows
    e.message, which carries body.detail)."""
    msg = "This ChatGPT account is connected to a different workspace. Disconnect it there before connecting here."
    with patch(
        "routers.oauth.request_device_code",
        new=AsyncMock(side_effect=OAuthBoundElsewhereError(msg)),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 403
    assert resp.json()["detail"] == msg


@pytest.mark.asyncio
async def test_poll_returns_pending_status(async_client):
    with patch(
        "routers.oauth.poll_device_code",
        new=AsyncMock(return_value=DevicePollPending),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}


@pytest.mark.asyncio
async def test_poll_returns_completed_status(async_client):
    with patch(
        "routers.oauth.poll_device_code",
        new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="user_test_123")),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
    assert resp.status_code == 200
    assert resp.json() == {"status": "completed", "account_id": "acc_1"}


@pytest.mark.asyncio
async def test_disconnect_revokes(async_client):
    with (
        patch("routers.oauth.revoke_user_oauth", new=AsyncMock()) as mock_revoke,
        patch("routers.oauth.revoke_provider_auth_in_container", new=AsyncMock()) as mock_logout,
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/disconnect")
    assert resp.status_code == 200
    mock_revoke.assert_awaited_once_with(user_id="user_test_123")
    # 6.6: disconnect must also clear the credential the running container
    # already imported into its SQLite store (models.authLogout), not just the
    # EFS seed file.
    mock_logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_keeps_oauth_row_and_provider_choice_when_agentcore_revoke_fails(async_client):
    """The DDB row is retry ownership for an authoritative AgentCore revoke. A backing-store
    failure must surface before either that row or provider choice is cleared."""
    with (
        patch("routers.oauth.revoke_user_oauth", new=AsyncMock()) as mock_revoke,
        patch(
            "routers.oauth.revoke_provider_auth_in_container",
            new=AsyncMock(side_effect=RuntimeError("s3 down")),
        ),
        patch("core.repositories.billing_repo.clear_provider_choice", new=AsyncMock()) as mock_clear,
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/disconnect")
    assert resp.status_code == 502
    mock_revoke.assert_not_awaited()
    mock_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_revokes_the_credential_before_it_drops_the_row(async_client):
    """The staged root `auth.json` IS the live credential, and the DDB row is this request's
    retry ownership -- so the revoke runs FIRST. Dropping the row over a surviving credential
    leaves an untracked grant nobody can disconnect.
    """
    order: list[str] = []
    with (
        patch(
            "routers.oauth.revoke_provider_auth_in_container",
            new=AsyncMock(side_effect=lambda *_a, **_k: order.append("revoke_credential")),
        ) as mock_logout,
        patch(
            "routers.oauth.revoke_user_oauth",
            new=AsyncMock(side_effect=lambda **kw: order.append("revoke_row")),
        ) as mock_revoke,
        patch("core.repositories.billing_repo.clear_provider_choice", new=AsyncMock()) as mock_clear,
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/disconnect")

    assert resp.status_code == 200
    assert order == ["revoke_credential", "revoke_row"]
    mock_logout.assert_awaited_once_with("user_test_123")
    mock_revoke.assert_awaited_once_with(user_id="user_test_123")
    # Without this the wizard sees provider_choice=chatgpt_oauth on a workspace with no
    # credential, and the runtime starts but cannot run inference.
    mock_clear.assert_awaited_once_with("user_test_123")


def _override(app, ctx: AuthContext):
    """Override get_current_user to return ``ctx`` for the duration of a test."""

    async def _mock():
        return ctx

    app.dependency_overrides[get_current_user] = _mock


@pytest.fixture
def override_auth(app):
    """Install an arbitrary AuthContext on get_current_user; clears on teardown."""

    def _install(ctx: AuthContext):
        _override(app, ctx)

    yield _install
    app.dependency_overrides.clear()


# Org admin who IS the recorded workspace owner (user_test_123 in user_test_123).
ORG_OWNER_ADMIN = AuthContext(user_id="user_test_123", human_session=True)
# A second org admin who is NOT the recorded owner.
ORG_OTHER_ADMIN = AuthContext(user_id="user_test_123", human_session=True)
# Plain org member (non-admin).
ORG_MEMBER = AuthContext(user_id="user_test_123", human_session=True)


class TestStatus:
    """GET /api/v1/oauth/chatgpt/status — wizard idempotency probe.

    Org context resolves the WORKSPACE's connection (bound to the recorded
    owner), not the caller's: any org member reading /status sees the same
    connected/disconnected answer for the shared container.
    """

    @pytest.mark.asyncio
    async def test_status_returns_connected_true_when_active_row_exists(self, async_client):
        with patch(
            "routers.oauth.get_decrypted_tokens_consistent",
            new=AsyncMock(
                return_value={
                    "account_id": "acc_x",
                    "access_token": "tok-a",
                    "refresh_token": "tok-r",
                }
            ),
        ):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "account_id": "acc_x"}

    @pytest.mark.asyncio
    async def test_status_returns_connected_false_when_no_row(self, async_client):
        with patch(
            "routers.oauth.get_decrypted_tokens_consistent",
            new=AsyncMock(return_value=None),
        ):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": False, "account_id": None}

    @pytest.mark.asyncio
    async def test_status_returns_connected_true_when_account_id_missing(self, async_client):
        # Older rows may not have account_id (extracted from JWT post-2026-04).
        with patch(
            "routers.oauth.get_decrypted_tokens_consistent",
            new=AsyncMock(return_value={"access_token": "tok-a", "refresh_token": "tok-r"}),
        ):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "account_id": None}

    @pytest.mark.asyncio
    async def test_status_workspace_with_row_bound_elsewhere_reports_disconnected(self, async_client, oauth_table):
        """THE P1 SCENARIO. user_test_123 connected ChatGPT for another org;
        their token row is bound there. When that human opens this workspace,
        the row belongs to the other org, so the probe must
        report disconnected. Otherwise the personal gate passes and the same
        ChatGPT account gets staged into a second container.

        Runs the real binding logic through the router (no service mock)."""
        _seed_active_row(user_id="user_test_123", bound_owner_id="org_other_999")

        resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": False, "account_id": None}


@pytest.mark.asyncio
async def test_poll_rejects_other_workspace_binding(async_client):
    with patch(
        "routers.oauth.poll_device_code",
        new=AsyncMock(side_effect=OAuthBoundElsewhereError("Return to your original workspace")),
    ):
        response = await async_client.post("/api/v1/oauth/chatgpt/poll")
    assert response.status_code == 403


class TestPollClaim:
    """POST /api/v1/oauth/chatgpt/poll: the tenant's connection is claimed by the person on completion."""

    @pytest.mark.asyncio
    async def test_oauth_poll_claims_user_tenant(self, async_client, override_auth):
        """A person is their own tenant: their completed sign-in claims their own tenant's connection, and no other."""
        override_auth(AuthContext(user_id="user_test_123", human_session=True))
        with (
            patch(
                "routers.oauth.poll_device_code",
                new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="user_test_123")),
            ) as mock_poll,
            patch("core.repositories.billing_repo.claim_oauth_connector", new=AsyncMock()) as mock_claim,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        mock_poll.assert_awaited_once_with(user_id="user_test_123", authorized_owner_id="user_test_123")
        mock_claim.assert_awaited_once_with("user_test_123", connector_user_id="user_test_123")

    @pytest.mark.asyncio
    async def test_poll_completion_pushes_fresh_credential_to_container(self, async_client, override_auth):
        """A completed device-code flow pushes the fresh token into the running container (reconnect heal)."""
        override_auth(AuthContext(user_id="user_test_123", human_session=True))
        with (
            patch(
                "routers.oauth.poll_device_code",
                new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="user_test_123")),
            ),
            patch("core.repositories.billing_repo.claim_oauth_connector", new=AsyncMock()),
            patch("routers.oauth.push_oauth_credential_to_container", new=AsyncMock(return_value=True)) as mock_push,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        mock_push.assert_awaited_once_with(owner_id="user_test_123", connector_id="user_test_123")

    @pytest.mark.asyncio
    async def test_poll_pending_does_not_claim(self, async_client, override_auth):
        """A still-pending poll must NOT claim ownership."""
        override_auth(AuthContext(user_id="user_test_123", human_session=True))
        with (
            patch("routers.oauth.poll_device_code", new=AsyncMock(return_value=DevicePollPending)),
            patch("core.repositories.billing_repo.claim_oauth_connector", new=AsyncMock()) as mock_claim,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        assert resp.json() == {"status": "pending"}
        mock_claim.assert_not_awaited()
