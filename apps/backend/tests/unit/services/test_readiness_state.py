"""core.services.readiness_state.compute_status — the sole readiness+
onboarding composer (spec PR-5, COMPOSER; INV-1).

Every lifecycle scenario the COMPOSER task lists (blocked,
provider_selection_required, cold-start [provisioning/starting], error,
parked, ready) plus INV-6 (onboarding status never gates chat) is pinned
here. `team_pending` is deliberately NOT a `compute_status` scenario — it's
a CALLER-ROLE fact (org member vs admin polling about the SAME owner),
which `compute_status(owner_id)` cannot see from `owner_id` alone; it's
covered at the router layer (test_container_status_role_blind_dispatch.py
et al.) and the shared TS `deriveUIPhase` (statusContract.test.ts).

The `TestTeamsState` class ports the four `_resolve_teams_status` unit
tests that lived in tests/unit/routers/test_container_status.py before
this PR moved that logic into the composer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import readiness_state
from core.services.messaging import ApiProblem


def _account(**overrides) -> dict:
    base = {"subscription_status": "active", "provider_choice": "bedrock_claude"}
    base.update(overrides)
    return base


async def _compute(
    owner_id: str,
    *,
    container: dict | None,
    account: dict | None,
    gateway_connected: bool = False,
    paperclip_repo: MagicMock | None = None,
    parked: bool = False,
) -> dict:
    if paperclip_repo is None:
        paperclip_repo = MagicMock()
        paperclip_repo.get_onboarding_state = AsyncMock(return_value=None)
        # get_onboarding_state's real implementation never returns None —
        # it returns the all-defaults dict. Callers below that don't care
        # about paperclip state pass a repo whose method returns exactly
        # that shape.
        paperclip_repo.get_onboarding_state = AsyncMock(
            return_value={
                "onboarding_generation": None,
                "funnel_state": "not_started",
                "funnel_submitted_at": None,
                "funnel_completed_at": None,
                "onboarding_goal_id": None,
                "onboarding_project_id": None,
                "onboarding_submitted_by": None,
                "bootstrap_state": "not_started",
                "bootstrap_dispatch_id": None,
                "bootstrap_dispatched_at": None,
                "bootstrap_delivered_at": None,
                "bootstrap_attempts": 0,
                "bootstrap_last_error": None,
                "teams_state": None,
                "company_id": None,
                "user_id": None,
            }
        )

    async def _health(*_a, **_kw):
        if not gateway_connected:
            raise ApiProblem("RUNTIME_UNAVAILABLE", "x", retryable=True)
        return {"ok": True}

    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(return_value=account)),
        patch("core.services.container_rpc.call", new=AsyncMock(side_effect=_health)),
        patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=paperclip_repo),
        patch("core.services.wake_policy.is_parked_trial", new=AsyncMock(return_value=parked)),
    ):
        return await readiness_state.compute_status(owner_id, container=container)


class TestResolveRuntimePhase:
    """Pure container-status -> runtime_phase mapping — the ONLY input to chat
    availability (INV-6)."""

    @pytest.mark.parametrize("status", ["provisioning", "stopped", "error", None, ""])
    def test_anything_not_running_is_provisioning(self, status):
        container = {"status": status} if status is not None else {}
        assert readiness_state.resolve_runtime_phase(container) == "provisioning"

    def test_a_running_row_is_ready_without_a_health_probe(self):
        """AWS parks an idle microVM invisibly and wakes it on invoke, so a probe would
        answer "asleep", not "not ready" -- and probing would wake the very box the poll
        exists to leave parked. There is no "starting" phase left to reach."""
        assert readiness_state.resolve_runtime_phase({"status": "running"}) == "ready"

    def test_none_container_is_provisioning(self):
        assert readiness_state.resolve_runtime_phase(None) == "provisioning"


class TestComputeStatusLifecycleScenarios:
    @pytest.mark.asyncio
    async def test_needs_signup_personal_no_billing_row(self):
        contract = await _compute("user_x", container=None, account=None)
        assert contract["lifecycle"] == "needs_signup"
        assert contract["runtime_phase"] == "provisioning"
        assert contract["chat_available"] is False

    @pytest.mark.asyncio
    async def test_blocked_org_no_billing_row(self):
        contract = await _compute("org_xyz", container=None, account=None)
        assert contract["lifecycle"] == "blocked"
        assert contract["chat_available"] is False

    @pytest.mark.asyncio
    async def test_provider_selection_required_org_active_no_provider(self):
        """Orgs have NO free tier — an org with an active workspace
        subscription but no provider_choice sits here, distinct from
        personal's trial_local path."""
        account = _account(subscription_status="active", provider_choice=None)
        contract = await _compute("org_xyz", container=None, account=account)
        assert contract["lifecycle"] == "provider_selection_required"
        assert contract["chat_available"] is False

    @pytest.mark.asyncio
    async def test_no_provider_selection_required_for_trial_local_personal(self):
        """trial_local is personal-only and never needs a provider choice —
        the free tier's provider is a config-path mode, not a stored
        provider_choice."""
        account = _account(subscription_status="trial_local", provider_choice=None)
        contract = await _compute("user_x", container=None, account=account)
        assert contract["lifecycle"] != "provider_selection_required"

    @pytest.mark.asyncio
    async def test_cold_start_provisioning(self):
        container = {"status": "provisioning", "substatus": None}
        contract = await _compute("user_x", container=container, account=_account())
        assert contract["lifecycle"] == "provisioning"
        assert contract["runtime_phase"] == "provisioning"
        assert contract["chat_available"] is False

    # NOTE: `test_cold_start_starting` was deleted -- it asserted `runtime_phase == "starting"`
    # for a running-but-not-gateway-connected row, which `resolve_runtime_phase` can no
    # longer produce (see the note above `test_running_with_gateway_liveness_is_ready`).

    @pytest.mark.asyncio
    async def test_error(self):
        container = {"status": "error", "substatus": "provision_failed", "last_error": "boom"}
        contract = await _compute("user_x", container=container, account=_account())
        assert contract["lifecycle"] == "error"
        assert contract["runtime_phase"] == "provisioning"
        assert contract["chat_available"] is False
        assert contract["last_error"] == "boom"

    @pytest.mark.asyncio
    async def test_parked(self):
        container = {"status": "stopped", "stop_reason": "trial_idle"}
        contract = await _compute(
            "user_x", container=container, account=_account(subscription_status="trial_local"), parked=True
        )
        assert contract["lifecycle"] == "parked"
        assert contract["parked"] is True
        assert contract["runtime_phase"] == "provisioning"
        assert contract["chat_available"] is False

    @pytest.mark.asyncio
    async def test_ready(self):
        container = {"status": "running", "substatus": None}
        contract = await _compute("user_x", container=container, account=_account(), gateway_connected=True)
        assert contract["lifecycle"] == "ready"
        assert contract["runtime_phase"] == "ready"
        assert contract["chat_available"] is True

    @pytest.mark.asyncio
    async def test_compute_status_never_probes_the_runtime(self):
        """No readiness path may RPC the runtime: an invoke is what wakes a parked
        microVM, and this poll runs every ~3s. (onboarding={} passed explicitly to skip
        the paperclip read entirely -- irrelevant to this scenario.)"""
        mock_call = AsyncMock(return_value={"ok": True})
        with (
            patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(return_value=_account())),
            patch("core.services.container_rpc.call", mock_call),
            patch("core.services.wake_policy.is_parked_trial", new=AsyncMock(return_value=False)),
        ):
            stopped = await readiness_state.compute_status("user_x", container={"status": "stopped"}, onboarding={})
            running = await readiness_state.compute_status("user_x", container={"status": "running"}, onboarding={})
        assert stopped["runtime_phase"] == "provisioning"
        assert running["runtime_phase"] == "ready"
        mock_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_parked_row_never_awaits_the_gateway_probe(self):
        """A parked row short-circuits to lifecycle="parked" before the
        gateway-liveness branch is ever reached."""
        container = {"status": "stopped", "stop_reason": "trial_idle"}
        mock_call = AsyncMock(return_value={"ok": True})
        with (
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value=_account(subscription_status="trial_local")),
            ),
            patch("core.services.container_rpc.call", mock_call),
            patch("core.services.wake_policy.is_parked_trial", new=AsyncMock(return_value=True)),
        ):
            contract = await readiness_state.compute_status("user_x", container=container, onboarding={})
        assert contract["lifecycle"] == "parked"
        mock_call.assert_not_awaited()


