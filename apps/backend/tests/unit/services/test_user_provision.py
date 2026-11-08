"""Unit tests for the unified ensure_user_ready helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from core.services import user_provision
from core.services.user_provision import ensure_user_ready


@pytest.fixture(autouse=True)
def _existing_billing_account():
    with patch.object(
        user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_create_trial_local_if_absent():
    """ensure_user_ready auto-defaults a genuinely-new owner into
    trial_local before the gate is evaluated (Task 3); stub it so
    pre-existing tests never touch real billing DDB. Tests that care
    about this call inspect/override the mock directly."""
    with patch(
        "core.services.user_provision.billing_repo.create_trial_local_if_absent",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _no_container_row():
    """``ensure_user_ready`` reads the containers row: only a RUNNING workspace gets the
    in-request owner seed reconcile. Default to NO row; the running tests override it."""
    with patch(
        "core.services.user_provision.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@pytest.fixture
def _container_running(_no_container_row):
    """Owner already has a RUNNING container: no executor run is dispatched, so
    the seed must be reconciled on this thread."""
    _no_container_row.return_value = {"status": "running", "service_name": "openclaw-x"}
    return _no_container_row


@pytest.fixture
def _stub_trial_default():
    """Keep billing initialization and member JOIN external to dispatch tests.

    The workspace trial tests below call the real initializer. Membership
    tests override the join stub with their own doubles.
    """
    with (
        patch(
            "core.services.user_provision.ensure_workspace_trial_default",
            new=AsyncMock(),
        ) as mock,
        patch.object(user_provision, "_ensure_workspace_member", AsyncMock()),
    ):
        yield mock


def _personal_auth(user_id: str = "user_123", email: str = "u@example.com") -> AuthContext:
    """Personal-context AuthContext (no org)."""
    return AuthContext(
        user_id=user_id,
        org_id=None,
        org_role=None,
        email=email,
    )


def _org_admin_auth(
    user_id: str = "user_123",
    org_id: str = "org_456",
    email: str = "u@example.com",
) -> AuthContext:
    """Org-admin AuthContext. is_org_context + is_org_admin are derived properties."""
    return AuthContext(
        user_id=user_id,
        org_id=org_id,
        org_role="org:admin",
        email=email,
    )


def _org_member_auth(
    user_id: str = "user_456",
    org_id: str = "org_456",
    email: str = "m@example.com",
) -> AuthContext:
    """Org-member AuthContext. is_org_admin returns False because role != org:admin."""
    return AuthContext(
        user_id=user_id,
        org_id=org_id,
        org_role="org:member",
        email=email,
    )


def _up_gate(code: str = "credits_required") -> MagicMock:
    gate = MagicMock()
    gate.code = code
    return gate


@pytest.mark.asyncio
async def test_ensure_user_ready_no_op_when_gate_blocks(_stub_trial_default):
    """If evaluate_provision_gate returns a Gate (not None), nothing
    is provisioned. Caller's pre-condition responsibility."""
    fake_gate = MagicMock()  # truthy non-None
    fake_gate.code = "subscription_required"
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=fake_gate),
        ),
        patch(
            "core.services.user_provision._ensure_owner_workspace",
            new=AsyncMock(),
        ) as mock_seed,
        patch(
            "core.services.user_provision.dispatch_provision",
            new=AsyncMock(),
        ) as mock_container,
    ):
        await ensure_user_ready(_org_admin_auth())
    mock_seed.assert_not_awaited()
    mock_container.assert_not_awaited()


# Workspace trial initialization.


@pytest.fixture
def _fresh_workspace_signup(monkeypatch):
    monkeypatch.setattr(user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [_org_admin_auth(), _org_member_auth()])
