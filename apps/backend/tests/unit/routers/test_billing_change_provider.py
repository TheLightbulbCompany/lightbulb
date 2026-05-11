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
        "provider_choice": "bedrock_claude",
    }


@pytest.fixture
def patch_background_provision():
    """Stub the asyncio.create_task path so we don't fire real ECS calls.

    change_provider does a local ``from core.services.container_provision
    import background_provision`` inside the function body, so patching the
    source module (not the importing module) is what intercepts. The
    AsyncMock returns a coroutine so ``asyncio.create_task`` happily wraps
    it without RuntimeWarning.
    """
    bg = AsyncMock()
    with patch("core.services.container_provision.background_provision", bg):
        yield bg


@pytest.mark.asyncio
async def test_change_provider_bedrock_to_byo_key_happy_path(
    async_client,
    active_personal_account,
    patch_background_provision,
):
    """Bedrock → BYO Anthropic: validates key exists, flips choice, no OAuth
    cleanup (we weren't on chatgpt_oauth), schedules a re-provision."""
    fresh_account = dict(active_personal_account)

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=fresh_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ) as mock_set_pc,
        patch(
            "core.repositories.api_key_repo.get_key",
            new=AsyncMock(return_value={"secret_arn": "arn:aws:secretsmanager:...:key"}),
        ) as mock_get_key,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "byo_key", "byo_provider": "anthropic"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["provider_choice"] == "byo_key"
    assert body["byo_provider"] == "anthropic"
    mock_get_key.assert_awaited_once_with("user_test_123", "anthropic")
    mock_set_pc.assert_awaited_once_with(
        "user_test_123",
        provider_choice="byo_key",
        byo_provider="anthropic",
        owner_type="personal",
    )


@pytest.mark.asyncio
async def test_change_provider_byo_key_without_saved_key_returns_409(
    async_client,
    active_personal_account,
    patch_background_provision,
):
    """The endpoint must refuse to flip provider_choice when the new BYO
    key isn't on file — otherwise the next chat would fail at ECS with no
    secret to inject. UI is expected to PUT /settings/keys first."""
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
            "core.repositories.api_key_repo.get_key",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "byo_key", "byo_provider": "openai"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "byo_key_missing"
    mock_set_pc.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_provider_chatgpt_without_oauth_tokens_returns_409(
    async_client,
    active_personal_account,
    patch_background_provision,
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
async def test_change_provider_chatgpt_blocked_for_org_context(
    async_client_org_admin,
    patch_background_provision,
):
    """Mirrors /trial-checkout's org guard — ChatGPT OAuth is personal-only
    (memory/project_chatgpt_oauth_personal_only.md). A switched-into-org
    admin can't pick OAuth from the change-provider modal either."""
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "owner_id": "org_test_456",
                "owner_type": "org",
                "stripe_subscription_id": "sub_x",
                "subscription_status": "active",
                "provider_choice": "bedrock_claude",
            },
        ),
    ):
        resp = await async_client_org_admin.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "org_chatgpt_oauth_forbidden"


@pytest.mark.asyncio
async def test_change_provider_inactive_subscription_returns_409(
    async_client,
    patch_background_provision,
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
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_change_provider_no_billing_account_returns_404(
    async_client,
    patch_background_provision,
):
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "no_billing_account"


@pytest.mark.asyncio
async def test_change_provider_same_choice_is_noop(
    async_client,
    active_personal_account,
    patch_background_provision,
):
    """No work + no container restart for an idle re-click."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=active_personal_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ) as mock_set_pc,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "noop"
    mock_set_pc.assert_not_awaited()
    patch_background_provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_provider_clears_oauth_when_leaving_chatgpt(
    async_client,
    patch_background_provision,
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
            "core.containers.workspace.delete_auth_profile_store",
            new=AsyncMock(return_value=None),
        ) as mock_delete_auth,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    mock_revoke.assert_awaited_once()
    mock_delete_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_provider_clears_byo_key_when_leaving_byo(
    async_client,
    patch_background_provision,
):
    """Leaving byo_key must delete the matching api_keys row + Secrets
    Manager secret. KeyService.delete_key handles both."""
    byo_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
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
            resp = await async_client.post(
                "/api/v1/billing/provider",
                json={"provider_choice": "bedrock_claude"},
            )

    assert resp.status_code == 200, resp.text
    instance.delete_key.assert_awaited_once_with("user_test_123", "openai")


@pytest.mark.asyncio
async def test_change_provider_invalid_provider_choice_returns_422(async_client):
    """Pydantic Literal rejects unknown values before any DDB read."""
    resp = await async_client.post(
        "/api/v1/billing/provider",
        json={"provider_choice": "cohere_command"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_change_provider_org_member_blocked(async_client_org_member):
    """Org members (non-admins) cannot change the org's provider — only
    admins can. Matches /trial-checkout's require_org_admin gate."""
    resp = await async_client_org_member.post(
        "/api/v1/billing/provider",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Hot path: Bedrock ↔ ChatGPT OAuth swap without a container restart
# ---------------------------------------------------------------------------
#
# These tests pin the *core promise* of the hot path:
#   1. config_patcher.replace_provider_block is called with the right shape.
#   2. background_provision is NOT called (no ECS restart).
#   3. For a ChatGPT-bound switch, auth-profiles.json is staged BEFORE the
#      openclaw.json patch (chokidar's reload would otherwise read a stale
#      or missing auth file and the openai-codex provider would 401 once).
#   4. The response carries restart=False so the UI doesn't show the
#      cutover banner unnecessarily.


@pytest.mark.asyncio
async def test_change_provider_bedrock_to_chatgpt_hot_path_no_restart(async_client):
    """Bedrock → ChatGPT OAuth must succeed via Track-1 config patch with
    no ECS restart. auth-profiles.json must land before openclaw.json."""
    bedrock_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "bedrock_claude",
    }

    call_order: list[str] = []

    async def _pre_stage(*, user_id, oauth_tokens):
        call_order.append("pre_stage")

    async def _replace(owner_id, *, providers, default_model, plugin_entries):
        call_order.append("replace")
        # The chatgpt_oauth branch sets ``models.providers`` to {} and the
        # default model to the openai-codex pair. Pin the contract so a
        # silent change to build_provider_block fails the gate test rather
        # than landing the wrong block on the running container.
        assert providers == {}
        assert default_model == {
            "primary": "openai-codex/gpt-5.5",
            "fallbacks": ["openai-codex/gpt-5.5"],
        }
        assert plugin_entries == {}

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=bedrock_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "tok", "account_id": "acc"}),
        ),
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(side_effect=_pre_stage),
        ),
        patch(
            "core.services.config_patcher.replace_provider_block",
            new=AsyncMock(side_effect=_replace),
        ),
        patch(
            "core.services.container_provision.background_provision",
            new=AsyncMock(),
        ) as mock_bg,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["provider_choice"] == "chatgpt_oauth"
    assert body["restart"] is False, "hot path must not restart the container"
    mock_bg.assert_not_awaited()
    # Pre-stage MUST land before the openclaw.json patch — chokidar would
    # otherwise reload-plugins onto an absent auth-profiles.json.
    assert call_order == ["pre_stage", "replace"], call_order