class TestInvariant6ChatAvailabilitySolelyRuntimePhase:
    """INV-6: onboarding status never gates chat — a degraded/awaiting-brief
    owner whose runtime is ready still gets chat_available=True."""

    @pytest.mark.asyncio
    async def test_failed_teams_seed_over_ready_runtime_is_still_chat_available(self):
        repo = MagicMock()
        repo.get_onboarding_state = AsyncMock(
            return_value={
                "onboarding_generation": None,
                "funnel_state": "not_started",
                "funnel_submitted_at": None,
                "funnel_completed_at": None,
                "onboarding_goal_id": None,
                "onboarding_project_id": None,
                "onboarding_submitted_by": None,
                "bootstrap_state": "not_started",
                "bootstrap_dispatch_id": None,
                "bootstrap_dispatched_at": None,
                "bootstrap_delivered_at": None,
                "bootstrap_attempts": 0,
                "bootstrap_last_error": None,
                "teams_state": "failed",
                "company_id": "co_1",
                "user_id": "creator_1",
            }
        )
        container = {"status": "running", "substatus": None}
        contract = await _compute(
            "user_x", container=container, account=_account(), gateway_connected=True, paperclip_repo=repo
        )
        assert contract["teams_state"] == "failed"
        assert contract["lifecycle"] == "ready"
        assert contract["runtime_phase"] == "ready"
        assert contract["chat_available"] is True

    @pytest.mark.asyncio
    async def test_not_yet_submitted_funnel_over_ready_runtime_is_still_chat_available(self):
        container = {"status": "running", "substatus": None}
        contract = await _compute("user_x", container=container, account=_account(), gateway_connected=True)
        assert contract["funnel"]["state"] == "not_started"
        assert contract["chat_available"] is True

    @pytest.mark.asyncio
    async def test_pending_brief_reads_as_a_submitted_funnel(self):
        """A brief the user submitted before their workspace could take it is
        "submitted" as far as the CLIENT is concerned — they answered, so the
        funnel must stop being shown to them. The row itself is still
        not_started (no claim, no goal, no project); combining the two facts
        into one verdict is this composer's job (INV-1)."""
        repo = MagicMock()
        repo.get_onboarding_state = AsyncMock(return_value={"funnel_state": "not_started", "pending_brief": True})
        contract = await _compute("user_x", container={"status": "running"}, account=_account(), paperclip_repo=repo)
        assert contract["funnel"]["state"] == "submitted"

    @pytest.mark.asyncio
    async def test_pending_brief_never_overrides_a_real_funnel_state(self):
        """A stale pending item left over from a drain that already landed
        must not drag a completed funnel backwards."""
        repo = MagicMock()
        repo.get_onboarding_state = AsyncMock(
            return_value={
                "funnel_state": "complete",
                "pending_brief": True,
                "onboarding_generation": "01GEN",
            }
        )
        contract = await _compute("user_x", container={"status": "running"}, account=_account(), paperclip_repo=repo)
        assert contract["funnel"]["state"] == "complete"
        assert contract["onboarded"] is True

    @pytest.mark.asyncio
    async def test_paperclip_read_failure_never_breaks_status(self):
        repo = MagicMock()
        repo.get_onboarding_state = AsyncMock(side_effect=RuntimeError("ddb down"))
        container = {"status": "running", "substatus": None}
        contract = await _compute(
            "user_x", container=container, account=_account(), gateway_connected=True, paperclip_repo=repo
        )
        assert contract["chat_available"] is True
        assert contract["teams_state"] is None

    @pytest.mark.asyncio
    async def test_billing_read_failure_never_breaks_status(self):
        container = {"status": "running", "substatus": None}
        paperclip_repo = MagicMock()
        paperclip_repo.get_onboarding_state = AsyncMock(
            return_value={
                "onboarding_generation": None,
                "funnel_state": "not_started",
                "funnel_submitted_at": None,
                "funnel_completed_at": None,
                "onboarding_goal_id": None,
                "onboarding_project_id": None,
                "onboarding_submitted_by": None,
                "bootstrap_state": "not_started",
                "bootstrap_dispatch_id": None,
                "bootstrap_dispatched_at": None,
                "bootstrap_delivered_at": None,
                "bootstrap_attempts": 0,
                "bootstrap_last_error": None,
                "teams_state": None,
                "company_id": None,
                "user_id": None,
            }
        )
        with (
            patch(
                "core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(side_effect=RuntimeError("ddb down"))
            ),
            patch("core.services.container_rpc.call", new=AsyncMock(return_value={"ok": True})),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=paperclip_repo),
            patch("core.services.wake_policy.is_parked_trial", new=AsyncMock(return_value=False)),
        ):
            contract = await readiness_state.compute_status("user_x", container=container)
        assert contract["chat_available"] is True


