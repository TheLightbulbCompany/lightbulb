"""Router-level tests for POST /billing/provider (post-onboarding switch)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def active_personal_account():
    return {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_customer_id": "cus_abc",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "openrouter_managed",
    }


@pytest.fixture(autouse=True)
def _managed_route_available(monkeypatch):
    """Switching INTO openrouter_managed checks its route first; make it available."""
    from core.config import settings

    monkeypatch.setattr(settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model", raising=False)
    monkeypatch.setattr("core.billing.openrouter_gate_url.gate_url", lambda: "https://gate.example/v1")
    monkeypatch.setattr("core.services.openrouter_keys.ensure_owner_key_for_settings", AsyncMock())


@pytest.fixture
def patch_dispatch_provision():
    """Stub the durable reconcile change_provider dispatches so no test reaches AWS.

    change_provider imports ``dispatch_reconcile`` inside the function body, so patching the
    source module (not the importing module) is what intercepts.
    """
    bg = AsyncMock(return_value=True)
    with patch("core.services.provision_dispatch.dispatch_reconcile", bg):
        yield bg


@pytest.fixture
def reprovision():
    """Stub the one seam the switch ends on: ``dispatch_reconcile`` (a durable SFN execution).

    The stop-first cold path is gone with the ECS runtime -- there is no service to scale to zero
    and no shared EFS-WAL SQLite for two tasks to corrupt, so the switch rewrites the owner's
    agent profiles in S3 and the agent reads them on its next turn. Assert on the yielded mock to
    prove a re-provision was scheduled.

    Without the row stub ``container_repo.get_by_owner_id`` hits real DynamoDB
    (ResourceNotFoundException). ``revoke_provider_auth_in_container`` (the
    authoritative credential revoke on the chatgpt-leaving branch) is stubbed too so it does not
    reach for real S3.
    """
    dispatch = AsyncMock(return_value=True)
    with (
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "owner", "status": "running"}),
        ),
        patch(
            "core.services.container_auth.revoke_provider_auth_in_container",
            new=AsyncMock(return_value=True),
        ),
        patch("core.services.provision_dispatch.dispatch_reconcile", dispatch),
    ):
        yield dispatch


@pytest.mark.asyncio
async def test_change_provider_rejects_byok_without_provider_or_credential_side_effects(async_client):
    """Legacy clients get an explicit retirement error before any billing write or secret lookup."""
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock()) as mock_get,
        patch("core.repositories.billing_repo.set_provider_choice", new=AsyncMock()) as mock_set,
        patch("core.services.key_service.get_secret_arn", new=AsyncMock()) as mock_secret,
        patch("core.services.provision_dispatch.dispatch_reconcile", new=AsyncMock()) as mock_dispatch,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "byo_key", "byo_provider": "openai"},
        )

    assert resp.status_code == 422
    mock_get.assert_not_awaited()
    mock_set.assert_not_awaited()
    mock_secret.assert_not_awaited()
    mock_dispatch.assert_not_awaited()


def test_change_provider_openapi_advertises_only_current_choices():
    from main import app

    operation = app.openapi()["paths"]["/api/v1/billing/provider"]["post"]
    schema_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema_name = schema_ref.rsplit("/", 1)[-1]
    body_schema = app.openapi()["components"]["schemas"][schema_name]
    assert body_schema["properties"]["provider_choice"]["enum"] == ["chatgpt_oauth", "openrouter_managed"]
    assert "byo_provider" not in body_schema["properties"]


@pytest.mark.asyncio
async def test_change_provider_chatgpt_without_oauth_tokens_returns_409(
    async_client,
    active_personal_account,
    patch_dispatch_provision,
):
    """Switching to ChatGPT OAuth without a completed device-code flow
    must 409 with a code the UI recognises, not silently flip provider_choice."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=active_personal_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ) as mock_set_pc,
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "oauth_required"
    mock_set_pc.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_owner_can_switch_to_chatgpt_oauth_and_is_recorded(
    async_client_org_admin,
):
    """Org owners may now run on chatgpt_oauth: the org admin's own ChatGPT
    account backs the org container. A legacy org row with no recorded
    connector is claimed by the first admin to switch, and that identity is
    persisted via set_provider_choice(connector_user_id=<caller>)."""
    org_account = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "openrouter_managed",
        # No oauth_connector_user_id — never been on chatgpt_oauth.
    }

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=org_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ) as mock_set_pc,
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "tok", "account_id": "acc"}),
        ),
        # Defensive: no container write is expected; stubbed so a regression
        # fails on assertions rather than on real DynamoDB.
        patch(
            "core.repositories.container_repo.update_fields",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "org_test_456", "status": "running"}),
        ),
        patch("core.services.provision_dispatch.dispatch_provision", MagicMock()),
        patch("asyncio.create_task", lambda coro: coro.close() or MagicMock()),
    ):
        resp = await async_client_org_admin.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["provider_choice"] == "chatgpt_oauth"
    # The owner identity (the switching admin, user_test_123) is recorded on
    # the org row so a non-owner admin can't later hijack the provider.
    mock_set_pc.assert_awaited_once_with(
        "org_test_456",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id="user_test_123",
    )


