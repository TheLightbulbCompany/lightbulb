"""Unit tests for the Admin Workspace-Wipe orchestrator (org-keyed).

Companion to tests/unit/services/test_account_wipe.py (user-keyed, which this
file does NOT touch — the existing user-keyed suite must stay green as-is).

Covers the 2026-09-21 incident shape: the containers row lives under the ORG
id, so the wipe snapshots by org id and the runtime step finds + deletes it;
billing/Stripe is preserved by default; preview writes nothing; execute
aborts on a stale preview; preview + execute enumerate the same steps from
the one shared registry.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.repositories.workspace_repo import Identity, WorkspaceRepo, Workspace
from core.services.workspace_wipe import (
    AmbiguousWorkspaceError,
    BillingPreservedError,
    StaleWorkspacePreviewError,
    WORKSPACE_STEP_ORDER,
    WorkspaceConfirmMismatchError,
    WorkspaceWipeError,
    normalize_layers,
    preview_workspace_wipe,
    resolve_workspace_org,
    wipe_workspace,
)
from tests.unit.services.test_account_wipe import EXPECTED_ORDER

ORG_ID = "org_3JL4xJ1FSutobOuhukUl2091xw2"
MEMBER_A = "user_member_a"
MEMBER_B = "user_member_b"


def _container_row(**overrides) -> dict:
    defaults = {
        "owner_id": ORG_ID,
        "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/abc",
        "bedrock_gate_token_hash": "hash-current",
        "bedrock_gate_superseded_hashes": ["hash-old"],
    }
    defaults.update(overrides)
    return defaults


def _workspace(**overrides) -> Workspace:
    now = datetime.now(timezone.utc)
    defaults = dict(
        org_id=ORG_ID,
        company_id="company_ws",
        status="active",
        created_at=now,
        updated_at=now,
        provisioner_user_id=MEMBER_A,
        member_user_ids={MEMBER_A, MEMBER_B},
    )
    defaults.update(overrides)
    return Workspace(**defaults)


def _identities() -> dict[str, Identity]:
    return {
        MEMBER_A: Identity(user_id=MEMBER_A, paperclip_user_id="pc_a", paperclip_email="a@example.com"),
        MEMBER_B: Identity(user_id=MEMBER_B, paperclip_user_id="pc_b", paperclip_email="b@example.com"),
    }


def _billing_row(**overrides) -> dict:
    defaults = {
        "owner_id": ORG_ID,
        "stripe_customer_id": "cus_ws",
        "stripe_subscription_id": "sub_ws",
        "metronome_customer_id": "met_ws",
        "company_stripe_account_id": "acct_ws",
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def ws_mocks():
    """Patch every workspace_wipe collaborator to a default success outcome."""
    container_get = AsyncMock(return_value=_container_row())
    billing_get = AsyncMock(return_value=_billing_row())
    billing_delete = AsyncMock(return_value=None)

    # Workspace rows by org id; identity rows by Clerk user id. Tests mutate
    # these dicts rather than re-stubbing the repo.
    workspaces = {ORG_ID: _workspace()}
    identities = _identities()
    paperclip_instance = MagicMock(spec=WorkspaceRepo)
    paperclip_instance.get_workspace = AsyncMock(side_effect=lambda oid, **_: workspaces.get(oid))
    paperclip_instance.get_identity = AsyncMock(side_effect=lambda uid, **_: identities.get(uid))
    paperclip_instance.delete = AsyncMock(return_value=None)
    paperclip_instance.delete_pending_brief = AsyncMock(return_value=True)
    paperclip_repo_cls = MagicMock(return_value=paperclip_instance)

    user_get = AsyncMock(side_effect=lambda uid: {"user_id": uid, "email": f"{uid}@example.com"})
    user_delete = AsyncMock(return_value=None)

    messaging_list_rooms = AsyncMock(return_value=["room-1", "room-2"])
    messaging_get_room = AsyncMock(return_value={"ownerId": ORG_ID})
    messaging_delete_room = AsyncMock(return_value=2)
    messaging_delete_owner = AsyncMock(return_value=3)
    phone_release = AsyncMock(return_value=1)
    search_delete = AsyncMock(return_value="dropped")

    secrets_list = AsyncMock(return_value=[f"lightbulb/dev/user-keys/{ORG_ID}/anthropic"])
    secrets_delete = AsyncMock(side_effect=lambda oid: [f"lightbulb/dev/user-keys/{oid}/anthropic"])
    agent_keys_list = AsyncMock(return_value=[f"lightbulb/dev/paperclip-agent-keys/{ORG_ID}/ceo"])
    agent_keys_delete = AsyncMock(return_value=1)
    integrations_list = AsyncMock(return_value=[{"owner_id": ORG_ID, "provider": "slack"}])
    integrations_delete = AsyncMock(side_effect=lambda oid: ["slack"] if oid == ORG_ID else [])
    oauth_revoke = AsyncMock(return_value=None)
    agent_email_query = AsyncMock(return_value=[{"owner_id": ORG_ID, "address": "ceo@acme.example"}])
    agent_email_delete = AsyncMock(return_value=["ceo@acme.example"])
    slack_list = AsyncMock(return_value=[{"team_id": "T1", "owner_id": ORG_ID}])
    slack_delete = AsyncMock(return_value=["T1"])
    slug_get = AsyncMock(return_value={"owner_id": ORG_ID, "slug": "acme"})
    slug_delete = AsyncMock(return_value="acme")
    workflows_list = AsyncMock(return_value=[{"workflow_id": "pwf_1", "owner_id": ORG_ID, "state": "provisioning"}])
    workflows_transition = AsyncMock(return_value={"workflow_id": "pwf_1", "state": "cancelled"})
    usage_delete = AsyncMock(return_value=5)
    gate_tokens_delete = AsyncMock(return_value=2)

    delete_runtime_fn = AsyncMock(return_value=_container_row())
    voice_wipe = AsyncMock()
    metronome_archive = AsyncMock(return_value=None)
    stripe_delete = AsyncMock(return_value=True)
    company_stripe_release = AsyncMock(return_value=True)

    clerk_get_org = AsyncMock(return_value={"id": ORG_ID, "name": "Acme"})
    clerk_delete_user = AsyncMock(return_value=None)
    clerk_delete_org = AsyncMock(return_value=None)
    clerk_memberships = AsyncMock(return_value=[])
    clerk_org_members = AsyncMock(return_value=[])
    openrouter_get = AsyncMock(return_value=None)
    openrouter_retire = AsyncMock(return_value=True)
    openrouter_delete = AsyncMock(return_value=None)
    join_link_get = AsyncMock(return_value={"org_id": ORG_ID, "token_hash": "h"})
    join_link_delete = AsyncMock(return_value=None)
    fast_gate_instance = MagicMock()
    fast_gate_instance.delete_row = AsyncMock(return_value=None)

    _patches = [
        ("core.services.workspace_wipe.container_repo.get_by_owner_id", container_get),
        ("core.services.workspace_wipe.billing_repo.get_by_owner_id", billing_get),
        ("core.services.workspace_wipe.billing_repo.delete", billing_delete),
        ("core.services.workspace_wipe.WorkspaceRepo", paperclip_repo_cls),
        ("core.services.workspace_wipe.user_repo.get", user_get),
        ("core.services.workspace_wipe.user_repo.delete", user_delete),
        ("core.services.workspace_wipe.messaging_repo.list_owner_room_ids", messaging_list_rooms),
        ("core.services.workspace_wipe.messaging_repo.get_room", messaging_get_room),
        ("core.services.workspace_wipe.messaging_repo.delete_room_partition", messaging_delete_room),
        ("core.services.workspace_wipe.messaging_repo.delete_owner_partition", messaging_delete_owner),
        ("core.services.workspace_wipe.voice_onboarding.release_number", phone_release),
        ("core.services.workspace_wipe.message_search.delete_owner_index", search_delete),
        ("core.services.workspace_wipe.list_owner_secret_names", secrets_list),
        ("core.services.workspace_wipe.delete_owner_secrets", secrets_delete),
        ("core.services.workspace_wipe.list_owner_agent_key_secret_names", agent_keys_list),
        ("core.services.workspace_wipe.delete_owner_agent_key_secrets", agent_keys_delete),
        ("core.services.workspace_wipe.integrations_repo.list_for_owner", integrations_list),
        ("core.services.workspace_wipe.integrations_repo.delete_all_for_owner", integrations_delete),
        ("core.services.workspace_wipe.revoke_user_oauth", oauth_revoke),
        ("core.services.workspace_wipe.agent_email_repo.query_by_owner", agent_email_query),
        ("core.services.workspace_wipe.agent_email_repo.delete_all_for_owner", agent_email_delete),
        ("core.services.workspace_wipe.slack_installs_repo.list_for_owner", slack_list),
        ("core.services.workspace_wipe.slack_installs_repo.delete_all_for_owner", slack_delete),
        ("core.services.workspace_wipe.company_slug_repo.get_by_owner", slug_get),
        ("core.services.workspace_wipe.company_slug_repo.delete_for_owner", slug_delete),
        ("core.services.workspace_wipe.provisioning_workflow_repo.list_for_owner", workflows_list),
        ("core.services.workspace_wipe.provisioning_workflow_repo.transition", workflows_transition),
        ("core.services.workspace_wipe.usage_repo.delete_all_for_owner", usage_delete),
        ("core.services.workspace_wipe.delete_gate_token_rows", gate_tokens_delete),
        ("core.services.workspace_wipe.delete_workspace_runtime", delete_runtime_fn),
        ("core.services.workspace_wipe.wipe_owner_voice", voice_wipe),
        ("core.services.workspace_wipe._archive_metronome_customer", metronome_archive),
        ("core.services.workspace_wipe.delete_stripe_customer", stripe_delete),
        ("core.services.workspace_wipe.company_stripe.release_and_unbind", company_stripe_release),
        ("core.services.workspace_wipe.clerk_admin.get_organization", clerk_get_org),
        ("core.services.workspace_wipe.clerk_admin.delete_user", clerk_delete_user),
        ("core.services.workspace_wipe.clerk_admin.delete_organization", clerk_delete_org),
        ("core.services.workspace_wipe.clerk_admin.list_user_organizations", clerk_memberships),
        ("core.services.workspace_wipe.clerk_admin.list_organization_members", clerk_org_members),
        ("core.services.workspace_wipe.openrouter_key_repo.get", openrouter_get),
        ("core.services.workspace_wipe.openrouter_key_repo.mark_retiring", openrouter_retire),
        ("core.services.workspace_wipe._delete_openrouter_key", openrouter_delete),
        ("core.services.workspace_wipe.workspace_join_repo.get_link", join_link_get),
        ("core.services.workspace_wipe.workspace_join_repo.delete_link", join_link_delete),
        ("core.services.workspace_wipe.MetronomeFastGate", MagicMock(return_value=fast_gate_instance)),
    ]
    with ExitStack() as _stack:
        for _target, _new in _patches:
            _stack.enter_context(patch(_target, new=_new))
        yield SimpleNamespace(
            container_get=container_get,
            billing_get=billing_get,
            billing_delete=billing_delete,
            paperclip_instance=paperclip_instance,
            workspaces=workspaces,
            identities=identities,
            user_delete=user_delete,
            messaging_list_rooms=messaging_list_rooms,
            messaging_delete_room=messaging_delete_room,
            messaging_delete_owner=messaging_delete_owner,
            phone_release=phone_release,
            search_delete=search_delete,
            secrets_delete=secrets_delete,
            agent_keys_delete=agent_keys_delete,
            integrations_delete=integrations_delete,
            oauth_revoke=oauth_revoke,
            agent_email_delete=agent_email_delete,
            slack_delete=slack_delete,
            slug_delete=slug_delete,
            workflows_transition=workflows_transition,
            usage_delete=usage_delete,
            gate_tokens_delete=gate_tokens_delete,
            delete_runtime=delete_runtime_fn,
            voice_wipe=voice_wipe,
            metronome_archive=metronome_archive,
            stripe_delete=stripe_delete,
            company_stripe_release=company_stripe_release,
            clerk_delete_user=clerk_delete_user,
            clerk_delete_org=clerk_delete_org,
            clerk_memberships=clerk_memberships,
            clerk_org_members=clerk_org_members,
            openrouter_get=openrouter_get,
            openrouter_retire=openrouter_retire,
            openrouter_delete=openrouter_delete,
            join_link_delete=join_link_delete,
            fast_gate_delete=fast_gate_instance.delete_row,
        )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_enumerates_org_blast_radius_and_preserves_billing(ws_mocks):
    report = await preview_workspace_wipe(ORG_ID)

    assert report.dry_run is True
    assert report.fingerprint
    assert report.billing_preserved is True
    by_system = {s.system: s for s in report.steps}
    # The incident shape: the containers row under the ORG id IS enumerated.
    assert by_system["runtime"].status == "would_delete"
    assert "agentcore_runtime_arn=arn:aws:bedrock-agentcore" in by_system["runtime"].detail
    assert by_system["messaging"].status == "would_delete"
    assert "2 owned room" in by_system["messaging"].detail
    # Counts + names, never values.
    assert "1 secret(s)" in next(s for s in report.steps if s.system == "owner_secrets").detail
    # Billing preserved by default — every billing step skipped, loudly.
    for system, action in [s for s in WORKSPACE_STEP_ORDER if s[0] in ("metronome", "stripe", "billing_row")]:
        step = next(s for s in report.steps if (s.system, s.action) == (system, action))
        assert step.status == "skipped", (system, action)
        assert "PRESERVED" in step.detail
    ws_mocks.stripe_delete.assert_not_awaited()
    ws_mocks.metronome_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_writes_nothing(ws_mocks):
    """dry_run writes nothing — asserted at the collaborator level: no delete
    entry point is invoked, only reads."""
    await preview_workspace_wipe(ORG_ID)

    ws_mocks.delete_runtime.assert_not_awaited()
    ws_mocks.gate_tokens_delete.assert_not_awaited()
    ws_mocks.messaging_delete_room.assert_not_awaited()
    ws_mocks.messaging_delete_owner.assert_not_awaited()
    ws_mocks.secrets_delete.assert_not_awaited()
    ws_mocks.agent_keys_delete.assert_not_awaited()
    ws_mocks.integrations_delete.assert_not_awaited()
    ws_mocks.oauth_revoke.assert_not_awaited()
    ws_mocks.agent_email_delete.assert_not_awaited()
    ws_mocks.slack_delete.assert_not_awaited()
    ws_mocks.slug_delete.assert_not_awaited()
    ws_mocks.user_delete.assert_not_awaited()
    ws_mocks.billing_delete.assert_not_awaited()
    ws_mocks.clerk_delete_user.assert_not_awaited()
    ws_mocks.clerk_delete_org.assert_not_awaited()
    ws_mocks.workflows_transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_rejects_non_org_id(ws_mocks):
    with pytest.raises(WorkspaceWipeError):
        await preview_workspace_wipe("user_123")


@pytest.mark.asyncio
async def test_preview_unknown_layer_rejected(ws_mocks):
    with pytest.raises(WorkspaceWipeError):
        await preview_workspace_wipe(ORG_ID, layers=["nope"])
    with pytest.raises(WorkspaceWipeError):
        await normalize_layers(["runtime", "nope"])


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_parked_members_runtime_step_says_scheduled(ws_mocks):
    async def _parked(owner_id, *, delete_profiles=False, report=None):
        report["runtime"] = "scheduled"
        return _container_row()

    ws_mocks.delete_runtime.side_effect = _parked
    preview = await preview_workspace_wipe(ORG_ID)
    report = await wipe_workspace(
        ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint=preview.fingerprint
    )
    step = {s.system: s for s in report.steps}["runtime"]
    assert step.status == "ok" and "teardown scheduled" in step.detail


@pytest.mark.asyncio
async def test_execute_finds_and_deletes_runtime_under_org_id(ws_mocks):
    """The incident's acceptance case: containers under the ORG id IS found
    and torn down (not 'skipped: no containers row')."""
    preview = await preview_workspace_wipe(ORG_ID)
    report = await wipe_workspace(
        ORG_ID,
        actor_admin_id="admin_1",
        confirm_org=ORG_ID,
        expected_fingerprint=preview.fingerprint,
    )

    assert report.dry_run is False
    ws_mocks.delete_runtime.assert_awaited_once_with(ORG_ID, delete_profiles=True, report={})
    ws_mocks.voice_wipe.assert_awaited_once_with(ORG_ID)
    by_system = {s.system: s for s in report.steps}
    assert by_system["runtime"].status == "ok"
    # Messaging under the org partition gone, and the number's PHONE# line row (gate 13).
    ws_mocks.messaging_delete_owner.assert_awaited_once_with(ORG_ID)
    ws_mocks.phone_release.assert_awaited_once_with(ORG_ID)
    # Clerk org deleted, and LAST.
    ws_mocks.clerk_delete_org.assert_awaited_once_with(ORG_ID)
    assert report.steps[-1].system == "clerk_org"
    assert ws_mocks.clerk_delete_user.await_count == 2


@pytest.mark.asyncio
async def test_execute_preserves_billing_by_default(ws_mocks):
    preview = await preview_workspace_wipe(ORG_ID)
    report = await wipe_workspace(
        ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint=preview.fingerprint
    )

    assert report.billing_preserved is True
    ws_mocks.stripe_delete.assert_not_awaited()
    ws_mocks.metronome_archive.assert_not_awaited()
    ws_mocks.company_stripe_release.assert_not_awaited()
    ws_mocks.billing_delete.assert_not_awaited()
    ws_mocks.fast_gate_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_billing_override_requires_the_phrase(ws_mocks):
    all_layers = ["runtime", "paperclip", "messaging", "secrets", "identity", "billing"]
    preview = await preview_workspace_wipe(ORG_ID, layers=all_layers, include_billing=True)
    with pytest.raises(BillingPreservedError):
        await wipe_workspace(
            ORG_ID,
            actor_admin_id="admin_1",
            confirm_org=ORG_ID,
            layers=["runtime", "paperclip", "messaging", "secrets", "identity", "billing"],
            include_billing=True,
            confirm_billing="yes-please",
            expected_fingerprint=preview.fingerprint,
        )
    ws_mocks.delete_runtime.assert_not_awaited()

    report = await wipe_workspace(
        ORG_ID,
        actor_admin_id="admin_1",
        confirm_org=ORG_ID,
        layers=["runtime", "paperclip", "messaging", "secrets", "identity", "billing"],
        include_billing=True,
        confirm_billing="DELETE-BILLING",
        expected_fingerprint=preview.fingerprint,
    )
    assert report.billing_preserved is False
    ws_mocks.fast_gate_delete.assert_awaited_once_with(ORG_ID)
    ws_mocks.stripe_delete.assert_awaited_once_with(ORG_ID)
    ws_mocks.metronome_archive.assert_awaited_once_with("met_ws")
    ws_mocks.billing_delete.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_execute_refuses_confirm_mismatch_before_any_delete(ws_mocks):
    preview = await preview_workspace_wipe(ORG_ID)
    with pytest.raises(WorkspaceConfirmMismatchError):
        await wipe_workspace(
            ORG_ID,
            actor_admin_id="admin_1",
            confirm_org="org_other",
            expected_fingerprint=preview.fingerprint,
        )
    ws_mocks.delete_runtime.assert_not_awaited()
    ws_mocks.clerk_delete_org.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_preview_aborts_before_any_delete(ws_mocks):
    preview = await preview_workspace_wipe(ORG_ID)
    # Drift: a room lands between preview and execute.
    ws_mocks.messaging_list_rooms.return_value = ["room-1", "room-2", "room-3"]

    with pytest.raises(StaleWorkspacePreviewError):
        await wipe_workspace(
            ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint=preview.fingerprint
        )

    ws_mocks.delete_runtime.assert_not_awaited()
    ws_mocks.messaging_delete_room.assert_not_awaited()
    ws_mocks.clerk_delete_org.assert_not_awaited()


@pytest.mark.asyncio
async def test_layer_selection_skips_unselected(ws_mocks):
    preview = await preview_workspace_wipe(ORG_ID, layers=["messaging"])
    # Same full registry, in order — unselected layers shown skipped, not omitted.
    assert [(s.system, s.action) for s in preview.steps] == list(WORKSPACE_STEP_ORDER)
    by_preview = {(s.system, s.action): s for s in preview.steps}
    assert by_preview[("messaging", "delete_owner_rooms_and_streams")].status == "would_delete"
    assert by_preview[("runtime", "delete_workspace_runtime")].status == "skipped"

    report = await wipe_workspace(
        ORG_ID,
        actor_admin_id="admin_1",
        confirm_org=ORG_ID,
        layers=["messaging"],
        expected_fingerprint=preview.fingerprint,
    )
    ws_mocks.messaging_delete_owner.assert_awaited_once_with(ORG_ID)
    ws_mocks.delete_runtime.assert_not_awaited()
    ws_mocks.clerk_delete_org.assert_not_awaited()
    assert any(s.system == "runtime" and s.status == "skipped" for s in report.steps)


async def _preview_then_wipe(**kwargs):
    preview = await preview_workspace_wipe(ORG_ID, **kwargs)
    return preview, await wipe_workspace(
        ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint=preview.fingerprint, **kwargs
    )


def _step(report, system, action=None):
    return next(s for s in report.steps if s.system == system and (action is None or s.action == action))


@pytest.mark.asyncio
async def test_members_come_from_clerk_when_paperclip_rows_are_gone(ws_mocks):
    """A prior half-wipe deleted the workspace + identity rows: Clerk still
    names the members, so their accounts are still found and deleted."""
    ws_mocks.workspaces.clear()
    ws_mocks.identities.clear()
    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_A, "email": "a@example.com"}]

    _, report = await _preview_then_wipe()

    ws_mocks.clerk_delete_user.assert_awaited_once_with(MEMBER_A)
    ws_mocks.user_delete.assert_awaited_once_with(MEMBER_A)
    assert any(c.args == (MEMBER_A,) for c in ws_mocks.secrets_delete.await_args_list)
    assert report.members == [
        {"user_id": MEMBER_A, "email": "a@example.com", "action": "delete_account", "other_orgs": []}
    ]


@pytest.mark.asyncio
async def test_rerun_after_clerk_user_failure_finishes(ws_mocks):
    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_A}, {"user_id": MEMBER_B}]
    ws_mocks.clerk_delete_user.side_effect = lambda uid: (
        (_ for _ in ()).throw(RuntimeError("clerk 500")) if uid == MEMBER_B else None
    )

    _, first = await _preview_then_wipe()
    assert _step(first, "clerk_user").status == "failed"
    # The org is KEPT so its member list stays recoverable for the re-run.
    assert _step(first, "clerk_org").status == "failed"
    ws_mocks.clerk_delete_org.assert_not_awaited()

    # Second run: the workspace row is gone; Clerk still lists B.
    ws_mocks.workspaces.clear()
    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_B}]
    ws_mocks.clerk_delete_user.side_effect = None
    ws_mocks.clerk_delete_user.reset_mock()
    _, second = await _preview_then_wipe()

    ws_mocks.clerk_delete_user.assert_awaited_once_with(MEMBER_B)
    ws_mocks.clerk_delete_org.assert_awaited_once_with(ORG_ID)
    assert _step(second, "clerk_org").status == "ok"


@pytest.mark.asyncio
async def test_shared_member_keeps_account_unless_opted_in(ws_mocks):
    ws_mocks.clerk_memberships.side_effect = lambda uid, **_: (
        [{"id": ORG_ID}, {"id": "org_other"}] if uid == MEMBER_B else [{"id": ORG_ID}]
    )

    preview, report = await _preview_then_wipe()

    by_uid = {m["user_id"]: m for m in preview.members}
    assert by_uid[MEMBER_A]["action"] == "delete_account"
    assert by_uid[MEMBER_B] == {
        "user_id": MEMBER_B,
        "email": "b@example.com",
        "action": "remove_membership",
        "other_orgs": ["org_other"],
    }
    assert "remove membership only (also in org_other)" in _step(preview, "clerk_user").detail
    ws_mocks.clerk_delete_user.assert_awaited_once_with(MEMBER_A)
    ws_mocks.user_delete.assert_awaited_once_with(MEMBER_A)
    assert all(c.args != (MEMBER_B,) for c in ws_mocks.secrets_delete.await_args_list)
    # B keeps their account, so B keeps their Paperclip identity row too (it is
    # their identity in org_other); A's and the workspace
    # row go.
    deleted_keys = [c.args[0] for c in ws_mocks.paperclip_instance.delete.await_args_list]
    assert MEMBER_B not in deleted_keys
    assert ORG_ID in deleted_keys and MEMBER_A in deleted_keys
    ws_mocks.clerk_delete_org.assert_awaited_once_with(ORG_ID)

    ws_mocks.clerk_delete_user.reset_mock()
    ws_mocks.paperclip_instance.delete.reset_mock()
    await _preview_then_wipe(delete_shared_members=True)
    assert {c.args[0] for c in ws_mocks.clerk_delete_user.await_args_list} == {MEMBER_A, MEMBER_B}
    ws_mocks.paperclip_instance.delete.assert_any_await(MEMBER_B)


@pytest.mark.asyncio
async def test_vendor_org_is_refused(ws_mocks):
    from core.config import settings

    with patch.object(settings, "VENDOR_ORG_ID", ORG_ID):
        with pytest.raises(WorkspaceWipeError, match="vendor"):
            await preview_workspace_wipe(ORG_ID)
        with pytest.raises(WorkspaceWipeError, match="vendor"):
            await wipe_workspace(ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint="x")
    ws_mocks.delete_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_acting_admin_member_is_refused(ws_mocks):
    preview = await preview_workspace_wipe(ORG_ID)
    with pytest.raises(WorkspaceWipeError, match="member"):
        await wipe_workspace(
            ORG_ID, actor_admin_id=MEMBER_A, confirm_org=ORG_ID, expected_fingerprint=preview.fingerprint
        )
    ws_mocks.delete_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_openrouter_active_key_is_retired_and_left_to_the_worker(ws_mocks):
    """Retiring is terminal for the wipe: the gate refuses non-active keys, so the row and
    secret are left for the settlement worker instead of failing the wipe."""
    ws_mocks.openrouter_get.return_value = {"owner_id": ORG_ID, "state": "active", "secret_name": "s"}
    _, report = await _preview_then_wipe()
    ws_mocks.openrouter_retire.assert_awaited_once_with(ORG_ID)
    ws_mocks.openrouter_delete.assert_not_awaited()
    step = _step(report, "openrouter_key")
    assert step.status == "ok"
    assert "retiring" in step.detail


@pytest.mark.asyncio
async def test_openrouter_disabled_key_row_and_secret_deleted(ws_mocks):
    ws_mocks.openrouter_get.return_value = {"owner_id": ORG_ID, "state": "disabled", "secret_name": "s"}
    _, report = await _preview_then_wipe()
    ws_mocks.openrouter_delete.assert_awaited_once_with(ORG_ID)
    assert _step(report, "openrouter_key").status == "ok"


@pytest.mark.asyncio
async def test_fingerprint_binds_layers_billing_and_shared_opt_in(ws_mocks):
    base = (await preview_workspace_wipe(ORG_ID)).fingerprint
    assert (await preview_workspace_wipe(ORG_ID, layers=["messaging"])).fingerprint != base
    assert (await preview_workspace_wipe(ORG_ID, delete_shared_members=True)).fingerprint != base
    all_layers = ["runtime", "paperclip", "messaging", "secrets", "identity", "billing"]
    assert (await preview_workspace_wipe(ORG_ID, layers=all_layers)).fingerprint != (
        await preview_workspace_wipe(ORG_ID, layers=all_layers, include_billing=True)
    ).fingerprint
    # Executing different layers than the confirmed preview is drift.
    with pytest.raises(StaleWorkspacePreviewError):
        await wipe_workspace(
            ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, layers=["messaging"], expected_fingerprint=base
        )


@pytest.mark.asyncio
async def test_execute_requires_a_fingerprint(ws_mocks):
    for fp in ("", None, "  "):
        with pytest.raises(WorkspaceWipeError, match="fingerprint"):
            await wipe_workspace(ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint=fp)
    ws_mocks.delete_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_scoped_leftovers_are_deleted(ws_mocks):
    await _preview_then_wipe()
    ws_mocks.join_link_delete.assert_awaited_once_with(ORG_ID)
    # The org's own pending brief is attempted even though members exist.
    ws_mocks.paperclip_instance.delete_pending_brief.assert_any_await(ORG_ID)


@pytest.mark.asyncio
async def test_search_index_drop_is_its_own_step_and_fails_alone(ws_mocks):
    ws_mocks.search_delete.side_effect = RuntimeError("search down")
    _, report = await _preview_then_wipe()
    step = _step(report, "search", "drop_owner_index")
    assert step.status == "failed"
    assert _step(report, "messaging").status == "ok"
    ws_mocks.search_delete.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_messaging_skips_foreign_room_and_continues(ws_mocks):
    with patch(
        "core.services.workspace_wipe.messaging_repo.get_room",
        new=AsyncMock(side_effect=lambda rid: {"ownerId": "org_vendor"} if rid == "room-1" else {"ownerId": ORG_ID}),
    ):
        _, report = await _preview_then_wipe()
    ws_mocks.messaging_delete_room.assert_awaited_once_with("room-2")
    ws_mocks.messaging_delete_owner.assert_awaited_once_with(ORG_ID)
    step = _step(report, "messaging")
    assert step.status == "ok"
    assert "room-1" in step.detail


# ---------------------------------------------------------------------------
# Registry parity: preview + execute share one registry; registry covers user path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_and_execute_enumerate_the_same_steps(ws_mocks):
    preview = await preview_workspace_wipe(ORG_ID)
    report = await wipe_workspace(
        ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint=preview.fingerprint
    )
    assert [(s.system, s.action) for s in preview.steps] == [(s.system, s.action) for s in report.steps]
    assert [(s.system, s.action) for s in report.steps] == list(WORKSPACE_STEP_ORDER)


def test_workspace_registry_covers_user_path_systems():
    """The shared registry covers every system the user-keyed path emits
    (plus clerk_org, which only a workspace wipe has) — the two paths cannot
    silently diverge. ``paperclip_membership`` is the user path's alternative
    to deleting the company, which the workspace wipe does outright. ``person_memory`` is a
    person's own data (theirs in every workspace); a workspace's sessions and drafts go with its
    runtime teardown (workspace_memory.delete_owner_memory)."""
    registry_systems = {system for system, _ in WORKSPACE_STEP_ORDER}
    assert set(EXPECTED_ORDER) - {"paperclip_membership", "person_memory"} <= registry_systems
    assert "clerk_org" in registry_systems


# ---------------------------------------------------------------------------
# User -> org resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_requires_explicit_org_when_memberships_differ(ws_mocks):
    ws_mocks.clerk_memberships.return_value = [
        {"id": ORG_ID, "slug": "a", "name": "A", "role": "org:admin"},
        {"id": "org_other", "slug": "b", "name": "B", "role": "org:member"},
    ]
    with pytest.raises(AmbiguousWorkspaceError):
        await resolve_workspace_org(MEMBER_A)
    # Explicit confirmation disambiguates.
    assert await resolve_workspace_org(MEMBER_A, confirm_org="org_other") == "org_other"
    with pytest.raises(WorkspaceConfirmMismatchError):
        await resolve_workspace_org(MEMBER_A, confirm_org="org_unknown")


@pytest.mark.asyncio
async def test_resolve_single_workspace_needs_no_confirmation(ws_mocks):
    ws_mocks.clerk_memberships.return_value = [{"id": ORG_ID}]
    assert await resolve_workspace_org(MEMBER_A) == ORG_ID


@pytest.mark.asyncio
async def test_resolve_with_no_clerk_membership_requires_explicit_org(ws_mocks):
    """Clerk is the only source now (no per-user paperclip row names an org)."""
    ws_mocks.clerk_memberships.return_value = []
    with pytest.raises(AmbiguousWorkspaceError):
        await resolve_workspace_org(MEMBER_A)
    assert await resolve_workspace_org(MEMBER_A, confirm_org=ORG_ID) == ORG_ID
    ws_mocks.paperclip_instance.get_workspace.assert_not_awaited()
    ws_mocks.paperclip_instance.get_identity.assert_not_awaited()


# ---------------------------------------------------------------------------
# User-keyed incident hardening (account_wipe): name the workspace on a miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_wipe_names_workspace_when_container_missing():
    """A user-keyed wipe whose member belongs to a workspace must not report
    a bare 'no containers row' — it names the org and points at the workspace
    wipe. Exercises the REAL account_wipe path with its own mocks."""
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import MagicMock as _MagicMock
    from unittest.mock import patch as _patch

    from core.services import account_wipe as _mod

    paperclip_instance = _MagicMock(spec=WorkspaceRepo)
    paperclip_instance.get_identity = _AsyncMock(return_value=_identities()[MEMBER_A])
    paperclip_instance.get_workspace = _AsyncMock(return_value=_workspace())
    paperclip_instance.get_pending_brief = _AsyncMock(return_value=None)
    paperclip_instance.delete_pending_brief = _AsyncMock(return_value=False)

    with ExitStack() as stack:
        stack.enter_context(_patch.object(_mod.user_repo, "get", new=_AsyncMock(return_value=None)))
        stack.enter_context(_patch.object(_mod.container_repo, "get_by_owner_id", new=_AsyncMock(return_value=None)))
        stack.enter_context(_patch.object(_mod, "WorkspaceRepo", new=_MagicMock(return_value=paperclip_instance)))
        stack.enter_context(_patch.object(_mod.billing_repo, "get_by_owner_id", new=_AsyncMock(return_value=None)))
        stack.enter_context(_patch.object(_mod.clerk_admin, "get_user", new=_AsyncMock(return_value=None)))
        stack.enter_context(
            _patch.object(_mod.clerk_admin, "list_user_organizations", new=_AsyncMock(return_value=[{"id": ORG_ID}]))
        )
        stack.enter_context(
            _patch.object(_mod.clerk_admin, "get_organization", new=_AsyncMock(return_value={"name": "Acme"}))
        )
        stack.enter_context(_patch.object(_mod.messaging_repo, "list_owner_room_ids", new=_AsyncMock(return_value=[])))
        stack.enter_context(
            _patch.object(_mod.provisioning_workflow_repo, "list_for_owner", new=_AsyncMock(return_value=[]))
        )
        stack.enter_context(_patch.object(_mod, "list_owner_secret_names", new=_AsyncMock(return_value=[])))
        stack.enter_context(_patch.object(_mod, "list_owner_agent_key_secret_names", new=_AsyncMock(return_value=[])))
        stack.enter_context(_patch.object(_mod.integrations_repo, "list_for_owner", new=_AsyncMock(return_value=[])))
        stack.enter_context(_patch.object(_mod.agent_email_repo, "query_by_owner", new=_AsyncMock(return_value=[])))
        stack.enter_context(_patch.object(_mod.slack_installs_repo, "list_for_owner", new=_AsyncMock(return_value=[])))
        stack.enter_context(_patch.object(_mod.company_slug_repo, "get_by_owner", new=_AsyncMock(return_value=None)))
        preview = await _mod.preview_wipe(MEMBER_A)

    runtime = next(s for s in preview.steps if s.system == "runtime")
    assert runtime.status == "skipped"
    assert ORG_ID in runtime.detail
    assert f"/admin/workspaces/{ORG_ID}/wipe" in runtime.detail


@pytest.mark.asyncio
async def test_clerk_member_list_failure_fails_closed(ws_mocks):
    """Clerk is the only way to find members with no paperclip row: a Clerk
    error must refuse the wipe, never read as an empty member list."""
    ws_mocks.clerk_org_members.side_effect = RuntimeError("clerk 503")
    with pytest.raises(RuntimeError):
        await preview_workspace_wipe(ORG_ID)
    ws_mocks.clerk_org_members.assert_awaited_with(ORG_ID, strict=True)
    ws_mocks.clerk_delete_org.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_secret_failure_keeps_clerk_org(ws_mocks):
    """A failed member-keyed step keeps the org, so a re-run can still find
    that member through the org's Clerk member list."""
    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_A}]
    ws_mocks.secrets_delete.side_effect = RuntimeError("throttled")

    _, report = await _preview_then_wipe()

    assert _step(report, "owner_secrets").status == "failed"
    assert _step(report, "clerk_org").status == "failed"
    ws_mocks.clerk_delete_org.assert_not_awaited()