class TestTeamsState:
    """Ported from tests/unit/routers/test_container_status.py's
    TestContainerStatusTeamsStatus (pre-COMPOSER _resolve_teams_status
    direct unit tests) — the read now lives in
    paperclip_repo.get_onboarding_state, composed by compute_status."""

    @pytest.mark.asyncio
    async def test_failed_row_surfaces_failed(self):
        repo = MagicMock(get_workspace=AsyncMock(return_value=SimpleNamespace(status="failed")))
        contract = await _compute(
            "user_x",
            container={"status": "running"},
            account=_account(),
            gateway_connected=True,
            paperclip_repo=_repo_from_get(repo),
        )
        assert contract["teams_state"] == "failed"

    @pytest.mark.asyncio
    async def test_active_row_surfaces_active_not_none(self):
        """Unlike the pre-COMPOSER wire contract (which omitted the field
        for anything but "failed"), the COMPOSER's own teams_state carries
        the raw status — routers/container.py's _status_response is what
        re-applies the omit-unless-failed wire contract."""
        repo = MagicMock(get_workspace=AsyncMock(return_value=SimpleNamespace(status="active")))
        contract = await _compute(
            "user_x",
            container={"status": "running"},
            account=_account(),
            gateway_connected=True,
            paperclip_repo=_repo_from_get(repo),
        )
        assert contract["teams_state"] == "active"

    @pytest.mark.asyncio
    async def test_no_row_surfaces_none(self):
        repo = MagicMock(get_workspace=AsyncMock(return_value=None))
        contract = await _compute(
            "user_x",
            container={"status": "running"},
            account=_account(),
            gateway_connected=True,
            paperclip_repo=_repo_from_get(repo),
        )
        assert contract["teams_state"] is None

    @pytest.mark.asyncio
    async def test_read_error_swallowed_surfaces_none(self):
        repo = MagicMock(get_workspace=AsyncMock(side_effect=RuntimeError("ddb down")))
        contract = await _compute(
            "user_x",
            container={"status": "running"},
            account=_account(),
            gateway_connected=True,
            paperclip_repo=_repo_from_get(repo),
        )
        assert contract["teams_state"] is None


