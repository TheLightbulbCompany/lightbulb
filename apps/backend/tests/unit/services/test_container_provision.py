"""Unit tests for the extracted background_provision helper."""

from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.config import BEDROCK_TRIAL_CHOICE
from core.containers.ecs_manager import EcsManager
from core.services.container_provision import background_provision, resolve_provider_choice


# ---------------------------------------------------------------------------
# resolve_provider_choice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_provider_choice_trial_local_short_circuits_to_bedrock_trial():
    """A trial_local row (the no-card local trial) short-circuits BEFORE the
    provider_choice/byo_provider lookup — `trial_local` is a
    subscription_status, never a stored provider_choice, so the provider must
    not be read off the row (a trial row carries none).

    It resolves to the one-tier `bedrock_trial` CONFIG MODE. Falling through to
    `bedrock_claude` would hand a card-less owner the full paid three-tier
    ladder — Opus included."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trial_local", "provider_choice": "byo_key"},
        )
        result = await resolve_provider_choice("user_x")
    # Short-circuits: the row's stray provider_choice/byo_provider is ignored.
    assert result == (BEDROCK_TRIAL_CHOICE, None)
    assert result[0] == "bedrock_trial"


@pytest.mark.asyncio
async def test_resolve_provider_choice_trial_local_writes_nothing():
    """`bedrock_trial` is a config-path mode, never a STORED provider_choice —
    resolving one must not write it back onto the billing row."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await resolve_provider_choice("user_x")
    repo.update_fields.assert_not_called()
    repo.upsert.assert_not_called()


def _literal_values(annotation) -> set[str]:
    """Flatten the string values out of a (possibly optional) Literal."""
    values: set[str] = set()
    for arg in get_args(annotation):
        if isinstance(arg, str):
            values.add(arg)
        else:
            values |= _literal_values(arg)
    return values


def test_bedrock_trial_is_not_an_acceptable_stored_provider_choice():
    """The only write paths for provider_choice are POST /billing/trial-checkout
    and POST /billing/provider. Both validate against a Literal that must NOT
    admit `bedrock_trial` — a request body could otherwise persist the trial
    config mode as a paid owner's provider, and the trial lockdown (one AIP, no
    cron/ACP/heartbeat) would silently apply to a paying customer."""
    from routers.billing import ChangeProviderRequest, TrialCheckoutRequest
    from schemas.user_schemas import SyncUserRequest

    for model in (TrialCheckoutRequest, ChangeProviderRequest, SyncUserRequest):
        allowed = _literal_values(model.model_fields["provider_choice"].annotation)
        assert allowed, f"{model.__name__}.provider_choice is not a Literal any more"
        assert BEDROCK_TRIAL_CHOICE not in allowed, f"{model.__name__} admits {BEDROCK_TRIAL_CHOICE}"


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


@pytest.fixture(autouse=True)
def _claim_always_wins():
    """background_provision's FIRST act is the single-flight lease claim.
    Default it to a won claim so the pre-existing dispatch tests exercise the
    provision path; the claim-semantics tests below override this patch with
    their own (innermost patch wins)."""
    with (
        patch(
            "core.services.container_provision.container_repo.try_claim_provision",
            new=AsyncMock(return_value="claim-fixture"),
        ),
        patch(
            "core.services.container_provision.container_repo.release_provision_to_error",
            new=AsyncMock(),
        ),
    ):
        yield


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
        claim_id="claim-fixture",
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
        claim_id="claim-fixture",
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


# ---------------------------------------------------------------------------
# background_provision — single-flight dispatch. The claim is the FIRST act;
# concurrent dispatches (the refresh-driven poll storm) stand down, failures
# release the lease to error (self-healing via the poll's auto-retry), and a
# superseded run writes nothing over the superseding run's state.


@pytest.mark.asyncio
async def test_background_provision_loser_stands_down_without_provisioning():
    from core.services.container_provision import background_provision

    with (
        patch(
            "core.services.container_provision.container_repo.try_claim_provision",
            new=AsyncMock(return_value=None),
        ),
        patch("core.services.container_provision.get_ecs_manager") as ecs_factory,
        patch("core.services.container_provision.billing_repo") as repo,
    ):
        await background_provision("user_1", "user_1")

    ecs_factory.assert_not_called()
    repo.get_by_owner_id.assert_not_called()  # stands down before ANY side read


