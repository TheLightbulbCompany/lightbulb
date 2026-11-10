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
def _chatgpt_allowed(monkeypatch):
    """Existing tests exercise the allowed path; the refusal tests below narrow it."""
    from core.config import Settings

    monkeypatch.setattr(Settings, "chatgpt_provider_allowed", lambda self, owner_id: True)


@pytest.fixture(autouse=True)
def workspace_oauth_dependencies():
    """The shared client is an org admin; keep router tests off real billing."""
    with (
        patch("routers.oauth.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("routers.oauth.resolve_token_owner", new=AsyncMock(return_value="user_test_123")),
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
async def test_start_refuses_403_for_non_allowlisted_workspace(async_client, monkeypatch):
    from core.config import settings

    monkeypatch.undo()
    monkeypatch.setattr(settings, "CHATGPT_PROVIDER_ORG_IDS", "org_internal")
    with patch("routers.oauth.request_device_code", new=AsyncMock()) as m:
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "chatgpt_internal_only"
    m.assert_not_awaited()


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
        new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="org_test_456")),
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
    mock_logout.assert_awaited_once_with("org_test_456")
    mock_revoke.assert_awaited_once_with(user_id="user_test_123")
    # Without this the wizard sees provider_choice=chatgpt_oauth on a workspace with no
    # credential, and the runtime starts but cannot run inference.
    mock_clear.assert_awaited_once_with("org_test_456")


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