@pytest.mark.asyncio
async def test_non_owner_admin_cannot_switch_org_to_chatgpt(
    async_client_org_admin,
):
    """One ChatGPT account ↔ one container, bound to the recorded owner. A
    different org admin (user_b) cannot re-select chatgpt_oauth once user_a
    has claimed it — 403 not_workspace_owner, no provider write."""
    org_account = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "openrouter_managed",
        # user_a is the recorded owner; the caller (admin fixture) is
        # user_test_123 — a different human.
        "oauth_connector_user_id": "user_a",
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=org_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ) as mock_set_pc,
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "tok"}),
        ),
    ):
        resp = await async_client_org_admin.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_workspace_owner"
    mock_set_pc.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_switch_away_revokes_owner_tokens_and_owner_keyed_file(
    async_client_org_admin,
    reprovision,
):
    """Leaving chatgpt_oauth on an org revokes the RECORDED OWNER's token row
    (user_a — the human who connected, not necessarily the switcher) and
    deletes the owner-keyed EFS auth file (keyed on org_id, the container
    owner)."""
    org_account = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
        "oauth_connector_user_id": "user_a",
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=org_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.oauth_service.revoke_user_oauth",
            new=AsyncMock(return_value=None),
        ) as mock_revoke,
        patch(
            "core.services.container_auth.revoke_provider_auth_in_container",
            new=AsyncMock(return_value=None),
        ) as mock_delete_auth,
    ):
        resp = await async_client_org_admin.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 200, resp.text
    # Tokens are keyed on the recorded human owner, not the org_id and not the
    # (possibly different) switching admin.
    mock_revoke.assert_awaited_once_with(user_id="user_a")
    # The credential is keyed on the WORKSPACE (org_id), not the human who connected it.
    assert mock_delete_auth.await_args_list[-1].args == ("org_test_456",)


@pytest.mark.asyncio
async def test_personal_switch_away_still_revokes(
    async_client,
    reprovision,
):
    """Personal owners are unchanged: owner_id == user, so revoke + delete
    both key on the same id. With no recorded connector, the cleanup falls
    back to the calling user."""
    chatgpt_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
        # No oauth_connector_user_id — personal rows predate the field; the
        # cleanup falls back to the calling user (which IS the owner).
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=chatgpt_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.oauth_service.revoke_user_oauth",
            new=AsyncMock(return_value=None),
        ) as mock_revoke,
        patch(
            "core.services.container_auth.revoke_provider_auth_in_container",
            new=AsyncMock(return_value=None),
        ) as mock_delete_auth,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 200, resp.text
    mock_revoke.assert_awaited_once_with(user_id="user_test_123")
    assert mock_delete_auth.await_args_list[-1].args == ("org_test_456",)


@pytest.mark.asyncio
async def test_change_provider_inactive_subscription_returns_409(
    async_client,
    patch_dispatch_provision,
):
    """change_provider is for existing subscribers only — a row with no
    active subscription must be routed back through /onboarding's signup
    flow, not allowed to mutate provider state on a dead row."""
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "owner_type": "personal",
                "subscription_status": "canceled",
                "stripe_customer_id": "cus_abc",
            },
        ),
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_change_provider_no_billing_account_returns_404(
    async_client,
    patch_dispatch_provision,
):
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "no_billing_account"