def _repo_from_get(get_mock_repo: MagicMock) -> MagicMock:
    """Wrap a bare `.get_workspace`-configured repo double into one whose
    `get_onboarding_state` calls through to the REAL implementation logic
    (resolve_owner_row + getattr-defensive projection) against whatever
    `.get_workspace` returns. The pending-brief lookup is stubbed so no test
    reaches DynamoDB."""
    from core.repositories.workspace_repo import WorkspaceRepo

    real = WorkspaceRepo()
    real.get_workspace = get_mock_repo.get_workspace
    real._has_pending_brief = AsyncMock(return_value=False)
    return real


class TestResolveOnboardingStateThreading:
    """resolve_onboarding_state (public) + compute_status's onboarding=
    kwarg -- the P2 fix that lets a caller (routers/container.py, threading
    into maybe_dispatch_bootstrap too) fetch the paperclip-companies row
    ONCE and share it, instead of compute_status re-fetching internally."""

    @pytest.mark.asyncio
    async def test_compute_status_does_not_refetch_when_onboarding_given(self):
        paperclip_repo = MagicMock()
        paperclip_repo.get_onboarding_state = AsyncMock(
            side_effect=AssertionError("compute_status must not re-fetch onboarding when given")
        )
        container = {"status": "running", "substatus": None}
        onboarding = {
            "onboarding_generation": "01G",
            "funnel_state": "submitted",
            "funnel_submitted_at": None,
            "funnel_completed_at": None,
            "onboarding_goal_id": None,
            "onboarding_project_id": None,
            "onboarding_submitted_by": "member_1",
            "bootstrap_state": "not_started",
            "bootstrap_dispatch_id": None,
            "bootstrap_dispatched_at": None,
            "bootstrap_delivered_at": None,
            "bootstrap_attempts": 0,
            "bootstrap_last_error": None,
            "teams_state": "active",
            "company_id": "co_1",
            "user_id": "creator_1",
        }
        with (
            patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(return_value=_account())),
            patch("core.services.container_rpc.call", new=AsyncMock(return_value={"ok": True})),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=paperclip_repo),
            patch("core.services.wake_policy.is_parked_trial", new=AsyncMock(return_value=False)),
        ):
            contract = await readiness_state.compute_status("user_x", container=container, onboarding=onboarding)
        paperclip_repo.get_onboarding_state.assert_not_awaited()
        assert contract["generations"]["onboarding_generation"] == "01G"
        assert contract["funnel"]["state"] == "submitted"
        assert contract["teams_state"] == "active"

    @pytest.mark.asyncio
    async def test_compute_status_fetches_onboarding_itself_when_omitted(self):
        """The default (omitted) posture is unchanged -- this is the
        existing behavior every other test in this file already relies on,
        pinned explicitly here alongside the given-onboarding case above."""
        paperclip_repo = MagicMock()
        paperclip_repo.get_onboarding_state = AsyncMock(
            return_value={
                "onboarding_generation": "01G",
                "funnel_state": "submitted",
                "funnel_submitted_at": None,
                "funnel_completed_at": None,
                "onboarding_goal_id": None,
                "onboarding_project_id": None,
                "onboarding_submitted_by": None,
                "bootstrap_state": "not_started",
                "bootstrap_dispatch_id": None,
                "bootstrap_dispatched_at": None,
                "bootstrap_delivered_at": None,
                "bootstrap_attempts": 0,
                "bootstrap_last_error": None,
                "teams_state": None,
                "company_id": None,
                "user_id": "creator_1",
            }
        )
        container = {"status": "running", "substatus": None}
        with (
            patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(return_value=_account())),
            patch("core.services.container_rpc.call", new=AsyncMock(return_value={"ok": True})),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=paperclip_repo),
            patch("core.services.wake_policy.is_parked_trial", new=AsyncMock(return_value=False)),
        ):
            contract = await readiness_state.compute_status("user_x", container=container)
        paperclip_repo.get_onboarding_state.assert_awaited_once_with("user_x")
        assert contract["generations"]["onboarding_generation"] == "01G"

    @pytest.mark.asyncio
    async def test_resolve_onboarding_state_delegates_to_the_repo(self):
        paperclip_repo = MagicMock()
        paperclip_repo.get_onboarding_state = AsyncMock(return_value={"onboarding_generation": "01G"})
        with patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=paperclip_repo):
            result = await readiness_state.resolve_onboarding_state("user_x")
        assert result == {"onboarding_generation": "01G"}
        paperclip_repo.get_onboarding_state.assert_awaited_once_with("user_x")

    @pytest.mark.asyncio
    async def test_resolve_onboarding_state_degrades_to_defaults_on_read_failure(self):
        """A read failure here must never raise -- mirrors compute_status's
        own degrade-to-defaults posture for every other input."""
        paperclip_repo = MagicMock()
        paperclip_repo.get_onboarding_state = AsyncMock(side_effect=RuntimeError("ddb down"))
        with patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=paperclip_repo):
            result = await readiness_state.resolve_onboarding_state("user_x")
        assert result["onboarding_generation"] is None
        assert result["funnel_state"] == "not_started"
        assert result["user_id"] is None
        assert result["onboarding_submitted_by"] is None

    # The keys `get_onboarding_state` returns for a
    # genuinely-not-started row -- an explicit literal, independent of
    # BOTH `ONBOARDING_STATE_DEFAULTS` and `resolve_onboarding_state`'s
    # implementation, so drift on either side fails loudly instead of a
    # tautological self-comparison. This is the regression guard for the
    # exact bug this task fixed: first_contact_state, first_contact_attempts,
    # first_contact_reason, and friday_agent_id silently dropped out of
    # readiness_state's hand-copy (commit 6b42f427e / #1820 paid this once
    # already).
    _EXPECTED_ONBOARDING_STATE_KEYS = frozenset(
        {
            "onboarding_generation",
            "funnel_state",
            "pending_brief",
            "funnel_submitted_at",
            "funnel_completed_at",
            "onboarding_goal_id",
            "onboarding_project_id",
            "onboarding_brief_text",
            "onboarding_submitted_by",
            "first_contact_state",
            "first_contact_attempts",
            "first_contact_reason",
            "friday_agent_id",
            "teams_state",
            "company_id",
            "user_id",
        }
    )

    def test_onboarding_state_defaults_have_the_expected_keys(self):
        """paperclip_repo.ONBOARDING_STATE_DEFAULTS must carry exactly the
        literal key set above -- fails if a key is added or removed there
        without updating this literal (or vice versa)."""
        from core.repositories import workspace_repo as paperclip_repo_module

        assert set(paperclip_repo_module.ONBOARDING_STATE_DEFAULTS.keys()) == self._EXPECTED_ONBOARDING_STATE_KEYS

    @pytest.mark.asyncio
    async def test_readiness_onboarding_keys_match_repo_defaults(self):
        """readiness_state's degrade-to-defaults dict (the except branch of
        resolve_onboarding_state) must carry the same literal 25-key set --
        a future hand-copy reappearing here (the exact failure mode this
        task fixed) fails against the literal, not against the thing being
        hand-copied."""
        paperclip_repo = MagicMock()
        paperclip_repo.get_onboarding_state = AsyncMock(side_effect=RuntimeError("ddb down"))
        with patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=paperclip_repo):
            result = await readiness_state.resolve_onboarding_state("user_x")
        assert set(result.keys()) == self._EXPECTED_ONBOARDING_STATE_KEYS

        # Cheap and documents intent, but the literal assertion above is the
        # one that actually catches drift -- this alone is tautological
        # (it compares resolve_onboarding_state's output to the exact
        # object it's a shallow copy of).
        from core.repositories import workspace_repo as paperclip_repo_module

        assert set(result.keys()) == set(paperclip_repo_module.ONBOARDING_STATE_DEFAULTS.keys())