async def test_workspace_trial_is_unarmed_and_shared_by_all_members(
    auth, _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    _mock_create_trial_local_if_absent.return_value = True
    with patch.object(user_provision, "capture_product_event") as capture:
        await user_provision.ensure_workspace_trial_default(auth, "org_456")
    _mock_create_trial_local_if_absent.assert_awaited_once_with(owner_id="org_456", owner_type="org", trial_end=None)
    event = capture.call_args.kwargs
    # The PERSON is the human who hit the door; the workspace rides as a property.
    assert event["distinct_id"] == auth.user_id
    assert event["event"] == "trial_started"
    assert event["properties"] == {"owner_type": "org", "workspace_id": "org_456"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth,workspace_id,status",
    [
        (_personal_auth(), "user_123", 403),
        # A personal workspace id is refused even from an org-context caller:
        # this helper only ever CREATES, and no new personal workspace exists.
        (_org_admin_auth(), "user_123", 403),
        (_org_admin_auth(), "org_other", 403),
    ],
)
async def test_workspace_trial_rejects_missing_or_mismatched_org(
    auth, workspace_id, status, _mock_create_trial_local_if_absent
):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await user_provision.ensure_workspace_trial_default(auth, workspace_id)
    assert exc.value.status_code == status
    _mock_create_trial_local_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_org_returns_before_the_gate_and_creates_nothing(_stub_trial_default):
    """The workspace-first fence: a personal (no-org) caller leaves
    ``ensure_user_ready`` as its FIRST statement — before the JOIN, the gate and
    the seed — so no personal world can be built for anyone who signs up alone."""
    gate = AsyncMock(return_value=_up_gate())
    with (
        patch.object(user_provision, "evaluate_provision_gate", gate),
        patch.object(user_provision, "_reconcile_owner_seed", AsyncMock()) as seed,
    ):
        assert await ensure_user_ready(_personal_auth()) is None
    gate.assert_not_awaited()
    seed.assert_not_awaited()
    user_provision._ensure_workspace_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_trial_existing_row_and_lost_create_do_not_emit_started(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    _mock_create_trial_local_if_absent.return_value = False
    with patch.object(user_provision, "capture_product_event") as capture:
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
        with patch.object(
            user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
        ):
            await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
    _mock_create_trial_local_if_absent.assert_awaited_once()
    capture.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_trial_write_failure_is_reported_once_per_day(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    from core.observability import product_analytics

    _mock_create_trial_local_if_absent.side_effect = RuntimeError("ddb down")
    product_analytics._daily_dedup_seen.clear()
    try:
        with patch.object(product_analytics, "capture_product_event") as capture:
            for _ in range(3):
                with pytest.raises(RuntimeError, match="ddb down"):
                    await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
        assert [c.kwargs["event"] for c in capture.call_args_list] == ["trial_start_requested", "trial_start_failed"]
        assert capture.call_args.kwargs["properties"] == {"workspace_id": "org_456", "error_code": "RuntimeError"}
        assert all(c.kwargs["distinct_id"] == _org_admin_auth().user_id for c in capture.call_args_list)
    finally:
        product_analytics._daily_dedup_seen.clear()


@pytest.mark.asyncio
async def test_workspace_trial_started_event_dedups_across_members(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    _mock_create_trial_local_if_absent.return_value = True
    with patch.object(user_provision, "capture_product_event") as capture:
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
        await user_provision.ensure_workspace_trial_default(_org_member_auth(), "org_456")
    first, second = [c.kwargs for c in capture.call_args_list]
    assert first["properties"] == {"owner_type": "org", "workspace_id": "org_456"}
    # Two humans, one workspace: different persons, one deterministic uuid, so
    # PostHog keeps the earlier capture and the trial counts once.
    assert first["distinct_id"] == _org_admin_auth().user_id
    assert second["distinct_id"] == _org_member_auth().user_id
    assert first["event_uuid"] == second["event_uuid"]


@pytest.mark.asyncio
async def test_org_created_webhook_keys_the_trial_on_the_creator(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    """The organization.created path has no AuthContext; it carries Clerk's
    ``created_by`` so the trial still lands on the creator's timeline."""
    _mock_create_trial_local_if_absent.return_value = True
    with (
        patch.object(user_provision, "capture_product_event") as capture,
        patch.object(user_provision, "evaluate_provision_gate", AsyncMock(return_value=None)),
        patch.object(user_provision, "dispatch_provision", AsyncMock(return_value=True)),
    ):
        await user_provision.provision_new_workspace("org_456", created_by="user_creator")
    event = capture.call_args.kwargs
    assert event["event"] == "trial_started"
    assert event["distinct_id"] == "user_creator"
    assert event["properties"]["workspace_id"] == "org_456"


# ---------------------------------------------------------------------------
# Owner seed placement. The OWNER workspace seed normally runs in the
# executor's runner (scripts.provision_runner step 0) — that is the #1442 fix.
# The ONE case that stays here is a workspace that is already running, so no
# provision will run at all. ensure_user_ready never dispatches a provision.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"status": "error"}, {"status": "stopped"}, {"status": "provisioning"}])
async def test_ensure_user_ready_never_dispatches_and_leaves_a_non_running_seed_to_the_executor(
    _stub_trial_default, _no_container_row, row
):
    """The Teams/proxy doors are a lazy JOIN, never a provision: whatever the row says,
    nothing is dispatched, the member JOIN runs, and a workspace that is not running is
    the executor's to seed (a retryable error waits for the user's Retry)."""
    _no_container_row.return_value = row
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.dispatch_provision", new=AsyncMock()) as dispatch,
        patch("core.services.user_provision._ensure_workspace_for_org", new=AsyncMock()) as inline_org,
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    dispatch.assert_not_awaited()
    inline_org.assert_not_awaited()
    user_provision._ensure_workspace_member.assert_awaited_once_with(org_id="org_xyz", member_user_id="user_admin")


@pytest.mark.asyncio
async def test_gate_up_org_member_branch_is_unchanged():
    """The gate-up member branch (#1435) is orthogonal to where the owner seed
    runs: with the gate UP, an org member still gets the idempotent
    membership-add and no container is dispatched."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=_up_gate())),
        patch("core.services.user_provision._ensure_workspace_member", new=AsyncMock()) as member,
        patch("core.services.user_provision.dispatch_provision", new=AsyncMock()) as container,
    ):
        await ensure_user_ready(_org_member_auth("user_member", "org_456", "m@example.com"))
    member.assert_awaited_once_with(org_id="org_456", member_user_id="user_member")
    container.assert_not_awaited()


# ---------------------------------------------------------------------------
# Self-heal on a box that is already up: the two regressions this design
# exists to prevent. Both used to be served by an INLINE seed reached through
# ensure_user_ready(provision_container=False); with the seed in the executor
# and the executor refusing a running row, "dispatch and hope" seeds nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_container_workspace_owner_seed_is_reconciled(_stub_trial_default, _container_running):
    """A running workspace with no active row seeds on the poll thread;
    the executor cannot help because running rows are unclaimable."""
    inline_owner = AsyncMock()
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_workspace_for_org", new=inline_owner),
    ):
        await ensure_user_ready(_org_admin_auth("user_p", "org_p", "p@example.com"))
    inline_owner.assert_awaited_once_with(org_id="org_p", user_id="user_p", is_admin=True)


@pytest.mark.asyncio
async def test_running_container_seed_failure_is_swallowed_and_metered(_stub_trial_default, _container_running):
    """The reconcile is best-effort: a failure records workspace.seed.error and never escapes the poll."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision._ensure_workspace_for_org",
            new=AsyncMock(side_effect=RuntimeError("registry down")),
        ),
        patch("core.services.user_provision.put_metric") as mock_metric,
    ):
        await ensure_user_ready(_org_admin_auth("user_p", "org_p", "p@example.com"))
    mock_metric.assert_any_call("workspace.seed.error")


@pytest.mark.asyncio
async def test_the_trial_default_offers_an_armed_trial_row_to_the_30_day_heal():
    """The 2026-09 prod cohort expired under the 3-day window; every trial-default door
    offers the row to the 30-day heal."""
    row = {"subscription_status": "trial_local", "trial_end": 123, "created_at": "2026-09-12T01:26:00+00:00"}
    with (
        patch.object(user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(user_provision.billing_repo, "extend_short_trial", AsyncMock()) as extend,
    ):
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
    extend.assert_awaited_once_with("org_456", row)

    with (
        patch.object(
            user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
        ),
        patch.object(user_provision.billing_repo, "extend_short_trial", AsyncMock()) as extend,
    ):
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
    extend.assert_not_awaited()


# ----------------------------------------------------------------------
# provision_new_workspace (Clerk organization.created)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_new_workspace_creates_the_trial_row_and_dispatches(monkeypatch):
    trial = AsyncMock()
    gate = AsyncMock(return_value=None)
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(user_provision, "_ensure_trial_row", trial)
    monkeypatch.setattr(user_provision, "evaluate_provision_gate", gate)
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("org_new", created_by="user_creator")
    trial.assert_awaited_once_with("org_new", actor_user_id="user_creator")
    gate.assert_awaited_once_with(owner_id="org_new", is_admin=True, actor_user_id="user_creator")
    dispatch.assert_awaited_once_with("org_new")


@pytest.mark.asyncio
async def test_provision_new_workspace_stops_at_a_gate(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(user_provision, "_ensure_trial_row", AsyncMock())
    monkeypatch.setattr(user_provision, "evaluate_provision_gate", AsyncMock(return_value=_up_gate()))
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("org_new")
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trial", "dispatched"),
    [(AsyncMock(side_effect=RuntimeError("ddb")), None), (AsyncMock(), False)],
)
async def test_provision_new_workspace_raises_so_svix_redelivers(monkeypatch, trial, dispatched):
    """The webhook is the ONLY thing that starts a first provision now (no status poll re-drives
    it), so a trial-row failure or a dispatch that did not start RAISES -- and is metered."""
    metrics = []
    monkeypatch.setattr(user_provision, "_ensure_trial_row", trial)
    monkeypatch.setattr(user_provision, "evaluate_provision_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(user_provision, "dispatch_provision", AsyncMock(return_value=dispatched))
    monkeypatch.setattr(user_provision, "put_metric", lambda name, **kw: metrics.append(name))
    with pytest.raises(RuntimeError):
        await user_provision.provision_new_workspace("org_new")
    assert metrics == ["provision.org_created.error"]


@pytest.mark.asyncio
async def test_provision_new_workspace_ignores_a_non_org_id(monkeypatch):
    trial = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(user_provision, "_ensure_trial_row", trial)
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("user_alone")
    trial.assert_not_awaited()
    dispatch.assert_not_awaited()
