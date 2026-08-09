"""Unit tests for the Admin Account-Wipe orchestrator
(core.services.account_wipe) — Admin Account-Wipe Task 3.

Every collaborator (each per-domain delete method, plus the two
compound httpx-wrapped helpers) is patched at the point of use inside
core.services.account_wipe, mirroring tests/unit/services/test_user_provision.py's
style: a `with (patch(...), patch(...), ...):` block per test (or via the
`wipe_mocks` fixture below for the common all-succeeds case), AsyncMock for
async collaborators, MagicMock for sync ones / return-value stand-ins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from core.billing.bedrock_gate_tokens import delete_gate_token_rows as real_delete_gate_token_rows
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services.account_wipe import (
    BootstrapAccountProtectedError,
    OrgWipeUnsupportedError,
    WipeReport,
    WipeStep,
    preview_wipe,
    resolve_account_emails,
    wipe_account,
)
from core.services.paperclip_user_delete import BootstrapUserProtectedError

OWNER_ID = "user_123"

# The canonical order wipe_account must run its steps in (design spec
# "complete teardown map" §steps 1-12; see account_wipe.py's module
# docstring for the spec-row -> system-name mapping).
EXPECTED_ORDER = [
    "paperclip_company",
    "paperclip_aurora_user",
    # The gate-token rows go BEFORE the ecs_service step, which destroys the
    # containers row that holds the only pointers to them.
    "bedrock_gate_tokens",
    "ecs_service",
    "efs_workspace_dir",
    # O6: replica-prefix delete rides right after the EFS wipe — leaving the
    # S3 replicas behind resurrects pre-wipe state (incl. auth rows) on the
    # owner's next provision. No-op ("ok") while LITESTREAM_BUCKET is unset.
    "litestream_replicas",
    "bedrock_aips",
    "bedrock_role",
    "metronome",
    "stripe",
    "usage_counters",
    "paperclip_row",
    "billing_row",
    "user_row",
    "paperclip_agent_keys",
    "clerk_user",
]


def _container_row(**overrides) -> dict:
    defaults = {
        "owner_id": OWNER_ID,
        "service_name": f"openclaw-{OWNER_ID}-abc123",
        "access_point_id": "fsap-0123456789abcdef0",
        "bedrock_aip_arns": {"sonnet": "arn:aws:bedrock:us-east-1:877352799272:inference-profile/sonnet-aip"},
        "bedrock_role_arn": f"arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-{OWNER_ID}",
        "bedrock_gate_token_hash": "hash-current",
        "bedrock_gate_superseded_hashes": ["hash-old-1", "hash-old-2"],
    }
    defaults.update(overrides)
    return defaults


def _paperclip_company(**overrides) -> PaperclipCompany:
    now = datetime.now(timezone.utc)
    defaults = dict(
        user_id=OWNER_ID,
        org_id="org_shared",
        company_id="company_abc",
        paperclip_user_id="pc_user_abc",
        paperclip_password_encrypted="enc-pw",
        service_token_encrypted="enc-token",
        status="active",
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return PaperclipCompany(**defaults)


def _billing_row(**overrides) -> dict:
    defaults = {
        "owner_id": OWNER_ID,
        "stripe_customer_id": "cus_abc123",
        "stripe_subscription_id": "sub_abc123",
        "metronome_customer_id": "met_abc123",
    }
    defaults.update(overrides)
    return defaults


def _user_row(**overrides) -> dict:
    defaults = {"user_id": OWNER_ID, "email": "owner@example.com"}
    defaults.update(overrides)
    return defaults


@pytest.fixture
def wipe_mocks():
    """Patch every account_wipe collaborator to a default success outcome.

    Yields a SimpleNamespace of the mocks so individual tests can override
    `.return_value` / `.side_effect` before calling wipe_account/preview_wipe,
    and can assert call counts/args afterward.
    """
    user_get = AsyncMock(return_value=_user_row())
    user_delete = AsyncMock(return_value=None)

    # resolve_account_emails calls clerk_admin.get_user first. Default it to
    # None (the CLERK_SECRET_KEY-unset / local-dev posture) so email resolution
    # falls back to the users-row email -- a non-bootstrap address, so the
    # up-front bootstrap guard is a no-op for every existing case.
    clerk_get_user = AsyncMock(return_value=None)

    container_get = AsyncMock(return_value=_container_row())

    paperclip_get = AsyncMock(return_value=_paperclip_company())
    paperclip_row_delete = AsyncMock(return_value=None)
    pending_brief_get = AsyncMock(return_value=None)
    pending_brief_delete = AsyncMock(return_value=None)
    paperclip_repo_instance = MagicMock()
    paperclip_repo_instance.get = paperclip_get
    paperclip_repo_instance.delete = paperclip_row_delete
    paperclip_repo_instance.get_pending_brief = pending_brief_get
    paperclip_repo_instance.delete_pending_brief = pending_brief_delete
    paperclip_repo_cls = MagicMock(return_value=paperclip_repo_instance)

    billing_get = AsyncMock(return_value=_billing_row())
    billing_delete = AsyncMock(return_value=None)

    paperclip_company_delete = AsyncMock(return_value=None)
    paperclip_user_delete_fn = AsyncMock(return_value=None)

    ecs_manager_stub = MagicMock()
    ecs_manager_stub.delete_user_service = AsyncMock(return_value=None)
    # The replica-delete step drains first — delete_user_service only SUBMITS
    # the teardown, so an in-flight litestream upload could otherwise land
    # after the prefix delete and be restored on the next provision.
    ecs_manager_stub.wait_until_drained = AsyncMock(return_value=True)
    get_ecs_manager_fn = MagicMock(return_value=ecs_manager_stub)

    workspace_stub = MagicMock()
    workspace_stub.delete_user_dir = MagicMock(return_value=None)
    workspace_stub.user_path.return_value.exists.return_value = True
    get_workspace_fn = MagicMock(return_value=workspace_stub)

    bedrock_aips_delete = AsyncMock(
        return_value=["arn:aws:bedrock:us-east-1:877352799272:inference-profile/sonnet-aip"]
    )
    bedrock_role_delete = AsyncMock(return_value=None)
    metronome_archive = AsyncMock(return_value=None)
    stripe_delete = AsyncMock(return_value=True)
    usage_delete = AsyncMock(return_value=3)
    clerk_delete = AsyncMock(return_value=None)
    agent_keys_list = AsyncMock(return_value=["isol8/dev/paperclip-agent-keys/user_abc123/agent-1"])
    agent_keys_delete = AsyncMock(return_value=1)
    # Real boto3 underneath — the moto-backed tests at the bottom of this file
    # swap the real implementation back in via side_effect.
    gate_tokens_delete = AsyncMock(return_value=3)

    with (
        patch("core.services.account_wipe.user_repo.get", new=user_get),
        patch("core.services.account_wipe.user_repo.delete", new=user_delete),
        patch("core.services.account_wipe.clerk_admin.get_user", new=clerk_get_user),
        patch("core.services.account_wipe.container_repo.get_by_owner_id", new=container_get),
        patch("core.services.account_wipe.PaperclipRepo", new=paperclip_repo_cls),
        patch("core.services.account_wipe.billing_repo.get_by_owner_id", new=billing_get),
        patch("core.services.account_wipe.billing_repo.delete", new=billing_delete),
        patch("core.services.account_wipe._delete_paperclip_company_call", new=paperclip_company_delete),
        patch("core.services.account_wipe.delete_paperclip_user", new=paperclip_user_delete_fn),
        patch("core.services.account_wipe.get_ecs_manager", new=get_ecs_manager_fn),
        patch("core.services.account_wipe.get_workspace", new=get_workspace_fn),
        patch("core.services.account_wipe.delete_owner_aips", new=bedrock_aips_delete),
        patch("core.services.account_wipe.delete_owner_bedrock_role", new=bedrock_role_delete),
        patch("core.services.account_wipe._archive_metronome_customer", new=metronome_archive),
        patch("core.services.account_wipe.delete_stripe_customer", new=stripe_delete),
        patch("core.services.account_wipe.usage_repo.delete_all_for_owner", new=usage_delete),
        patch("core.services.account_wipe.clerk_admin.delete_user", new=clerk_delete),
        patch("core.services.account_wipe.list_owner_agent_key_secret_names", new=agent_keys_list),
        patch("core.services.account_wipe.delete_owner_agent_key_secrets", new=agent_keys_delete),
        patch("core.services.account_wipe.delete_gate_token_rows", new=gate_tokens_delete),
    ):
        yield SimpleNamespace(
            user_get=user_get,
            user_delete=user_delete,
            clerk_get_user=clerk_get_user,
            container_get=container_get,
            paperclip_get=paperclip_get,
            paperclip_row_delete=paperclip_row_delete,
            pending_brief_get=pending_brief_get,
            pending_brief_delete=pending_brief_delete,
            paperclip_repo_instance=paperclip_repo_instance,
            paperclip_repo_cls=paperclip_repo_cls,
            billing_get=billing_get,
            billing_delete=billing_delete,
            paperclip_company_delete=paperclip_company_delete,
            paperclip_user_delete_fn=paperclip_user_delete_fn,
            ecs_manager_stub=ecs_manager_stub,
            workspace_stub=workspace_stub,
            bedrock_aips_delete=bedrock_aips_delete,
            bedrock_role_delete=bedrock_role_delete,
            metronome_archive=metronome_archive,
            stripe_delete=stripe_delete,
            usage_delete=usage_delete,
            clerk_delete=clerk_delete,
            agent_keys_list=agent_keys_list,
            agent_keys_delete=agent_keys_delete,
            gate_tokens_delete=gate_tokens_delete,
        )


# ---------------------------------------------------------------------------
# wipe_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wipe_account_runs_steps_in_spec_order(wipe_mocks):
    """(a) Every collaborator succeeds -> every step is "ok", in the exact
    spec-mandated order, with Clerk last."""
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert isinstance(report, WipeReport)
    assert report.owner_id == OWNER_ID
    assert report.dry_run is False
    assert [s.system for s in report.steps] == EXPECTED_ORDER
    assert all(s.status == "ok" for s in report.steps), report.steps
    assert report.steps[-1].system == "clerk_user"

    wipe_mocks.paperclip_company_delete.assert_awaited_once_with("company_abc")
    wipe_mocks.paperclip_user_delete_fn.assert_awaited_once_with(
        paperclip_user_id="pc_user_abc", email="owner@example.com", env="dev"
    )
    wipe_mocks.ecs_manager_stub.delete_user_service.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.workspace_stub.delete_user_dir.assert_called_once_with(OWNER_ID)
    wipe_mocks.bedrock_aips_delete.assert_awaited_once_with(
        OWNER_ID,
        aip_arns=["arn:aws:bedrock:us-east-1:877352799272:inference-profile/sonnet-aip"],
    )
    wipe_mocks.bedrock_role_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.metronome_archive.assert_awaited_once_with("met_abc123")
    wipe_mocks.stripe_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.usage_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.paperclip_row_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.billing_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.user_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.agent_keys_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)


@pytest.mark.asyncio
async def test_sweeps_per_agent_paperclip_key_secrets_before_clerk(wipe_mocks):
    """The per-agent Paperclip key-secret sweep runs (dogfood gap fix), records
    the deleted count, and lands before the always-last Clerk delete."""
    wipe_mocks.agent_keys_delete.return_value = 3
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = next(s for s in report.steps if s.system == "paperclip_agent_keys")
    assert step.status == "ok"
    assert "3" in step.detail
    systems = [s.system for s in report.steps]
    assert systems.index("paperclip_agent_keys") < systems.index("clerk_user")


@pytest.mark.asyncio
async def test_preview_lists_per_agent_paperclip_key_secrets(wipe_mocks):
    """preview_wipe surfaces the per-agent key secrets by name-prefix count."""
    wipe_mocks.agent_keys_list.return_value = [
        "isol8/dev/paperclip-agent-keys/user_123/a1",
        "isol8/dev/paperclip-agent-keys/user_123/a2",
    ]
    report = await preview_wipe(OWNER_ID)

    wipe_mocks.agent_keys_list.assert_awaited_once_with(OWNER_ID)
    step = next(s for s in report.steps if s.system == "paperclip_agent_keys")
    assert step.status == "would_delete"
    assert "2 per-agent" in step.detail


@pytest.mark.asyncio
async def test_bedrock_aips_step_passes_prefetched_arns_not_a_live_reread(wipe_mocks):
    """Regression test for the containers-row-deletion ordering hazard: the
    `ecs_service` step (step 3) deletes the containers row as a side effect
    of ecs_manager.delete_user_service, and the LATER `bedrock_aips` step
    (step 5) used to call bedrock_aip.delete_owner_aips(owner_id) with no
    ARNs, which internally re-read the (by-then-deleted) containers row via
    read_owner_aips, found nothing, and silently no-op'd while still
    reporting status="ok" -- orphaning the Bedrock inference profile + the
    bedrock-aip-owners reverse row on every real wipe.

    The fix: pass the ARNs already captured in the upfront identifier
    snapshot (`ids.container["bedrock_aip_arns"]`) explicitly via the
    `aip_arns=` kwarg, so the bedrock_aips step is fully independent of
    whether a prior step already deleted the containers row."""
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["bedrock_aips"].status == "ok"

    # The call MUST carry aip_arns= populated from the pre-fetched snapshot
    # -- not a bare owner_id call that would force delete_owner_aips to fall
    # back to its own (now-stale-by-ordering) containers-row re-read.
    wipe_mocks.bedrock_aips_delete.assert_awaited_once_with(
        OWNER_ID,
        aip_arns=["arn:aws:bedrock:us-east-1:877352799272:inference-profile/sonnet-aip"],
    )
    call = wipe_mocks.bedrock_aips_delete.await_args
    assert "aip_arns" in call.kwargs, "delete_owner_aips must be called with the aip_arns kwarg, not rely on a re-read"
    assert call.kwargs["aip_arns"], "aip_arns must be populated (non-empty) from the identifier snapshot"

    # container_repo.get_by_owner_id is read exactly ONCE for the whole wipe
    # (the upfront identifier snapshot in _read_identifiers) -- the
    # bedrock_aips step does not trigger a second read of the containers row,
    # proving it isn't depending on that row still being present.
    wipe_mocks.container_get.assert_awaited_once_with(OWNER_ID)


@pytest.mark.asyncio
async def test_early_step_failure_does_not_abort_later_steps(wipe_mocks):
    """(b) THE key property: a collaborator raising leaves that one step
    "failed" but every later step -- especially the Clerk delete, last --
    still runs."""
    wipe_mocks.ecs_manager_stub.delete_user_service.side_effect = RuntimeError("ECS API down")

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["ecs_service"].status == "failed"
    assert "ECS API down" in (by_system["ecs_service"].error or "")

    # Every step after the failure still ran and succeeded.
    for system in ("efs_workspace_dir", "bedrock_aips", "bedrock_role", "metronome", "stripe", "usage_counters"):
        assert by_system[system].status == "ok", f"{system} should still have run: {by_system[system]}"

    # Clerk — last step — must still have been called despite the earlier failure.
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)
    assert by_system["clerk_user"].status == "ok"

    # The step order itself is unaffected by the failure.
    assert [s.system for s in report.steps] == EXPECTED_ORDER


@pytest.mark.asyncio
async def test_bootstrap_user_protected_error_recorded_as_failed_step(wipe_mocks):
    """BootstrapUserProtectedError from paperclip_user_delete is caught like
    any other failure -- it does not abort the wipe."""
    wipe_mocks.paperclip_user_delete_fn.side_effect = BootstrapUserProtectedError(
        "Refusing to delete Paperclip user pc_user_abc: admin@isol8.co is a bootstrap admin."
    )

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["paperclip_aurora_user"].status == "failed"
    assert "bootstrap admin" in (by_system["paperclip_aurora_user"].error or "")
    # Later steps, including Clerk, still ran.
    assert by_system["clerk_user"].status == "ok"
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)


@pytest.mark.asyncio
async def test_skips_stripe_when_no_stripe_customer_id(wipe_mocks):
    """(c) trial_local shape: billing row present but no stripe_customer_id
    -> skipped, and delete_stripe_customer is never called."""
    wipe_mocks.billing_get.return_value = _billing_row(stripe_customer_id=None)

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["stripe"].status == "skipped"
    wipe_mocks.stripe_delete.assert_not_awaited()
    # Metronome is independent — still ran since metronome_customer_id is present.
    assert by_system["metronome"].status == "ok"


@pytest.mark.asyncio
async def test_skips_metronome_when_no_metronome_customer_id(wipe_mocks):
    wipe_mocks.billing_get.return_value = _billing_row(metronome_customer_id=None)

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["metronome"].status == "skipped"
    wipe_mocks.metronome_archive.assert_not_awaited()
    assert by_system["stripe"].status == "ok"


@pytest.mark.asyncio
async def test_skips_billing_dependent_steps_when_no_billing_row(wipe_mocks):
    wipe_mocks.billing_get.return_value = None

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["metronome"].status == "skipped"
    assert by_system["stripe"].status == "skipped"
    assert by_system["billing_row"].status == "skipped"
    wipe_mocks.billing_delete.assert_not_awaited()
    # Non-billing steps still ran normally.
    assert by_system["clerk_user"].status == "ok"


@pytest.mark.asyncio
async def test_skips_paperclip_steps_when_no_paperclip_row(wipe_mocks):
    """No paperclip-companies row -> paperclip_company, paperclip_aurora_user,
    and paperclip_row all skip; the rest of the wipe still runs."""
    wipe_mocks.paperclip_get.return_value = None

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["paperclip_company"].status == "skipped"
    assert by_system["paperclip_aurora_user"].status == "skipped"
    assert by_system["paperclip_row"].status == "skipped"
    wipe_mocks.paperclip_company_delete.assert_not_awaited()
    wipe_mocks.paperclip_user_delete_fn.assert_not_awaited()
    wipe_mocks.paperclip_row_delete.assert_not_awaited()
    assert by_system["ecs_service"].status == "ok"
    assert by_system["clerk_user"].status == "ok"


@pytest.mark.asyncio
async def test_skips_container_dependent_steps_when_no_containers_row(wipe_mocks):
    wipe_mocks.container_get.return_value = None

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["ecs_service"].status == "skipped"
    assert by_system["bedrock_aips"].status == "skipped"
    assert by_system["bedrock_role"].status == "skipped"
    assert by_system["bedrock_gate_tokens"].status == "skipped"
    wipe_mocks.gate_tokens_delete.assert_not_awaited()
    wipe_mocks.ecs_manager_stub.delete_user_service.assert_not_awaited()
    wipe_mocks.bedrock_aips_delete.assert_not_awaited()
    wipe_mocks.bedrock_role_delete.assert_not_awaited()
    # EFS workspace-dir delete is unconditional -- not gated on the containers row.
    assert by_system["efs_workspace_dir"].status == "ok"


@pytest.mark.asyncio
async def test_org_owner_raises_and_deletes_nothing(wipe_mocks):
    """(d) org owner -> raises OrgWipeUnsupportedError, and no read or
    delete collaborator is ever touched."""
    with pytest.raises(OrgWipeUnsupportedError):
        await wipe_account("org_456", actor_admin_id="admin_1")

    wipe_mocks.user_get.assert_not_awaited()
    wipe_mocks.container_get.assert_not_awaited()
    wipe_mocks.billing_get.assert_not_awaited()
    wipe_mocks.paperclip_company_delete.assert_not_awaited()
    wipe_mocks.clerk_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_clerk_delete_still_runs_when_every_other_step_fails(wipe_mocks):
    """Extreme partial-failure case: every collaborator before Clerk raises.
    Clerk delete must still run and the report must still be returned."""
    wipe_mocks.paperclip_company_delete.side_effect = RuntimeError("paperclip down")
    wipe_mocks.paperclip_user_delete_fn.side_effect = RuntimeError("aurora down")
    wipe_mocks.ecs_manager_stub.delete_user_service.side_effect = RuntimeError("ecs down")
    wipe_mocks.workspace_stub.delete_user_dir.side_effect = RuntimeError("efs down")
    wipe_mocks.bedrock_aips_delete.side_effect = RuntimeError("bedrock aip down")
    wipe_mocks.bedrock_role_delete.side_effect = RuntimeError("bedrock role down")
    wipe_mocks.metronome_archive.side_effect = RuntimeError("metronome down")
    wipe_mocks.stripe_delete.side_effect = RuntimeError("stripe down")
    wipe_mocks.usage_delete.side_effect = RuntimeError("usage down")
    wipe_mocks.gate_tokens_delete.side_effect = RuntimeError("gate tokens down")
    wipe_mocks.paperclip_row_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.billing_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.user_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.agent_keys_delete.side_effect = RuntimeError("sm down")

    with (
        patch("core.services.account_wipe.settings.LITESTREAM_BUCKET", "isol8-dev-openclaw-replicas"),
        patch(
            "core.services.litestream_replicas.delete_replica_prefix",
            new=AsyncMock(return_value=False),  # helper never raises; False => step failed
        ),
    ):
        report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert [s.system for s in report.steps] == EXPECTED_ORDER
    assert all(s.status == "failed" for s in report.steps[:-1])
    assert report.steps[-1].system == "clerk_user"
    assert report.steps[-1].status == "ok"
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)


# ---------------------------------------------------------------------------
# preview_wipe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_wipe_calls_no_delete_method(wipe_mocks):
    """(e) preview_wipe is read-only: none of the delete collaborators are
    ever invoked, and every step is would_delete/skipped."""
    report = await preview_wipe(OWNER_ID)

    assert report.dry_run is True
    assert report.owner_id == OWNER_ID
    assert {s.status for s in report.steps} <= {"would_delete", "skipped"}
    assert [s.system for s in report.steps] == EXPECTED_ORDER

    wipe_mocks.paperclip_company_delete.assert_not_awaited()
    wipe_mocks.paperclip_user_delete_fn.assert_not_awaited()
    wipe_mocks.ecs_manager_stub.delete_user_service.assert_not_awaited()
    wipe_mocks.workspace_stub.delete_user_dir.assert_not_called()
    wipe_mocks.bedrock_aips_delete.assert_not_awaited()
    wipe_mocks.bedrock_role_delete.assert_not_awaited()
    wipe_mocks.metronome_archive.assert_not_awaited()
    wipe_mocks.stripe_delete.assert_not_awaited()
    wipe_mocks.usage_delete.assert_not_awaited()
    wipe_mocks.paperclip_row_delete.assert_not_awaited()
    wipe_mocks.pending_brief_delete.assert_not_awaited()
    wipe_mocks.gate_tokens_delete.assert_not_awaited()
    wipe_mocks.billing_delete.assert_not_awaited()
    wipe_mocks.user_delete.assert_not_awaited()
    wipe_mocks.clerk_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_wipe_would_delete_everything_present(wipe_mocks):
    with patch("core.services.account_wipe.settings.LITESTREAM_BUCKET", "isol8-dev-openclaw-replicas"):
        report = await preview_wipe(OWNER_ID)

    by_system = {s.system: s for s in report.steps}
    for system in EXPECTED_ORDER:
        assert by_system[system].status == "would_delete", f"{system}: {by_system[system]}"


@pytest.mark.asyncio
async def test_preview_wipe_skips_absent_resources(wipe_mocks):
    wipe_mocks.paperclip_get.return_value = None
    wipe_mocks.container_get.return_value = None
    wipe_mocks.billing_get.return_value = None
    wipe_mocks.user_get.return_value = None
    wipe_mocks.workspace_stub.user_path.return_value.exists.return_value = False

    report = await preview_wipe(OWNER_ID)

    by_system = {s.system: s for s in report.steps}
    assert by_system["paperclip_company"].status == "skipped"
    assert by_system["paperclip_aurora_user"].status == "skipped"
    assert by_system["ecs_service"].status == "skipped"
    assert by_system["efs_workspace_dir"].status == "skipped"
    assert by_system["bedrock_gate_tokens"].status == "skipped"
    assert by_system["bedrock_aips"].status == "skipped"
    assert by_system["bedrock_role"].status == "skipped"
    assert by_system["metronome"].status == "skipped"
    assert by_system["stripe"].status == "skipped"
    assert by_system["paperclip_row"].status == "skipped"
    assert by_system["billing_row"].status == "skipped"
    assert by_system["user_row"].status == "skipped"
    # usage_counters and clerk_user have no cheap existence check -- always would_delete.
    assert by_system["usage_counters"].status == "would_delete"
    assert by_system["clerk_user"].status == "would_delete"


@pytest.mark.asyncio
async def test_preview_wipe_org_owner_raises(wipe_mocks):
    with pytest.raises(OrgWipeUnsupportedError):
        await preview_wipe("org_456")
    wipe_mocks.user_get.assert_not_awaited()


# ---------------------------------------------------------------------------
# resolve_account_emails — Clerk-first, users-row fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_account_emails_prefers_clerk_addresses(wipe_mocks):
    """Clerk is authoritative: every Clerk email_address is returned first, in
    order, and the users-row email is appended only if distinct."""
    wipe_mocks.clerk_get_user.return_value = {
        "email_addresses": [
            {"email_address": "primary@clerk.com"},
            {"email_address": "secondary@clerk.com"},
        ]
    }
    wipe_mocks.user_get.return_value = _user_row(email="owner@example.com")

    emails = await resolve_account_emails(OWNER_ID)

    assert emails == ["primary@clerk.com", "secondary@clerk.com", "owner@example.com"]


@pytest.mark.asyncio
async def test_resolve_account_emails_falls_back_to_users_row_when_clerk_unavailable(wipe_mocks):
    """CLERK_SECRET_KEY-unset posture: get_user returns None, so resolution
    falls back to the users-row email alone."""
    wipe_mocks.clerk_get_user.return_value = None
    wipe_mocks.user_get.return_value = _user_row(email="owner@example.com")

    emails = await resolve_account_emails(OWNER_ID)

    assert emails == ["owner@example.com"]


@pytest.mark.asyncio
async def test_resolve_account_emails_dedupes_and_tolerates_no_email(wipe_mocks):
    """A users-row email equal to a Clerk one is not duplicated; a fully empty
    account (Clerk None + no users row) resolves to an empty list."""
    wipe_mocks.clerk_get_user.return_value = {"email_addresses": [{"email_address": "dup@example.com"}]}
    wipe_mocks.user_get.return_value = _user_row(email="dup@example.com")
    assert await resolve_account_emails(OWNER_ID) == ["dup@example.com"]

    wipe_mocks.clerk_get_user.return_value = None
    wipe_mocks.user_get.return_value = None
    assert await resolve_account_emails(OWNER_ID) == []


# ---------------------------------------------------------------------------
# Bootstrap-account refusal — fires up front, before any teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wipe_account_refuses_bootstrap_admin_before_any_teardown(wipe_mocks):
    """The resolved email being a bootstrap Paperclip admin identity raises
    BootstrapAccountProtectedError up front -- NO teardown collaborator runs
    (in particular the Clerk delete, which would otherwise always run last)."""
    wipe_mocks.user_get.return_value = _user_row(email="admin@isol8.co")

    with pytest.raises(BootstrapAccountProtectedError):
        await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    wipe_mocks.paperclip_company_delete.assert_not_awaited()
    wipe_mocks.paperclip_user_delete_fn.assert_not_awaited()
    wipe_mocks.ecs_manager_stub.delete_user_service.assert_not_awaited()
    wipe_mocks.workspace_stub.delete_user_dir.assert_not_called()
    wipe_mocks.bedrock_aips_delete.assert_not_awaited()
    wipe_mocks.bedrock_role_delete.assert_not_awaited()
    wipe_mocks.metronome_archive.assert_not_awaited()
    wipe_mocks.stripe_delete.assert_not_awaited()
    wipe_mocks.usage_delete.assert_not_awaited()
    wipe_mocks.paperclip_row_delete.assert_not_awaited()
    wipe_mocks.billing_delete.assert_not_awaited()
    wipe_mocks.user_delete.assert_not_awaited()
    wipe_mocks.clerk_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_wipe_account_refuses_bootstrap_admin_from_pre_resolved_emails(wipe_mocks):
    """When the router passes pre-resolved emails, the guard still fires off
    that list (case-insensitively) without re-resolving."""
    with pytest.raises(BootstrapAccountProtectedError):
        await wipe_account(OWNER_ID, actor_admin_id="admin_1", account_emails=["Admin+Paperclip@isol8.co"])

    wipe_mocks.clerk_delete.assert_not_awaited()
    wipe_mocks.paperclip_company_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_wipe_refuses_bootstrap_admin(wipe_mocks):
    """preview_wipe applies the same up-front refusal so an admin never even
    sees a would-delete plan for a bootstrap account."""
    wipe_mocks.clerk_get_user.return_value = {"email_addresses": [{"email_address": "admin@isol8.co"}]}

    with pytest.raises(BootstrapAccountProtectedError):
        await preview_wipe(OWNER_ID)


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_wipe_report_to_dict_shape():
    report = WipeReport(
        owner_id=OWNER_ID,
        dry_run=True,
        steps=[
            WipeStep(system="paperclip_company", action="delete_company", status="would_delete", detail="x"),
            WipeStep(system="clerk_user", action="delete_user", status="failed", error="boom"),
        ],
    )
    d = report.to_dict()
    assert d == {
        "owner_id": OWNER_ID,
        "dry_run": True,
        "steps": [
            {
                "system": "paperclip_company",
                "action": "delete_company",
                "status": "would_delete",
                "detail": "x",
                "error": None,
            },
            {
                "system": "clerk_user",
                "action": "delete_user",
                "status": "failed",
                "detail": "",
                "error": "boom",
            },
        ],
    }


@pytest.mark.asyncio
async def test_replica_delete_waits_for_drain(wipe_mocks):
    """Ordering matters: a still-running container's litestream can upload
    after the prefix delete, and the next provision would restore the state
    the wipe reported as erased."""
    order: list[str] = []
    wipe_mocks.ecs_manager_stub.wait_until_drained = AsyncMock(
        side_effect=lambda *a, **k: order.append("drain") or True
    )

    async def _del(owner_id, *, agent_id=None):
        order.append("delete_replicas")
        return True

    with (
        patch("core.services.account_wipe.settings.LITESTREAM_BUCKET", "isol8-dev-openclaw-replicas"),
        patch("core.services.litestream_replicas.delete_replica_prefix", new=_del),
    ):
        await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert order == ["drain", "delete_replicas"], order


@pytest.mark.asyncio
async def test_replica_delete_refuses_when_drain_times_out(wipe_mocks):
    """A drain timeout must FAIL the step, not delete anyway: deleting while a
    writer may still be live lets a late upload repopulate the prefix, and the
    next provision restores state the wipe reported as erased."""
    wipe_mocks.ecs_manager_stub.wait_until_drained = AsyncMock(return_value=False)
    deleted = False

    async def _del(owner_id, *, agent_id=None):
        nonlocal deleted
        deleted = True
        return True

    with (
        patch("core.services.account_wipe.settings.LITESTREAM_BUCKET", "isol8-dev-openclaw-replicas"),
        patch("core.services.litestream_replicas.delete_replica_prefix", new=_del),
    ):
        report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = {s.system: s for s in report.steps}["litestream_replicas"]
    assert step.status == "failed"
    assert "did not drain" in (step.error or "")
    assert deleted is False, "must not delete replicas while a writer may be live"


# ---------------------------------------------------------------------------
# The two residue gaps found by auditing three real prod wipes (2026-08-06):
# orphaned bedrock-gate token rows, and surviving __pending_brief__ items.
# These assert REAL DynamoDB state (moto) rather than mock call counts —
# both bugs were "the step reported ok/skipped while the row was still there".
# ---------------------------------------------------------------------------


@pytest.fixture
def real_ddb(wipe_mocks, monkeypatch):
    """moto-backed gate-tokens + paperclip-companies tables, with the two
    collaborators that touch them swapped back to their real implementations.

    The `wipe_mocks` doubles stand in for boto3 everywhere else; here the point
    is what actually survives in the table, so the real code has to run."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-gate-tokens",
            KeySchema=[{"AttributeName": "token_hash", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "token_hash", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-paperclip-companies",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # PaperclipRepo resolves its table via core.dynamodb.get_table (prefix
        # applied at call time); bedrock_gate_tokens takes the explicit env
        # override. Same wiring as tests/unit/billing/test_bedrock_gate_tokens.py.
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "test-gate-tokens")
        wipe_mocks.gate_tokens_delete.side_effect = real_delete_gate_token_rows
        real_repo = PaperclipRepo(table_name="paperclip-companies")
        wipe_mocks.paperclip_repo_cls.side_effect = lambda *a, **k: real_repo
        yield client


def _put_gate_row(client, token_hash: str, owner_id: str = OWNER_ID) -> None:
    client.put_item(
        TableName="test-gate-tokens",
        Item={"token_hash": {"S": token_hash}, "owner_id": {"S": owner_id}, "denied": {"BOOL": False}},
    )


def _gate_rows(client) -> set[str]:
    return {i["token_hash"]["S"] for i in client.scan(TableName="test-gate-tokens")["Items"]}


def _put_pending_brief(client, owner_id: str) -> None:
    client.put_item(
        TableName="test-paperclip-companies",
        Item={
            "user_id": {"S": f"__pending_brief__:{owner_id}"},
            "kind": {"S": "pending_brief"},
            "brief_state": {"S": "pending"},
            "owner_id": {"S": owner_id},
        },
    )


def _paperclip_keys(client) -> set[str]:
    return {i["user_id"]["S"] for i in client.scan(TableName="test-paperclip-companies")["Items"]}


@pytest.mark.asyncio
async def test_wipe_deletes_current_and_superseded_gate_token_rows(wipe_mocks, real_ddb):
    """Gap 1. The gate-tokens table is hash-keyed with no owner index, so the
    containers row's pointer + tracked superseded list is the only handle on
    an owner's rows. Before this step existed the wipe deleted that row and
    left the tokens behind: unfindable, unexpirable, denied=False forever."""
    for h in ("hash-current", "hash-old-1", "hash-old-2"):
        _put_gate_row(real_ddb, h)
    _put_gate_row(real_ddb, "hash-someone-else", owner_id="user_other")

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = {s.system: s for s in report.steps}["bedrock_gate_tokens"]
    assert step.status == "ok", step
    assert "deleted 3 of 3" in step.detail
    assert _gate_rows(real_ddb) == {"hash-someone-else"}


@pytest.mark.asyncio
async def test_gate_tokens_are_gone_before_delete_user_service_runs(wipe_mocks, real_ddb):
    """Gap 1, ordering. delete_user_service destroys the containers row — the
    only thing naming these hashes — so the token rows have to be gone by the
    time it is called, not merely deleted somewhere in the same wipe."""
    for h in ("hash-current", "hash-old-1", "hash-old-2"):
        _put_gate_row(real_ddb, h)
    surviving_at_ecs_teardown: list[str] = []

    async def _delete_user_service(owner_id):
        surviving_at_ecs_teardown.extend(sorted(_gate_rows(real_ddb)))

    wipe_mocks.ecs_manager_stub.delete_user_service = AsyncMock(side_effect=_delete_user_service)

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert surviving_at_ecs_teardown == [], (
        f"gate-token rows still live when the containers row was destroyed: {surviving_at_ecs_teardown}"
    )
    systems = [s.system for s in report.steps]
    assert systems.index("bedrock_gate_tokens") < systems.index("ecs_service")


@pytest.mark.asyncio
async def test_gate_token_step_is_a_clean_skip_for_a_non_bedrock_owner(wipe_mocks):
    """chatgpt_oauth / byo_key owners never mint a gate token — their
    containers row carries neither attribute. That is a skip, not a failure."""
    row = _container_row()
    row.pop("bedrock_gate_token_hash")
    row.pop("bedrock_gate_superseded_hashes")
    wipe_mocks.container_get.return_value = row

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = {s.system: s for s in report.steps}["bedrock_gate_tokens"]
    assert step.status == "skipped"
    assert step.error is None
    wipe_mocks.gate_tokens_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_wipe_deletes_the_owners_pending_brief_and_leaves_others_alone(wipe_mocks, real_ddb):
    """Gap 2, in the exact prod shape: NO paperclip-companies row (the step
    reported "SKIPPED — no paperclip-companies row") while a
    __pending_brief__:{owner_id} item for that same owner sat in the table."""
    _put_pending_brief(real_ddb, OWNER_ID)
    _put_pending_brief(real_ddb, "user_other")

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = {s.system: s for s in report.steps}["paperclip_row"]
    assert step.status == "ok", step
    assert "pending_brief row deleted" in step.detail
    assert _paperclip_keys(real_ddb) == {"__pending_brief__:user_other"}


@pytest.mark.asyncio
async def test_preview_leaves_gate_token_and_pending_brief_rows_in_place(wipe_mocks, real_ddb):
    """The preview shares the step list but must still delete nothing."""
    for h in ("hash-current", "hash-old-1", "hash-old-2"):
        _put_gate_row(real_ddb, h)
    _put_pending_brief(real_ddb, OWNER_ID)

    report = await preview_wipe(OWNER_ID)

    assert {s.status for s in report.steps} <= {"would_delete", "skipped"}
    by_system = {s.system: s for s in report.steps}
    assert by_system["bedrock_gate_tokens"].status == "would_delete"
    assert "3 gate-token row(s)" in by_system["bedrock_gate_tokens"].detail
    assert by_system["paperclip_row"].status == "would_delete"
    assert "pending_brief row" in by_system["paperclip_row"].detail

    assert _gate_rows(real_ddb) == {"hash-current", "hash-old-1", "hash-old-2"}
    assert _paperclip_keys(real_ddb) == {f"__pending_brief__:{OWNER_ID}"}