@pytest.mark.asyncio
async def test_change_provider_same_choice_is_noop(
    async_client,
    active_personal_account,
    patch_dispatch_provision,
):
    """No commit for an idle re-click -- only the idempotent reconcile, which is how a lost
    switch is retried."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=active_personal_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ) as mock_set_pc,
        patch("core.repositories.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "noop"
    mock_set_pc.assert_not_awaited()
    patch_dispatch_provision.assert_called_once()


@pytest.mark.asyncio
async def test_change_provider_clears_oauth_when_leaving_chatgpt(
    async_client,
    reprovision,
):
    """Leaving chatgpt_oauth must revoke the tokens row + delete the EFS
    auth-profiles.json so a re-switch back to chatgpt_oauth re-runs the
    device-code flow against fresh credentials, not stale ones."""
    chatgpt_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=chatgpt_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.oauth_service.revoke_user_oauth",
            new=AsyncMock(return_value=None),
        ) as mock_revoke,
        patch(
            "core.services.container_auth.revoke_provider_auth_in_container",
            new=AsyncMock(return_value=None),
        ) as mock_delete_auth,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert mock_revoke.await_count == 1
    assert mock_delete_auth.await_count >= 1


@pytest.mark.asyncio
async def test_change_provider_keeps_chatgpt_choice_and_oauth_row_when_agentcore_revoke_fails(
    async_client,
    reprovision,
):
    """A failed authoritative AgentCore unstage must leave the old provider and its token row
    intact so the same switch can retry instead of skipping cleanup as an already-finished move."""
    chatgpt_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
    }
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(return_value=chatgpt_account)),
        patch("core.repositories.billing_repo.set_provider_choice", new=AsyncMock()) as mock_set,
        patch("core.services.oauth_service.revoke_user_oauth", new=AsyncMock()) as mock_revoke,
        patch(
            "core.services.container_auth.revoke_provider_auth_in_container",
            new=AsyncMock(side_effect=RuntimeError("s3 down")),
        ),
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "oauth_revoke_failed"
    mock_set.assert_not_awaited()
    mock_revoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_provider_clears_byo_key_when_leaving_byo(
    async_client_org_admin,
    reprovision,
):
    """Leaving byo_key must delete the matching api_keys row + Secrets
    Manager secret. KeyService.delete_key handles both. Keys are stored under
    the workspace OWNER (org id), never the acting user — deleting under the
    user id silently left the old key behind."""
    byo_account = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "byo_key",
        "byo_provider": "openai",
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=byo_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
    ):
        instance = MagicMock()
        instance.delete_key = AsyncMock(return_value=True)
        with patch(
            "core.services.key_service.KeyService",
            return_value=instance,
        ):
            resp = await async_client_org_admin.post(
                "/api/v1/billing/provider",
                json={"provider_choice": "openrouter_managed"},
            )

    assert resp.status_code == 200, resp.text
    instance.delete_key.assert_awaited_once_with("org_test_456", "openai")


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["cohere_command", "bedrock_claude", "bedrock_trial"])
async def test_change_provider_invalid_provider_choice_returns_422(async_client, choice):
    """Pydantic Literal rejects unknown and retired values before any DDB read."""
    resp = await async_client.post(
        "/api/v1/billing/provider",
        json={"provider_choice": choice},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_change_provider_org_member_blocked(async_client_org_member):
    """Org members (non-admins) cannot change the org's provider — only
    admins can. Matches /trial-checkout's require_org_admin gate."""
    resp = await async_client_org_member.post(
        "/api/v1/billing/provider",
        json={"provider_choice": "openrouter_managed"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The switch always re-provisions.
# ---------------------------------------------------------------------------
#
# The hot/cold split went with ``openclaw.json``: the hot half patched the three provider-block
# subtrees in place and relied on the gateway's chokidar reload to swap providers without a
# restart. A Hermes profile has no such file and no live reload, and the provision it fell back
# to is no longer a task-def re-register plus a container roll -- it is two S3 writes, so there
# is nothing left for a second path to save.


@pytest.mark.asyncio
async def test_change_provider_leaving_chatgpt_revokes_and_reprovisions(async_client, reprovision):
    """Leaving chatgpt_oauth: the credential revoke and both token rows still go, the switch
    re-provisions, and ``restart: True`` keeps the UI's cutover banner honest."""
    chatgpt_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
    }

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=chatgpt_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.oauth_service.revoke_user_oauth",
            new=AsyncMock(return_value=None),
        ) as mock_revoke,
        patch(
            "core.services.container_auth.revoke_provider_auth_in_container",
            new=AsyncMock(return_value=None),
        ) as mock_delete_auth,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "openrouter_managed"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restart"] is True
    reprovision.assert_called_once()
    # Side-effect cleanup for leaving chatgpt_oauth must still run.
    assert mock_revoke.await_count == 1
    assert mock_delete_auth.await_count >= 1


@pytest.mark.asyncio
async def test_phantom_connector_self_heals_on_switch_into_chatgpt(
    async_client_org_admin,
):
    """A recorded connector naming no Clerk user must not 403 a real admin's
    switch INTO chatgpt_oauth: the reclaim clears the phantom (CAS) and the
    switch proceeds as the first claim, recording the switching admin."""
    from unittest.mock import MagicMock

    org_account = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "openrouter_managed",
        # Phantom: names no Clerk user. The caller (user_test_123) is a real
        # admin who would otherwise eat a permanent 403 here.
        "oauth_connector_user_id": "user_ghost",
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=org_account),
        ),
        patch(
            "core.services.oauth_service.maybe_clear_phantom_connector",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ) as mock_set_pc,
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "tok", "account_id": "acc"}),
        ),
        patch(
            "core.repositories.container_repo.update_fields",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "org_test_456", "status": "running"}),
        ),
        patch("core.services.provision_dispatch.dispatch_provision", MagicMock()),
        patch("asyncio.create_task", lambda coro: coro.close() or MagicMock()),
    ):
        resp = await async_client_org_admin.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 200, resp.text
    mock_set_pc.assert_awaited_once_with(
        "org_test_456",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id="user_test_123",
    )
