"""Unit tests for the extracted background_provision helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager
from core.services.container_provision import background_provision, resolve_provider_choice


# ---------------------------------------------------------------------------
# resolve_provider_choice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_provider_choice_trial_local_returns_meta():
    """A trial_local row (the no-card local trial) resolves to the `meta`
    provider mode. `meta` is a config-path mode, never a stored
    provider_choice, so it must NOT be read off the row — it short-circuits
    before the provider_choice/byo_provider lookup entirely."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trial_local"},
        )
        result = await resolve_provider_choice("user_x")
    assert result == ("meta", None)


@pytest.mark.asyncio
async def test_resolve_provider_choice_bedrock_unchanged():
    """Existing bedrock_claude path is untouched by the trial_local branch."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "provider_choice": "bedrock_claude",
            },
        )
        result = await resolve_provider_choice("user_x")
    assert result == ("bedrock_claude", None)


@pytest.mark.asyncio
async def test_resolve_provider_choice_byo_key_unchanged():
    """Existing byo_key path (with its byo_provider) is untouched."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "provider_choice": "byo_key",
                "byo_provider": "anthropic",
            },
        )
        result = await resolve_provider_choice("user_x")
    assert result == ("byo_key", "anthropic")


@pytest.mark.asyncio
async def test_resolve_provider_choice_no_row_defaults_to_bedrock():
    """No billing row at all still falls back to bedrock_claude — the
    legacy default for owners onboarded before per-owner provider_choice
    storage. Must not be confused with trial_local."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value=None)
        result = await resolve_provider_choice("user_x")
    assert result == ("bedrock_claude", None)


@pytest.mark.asyncio
async def test_background_provision_resolves_provider_and_dispatches():
    """background_provision reads provider_choice from billing_repo
    via _resolve_provider_choice and calls ecs.provision_user_container
    with the resolved values."""
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="svc-name")
    with (
        patch(
            "core.services.container_provision.resolve_provider_choice",
            new=AsyncMock(return_value=("bedrock_claude", None)),
        ),
        patch(
            "core.services.container_provision.get_ecs_manager",
            return_value=fake_ecs,
        ),
    ):
        await background_provision("user_123", "user_123")
    fake_ecs.provision_user_container.assert_awaited_once_with(
        "user_123",
        provider_choice="bedrock_claude",
        byo_provider=None,
        owner_type="personal",
    )


@pytest.mark.asyncio
async def test_background_provision_passes_org_owner_type():
    """An org owner_id (org_*) must reprovision with owner_type='org' — config
    generation keys org-specific behavior (e.g. gateway browser mode) off it,
    so defaulting to 'personal' would silently rewrite an org's config."""
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="svc-name")
    with (
        patch(
            "core.services.container_provision.resolve_provider_choice",
            new=AsyncMock(return_value=("chatgpt_oauth", None)),
        ),
        patch(
            "core.services.container_provision.get_ecs_manager",
            return_value=fake_ecs,
        ),
    ):
        await background_provision("org_abc", "user_admin")
    fake_ecs.provision_user_container.assert_awaited_once_with(
        "org_abc",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        owner_type="org",
    )


@pytest.mark.asyncio
async def test_background_provision_swallows_exceptions():
    """Exceptions are logged, not raised. The helper is an
    asyncio.create_task target; raising would create unhandled
    task exceptions."""
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch(
            "core.services.container_provision.resolve_provider_choice",
            new=AsyncMock(return_value=("bedrock_claude", None)),
        ),
        patch(
            "core.services.container_provision.get_ecs_manager",
            return_value=fake_ecs,
        ),
    ):
        # Should NOT raise.
        await background_provision("user_123", "user_123")


@pytest.mark.asyncio
async def test_background_provision_emits_error_metric_on_failure():
    """On failure, the swallowed exception must still be alarmable: emit
    container.provision{status=error} — the SAME metric+dimension set the
    synchronous ecs_manager failure path uses, so it lands on the exact
    CloudWatch stream the W1 provision-error-rate alarm queries. (No `phase`
    dim: EMF publishes one stream per full dimension-key set, so an extra dim
    would create a stream no alarm watches.)"""
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch(
            "core.services.container_provision.resolve_provider_choice",
            new=AsyncMock(return_value=("bedrock_claude", None)),
        ),
        patch(
            "core.services.container_provision.get_ecs_manager",
            return_value=fake_ecs,
        ),
        patch("core.observability.metrics.put_metric") as mock_put,
    ):
        # Should NOT raise even though provisioning failed.
        await background_provision("user_123", "user_123")

    error_calls = [c for c in mock_put.call_args_list if c.args and c.args[0] == "container.provision"]
    assert len(error_calls) == 1, f"expected one container.provision emission, got {error_calls}"
    assert error_calls[0].kwargs.get("dimensions") == {"status": "error"}


@pytest.mark.asyncio
async def test_background_provision_metric_failure_does_not_raise():
    """A metrics outage in the except handler must not turn the swallow into a
    raise (the task is fire-and-forget)."""
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch(
            "core.services.container_provision.resolve_provider_choice",
            new=AsyncMock(return_value=("bedrock_claude", None)),
        ),
        patch(
            "core.services.container_provision.get_ecs_manager",
            return_value=fake_ecs,
        ),
        patch(
            "core.observability.metrics.put_metric",
            side_effect=RuntimeError("cloudwatch down"),
        ),
    ):
        # Should NOT raise.
        await background_provision("user_123", "user_123")


async def test_pre_stage_oauth_reads_connector_writes_owner():
    """The token READ key (connector / workspace owner) is decoupled from
    the EFS WRITE key (container owner / org_id). For an org, tokens live
    under the owner's Clerk user_id but the file must land on the org's
    EFS path — so a single ``user_id`` arg cannot serve both."""
    mgr = object.__new__(EcsManager)  # no AWS clients needed for this path
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ) as read,
        patch(
            "core.containers.workspace.pre_stage_auth_profile_store",
            new=AsyncMock(),
        ) as write,
    ):
        await EcsManager._pre_stage_oauth_for_user(mgr, "org_1", token_owner_id="user_a")
    read.assert_awaited_once_with(user_id="user_a", bound_to="org_1")
    write.assert_awaited_once()
    assert write.call_args.kwargs["user_id"] == "org_1"