@pytest.mark.asyncio
async def test_background_provision_winner_threads_the_fencing_token():
    from core.services.container_provision import background_provision

    manager = MagicMock()
    manager.provision_user_container = AsyncMock(return_value="svc-1")
    with (
        patch(
            "core.services.container_provision.container_repo.try_claim_provision",
            new=AsyncMock(return_value="claim-123"),
        ),
        patch("core.services.container_provision.get_ecs_manager", return_value=manager),
        patch("core.services.container_provision.billing_repo") as repo,
    ):
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await background_provision("user_1", "user_1")

    kwargs = manager.provision_user_container.await_args.kwargs
    assert kwargs["claim_id"] == "claim-123"


@pytest.mark.asyncio
async def test_background_provision_failure_releases_the_lease_to_error():
    from core.services.container_provision import background_provision

    manager = MagicMock()
    manager.provision_user_container = AsyncMock(side_effect=RuntimeError("mint blew up"))
    release = AsyncMock()
    with (
        patch(
            "core.services.container_provision.container_repo.try_claim_provision",
            new=AsyncMock(return_value="claim-123"),
        ),
        patch(
            "core.services.container_provision.container_repo.release_provision_to_error",
            new=release,
        ),
        patch("core.services.container_provision.get_ecs_manager", return_value=manager),
        patch("core.services.container_provision.billing_repo") as repo,
    ):
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await background_provision("user_1", "user_1")  # must not raise (create_task target)

    release.assert_awaited_once()
    assert release.await_args.args == ("user_1", "claim-123")
    # The release now also carries a classified failure cause. RuntimeError is
    # unclassified, so it defaults to retryable=True with a non-empty cause.
    assert release.await_args.kwargs["retryable"] is True
    assert release.await_args.kwargs["last_error"]


@pytest.mark.asyncio
async def test_background_provision_superseded_writes_nothing():
    """A deliberate reprovision took over mid-run: the poll run must stop
    quietly — releasing to error here would clobber the superseding run."""
    from core.repositories.container_repo import ProvisionSuperseded
    from core.services.container_provision import background_provision

    manager = MagicMock()
    manager.provision_user_container = AsyncMock(side_effect=ProvisionSuperseded("taken over"))
    release = AsyncMock()
    with (
        patch(
            "core.services.container_provision.container_repo.try_claim_provision",
            new=AsyncMock(return_value="claim-123"),
        ),
        patch(
            "core.services.container_provision.container_repo.release_provision_to_error",
            new=release,
        ),
        patch("core.services.container_provision.get_ecs_manager", return_value=manager),
        patch("core.services.container_provision.billing_repo") as repo,
    ):
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await background_provision("user_1", "user_1")

    release.assert_not_awaited()


# ---------------------------------------------------------------------------
# background_provision — failure-cause classification. On the error transition
# the release records last_error (a short human-readable cause) + retryable, so
# the status endpoint can tell a transient failure (silently re-dispatched by
# the poll) from a deterministic one (surfaced to the owner instead of looped).


def _run_failing_provision(side_effect):
    """Drive background_provision to its error path with a given side effect on
    provision_user_container, returning the (patched) release mock so the test
    can assert the classified cause it was called with."""
    manager = MagicMock()
    manager.provision_user_container = AsyncMock(side_effect=side_effect)
    release = AsyncMock()
    ctx = (
        patch(
            "core.services.container_provision.container_repo.try_claim_provision",
            new=AsyncMock(return_value="claim-123"),
        ),
        patch(
            "core.services.container_provision.container_repo.release_provision_to_error",
            new=release,
        ),
        patch("core.services.container_provision.get_ecs_manager", return_value=manager),
        patch("core.services.container_provision.billing_repo"),
    )
    return release, ctx


@pytest.mark.asyncio
async def test_background_provision_deterministic_4xx_is_not_retryable():
    """A 4xx PaperclipApiError is deterministic -- re-running the identical
    provision fails identically -- so it records retryable=False + a cause,
    letting the status endpoint surface it instead of looping the poll."""
    from core.services.container_provision import background_provision
    from core.services.paperclip_admin_client import PaperclipApiError

    release, (c1, c2, c3, c4) = _run_failing_provision(PaperclipApiError("bad company slug", 400, {"error": "bad"}))
    with c1, c2, c3, c4 as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await background_provision("user_1", "user_1")

    assert release.await_args.args == ("user_1", "claim-123")
    assert release.await_args.kwargs["retryable"] is False
    assert release.await_args.kwargs["last_error"]  # non-empty cause string


