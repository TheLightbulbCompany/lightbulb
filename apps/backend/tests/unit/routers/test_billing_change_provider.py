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
def patch_dispatch_provision():
    """Stub the asyncio.create_task path so we don't fire real ECS calls.

    change_provider does a local ``from core.services.container_provision
    import dispatch_provision`` inside the function body, so patching the
    source module (not the importing module) is what intercepts. The
    AsyncMock returns a coroutine so ``asyncio.create_task`` happily wraps
    it without RuntimeWarning.
    """
    bg = AsyncMock()
    with patch("core.services.provision_dispatch.dispatch_provision", bg):
        yield bg


@pytest.fixture
def cold_reprovision():
    """Stub the F1 stop-first cold-reprovision seam (PR #1412).

    The cold path no longer dispatches ``dispatch_provision`` directly.
    It now:
        manager = get_ecs_manager()
        if await container_repo.get_by_owner_id(owner) is not None:
            await container_repo.takeover_provision(owner)
            await manager.stop_user_service(owner, reason="reprovision")
        asyncio.create_task(manager.drain_then_dispatch_provision(owner))
    (``drain_then_dispatch_provision`` is what funnels to dispatch_provision
    one layer down, after the drain-wait.)

    Without these stubs ``container_repo.get_by_owner_id`` hits real DynamoDB
    (ResourceNotFoundException), the except-block reverts the just-committed
    provider, and the endpoint 502s ``reprovision_stop_failed``. This fixture
    is the current cold-path contract; assert on the yielded manager's
    ``drain_then_dispatch_provision`` (called via create_task) to prove a
    re-provision was scheduled.

    ``revoke_provider_auth_in_container`` (best-effort live-token logout on the
    chatgpt-leaving branch) is stubbed too so it doesn't reach for a real
    running container.
    """
    manager = MagicMock()
    manager.stop_user_service = AsyncMock(return_value=None)
    manager.drain_then_dispatch_provision = AsyncMock(return_value={"status": "reprovisioning"})
    manager.resolve_running_container = AsyncMock(return_value=(None, None))
    with (
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "owner", "status": "running"}),
        ),
        patch(
            "core.repositories.container_repo.takeover_provision",
            new=AsyncMock(return_value="lease-token"),
        ),
        patch(
            "core.services.container_auth.revoke_provider_auth_in_container",
            new=AsyncMock(return_value=True),
        ),
    ):
        yield manager


@pytest.mark.asyncio
async def test_change_provider_bedrock_to_byo_key_happy_path(
    async_client,
    active_personal_account,
    cold_reprovision,
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
            "core.services.key_service.get_secret_arn",
            new=AsyncMock(return_value="arn:aws:secretsmanager:...:key"),
        ) as mock_get_secret_arn,
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
    mock_get_secret_arn.assert_awaited_once_with("user_test_123", "anthropic")
    mock_set_pc.assert_awaited_once_with(
        "user_test_123",
        provider_choice="byo_key",
        byo_provider="anthropic",
        # Non-chatgpt switch always passes connector_user_id=None so the repo
        # REMOVEs any stale connector from the row.
        connector_user_id=None,
    )


@pytest.mark.asyncio
async def test_change_provider_byo_key_without_saved_key_returns_409(
    async_client,
    active_personal_account,
    patch_dispatch_provision,
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
            "core.services.key_service.get_secret_arn",
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
        "provider_choice": "bedrock_claude",
        # No oauth_connector_user_id — never been on chatgpt_oauth.
    }

    async def _replace(
        owner_id,
        *,
        providers,
        default_model,
        plugin_entries,
        ceo_model,
        cheap_model,
        heartbeat_model,
        utility_model,
        subagent_model,
        llm_task_model,
        web_search_provider,
        tool_search_mode,
    ):
        # Hot swap into chatgpt_oauth — shape pinned by the personal hot-path
        # test; here we only care that the org claim + connector write happen.
        pass

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
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.config_patcher.replace_provider_block",
            new=AsyncMock(side_effect=_replace),
        ),
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
        "provider_choice": "bedrock_claude",
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
    cold_reprovision,
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
            "core.services.key_service.get_secret_arn",
            new=AsyncMock(return_value="arn:aws:secretsmanager:...:key"),
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
        resp = await async_client_org_admin.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "byo_key", "byo_provider": "anthropic"},
        )

    assert resp.status_code == 200, resp.text
    # Tokens are keyed on the recorded human owner, not the org_id and not the
    # (possibly different) switching admin.
    mock_revoke.assert_awaited_once_with(user_id="user_a")
    # The EFS auth-profiles.json lives under the CONTAINER OWNER (org_id).
    mock_delete_auth.assert_awaited_once_with(user_id="org_test_456")