# ---------------------------------------------------------------------------
# The workspace's rows in paperclip-companies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_row_and_identity_rows_are_deleted(ws_mocks):
    preview, report = await _preview_then_wipe()
    ws_mocks.paperclip_instance.get_workspace.assert_awaited_with(ORG_ID)
    # The org row and both deletable members' identity rows go.
    deleted_keys = [c.args[0] for c in ws_mocks.paperclip_instance.delete.await_args_list]
    assert deleted_keys == [ORG_ID, MEMBER_A, MEMBER_B]
    assert _step(report, "paperclip_row").status == "ok"
    assert f"{ORG_ID}: workspace row" in _step(report, "paperclip_row").detail


@pytest.mark.asyncio
async def test_members_are_the_union_of_clerk_and_the_workspace_row(ws_mocks):
    """A human who joined the company but left Clerk, and a Clerk member who
    never joined the company, are both members; identity comes from
    ``get_identity``."""
    ws_mocks.workspaces[ORG_ID] = _workspace(member_user_ids={MEMBER_A})
    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_B, "email": "b-clerk@example.com"}]
    ws_mocks.identities.pop(MEMBER_B)

    preview = await preview_workspace_wipe(ORG_ID)

    assert [m["user_id"] for m in preview.members] == [MEMBER_A, MEMBER_B]
    assert {c.args[0] for c in ws_mocks.paperclip_instance.get_identity.await_args_list} == {MEMBER_A, MEMBER_B}
    by_uid = {m["user_id"]: m for m in preview.members}
    assert by_uid[MEMBER_A]["email"] == "a@example.com"
    assert by_uid[MEMBER_B]["email"] == "b-clerk@example.com"