@pytest.mark.asyncio
async def test_background_provision_transient_5xx_is_retryable():
    """A 5xx PaperclipApiError is transient -- retryable=True so the poll
    silently re-dispatches, no owner-facing error for a self-healing failure."""
    from core.services.container_provision import background_provision
    from core.services.paperclip_admin_client import PaperclipApiError

    release, (c1, c2, c3, c4) = _run_failing_provision(PaperclipApiError("paperclip unavailable", 503, {}))
    with c1, c2, c3, c4 as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await background_provision("user_1", "user_1")

    assert release.await_args.kwargs["retryable"] is True
    assert release.await_args.kwargs["last_error"]


@pytest.mark.asyncio
async def test_background_provision_ecs_infra_error_is_retryable():
    """EcsManagerError wraps AWS Spot-reclamation / throttle / capacity
    ClientErrors -- transient infra, retryable=True."""
    from core.containers.ecs_manager import EcsManagerError
    from core.services.container_provision import background_provision

    release, (c1, c2, c3, c4) = _run_failing_provision(
        EcsManagerError("Failed to create ECS service: ThrottlingException", "user_1")
    )
    with c1, c2, c3, c4 as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await background_provision("user_1", "user_1")

    assert release.await_args.kwargs["retryable"] is True
    assert release.await_args.kwargs["last_error"]


@pytest.mark.asyncio
async def test_background_provision_validation_error_is_not_retryable():
    """A ValueError is a deterministic config/shape bug -- retryable=False."""
    from core.services.container_provision import background_provision

    release, (c1, c2, c3, c4) = _run_failing_provision(ValueError("bad provider_choice"))
    with c1, c2, c3, c4 as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await background_provision("user_1", "user_1")

    assert release.await_args.kwargs["retryable"] is False
    assert release.await_args.kwargs["last_error"]


# ---------------------------------------------------------------------------
# background_provision — PROVISION_EXECUTOR flag routing


async def test_background_provision_sfn_flag_dispatches(monkeypatch):
    from core.services import container_provision

    monkeypatch.setattr(container_provision.settings, "PROVISION_EXECUTOR", "sfn")
    dispatched = AsyncMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatched)
    claimed = AsyncMock()
    monkeypatch.setattr(container_provision.container_repo, "try_claim_provision", claimed)
    await container_provision.background_provision("user_a", "user_a")
    dispatched.assert_awaited_once_with("user_a")
    claimed.assert_not_awaited()  # inline path never entered


async def test_background_provision_inline_flag_unchanged(monkeypatch):
    from core.services import container_provision

    monkeypatch.setattr(container_provision.settings, "PROVISION_EXECUTOR", "inline")
    dispatched = AsyncMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatched)
    monkeypatch.setattr(container_provision.container_repo, "try_claim_provision", AsyncMock(return_value=None))
    await container_provision.background_provision("user_a", "user_a")
    dispatched.assert_not_awaited()


class TestIsRetryableProvisionError:
    """Exit-code classifier for the one-shot provision runner."""

    def test_retryable_attr_true_wins(self):
        from core.services.container_provision import is_retryable_provision_error

        exc = Exception("anything")
        exc.retryable = True
        assert is_retryable_provision_error(exc) is True

    def test_retryable_attr_false_wins(self):
        from core.services.container_provision import is_retryable_provision_error

        # Even a message with no deterministic marker: self-classification rules.
        exc = Exception("ThrottlingException")
        exc.retryable = False
        assert is_retryable_provision_error(exc) is False

    def test_paperclip_429_is_retryable(self):
        from core.services.container_provision import is_retryable_provision_error
        from core.services.paperclip_admin_client import PaperclipApiError

        exc = PaperclipApiError("rate limited", status_code=429, body=None)
        assert is_retryable_provision_error(exc) is True

    def test_paperclip_422_is_deterministic(self):
        from core.services.container_provision import is_retryable_provision_error
        from core.services.paperclip_admin_client import PaperclipApiError

        exc = PaperclipApiError("unprocessable", status_code=422, body=None)
        assert is_retryable_provision_error(exc) is False

    def test_deterministic_marker_message(self):
        from core.services.container_provision import is_retryable_provision_error

        exc = Exception("byo_provider is required when provider_choice == 'byo_key'")
        assert is_retryable_provision_error(exc) is False

    def test_plain_throttle_defaults_retryable(self):
        from core.services.container_provision import is_retryable_provision_error

        exc = Exception("ECS ThrottlingException")
        assert is_retryable_provision_error(exc) is True