# Org admin who IS the recorded workspace owner (user_test_123 in org_test_456).
ORG_OWNER_ADMIN = AuthContext(
    user_id="user_test_123",
    org_id="org_test_456",
    org_role="org:admin",
    org_slug="test-org",
)
# A second org admin who is NOT the recorded owner.
ORG_OTHER_ADMIN = AuthContext(
    user_id="user_test_999",
    org_id="org_test_456",
    org_role="org:admin",
    org_slug="test-org",
)
# Plain org member (non-admin).
ORG_MEMBER = AuthContext(
    user_id="user_test_789",
    org_id="org_test_456",
    org_role="org:member",
    org_slug="test-org",
)


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
    async def test_status_org_reports_owner_connection(self, async_client, override_auth):
        """Org member calls /status; the billing row records user_a as the
        ChatGPT connector and user_a has tokens on file. The probe must report
        the WORKSPACE's connection — connected=True — even though the caller
        (user_test_789) is a different person who never signed in.

        The token lookup must key on the resolved owner (user_a), not the
        caller; otherwise a member would always see disconnected.
        """
        override_auth(ORG_MEMBER)
        with (
            patch(
                "routers.oauth.resolve_token_owner",
                new=AsyncMock(return_value="user_a"),
            ) as mock_resolve,
            patch(
                "routers.oauth.get_decrypted_tokens_consistent",
                new=AsyncMock(return_value={"account_id": "acc_owner"}),
            ) as mock_tokens,
        ):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "account_id": "acc_owner"}
        mock_resolve.assert_awaited_once_with(owner_id="org_test_456")
        mock_tokens.assert_awaited_once_with(user_id="user_a", bound_to="org_test_456")

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
    async def test_status_workspace_with_own_bound_row_reports_connected(self, async_client, oauth_table):
        """Control for the P1 test: this workspace's bound row reads as
        connected — the binding only rejects OTHER workspaces."""
        _seed_active_row(user_id="user_test_123", bound_owner_id="org_test_456")

        resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "account_id": "acc_x"}

    @pytest.mark.asyncio
    async def test_status_org_with_connector_row_bound_to_org_reports_connected(
        self, async_client, override_auth, oauth_table
    ):
        """Org member hits /status; the connector (user_a) has a row bound to
        the org. The probe resolves the connector, then reads with bound_to=org
        and finds the org-bound row → connected. Runs the real binding logic."""
        override_auth(ORG_MEMBER)
        _seed_active_row(user_id="user_a", bound_owner_id="org_test_456", account_id="acc_owner")

        with patch("routers.oauth.resolve_token_owner", new=AsyncMock(return_value="user_a")):
            resp = await async_client.get("/api/v1/oauth/chatgpt/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "account_id": "acc_owner"}


class TestStartOrgRules:
    """POST /api/v1/oauth/chatgpt/start — org owner-gating.

    Org owners may connect ChatGPT; the connection binds to the workspace
    owner (set-once). Only an org admin may /start, and once an owner is
    recorded only that owner may re-connect (e.g. after token expiry).
    """

    @pytest.mark.asyncio
    async def test_start_org_requires_admin(self, async_client, override_auth):
        """Org MEMBER -> 403; org ADMIN with no recorded owner yet -> 200."""
        fake_resp = DeviceCodeResponse(
            user_code="TEST-1234",
            verification_uri="https://chatgpt.com/codex",
            expires_in=900,
            interval=5,
        )

        # Member is rejected before any device-code request.
        override_auth(ORG_MEMBER)
        with patch(
            "routers.oauth.request_device_code",
            new=AsyncMock(return_value=fake_resp),
        ) as mock_request:
            resp = await async_client.post("/api/v1/oauth/chatgpt/start")
        assert resp.status_code == 403
        mock_request.assert_not_awaited()

        # Admin with no recorded owner yet reaches the device-code request, and
        # the session is bound to the ORG at /start — where the owner gate runs.
        override_auth(ORG_OWNER_ADMIN)
        with (
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "routers.oauth.request_device_code",
                new=AsyncMock(return_value=fake_resp),
            ) as mock_request,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/start")
        assert resp.status_code == 200
        mock_request.assert_awaited_once_with(user_id="user_test_123", bound_owner_id="org_test_456")

    @pytest.mark.asyncio
    async def test_start_org_rejects_non_owner_admin(self, async_client, override_auth):
        """billing row owner=user_test_123; admin user_test_999 /start -> 403
        with a workspace-owner message. The recorded owner (user_test_123)
        re-connecting -> 200 (re-connect after token expiry is the owner's
        right)."""
        fake_resp = DeviceCodeResponse(
            user_code="TEST-1234",
            verification_uri="https://chatgpt.com/codex",
            expires_in=900,
            interval=5,
        )
        row = {"owner_id": "org_test_456", "oauth_connector_user_id": "user_test_123"}

        # A different admin cannot hijack an already-claimed connection.
        override_auth(ORG_OTHER_ADMIN)
        with (
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "routers.oauth.request_device_code",
                new=AsyncMock(return_value=fake_resp),
            ) as mock_request,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/start")
        assert resp.status_code == 403
        assert "workspace owner" in resp.json()["detail"].lower()
        mock_request.assert_not_awaited()

        # The recorded owner may re-connect.
        override_auth(ORG_OWNER_ADMIN)
        with (
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "routers.oauth.request_device_code",
                new=AsyncMock(return_value=fake_resp),
            ) as mock_request,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/start")
        assert resp.status_code == 200
        mock_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_rejects_missing_workspace(self, async_client, override_auth):
        """No active workspace must fail before opening a device-code flow."""
        override_auth(AuthContext(user_id="user_test_123"))
        fake_resp = DeviceCodeResponse(
            user_code="TEST-1234",
            verification_uri="https://chatgpt.com/codex",
            expires_in=900,
            interval=5,
        )
        with patch(
            "routers.oauth.request_device_code",
            new=AsyncMock(return_value=fake_resp),
        ) as mock_request:
            resp = await async_client.post("/api/v1/oauth/chatgpt/start")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "workspace_required"
        mock_request.assert_not_awaited()


class TestPollOrgClaim:
    """POST /api/v1/oauth/chatgpt/poll — org ownership claim on completion.

    Binding + authorization are decided at /start (owner-gated) and carried on
    the pending row; /poll surfaces that decision as DevicePollResult.bound_owner_id
    and can only complete what /start authorized. The caller's Clerk context at
    poll time NEVER drives the claim or the binding — switching contexts between
    start and poll cannot re-target either.
    """

    @pytest.mark.asyncio
    async def test_poll_member_personal_start_then_org_poll_does_not_claim(self, async_client, override_auth):
        """THE ATTACK. A non-admin member calls /start in PERSONAL context
        (ungated — they bind the session to themselves) and completes the user
        code. They then switch their Clerk context to the org and call /poll.

        The session was authorized FOR the member personally at /start, so the
        completed row is bound to the member's user_id (carried on
        DevicePollResult.bound_owner_id). The claim is driven by THAT binding,
        not the caller's current org context: claim_oauth_connector must NOT
        fire, so the member can never become the org's set-once connector and
        lock out a legitimate admin."""
        override_auth(ORG_MEMBER)
        with (
            patch(
                "routers.oauth.poll_device_code",
                # The session bound itself to the member at /start (personal).
                new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="user_test_789")),
            ) as mock_poll,
            patch(
                "core.repositories.billing_repo.claim_oauth_connector",
                new=AsyncMock(),
            ) as mock_claim,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 403
        mock_poll.assert_not_awaited()
        mock_claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_poll_completion_claims_owner_once_for_org(self, async_client, override_auth):
        """Org admin happy path: started + owner-gated in ORG context at /start,
        so the session is bound to the org. /poll surfaces bound_owner_id=org and
        claims the admin as the org's connector. claim_oauth_connector awaited
        with (org_id, connector_user_id=<admin>). The repo layer makes this
        set-once; the router just fires the claim on the completing sign-in."""
        override_auth(ORG_OWNER_ADMIN)
        with (
            patch(
                "routers.oauth.poll_device_code",
                new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="org_test_456")),
            ) as mock_poll,
            patch(
                "core.repositories.billing_repo.claim_oauth_connector",
                new=AsyncMock(),
            ) as mock_claim,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        assert resp.json() == {"status": "completed", "account_id": "acc_1"}
        mock_poll.assert_awaited_once_with(user_id="user_test_123", authorized_owner_id="org_test_456")
        mock_claim.assert_awaited_once_with("org_test_456", connector_user_id="user_test_123")

    @pytest.mark.asyncio
    async def test_poll_completion_pushes_fresh_credential_to_container(self, async_client, override_auth):
        """On a completed device-code flow the fresh token must be pushed into
        the running container (reconnect heal) — staged under the container
        owner, token read under the connector — so a same-provider reconnect
        actually reaches the live container instead of stranding the token in
        DDB while the container keeps a spent token."""
        override_auth(ORG_OWNER_ADMIN)
        with (
            patch(
                "routers.oauth.poll_device_code",
                new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="org_test_456")),
            ),
            patch("core.repositories.billing_repo.claim_oauth_connector", new=AsyncMock()),
            patch(
                "routers.oauth.push_oauth_credential_to_container",
                new=AsyncMock(return_value=True),
            ) as mock_push,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        mock_push.assert_awaited_once_with(owner_id="org_test_456", connector_id="user_test_123")

    @pytest.mark.asyncio
    async def test_poll_org_start_then_personal_poll_still_claims_org(self, async_client, override_auth):
        """Context switch the OTHER way: the session was started + owner-gated in
        ORG context (bound to the org), but the caller polls in PERSONAL context.
        The claim STILL happens for the org — the session's binding wins over the
        caller's current context. connector_user_id is the polling caller's
        user_id (the human who held the session)."""
        # Personal context: no org_id. The session, however, was bound to the org.
        with (
            patch(
                "routers.oauth.poll_device_code",
                new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="org_test_456")),
            ),
            patch(
                "core.repositories.billing_repo.claim_oauth_connector",
                new=AsyncMock(),
            ) as mock_claim,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        assert resp.json() == {"status": "completed", "account_id": "acc_1"}
        mock_claim.assert_awaited_once_with("org_test_456", connector_user_id="user_test_123")

    @pytest.mark.asyncio
    async def test_poll_pending_does_not_claim(self, async_client, override_auth):
        """A still-pending poll must NOT claim ownership."""
        override_auth(ORG_OWNER_ADMIN)
        with (
            patch(
                "routers.oauth.poll_device_code",
                new=AsyncMock(return_value=DevicePollPending),
            ),
            patch(
                "core.repositories.billing_repo.claim_oauth_connector",
                new=AsyncMock(),
            ) as mock_claim,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        assert resp.json() == {"status": "pending"}
        mock_claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_poll_personal_bound_session_does_not_claim(self, async_client):
        """Personal /poll of a personally-bound session: no org claim (a personal
        binding never names an org). /poll passes no binding arg."""
        with (
            patch(
                "routers.oauth.poll_device_code",
                new=AsyncMock(return_value=DevicePollResult(account_id="acc_1", bound_owner_id="user_test_123")),
            ) as mock_poll,
            patch(
                "core.repositories.billing_repo.claim_oauth_connector",
                new=AsyncMock(),
            ) as mock_claim,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/poll")
        assert resp.status_code == 200
        mock_poll.assert_awaited_once_with(user_id="user_test_123", authorized_owner_id="org_test_456")
        mock_claim.assert_not_awaited()


class TestDisconnectOrgRules:
    """POST /api/v1/oauth/chatgpt/disconnect — org owner-gating + revocation."""

    @pytest.mark.asyncio
    async def test_disconnect_org_owner_revokes_and_clears(self, async_client, override_auth):
        """Owner user_test_123 /disconnect revokes their own OAuth token row, revokes the
        credential keyed on the WORKSPACE (org_id), and clears provider_choice on the org
        billing row."""
        override_auth(ORG_OWNER_ADMIN)
        row = {"owner_id": "org_test_456", "oauth_connector_user_id": "user_test_123"}
        with (
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value=row),
            ),
            patch("routers.oauth.revoke_user_oauth", new=AsyncMock()) as mock_revoke,
            patch("routers.oauth.revoke_provider_auth_in_container", new=AsyncMock()) as mock_delete,
            patch(
                "core.repositories.billing_repo.clear_provider_choice",
                new=AsyncMock(),
            ) as mock_clear,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/disconnect")
        assert resp.status_code == 200
        # The token row is keyed on the HUMAN connector; the credential itself is keyed on
        # the WORKSPACE.
        mock_revoke.assert_awaited_once_with(user_id="user_test_123")
        mock_delete.assert_awaited_once_with("org_test_456")
        mock_clear.assert_awaited_once_with("org_test_456")

    @pytest.mark.asyncio
    async def test_disconnect_org_rejects_non_owner_admin(self, async_client, override_auth):
        """billing row owner=user_test_123; admin user_test_999 /disconnect ->
        403, and nothing is revoked or deleted."""
        override_auth(ORG_OTHER_ADMIN)
        row = {"owner_id": "org_test_456", "oauth_connector_user_id": "user_test_123"}
        with (
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value=row),
            ),
            patch("routers.oauth.revoke_user_oauth", new=AsyncMock()) as mock_revoke,
            patch("routers.oauth.revoke_provider_auth_in_container", new=AsyncMock()) as mock_delete,
            patch(
                "core.repositories.billing_repo.clear_provider_choice",
                new=AsyncMock(),
            ) as mock_clear,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/disconnect")
        assert resp.status_code == 403
        assert "workspace owner" in resp.json()["detail"].lower()
        mock_revoke.assert_not_awaited()
        mock_delete.assert_not_awaited()
        mock_clear.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_org_member_rejected(self, async_client, override_auth):
        """A non-admin org member cannot disconnect the workspace connection."""
        override_auth(ORG_MEMBER)
        with (
            patch("routers.oauth.revoke_user_oauth", new=AsyncMock()) as mock_revoke,
            patch(
                "core.repositories.billing_repo.clear_provider_choice",
                new=AsyncMock(),
            ) as mock_clear,
        ):
            resp = await async_client.post("/api/v1/oauth/chatgpt/disconnect")
        assert resp.status_code == 403
        mock_revoke.assert_not_awaited()
        mock_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_self_heals_phantom_connector(async_client):
    """A recorded connector naming no Clerk user must not 403 a real admin.

    The reclaim clears the phantom (CAS) and the /start flow proceeds as
    unclaimed — this is the recovery path for checkout-recorded ghost ids.
    """
    phantom_row = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "oauth_connector_user_id": "user_ghost",
    }
    fake_resp = DeviceCodeResponse(
        user_code="TEST-1234",
        verification_uri="https://chatgpt.com/codex",
        expires_in=900,
        interval=5,
    )
    with (
        patch("routers.oauth.billing_repo.get_by_owner_id", new=AsyncMock(return_value=phantom_row)),
        patch(
            "core.services.oauth_service.maybe_clear_phantom_connector",
            new=AsyncMock(return_value=True),
        ),
        patch("routers.oauth.request_device_code", new=AsyncMock(return_value=fake_resp)),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_code"] == "TEST-1234"


@pytest.mark.asyncio
async def test_start_keeps_403_for_real_connector(async_client):
    """Reclaim refusing (recorded owner is real) keeps the 403 — no hijack."""
    real_row = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "oauth_connector_user_id": "user_real_owner",
    }
    with (
        patch("routers.oauth.billing_repo.get_by_owner_id", new=AsyncMock(return_value=real_row)),
        patch(
            "core.services.oauth_service.maybe_clear_phantom_connector",
            new=AsyncMock(return_value=False),
        ),
    ):
        resp = await async_client.post("/api/v1/oauth/chatgpt/start")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_poll_rejects_former_admin_before_exchanging_tokens(async_client, override_auth):
    override_auth(ORG_MEMBER)
    with patch("routers.oauth.poll_device_code", new=AsyncMock()) as exchange:
        response = await async_client.post("/api/v1/oauth/chatgpt/poll")
    assert response.status_code == 403
    exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_rejects_other_workspace_binding(async_client):
    with patch(
        "routers.oauth.poll_device_code",
        new=AsyncMock(side_effect=OAuthBoundElsewhereError("Return to your original workspace")),
    ):
        response = await async_client.post("/api/v1/oauth/chatgpt/poll")
    assert response.status_code == 403
