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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _member_rows() -> list[dict]:
    return [
        {
            "user_id": MEMBER_A,
            "org_id": ORG_ID,
            "company_id": "company_ws",
            "paperclip_user_id": "pc_a",
            "paperclip_email": "a@example.com",
            "status": "active",
        },
        {
            "user_id": MEMBER_B,
            "org_id": ORG_ID,
            "company_id": "company_ws",
            "paperclip_user_id": "pc_b",
            "paperclip_email": "b@example.com",
            "status": "active",
        },
    ]


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

    paperclip_instance = MagicMock()
    paperclip_instance.list_by_org_id = AsyncMock(return_value=_member_rows())
    paperclip_instance.get_org_company_id = AsyncMock(return_value="company_ws")
    paperclip_instance.get = AsyncMock(return_value=MagicMock(org_id=ORG_ID, user_id=MEMBER_A, company_id="company_ws"))
    paperclip_instance.delete = AsyncMock(return_value=None)
    paperclip_instance.delete_pending_brief = AsyncMock(return_value=True)
    paperclip_repo_cls = MagicMock(return_value=paperclip_instance)

    user_get = AsyncMock(side_effect=lambda uid: {"user_id": uid, "email": f"{uid}@example.com"})
    user_delete = AsyncMock(return_value=None)

    messaging_list_rooms = AsyncMock(return_value=["room-1", "room-2"])
    messaging_get_room = AsyncMock(return_value={"ownerId": ORG_ID})
    messaging_delete_room = AsyncMock(return_value=2)
    messaging_delete_owner = AsyncMock(return_value=3)

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

    paperclip_company_delete = AsyncMock(return_value=None)
    paperclip_user_delete_fn = AsyncMock(return_value=None)
    delete_runtime_fn = AsyncMock(return_value=_container_row())
    metronome_archive = AsyncMock(return_value=None)
    stripe_delete = AsyncMock(return_value=True)
    company_stripe_release = AsyncMock(return_value=True)

    clerk_get_org = AsyncMock(return_value={"id": ORG_ID, "name": "Acme"})
    clerk_delete_user = AsyncMock(return_value=None)
    clerk_delete_org = AsyncMock(return_value=None)
    clerk_memberships = AsyncMock(return_value=[])

    _patches = [
        ("core.services.workspace_wipe.container_repo.get_by_owner_id", container_get),
        ("core.services.workspace_wipe.billing_repo.get_by_owner_id", billing_get),
        ("core.services.workspace_wipe.billing_repo.delete", billing_delete),
        ("core.services.workspace_wipe.PaperclipRepo", paperclip_repo_cls),
        ("core.services.workspace_wipe.user_repo.get", user_get),
        ("core.services.workspace_wipe.user_repo.delete", user_delete),
        ("core.services.workspace_wipe.messaging_repo.list_owner_room_ids", messaging_list_rooms),
        ("core.services.workspace_wipe.messaging_repo.get_room", messaging_get_room),
        ("core.services.workspace_wipe.messaging_repo.delete_room_partition", messaging_delete_room),
        ("core.services.workspace_wipe.messaging_repo.delete_owner_partition", messaging_delete_owner),
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
        ("core.services.workspace_wipe._delete_paperclip_company_call", paperclip_company_delete),
        ("core.services.workspace_wipe.delete_paperclip_user", paperclip_user_delete_fn),
        ("core.services.workspace_wipe.delete_workspace_runtime", delete_runtime_fn),
        ("core.services.workspace_wipe._archive_metronome_customer", metronome_archive),
        ("core.services.workspace_wipe.delete_stripe_customer", stripe_delete),
        ("core.services.workspace_wipe.company_stripe.release_and_unbind", company_stripe_release),
        ("core.services.workspace_wipe.clerk_admin.get_organization", clerk_get_org),
        ("core.services.workspace_wipe.clerk_admin.delete_user", clerk_delete_user),
        ("core.services.workspace_wipe.clerk_admin.delete_organization", clerk_delete_org),
        ("core.services.workspace_wipe.clerk_admin.list_user_organizations", clerk_memberships),
    ]
    with ExitStack() as _stack:
        for _target, _new in _patches:
            _stack.enter_context(patch(_target, new=_new))
        yield SimpleNamespace(
            container_get=container_get,
            billing_get=billing_get,
            billing_delete=billing_delete,
            paperclip_instance=paperclip_instance,
            user_delete=user_delete,
            messaging_list_rooms=messaging_list_rooms,
            messaging_delete_room=messaging_delete_room,
            messaging_delete_owner=messaging_delete_owner,
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
            paperclip_company_delete=paperclip_company_delete,
            paperclip_user_delete_fn=paperclip_user_delete_fn,
            delete_runtime=delete_runtime_fn,
            metronome_archive=metronome_archive,
            stripe_delete=stripe_delete,
            company_stripe_release=company_stripe_release,
            clerk_delete_user=clerk_delete_user,
            clerk_delete_org=clerk_delete_org,
            clerk_memberships=clerk_memberships,
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
    assert by_system["paperclip_company"].status == "would_delete"
    assert "company_ws" in by_system["paperclip_company"].detail
    assert MEMBER_A in by_system["paperclip_company"].detail
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
    ws_mocks.paperclip_company_delete.assert_not_awaited()
    ws_mocks.paperclip_user_delete_fn.assert_not_awaited()
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
    ws_mocks.delete_runtime.assert_awaited_once_with(ORG_ID, delete_profiles=True)
    by_system = {s.system: s for s in report.steps}
    assert by_system["runtime"].status == "ok"
    # Paperclip company deleted (workspace delete, not refused) + members archived by identity.
    ws_mocks.paperclip_company_delete.assert_awaited_once_with("company_ws")
    assert ws_mocks.paperclip_user_delete_fn.await_count == 2
    # Messaging under the org partition gone.
    ws_mocks.messaging_delete_owner.assert_awaited_once_with(ORG_ID)
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


@pytest.mark.asyncio
async def test_billing_override_requires_the_phrase(ws_mocks):
    preview = await preview_workspace_wipe(ORG_ID, include_billing=True)
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
    ws_mocks.paperclip_company_delete.assert_not_awaited()
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
    ws_mocks.paperclip_company_delete.assert_not_awaited()
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
    ws_mocks.paperclip_company_delete.assert_not_awaited()
    ws_mocks.clerk_delete_org.assert_not_awaited()
    assert any(s.system == "runtime" and s.status == "skipped" for s in report.steps)


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
    silently diverge."""
    registry_systems = {system for system, _ in WORKSPACE_STEP_ORDER}
    assert set(EXPECTED_ORDER) <= registry_systems
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
    ws_mocks.clerk_memberships.return_value = []
    assert await resolve_workspace_org(MEMBER_A) == ORG_ID


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

    paperclip_instance = _MagicMock()
    paperclip_instance.get = _AsyncMock(
        return_value=_MagicMock(user_id=MEMBER_A, org_id=ORG_ID, company_id="company_ws", paperclip_user_id="pc_a")
    )
    paperclip_instance.get_pending_brief = _AsyncMock(return_value=None)
    paperclip_instance.delete_pending_brief = _AsyncMock(return_value=False)
    paperclip_instance.get_user_ids_for_company = _AsyncMock(return_value=[MEMBER_A])
    paperclip_instance.find_owner_row = _AsyncMock(return_value=None)

    with ExitStack() as stack:
        stack.enter_context(_patch.object(_mod.user_repo, "get", new=_AsyncMock(return_value=None)))
        stack.enter_context(_patch.object(_mod.container_repo, "get_by_owner_id", new=_AsyncMock(return_value=None)))
        stack.enter_context(_patch.object(_mod, "PaperclipRepo", new=_MagicMock(return_value=paperclip_instance)))
        stack.enter_context(_patch.object(_mod.billing_repo, "get_by_owner_id", new=_AsyncMock(return_value=None)))
        stack.enter_context(_patch.object(_mod.clerk_admin, "get_user", new=_AsyncMock(return_value=None)))
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