@pytest.mark.asyncio
async def test_org_byo_key_prereq_is_owner_scoped_not_clerk_user_scoped(
    async_client_org_admin,
    cold_reprovision,
):
    """An org's BYOK secret is stored under the ORG id, so the prerequisite
    check must look it up there too.

    Every route in routers/settings_keys.py (list/set/delete) keys the secret
    on ``resolve_owner_id(auth)`` — org_id for an org — and the provisioner
    reads it back the same way (``ecs_manager._build_byo_secrets_for_user``).
    This check previously passed the CLERK user id instead. For a personal
    owner the two ids are identical, which is exactly why it went unnoticed;
    for an ORG they diverge, so an admin who had just saved a key got a
    spurious ``byo_key_missing`` and could never complete the switch —
    dead-ending the whole org BYO path, in-app and on web.

    Contrast the chatgpt_oauth branch, which legitimately uses BOTH ids:
    OAuth tokens are per-HUMAN bound to a workspace. A BYOK secret has no
    such per-human identity.
    """
    org_account = {
        "owner_id": "org_test_456",
        "owner_type": "org",
        "stripe_subscription_id": "sub_x",
        "subscription_status": "active",
        "provider_choice": "bedrock_claude",
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
            "core.services.key_service.get_secret_arn",
            new=AsyncMock(return_value="arn:aws:secretsmanager:...:key"),
        ) as mock_get_secret_arn,
    ):
        resp = await async_client_org_admin.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "byo_key", "byo_provider": "anthropic"},
        )

    assert resp.status_code == 200, resp.text
    # The ORG id — NOT the switching admin's personal clerk id.
    mock_get_secret_arn.assert_awaited_once_with("org_test_456", "anthropic")


@pytest.mark.asyncio
async def test_personal_switch_away_still_revokes(
    async_client,
    cold_reprovision,
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
            "core.containers.workspace.delete_auth_profile_store",
            new=AsyncMock(return_value=None),
        ) as mock_delete_auth,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 200, resp.text
    mock_revoke.assert_awaited_once_with(user_id="user_test_123")
    mock_delete_auth.assert_awaited_once_with(user_id="user_test_123")


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
            json={"provider_choice": "bedrock_claude"},
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
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "no_billing_account"