@pytest.mark.asyncio
async def test_change_provider_chatgpt_to_bedrock_hot_path_no_restart(async_client):
    """ChatGPT OAuth → Bedrock: revoke + delete auth-profiles.json first
    (existing side-effect cleanup), then hot-swap. No container restart."""
    chatgpt_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
    }

    async def _replace(owner_id, *, providers, default_model, plugin_entries):
        # Pin the Bedrock-bound contract. amazon-bedrock provider entry +
        # discovery=false + us. inference-profile model IDs.
        assert "amazon-bedrock" in providers
        assert providers["amazon-bedrock"]["api"] == "bedrock-converse-stream"
        assert default_model["primary"].startswith("amazon-bedrock/us.")
        assert plugin_entries["amazon-bedrock"]["config"]["discovery"]["enabled"] is False

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
            "core.containers.workspace.delete_auth_profile_store",
            new=AsyncMock(return_value=None),
        ) as mock_delete_auth,
        patch(
            "core.services.config_patcher.replace_provider_block",
            new=AsyncMock(side_effect=_replace),
        ) as mock_replace,
        patch(
            "core.services.container_provision.background_provision",
            new=AsyncMock(),
        ) as mock_bg,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restart"] is False
    mock_bg.assert_not_awaited()
    mock_replace.assert_awaited_once()
    # Side-effect cleanup must still run on the hot path.
    mock_revoke.assert_awaited_once()
    mock_delete_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_provider_hot_path_falls_back_to_cold_on_missing_config(async_client):
    """If openclaw.json doesn't exist yet (e.g. owner never provisioned a
    container), replace_provider_block raises ConfigPatchError. The endpoint
    must NOT 500 — it falls back to a full re-provision so the file gets
    written from scratch."""
    from core.services.config_patcher import ConfigPatchError

    bedrock_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "bedrock_claude",
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=bedrock_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "tok"}),
        ),
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.config_patcher.replace_provider_block",
            new=AsyncMock(side_effect=ConfigPatchError("Config not found")),
        ),
        patch(
            "core.services.container_provision.background_provision",
            new=AsyncMock(),
        ) as mock_bg,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restart"] is True, "cold-path fallback must report restart=True"
    mock_bg.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_provider_bedrock_to_byo_always_cold_path(async_client, patch_background_provision):
    """BYO transitions can't avoid the restart — ECS env vars are bake-time.
    Even if openclaw.json patch would work, we MUST re-register the task
    definition with the new Secrets Manager secret ARN. restart=True."""
    bedrock_account = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "bedrock_claude",
    }
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=bedrock_account),
        ),
        patch(
            "core.repositories.billing_repo.set_provider_choice",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.repositories.api_key_repo.get_key",
            new=AsyncMock(return_value={"secret_arn": "arn:..."}),
        ),
        patch(
            "core.services.config_patcher.replace_provider_block",
            new=AsyncMock(),
        ) as mock_replace,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "byo_key", "byo_provider": "openai"},
        )

    assert resp.status_code == 200
    assert resp.json()["restart"] is True
    # BYO path must not touch the hot helper — the cold path writes a fresh
    # openclaw.json end-to-end.
    mock_replace.assert_not_awaited()
    patch_background_provision.assert_awaited_once()