@pytest.mark.asyncio
async def test_workspace_row_read_failure_aborts_before_any_step(ws_mocks):
    ws_mocks.paperclip_instance.get_workspace.side_effect = RuntimeError("ddb 500")
    with pytest.raises(RuntimeError):
        await preview_workspace_wipe(ORG_ID)
    with pytest.raises(RuntimeError):
        await wipe_workspace(ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint="x")
    ws_mocks.paperclip_instance.delete.assert_not_awaited()
    ws_mocks.delete_runtime.assert_not_awaited()
    ws_mocks.gate_tokens_delete.assert_not_awaited()
    ws_mocks.clerk_delete_org.assert_not_awaited()


# ---------------------------------------------------------------------------
# Self-serve: the sole member deletes their account (App Store 5.1.1(v))
# ---------------------------------------------------------------------------


def _sole_member(ws_mocks) -> None:
    ws_mocks.workspaces[ORG_ID] = _workspace(member_user_ids={MEMBER_A})
    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_A}]


@pytest.mark.asyncio
async def test_self_serve_sole_member_wipes_workspace_and_cancels_stripe(ws_mocks):
    from core.services.workspace_wipe import wipe_own_workspace

    _sole_member(ws_mocks)
    report = await wipe_own_workspace(ORG_ID, user_id=MEMBER_A)

    assert not [s for s in report.steps if s.status == "failed"]
    assert report.billing_preserved is False
    ws_mocks.stripe_delete.assert_awaited_once_with(ORG_ID)  # cancels the subscription, then the customer
    ws_mocks.delete_runtime.assert_awaited_once_with(ORG_ID, delete_profiles=True, report={})
    ws_mocks.clerk_delete_user.assert_awaited_once_with(MEMBER_A)
    ws_mocks.clerk_delete_org.assert_awaited_once_with(ORG_ID)