@pytest.mark.asyncio
async def test_change_provider_same_choice_is_noop(
    async_client,
    active_personal_account,
    patch_dispatch_provision,
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
    patch_dispatch_provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_provider_clears_oauth_when_leaving_chatgpt(
    async_client,
    cold_reprovision,
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
    cold_reprovision,
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
#   2. dispatch_provision is NOT called (no ECS restart).
#   3. For a ChatGPT-bound switch, auth-profiles.json is staged BEFORE the
#      openclaw.json patch (chokidar's reload would otherwise read a stale
#      or missing auth file and the openai provider would 401 once).
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

    async def _clear(*, user_id):
        call_order.append("clear")
        return True

    async def _replace(
        owner_id,
        *,
        providers,
        default_model,
        plugin_entries,
        ceo_model,
        cheap_model,
        heartbeat_model,
        utility_model,
        subagent_model,
        llm_task_model,
        web_search_provider,
        tool_search_mode,
    ):
        call_order.append("replace")
        # The chatgpt_oauth branch sets ``models.providers`` to {} and the
        # default model to the openai pair. Pin the contract so a
        # silent change to build_provider_block fails the gate test rather
        # than landing the wrong block on the running container.
        assert providers == {}
        assert default_model == {
            "primary": "openai/gpt-5.5",
            "fallbacks": ["openai/gpt-5.4"],
        }
        assert plugin_entries == {}
        # chatgpt_oauth is flat-rate — no premium tier, so the CEO's stale
        # amazon-bedrock Opus override MUST be cleared (ceo_model=None).
        assert ceo_model is None
        # chatgpt_oauth has NO usable cheap tier: its Codex runtime ignores/
        # rejects an OpenClaw-side compaction summarizer override, so a pinned
        # aux model (gpt-5.4-mini) makes compaction fail and the session
        # overflow. cheap_model_ref(chatgpt_oauth, None) returns None, so the
        # aux slots MUST be cleared here too — critical on this hot path, where
        # a bedrock owner switching to chatgpt would otherwise inherit the stale
        # bedrock-haiku compaction override and hit the overflow bug.
        assert cheap_model is None
        # The heartbeat is an ordinary agent turn the Codex OAuth token CAN
        # serve on the mini tier (unlike compaction) — so the hot swap re-pins
        # it rather than clearing it with the compaction slots. Same for the
        # utility model (titles etc.). Subagents re-pin to the standard tier
        # so children never inherit a stale bedrock ref; llm-task has no cheap
        # tier on this path (cheap_model is None) and goes enable-only.
        assert heartbeat_model == "openai/gpt-5.4-mini"
        assert utility_model == "openai/gpt-5.4-mini"
        assert subagent_model == "openai/gpt-5.5"
        assert llm_task_model is None
        # The search pin must be DROPPED on the Codex path so hosted search
        # engages (mirrors provision-time omission).
        assert web_search_provider is None
        # Same for toolSearch: never written for chatgpt_oauth at provision,
        # so the swap must delete any stale embedded-runtime key.
        assert tool_search_mode is None

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
            "core.containers.workspace.clear_default_agent_auth_profile_row",
            new=AsyncMock(side_effect=_clear),
        ),
        patch(
            "core.services.config_patcher.replace_provider_block",
            new=AsyncMock(side_effect=_replace),
        ),
        patch(
            "core.services.provision_dispatch.dispatch_provision",
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
    # otherwise reload-plugins onto an absent auth-profiles.json. The stale
    # default-agent auth row is cleared AFTER staging (so the freshly staged
    # oauth.json re-imports on the running container) and BEFORE the config
    # patch triggers the reload.
    assert call_order == ["pre_stage", "clear", "replace"], call_order


@pytest.mark.asyncio
async def test_change_provider_chatgpt_to_bedrock_takes_cold_path(async_client, cold_reprovision):
    """ChatGPT OAuth → Bedrock: a switch INTO bedrock_claude must take the
    COLD path, NOT the hot path (Plan 2 Task 6). The per-owner scoped task
    role is attached only by a task-def re-register in provision_user_container,
    which the hot path never does; the config builder also fails closed on
    bedrock without aip_arns, so the hot path could not synthesize a bedrock
    block anyway. The existing chatgpt-leaving side-effect cleanup (revoke +
    delete auth-profiles.json) must still run, and restart=True so the UI
    shows the cutover banner.
    """
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
        patch(
            "core.services.config_patcher.replace_provider_block",
            new=AsyncMock(),
        ) as mock_replace,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "bedrock_claude"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Cold path: container restarts (task-def re-register attaches the scoped
    # per-owner role), the drain-then-reprovision is dispatched (which funnels
    # to dispatch_provision one layer down), hot helper NOT called.
    assert body["restart"] is True
    cold_reprovision.drain_then_dispatch_provision.assert_called_once()
    mock_replace.assert_not_awaited()
    # Side-effect cleanup for leaving chatgpt_oauth must still run.
    mock_revoke.assert_awaited_once()
    mock_delete_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_provider_hot_path_falls_back_to_cold_on_missing_config(async_client, cold_reprovision):
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
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restart"] is True, "cold-path fallback must report restart=True"
    cold_reprovision.drain_then_dispatch_provision.assert_called_once()


@pytest.mark.asyncio
async def test_change_provider_bedrock_to_byo_always_cold_path(async_client, cold_reprovision):
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
            "core.services.key_service.get_secret_arn",
            new=AsyncMock(return_value="arn:..."),
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
    cold_reprovision.drain_then_dispatch_provision.assert_called_once()


@pytest.mark.asyncio
async def test_change_provider_to_chatgpt_falls_cold_when_clear_needs_restart(async_client, cold_reprovision):
    """O6 local-SQLite layout: the auth-row clear cannot run in place (store is
    container-local) — it writes a boot sentinel and reports restart_required.
    The hot swap must ABORT and fall back to the stop-first cold path, whose
    restart contract the frontend already renders. replace_provider_block must
    never run on the aborted hot path (a patched config without a cleared auth
    row would keep serving the stale token)."""
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
            new=AsyncMock(return_value={"access_token": "tok", "account_id": "acc"}),
        ),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()),
        patch(
            "core.containers.workspace.clear_default_agent_auth_profile_row",
            new=AsyncMock(return_value="restart_required"),
        ),
        patch("core.services.config_patcher.replace_provider_block", new=AsyncMock()) as mock_replace,
    ):
        resp = await async_client.post(
            "/api/v1/billing/provider",
            json={"provider_choice": "chatgpt_oauth"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["provider_choice"] == "chatgpt_oauth"
    assert body["restart"] is True, "must take the cold path (restart contract)"
    mock_replace.assert_not_awaited()
    cold_reprovision.drain_then_dispatch_provision.assert_called_once()