class TestGenerationThreading:
    @pytest.mark.asyncio
    async def test_provision_generation_flows_from_container_row(self):
        container = {"status": "running", "provision_generation": "01J_PROVISION_GEN"}
        contract = await _compute("user_x", container=container, account=_account(), gateway_connected=True)
        assert contract["generations"]["provision_generation"] == "01J_PROVISION_GEN"

    @pytest.mark.asyncio
    async def test_onboarding_generation_flows_from_paperclip_row(self):
        repo = MagicMock()
        repo.get_onboarding_state = AsyncMock(
            return_value={
                "onboarding_generation": "01J_ONBOARDING_GEN",
                "funnel_state": "complete",
                "funnel_submitted_at": "2026-07-01T00:00:00Z",
                "funnel_completed_at": "2026-07-01T00:01:00Z",
                "onboarding_goal_id": "goal_1",
                "onboarding_project_id": "proj_1",
                "onboarding_submitted_by": "member_1",
                "bootstrap_state": "delivered",
                "bootstrap_dispatch_id": "disp_1",
                "bootstrap_dispatched_at": "2026-07-01T00:01:01Z",
                "bootstrap_delivered_at": "2026-07-01T00:01:30Z",
                "bootstrap_attempts": 1,
                "bootstrap_last_error": None,
                "teams_state": "active",
                "company_id": "co_1",
                "user_id": "creator_1",
            }
        )
        container = {"status": "running"}
        contract = await _compute(
            "user_x", container=container, account=_account(), gateway_connected=True, paperclip_repo=repo
        )
        assert contract["generations"]["onboarding_generation"] == "01J_ONBOARDING_GEN"
        assert contract["funnel"]["state"] == "complete"
        # RECONCILE-WITH-ONBOARD: once a real onboarding_generation exists,
        # `onboarded` follows funnel_state == "complete", not the legacy
        # per-container onboarded_at stamp.
        assert contract["onboarded"] is True

    @pytest.mark.asyncio
    async def test_onboarded_falls_back_to_legacy_onboarded_at_when_no_onboarding_generation_yet(self):
        """Transitional shim: until ONBOARD's writers ship,
        onboarding_generation is always None on a fresh branch, so
        `onboarded` must keep reading the legacy per-container
        `onboarded_at` stamp rather than going permanently False."""
        container = {"status": "running", "onboarded_at": "2026-07-01T00:00:00Z"}
        contract = await _compute("user_x", container=container, account=_account(), gateway_connected=True)
        assert contract["onboarded"] is True

        container_fresh = {"status": "running"}
        contract2 = await _compute("user_x", container=container_fresh, account=_account(), gateway_connected=True)
        assert contract2["onboarded"] is False