@pytest.mark.asyncio
async def test_self_serve_account_in_another_org_is_still_deleted(ws_mocks):
    """The caller asked to delete their ACCOUNT, not just leave this workspace."""
    from core.services.workspace_wipe import wipe_own_workspace

    _sole_member(ws_mocks)
    ws_mocks.clerk_memberships.return_value = [{"id": ORG_ID}, {"id": "org_other"}]
    await wipe_own_workspace(ORG_ID, user_id=MEMBER_A)

    ws_mocks.clerk_delete_user.assert_awaited_once_with(MEMBER_A)


@pytest.mark.asyncio
async def test_self_serve_other_clerk_members_means_leave_not_wipe(ws_mocks):
    """Another Clerk member remains: nothing is deleted (join link included); the caller leaves instead."""
    from core.services.workspace_wipe import OtherMembersRemainError, wipe_own_workspace

    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_A}, {"user_id": MEMBER_B}]
    with pytest.raises(OtherMembersRemainError):
        await wipe_own_workspace(ORG_ID, user_id=MEMBER_A)
    ws_mocks.join_link_delete.assert_not_awaited()
    ws_mocks.delete_runtime.assert_not_awaited()
    ws_mocks.stripe_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_serve_leftover_row_is_a_plain_permanent_refusal(ws_mocks):
    """A paperclip row for someone no longer in Clerk: one member source (the snapshot)
    decides, and the error says retrying will not clear it."""
    from core.services.workspace_wipe import SelfServeBlockedError, wipe_own_workspace

    ws_mocks.clerk_org_members.return_value = [{"user_id": MEMBER_A}]  # B only has a leftover row
    with pytest.raises(SelfServeBlockedError, match="won't fix"):
        await wipe_own_workspace(ORG_ID, user_id=MEMBER_A)
    ws_mocks.join_link_delete.assert_not_awaited()
    ws_mocks.delete_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_serve_deletes_join_link_before_the_wipe_snapshot(ws_mocks):
    """Nobody can join mid-wipe: the link goes before the snapshot the wipe executes from."""
    from core.services.workspace_wipe import wipe_own_workspace

    _sole_member(ws_mocks)
    order: list[str] = []
    ws_mocks.join_link_delete.side_effect = lambda *_: order.append("link")
    ws_mocks.container_get.side_effect = lambda *_a, **_k: order.append("snapshot") or _container_row()
    await wipe_own_workspace(ORG_ID, user_id=MEMBER_A)

    # sole-member check, then the link, then the preview + execute snapshots
    assert order[:3] == ["snapshot", "link", "snapshot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["stripe_delete", "metronome_archive"])
async def test_failed_money_step_holds_back_billing_row_and_clerk(ws_mocks, failing):
    """A live subscription must never outlive its billing row or its Clerk identities."""
    getattr(ws_mocks, failing).side_effect = RuntimeError("stripe 500")
    opts = {"layers": ["runtime", "paperclip", "messaging", "secrets", "identity", "billing"], "include_billing": True}
    preview = await preview_workspace_wipe(ORG_ID, **opts)
    report = await wipe_workspace(
        ORG_ID,
        actor_admin_id="admin_1",
        confirm_org=ORG_ID,
        confirm_billing="DELETE-BILLING",
        expected_fingerprint=preview.fingerprint,
        **opts,
    )
    for system in ("billing_row", "clerk_user", "clerk_org"):
        assert _step(report, system).status == "failed"
        assert "held back" in _step(report, system).error
    ws_mocks.billing_delete.assert_not_awaited()
    ws_mocks.clerk_delete_user.assert_not_awaited()
    ws_mocks.clerk_delete_org.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_serve_retry_after_partial_wipe_does_not_fail(ws_mocks):
    """A retry after the rows are gone (Clerk user delete failed last time) finishes cleanly."""
    from core.services.workspace_wipe import wipe_own_workspace

    _sole_member(ws_mocks)
    ws_mocks.container_get.return_value = None
    ws_mocks.billing_get.return_value = None
    ws_mocks.workspaces.pop(ORG_ID)
    report = await wipe_own_workspace(ORG_ID, user_id=MEMBER_A)

    assert not [s for s in report.steps if s.status == "failed"]
    ws_mocks.clerk_delete_user.assert_awaited_once_with(MEMBER_A)


TEXT_ROW = {"owner_id": ORG_ID, "address": "+15550001111", "kind": "text", "photon_resource_id": "res_1"}
MAIL_ROW = {"owner_id": ORG_ID, "address": "ceo@acme.example", "kind": "mail"}


@pytest.mark.asyncio
async def test_agent_email_step_deprovisions_text_rows_then_deletes(ws_mocks):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from core.services import workspace_wipe

    calls = MagicMock()
    deprovision = AsyncMock(side_effect=lambda n: calls.deprovision(n))
    ws_mocks.agent_email_delete.side_effect = lambda oid: calls.delete(oid) or ["x"]
    with patch("core.services.text_lines.release_line", new=deprovision):
        out = await workspace_wipe._agent_email_run(SimpleNamespace(org_id=ORG_ID, email_rows=[MAIL_ROW, TEXT_ROW]))
    deprovision.assert_awaited_once_with(ORG_ID)
    assert [c[0] for c in calls.mock_calls if c[0] in ("deprovision", "delete")] == ["deprovision", "delete"]
    assert "released 1 text line(s)" in out


@pytest.mark.asyncio
async def test_deprovision_failure_does_not_abort(ws_mocks):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.services import workspace_wipe

    deprovision = AsyncMock(side_effect=RuntimeError("sendblue down"))
    with patch("core.services.text_lines.release_line", new=deprovision):
        out = await workspace_wipe._agent_email_run(SimpleNamespace(org_id=ORG_ID, email_rows=[TEXT_ROW]))
    ws_mocks.agent_email_delete.assert_awaited_once_with(ORG_ID)
    assert "released 0 text line(s)" in out and "FAILED to release: +15550001111" in out


@pytest.mark.asyncio
async def test_workspace_wipe_completes_without_aurora(ws_mocks):
    from tests.unit.services.test_account_wipe import _paperclip_and_aurora_gone

    with _paperclip_and_aurora_gone():
        preview = await preview_workspace_wipe(ORG_ID)
        report = await wipe_workspace(
            ORG_ID, actor_admin_id="admin_1", confirm_org=ORG_ID, expected_fingerprint=preview.fingerprint
        )

    for r in (preview, report):
        assert not {s.system for s in r.steps} & {"paperclip_company", "paperclip_aurora_user"}
    assert [s for s in report.steps if s.status == "failed"] == []
    deleted = [c.args[0] for c in ws_mocks.paperclip_instance.delete.await_args_list]
    assert sorted(deleted) == sorted([ORG_ID, MEMBER_A, MEMBER_B])
    assert ws_mocks.user_delete.await_count == 2
    ws_mocks.clerk_delete_org.assert_awaited_once_with(ORG_ID)
