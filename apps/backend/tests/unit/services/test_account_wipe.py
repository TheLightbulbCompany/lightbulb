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

from types import SimpleNamespace
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest

from core import tasks_schema as ts
from moto import mock_aws

from core.billing.bedrock_gate_tokens import delete_gate_token_rows as real_delete_gate_token_rows
from core.repositories import workspace_repo
from core.repositories.workspace_repo import Identity, WorkspaceRepo
from core.services.account_wipe import (
    MalformedUserIdError,
    WipeReport,
    WipeStep,
    preview_wipe,
    resolve_account_emails,
    wipe_account,
)

OWNER_ID = "user_123"

# The canonical order wipe_account must run its steps in (design spec
# "complete teardown map" §steps 1-12; see account_wipe.py's module
# docstring for the spec-row -> system-name mapping).
EXPECTED_ORDER = [
    # The gate-token rows go BEFORE the runtime step, which destroys the
    # containers row that holds the only pointers to them.
    "bedrock_gate_tokens",
    # Meeting bots, Temporal workflows and Schedules, the tasks partition: before the runtime and the rooms go.
    "engine",
    # One step for the whole runtime: the AgentCore runtime + boot secret, the S3 profile
    # prefix (the owner's CONTENT on this transport), and the containers row.
    # No bedrock_aips / bedrock_role steps either: the owner's Bedrock footprint is reaped
    # inside it, by the shared reaper at the seam every teardown path crosses
    # (bedrock_aip.reap_owner_bedrock_resources).
    "runtime",
    "metronome",
    "metronome_fast_gate",
    "stripe",
    # Step 8b shares the "stripe" system name with Step 8 above -- one is the
    # subscription-billing customer delete, the other is the company Stripe
    # account deauthorize (never a delete; see account_wipe.py's docstring).
    "stripe",
    "usage_counters",
    "messaging",
    # The person's own memory data: uploaded sessions, drafts, episodes, computer keys.
    "person_memory",
    "paperclip_row",
    "billing_row",
    "user_row",
    # Credential + identity leftovers: without these a "wiped" owner kept
    # their BYOK LLM keys, ChatGPT tokens and integration credentials in
    # Secrets Manager, their agent email addresses still routed inbound SES
    # mail, and their company slug stayed reserved forever. All BEFORE
    # clerk_user, which stays last so nothing regrows.
    "owner_secrets",
    "openrouter_key",
    "integrations",
    "oauth_state",
    "oauth_tokens",
    "agent_email_addresses",
    "slack_installs",
    "company_slug",
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


def _identity(**overrides) -> Identity:
    defaults = dict(
        user_id=OWNER_ID,
        paperclip_user_id="pc_user_abc",
        paperclip_email="owner@example.com",
    )
    defaults.update(overrides)
    return Identity(**defaults)


def _billing_row(**overrides) -> dict:
    defaults = {
        "owner_id": OWNER_ID,
        "stripe_customer_id": "cus_abc123",
        "stripe_subscription_id": "sub_abc123",
        "metronome_customer_id": "met_abc123",
        "company_stripe_account_id": "acct_xyz789",
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
    # falls back to the users-row email.
    clerk_get_user = AsyncMock(return_value=None)

    container_get = AsyncMock(return_value=_container_row())

    # Identity rows by Clerk user id. Tests mutate this dict (e.g. pop the owner's identity) instead of re-stubbing.
    identities = {OWNER_ID: _identity()}
    paperclip_get_identity = AsyncMock(side_effect=lambda uid, **_: identities.get(uid))
    paperclip_row_delete = AsyncMock(return_value=None)
    pending_brief_get = AsyncMock(return_value=None)
    pending_brief_delete = AsyncMock(return_value=None)
    paperclip_repo_instance = MagicMock(spec=WorkspaceRepo)
    paperclip_repo_instance.get_identity = paperclip_get_identity
    paperclip_repo_instance.delete = paperclip_row_delete
    paperclip_repo_instance.get_pending_brief = pending_brief_get
    paperclip_repo_instance.delete_pending_brief = pending_brief_delete
    paperclip_repo_cls = MagicMock(return_value=paperclip_repo_instance)
    billing_get = AsyncMock(return_value=_billing_row())
    billing_delete = AsyncMock(return_value=None)

    delete_workspace_runtime_fn = AsyncMock(return_value=None)
    voice_wipe = AsyncMock()
    spark_forget = AsyncMock()  # Spark sessions + transcripts: covered by test_spark_transcripts

    bedrock_aips_delete = AsyncMock(
        return_value=["arn:aws:bedrock:us-east-1:877352799272:inference-profile/sonnet-aip"]
    )
    bedrock_role_delete = AsyncMock(return_value=None)
    metronome_archive = AsyncMock(return_value=None)
    stripe_delete = AsyncMock(return_value=True)
    company_stripe_deauthorize = AsyncMock(return_value=True)
    billing_clear_company_stripe = AsyncMock(return_value={})
    usage_delete = AsyncMock(return_value=3)
    messaging_list_rooms = AsyncMock(return_value=["room-1", "room-2"])
    messaging_get_room = AsyncMock(return_value={"ownerId": OWNER_ID})
    messaging_delete_room = AsyncMock(return_value=2)
    messaging_delete_owner = AsyncMock(return_value=3)
    phone_release = AsyncMock(return_value=1)
    person_memory_delete = AsyncMock(return_value=None)
    clerk_delete = AsyncMock(return_value=None)
    # Credential leftovers. These reach Secrets Manager, DDB and OpenAI for
    # real, so all must be stubbed or the suite makes live AWS calls.
    owner_secrets_list = AsyncMock(return_value=["lightbulb/dev/user-keys/user_abc123/anthropic"])
    owner_secrets_delete = AsyncMock(return_value=["lightbulb/dev/user-keys/user_abc123/anthropic"])
    integrations_list = AsyncMock(return_value=[{"owner_id": "user_abc123", "provider": "slack"}])
    integrations_delete = AsyncMock(return_value=["slack"])
    oauth_state_delete = AsyncMock(return_value=0)
    oauth_revoke = AsyncMock(return_value=None)
    agent_email_query = AsyncMock(return_value=[{"owner_id": "user_abc123", "address": "friday@acme.isol8.co"}])
    agent_email_delete = AsyncMock(return_value=["friday@acme.isol8.co"])
    slack_installs_list = AsyncMock(return_value=[{"team_id": "T1", "owner_id": "user_abc123"}])
    slack_installs_delete = AsyncMock(return_value=["T1"])
    slug_get = AsyncMock(return_value={"owner_id": "user_abc123", "slug": "acme"})
    slug_delete = AsyncMock(return_value="acme")
    # Real boto3 underneath — the moto-backed tests at the bottom of this file
    # swap the real implementation back in via side_effect.
    gate_tokens_delete = AsyncMock(return_value=3)
    engine_wipe = AsyncMock(return_value="engine state removed")
    openrouter_get = AsyncMock(return_value=None)
    openrouter_retire = AsyncMock(return_value=True)
    held_number = AsyncMock(return_value="")
    fast_gate_delete = AsyncMock(return_value="metronome fast-gate row deleted")

    # ExitStack, not a parenthesised `with (patch(...), ...)` chain: that
    # form is one statically nested block PER patch and CPython caps those
    # at 20. Adding the credential-leftover stubs tipped it over and the
    # module stopped compiling with SyntaxError. A list + ExitStack has no
    # such ceiling.
    _patches = [
        ("core.services.account_wipe.user_repo.get", user_get),
        ("core.services.account_wipe.user_repo.delete", user_delete),
        ("core.services.account_wipe.clerk_admin.get_user", clerk_get_user),
        ("core.services.account_wipe.container_repo.get_by_owner_id", container_get),
        ("core.services.account_wipe.WorkspaceRepo", paperclip_repo_cls),
        ("core.services.account_wipe.billing_repo.get_by_owner_id", billing_get),
        ("core.services.account_wipe.billing_repo.delete", billing_delete),
        ("core.services.account_wipe.delete_workspace_runtime", delete_workspace_runtime_fn),
        ("core.services.account_wipe.wipe_owner_voice", voice_wipe),
        ("core.voice.os_voice.forget", spark_forget),
        # Patched on the bedrock_aip module, not on account_wipe: the wipe no
        # longer calls these itself. They stay stubbed as a safety net — the
        # reaper runs inside delete_workspace_runtime, which is stubbed here, so a
        # real AWS call from this suite would mean the seam wiring broke.
        ("core.billing.bedrock_aip.delete_owner_aips", bedrock_aips_delete),
        ("core.billing.bedrock_aip.delete_owner_bedrock_role", bedrock_role_delete),
        ("core.services.account_wipe._archive_metronome_customer", metronome_archive),
        ("core.services.account_wipe.delete_stripe_customer", stripe_delete),
        ("core.services.account_wipe.company_stripe.deauthorize", company_stripe_deauthorize),
        ("core.services.account_wipe.billing_repo.clear_company_stripe_account", billing_clear_company_stripe),
        ("core.services.account_wipe.usage_repo.delete_all_for_owner", usage_delete),
        ("core.services.account_wipe.messaging_repo.list_owner_room_ids", messaging_list_rooms),
        ("core.services.account_wipe.messaging_repo.get_room", messaging_get_room),
        ("core.services.account_wipe.messaging_repo.delete_room_partition", messaging_delete_room),
        ("core.services.account_wipe.messaging_repo.delete_owner_partition", messaging_delete_owner),
        ("core.services.account_wipe.voice_onboarding.release_number", phone_release),
        ("core.services.workspace_memory.delete_person_memory", person_memory_delete),
        ("core.services.account_wipe.clerk_admin.delete_user", clerk_delete),
        ("core.services.account_wipe.delete_gate_token_rows", gate_tokens_delete),
        ("core.services.account_wipe.wipe_engine_state", engine_wipe),
        ("core.services.account_wipe.list_owner_secret_names", owner_secrets_list),
        ("core.services.account_wipe.delete_owner_secrets", owner_secrets_delete),
        ("core.services.account_wipe.integrations_repo.list_for_owner", integrations_list),
        ("core.services.account_wipe.integrations_repo.delete_all_for_owner", integrations_delete),
        ("core.services.account_wipe.revoke_user_oauth", oauth_revoke),
        ("core.services.account_wipe.oauth_state_repo.delete_all_for_owner", oauth_state_delete),
        ("core.services.account_wipe.agent_email_repo.query_by_owner", agent_email_query),
        ("core.services.account_wipe.agent_email_repo.delete_all_for_owner", agent_email_delete),
        ("core.services.account_wipe.slack_installs_repo.list_for_owner", slack_installs_list),
        ("core.services.account_wipe.slack_installs_repo.delete_all_for_owner", slack_installs_delete),
        ("core.services.account_wipe.company_slug_repo.get_by_owner", slug_get),
        ("core.services.account_wipe.company_slug_repo.delete_for_owner", slug_delete),
        ("core.services.account_wipe.openrouter_key_repo.get", openrouter_get),
        ("core.services.account_wipe.openrouter_key_repo.mark_retiring", openrouter_retire),
        ("core.services.account_wipe.voice_onboarding.held_number", held_number),
        ("core.services.account_wipe._delete_fast_gate_row", fast_gate_delete),
    ]
    with ExitStack() as _stack:
        for _target, _new in _patches:
            _stack.enter_context(patch(_target, new=_new))
        yield SimpleNamespace(
            user_get=user_get,
            user_delete=user_delete,
            clerk_get_user=clerk_get_user,
            container_get=container_get,
            identities=identities,
            paperclip_get_identity=paperclip_get_identity,
            paperclip_row_delete=paperclip_row_delete,
            pending_brief_get=pending_brief_get,
            pending_brief_delete=pending_brief_delete,
            paperclip_repo_instance=paperclip_repo_instance,
            paperclip_repo_cls=paperclip_repo_cls,
            billing_get=billing_get,
            billing_delete=billing_delete,
            delete_workspace_runtime=delete_workspace_runtime_fn,
            voice_wipe=voice_wipe,
            bedrock_aips_delete=bedrock_aips_delete,
            bedrock_role_delete=bedrock_role_delete,
            metronome_archive=metronome_archive,
            stripe_delete=stripe_delete,
            company_stripe_deauthorize=company_stripe_deauthorize,
            billing_clear_company_stripe=billing_clear_company_stripe,
            usage_delete=usage_delete,
            messaging_list_rooms=messaging_list_rooms,
            messaging_get_room=messaging_get_room,
            messaging_delete_room=messaging_delete_room,
            messaging_delete_owner=messaging_delete_owner,
            phone_release=phone_release,
            person_memory_delete=person_memory_delete,
            clerk_delete=clerk_delete,
            owner_secrets_delete=owner_secrets_delete,
            integrations_delete=integrations_delete,
            oauth_state_delete=oauth_state_delete,
            oauth_revoke=oauth_revoke,
            agent_email_delete=agent_email_delete,
            slack_installs_list=slack_installs_list,
            slack_installs_delete=slack_installs_delete,
            slug_delete=slug_delete,
            gate_tokens_delete=gate_tokens_delete,
            engine_wipe=engine_wipe,
            openrouter_get=openrouter_get,
            openrouter_retire=openrouter_retire,
            held_number=held_number,
            fast_gate_delete=fast_gate_delete,
        )


# ---------------------------------------------------------------------------
# wipe_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_parked_members_runtime_step_says_scheduled(wipe_mocks):
    async def _parked(owner_id, *, delete_profiles=False, report=None):
        report["runtime"] = "scheduled"

    wipe_mocks.delete_workspace_runtime.side_effect = _parked
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")
    (step,) = [s for s in report.steps if s.system == "runtime"]
    assert step.status == "ok" and "teardown scheduled" in step.detail


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

    wipe_mocks.delete_workspace_runtime.assert_awaited_once_with(OWNER_ID, delete_profiles=True, report={})
    wipe_mocks.voice_wipe.assert_awaited_once_with(OWNER_ID)
    # The Bedrock footprint is NOT the wipe's job any more — delete_user_service
    # reaps it at the seam. Stubbing that method (above) is what stops these.
    wipe_mocks.bedrock_aips_delete.assert_not_awaited()
    wipe_mocks.bedrock_role_delete.assert_not_awaited()
    wipe_mocks.metronome_archive.assert_awaited_once_with("met_abc123")
    wipe_mocks.stripe_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.company_stripe_deauthorize.assert_awaited_once_with(account_id="acct_xyz789")
    wipe_mocks.billing_clear_company_stripe.assert_awaited_once_with(OWNER_ID, account_id="acct_xyz789")
    wipe_mocks.usage_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.messaging_list_rooms.assert_awaited_once_with(OWNER_ID)
    assert [call.args for call in wipe_mocks.messaging_get_room.await_args_list] == [
        ("room-1",),
        ("room-2",),
    ]
    assert [call.args for call in wipe_mocks.messaging_delete_room.await_args_list] == [
        ("room-1",),
        ("room-2",),
    ]
    wipe_mocks.messaging_delete_owner.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.phone_release.assert_awaited_once_with(OWNER_ID)  # gate 13: the number's PHONE# line row
    wipe_mocks.paperclip_row_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.billing_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.user_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.oauth_revoke.assert_awaited_once_with(user_id=OWNER_ID)


@pytest.mark.asyncio
async def test_person_memory_is_its_own_step_and_fails_alone(wipe_mocks):
    """The person's sessions, drafts, episodes and computer keys go with the account."""
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")
    step = next(step for step in report.steps if step.system == "person_memory")
    assert (step.action, step.status) == ("delete_person_memory", "ok")
    wipe_mocks.person_memory_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.person_memory_delete.side_effect = RuntimeError("memory down")
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")
    assert next(s for s in report.steps if s.system == "person_memory").status == "failed"


@pytest.mark.asyncio
async def test_messaging_wipe_keeps_owner_catalog_after_partial_failure_for_retry(wipe_mocks):
    wipe_mocks.messaging_delete_room.side_effect = [2, RuntimeError("delete failed")]

    first = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert next(step for step in first.steps if step.system == "messaging").status == "failed"
    wipe_mocks.messaging_delete_owner.assert_not_awaited()

    wipe_mocks.messaging_delete_room.reset_mock(side_effect=True)
    wipe_mocks.messaging_delete_room.return_value = 2
    second = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert next(step for step in second.steps if step.system == "messaging").status == "ok"
    assert [call.args for call in wipe_mocks.messaging_delete_room.await_args_list] == [
        ("room-1",),
        ("room-2",),
    ]
    wipe_mocks.messaging_delete_owner.assert_awaited_once_with(OWNER_ID)


@pytest.mark.asyncio
async def test_messaging_wipe_refuses_cross_owner_catalog_entry(wipe_mocks):
    wipe_mocks.messaging_get_room.side_effect = [
        {"ownerId": OWNER_ID},
        {"ownerId": "user_other"},
    ]

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = next(step for step in report.steps if step.system == "messaging")
    assert step.status == "failed"
    assert "owned elsewhere" in (step.error or "")
    wipe_mocks.messaging_delete_room.assert_awaited_once_with("room-1")
    wipe_mocks.messaging_delete_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_wipe_delegates_the_bedrock_footprint_to_the_seam(wipe_mocks):
    """The wipe owns no Bedrock steps of its own any more.

    It used to, and they were the ONLY teardown path that reaped: the
    entitlement-cancellation webhook and both /debug teardowns leaked a role
    and its inference profiles on every use, into the same account-wide IAM
    quota (dev, staging and prod share one AWS account). The reap moved to
    ``ecs_manager.delete_user_service`` — the seam all four paths cross — so no
    path can forget it and there is one implementation instead of two.

    That also fixed a bug this suite could not see: the old role step was gated
    on ``bedrock_role_arn`` being present on the upfront snapshot, so a legacy
    row without the field silently skipped the delete and stranded the role.

    Delegation is asserted by absence — no bedrock systems in the report, and
    no direct calls, because delete_user_service is stubbed here."""
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    systems = {s.system for s in report.steps}
    assert "bedrock_aips" not in systems
    assert "bedrock_role" not in systems
    wipe_mocks.bedrock_aips_delete.assert_not_awaited()
    wipe_mocks.bedrock_role_delete.assert_not_awaited()

    # The seam is reached exactly once, and it is what carries the reap.
    wipe_mocks.delete_workspace_runtime.assert_awaited_once_with(OWNER_ID, delete_profiles=True, report={})

    # The containers row is still read exactly TWICE for the whole wipe: the
    # upfront identifier snapshot, and the gate-token step's strongly-consistent
    # re-read. Dropping the two steps added no read and removed none.
    assert [c.args for c in wipe_mocks.container_get.await_args_list] == [(OWNER_ID,), (OWNER_ID,)]
    assert [c.kwargs for c in wipe_mocks.container_get.await_args_list] == [{}, {"consistent": True}]


@pytest.mark.asyncio
async def test_early_step_failure_does_not_abort_later_steps(wipe_mocks):
    """(b) THE key property: a collaborator raising leaves that one step
    "failed" but every later step -- especially the Clerk delete, last --
    still runs."""
    wipe_mocks.delete_workspace_runtime.side_effect = RuntimeError("ECS API down")

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["runtime"].status == "failed"
    assert "ECS API down" in (by_system["runtime"].error or "")

    # Every step after the failure still ran and succeeded.
    for system in ("metronome", "usage_counters", "messaging"):
        assert by_system[system].status == "ok", f"{system} should still have run: {by_system[system]}"

    # "stripe" covers two steps (Step 8 delete_stripe_customer, Step 8b
    # deauthorize_company_account) -- disambiguate by action so both are
    # independently checked instead of one silently shadowing the other.
    stripe_steps = {s.action: s for s in report.steps if s.system == "stripe"}
    assert stripe_steps["delete_stripe_customer"].status == "ok"
    assert stripe_steps["deauthorize_company_account"].status == "ok"

    # Clerk — last step — must still have been called despite the earlier failure.
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)
    assert by_system["clerk_user"].status == "ok"

    # The step order itself is unaffected by the failure.
    assert [s.system for s in report.steps] == EXPECTED_ORDER


@pytest.mark.asyncio
async def test_skips_stripe_when_no_stripe_customer_id(wipe_mocks):
    """(c) trial_local shape: billing row present but no stripe_customer_id
    -> skipped, and delete_stripe_customer is never called. The sibling
    company-account step is independent (company_stripe_account_id is still
    on the row), so it still runs -- two steps share the "stripe" system name,
    disambiguated here by action."""
    wipe_mocks.billing_get.return_value = _billing_row(stripe_customer_id=None)

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    stripe_steps = {s.action: s for s in report.steps if s.system == "stripe"}
    assert stripe_steps["delete_stripe_customer"].status == "skipped"
    wipe_mocks.stripe_delete.assert_not_awaited()
    assert stripe_steps["deauthorize_company_account"].status == "ok"
    by_system = {s.system: s for s in report.steps}
    # Metronome is independent — still ran since metronome_customer_id is present.
    assert by_system["metronome"].status == "ok"


@pytest.mark.asyncio
async def test_skips_metronome_when_no_metronome_customer_id(wipe_mocks):
    wipe_mocks.billing_get.return_value = _billing_row(metronome_customer_id=None)

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["metronome"].status == "skipped"
    wipe_mocks.metronome_archive.assert_not_awaited()
    # Disambiguate by action -- "stripe" covers Step 8 (delete_stripe_customer)
    # and Step 8b (deauthorize_company_account); both are independent of
    # metronome_customer_id, and are still on the row here (default fixture).
    stripe_steps = {s.action: s for s in report.steps if s.system == "stripe"}
    assert stripe_steps["delete_stripe_customer"].status == "ok"


@pytest.mark.asyncio
async def test_skips_billing_dependent_steps_when_no_billing_row(wipe_mocks):
    wipe_mocks.billing_get.return_value = None

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["metronome"].status == "skipped"
    assert by_system["billing_row"].status == "skipped"
    wipe_mocks.billing_delete.assert_not_awaited()
    # No billing row -> both "stripe"-system steps skip independently.
    stripe_steps = {s.action: s for s in report.steps if s.system == "stripe"}
    assert stripe_steps["delete_stripe_customer"].status == "skipped"
    assert stripe_steps["deauthorize_company_account"].status == "skipped"
    # Non-billing steps still ran normally.
    assert by_system["clerk_user"].status == "ok"


@pytest.mark.asyncio
async def test_skips_paperclip_user_steps_when_no_identity_row(wipe_mocks):
    """No identity row -> paperclip_row still deletes by key (idempotent to delete) and the rest of the wipe
    still runs.

    paperclip_row itself does NOT skip: its pending-brief delete is attempted
    unconditionally (the snapshot predates every step), so the step
    runs and reports that it found nothing."""
    wipe_mocks.identities.pop(OWNER_ID)

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["paperclip_row"].status == "ok"
    assert by_system["paperclip_row"].detail == "no paperclip-companies or pending_brief row"
    wipe_mocks.paperclip_row_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.pending_brief_delete.assert_awaited_once_with(OWNER_ID)
    assert by_system["runtime"].status == "ok"
    assert by_system["clerk_user"].status == "ok"


@pytest.mark.asyncio
async def test_skips_container_dependent_steps_when_no_containers_row(wipe_mocks):
    """No containers row: the seam still runs for its owner-keyed cleanups (a re-run after a late failure
    finds the row already gone); only the row-named handles have nothing to sweep."""
    wipe_mocks.container_get.return_value = None

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    by_system = {s.system: s for s in report.steps}
    assert by_system["runtime"].status == "ok"
    assert by_system["runtime"].detail.startswith("no containers row; owner-keyed cleanup done")
    assert "bedrock_aips" not in by_system
    assert "bedrock_role" not in by_system
    # The gate-token step always runs (it re-reads the row itself) but finds
    # nothing to delete when there is no row at all.
    assert by_system["bedrock_gate_tokens"].status == "ok"
    assert by_system["bedrock_gate_tokens"].detail == "no bedrock-gate token hash on containers row"
    wipe_mocks.gate_tokens_delete.assert_not_awaited()
    wipe_mocks.delete_workspace_runtime.assert_awaited_once_with(OWNER_ID, delete_profiles=True, report={})
    # EFS workspace-dir delete is unconditional -- not gated on the containers row.


@pytest.mark.asyncio
async def test_clerk_delete_still_runs_when_every_other_step_fails(wipe_mocks):
    """Extreme partial-failure case: every collaborator before Clerk raises.
    Clerk delete must still run and the report must still be returned."""
    wipe_mocks.delete_workspace_runtime.side_effect = RuntimeError("ecs down")
    wipe_mocks.bedrock_aips_delete.side_effect = RuntimeError("bedrock aip down")
    wipe_mocks.bedrock_role_delete.side_effect = RuntimeError("bedrock role down")
    wipe_mocks.metronome_archive.side_effect = RuntimeError("metronome down")
    wipe_mocks.stripe_delete.side_effect = RuntimeError("stripe down")
    wipe_mocks.company_stripe_deauthorize.side_effect = RuntimeError("company stripe down")
    wipe_mocks.usage_delete.side_effect = RuntimeError("usage down")
    wipe_mocks.gate_tokens_delete.side_effect = RuntimeError("gate tokens down")
    wipe_mocks.paperclip_row_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.billing_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.user_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.owner_secrets_delete.side_effect = RuntimeError("sm down")
    wipe_mocks.integrations_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.oauth_state_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.oauth_revoke.side_effect = RuntimeError("openai down")
    wipe_mocks.agent_email_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.slack_installs_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.slug_delete.side_effect = RuntimeError("ddb down")
    wipe_mocks.messaging_list_rooms.side_effect = RuntimeError("ddb down")
    wipe_mocks.person_memory_delete.side_effect = RuntimeError("memory down")
    wipe_mocks.engine_wipe.side_effect = RuntimeError("temporal down")
    wipe_mocks.openrouter_get.side_effect = RuntimeError("ddb down")
    wipe_mocks.fast_gate_delete.side_effect = RuntimeError("ddb down")

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert [s.system for s in report.steps] == EXPECTED_ORDER
    assert all(s.status == "failed" for s in report.steps[:-1])
    assert report.steps[-1].system == "clerk_user"
    assert report.steps[-1].status == "ok"
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)


@pytest.mark.asyncio
async def test_preview_and_execute_agree(wipe_mocks):
    """Same systems, same order."""
    preview = await preview_wipe(OWNER_ID)
    executed = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert [(s.system, s.action) for s in preview.steps] == [(s.system, s.action) for s in executed.steps]


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

    wipe_mocks.delete_workspace_runtime.assert_not_awaited()
    wipe_mocks.bedrock_aips_delete.assert_not_awaited()
    wipe_mocks.bedrock_role_delete.assert_not_awaited()
    wipe_mocks.metronome_archive.assert_not_awaited()
    wipe_mocks.stripe_delete.assert_not_awaited()
    wipe_mocks.company_stripe_deauthorize.assert_not_awaited()
    wipe_mocks.billing_clear_company_stripe.assert_not_awaited()
    wipe_mocks.usage_delete.assert_not_awaited()
    wipe_mocks.paperclip_row_delete.assert_not_awaited()
    wipe_mocks.pending_brief_delete.assert_not_awaited()
    wipe_mocks.gate_tokens_delete.assert_not_awaited()
    wipe_mocks.billing_delete.assert_not_awaited()
    wipe_mocks.user_delete.assert_not_awaited()
    wipe_mocks.clerk_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_wipe_would_delete_everything_present(wipe_mocks):
    wipe_mocks.openrouter_get.return_value = {"owner_id": OWNER_ID, "state": "active"}
    with patch("core.services.account_wipe.settings.OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas"):
        report = await preview_wipe(OWNER_ID)

    by_system = {s.system: s for s in report.steps}
    for system in EXPECTED_ORDER:
        assert by_system[system].status == "would_delete", f"{system}: {by_system[system]}"


@pytest.mark.asyncio
async def test_preview_wipe_skips_absent_resources(wipe_mocks):
    wipe_mocks.identities.pop(OWNER_ID)
    wipe_mocks.container_get.return_value = None
    wipe_mocks.billing_get.return_value = None
    wipe_mocks.user_get.return_value = None

    report = await preview_wipe(OWNER_ID)

    assert [s.system for s in report.steps] == EXPECTED_ORDER
    by_system = {s.system: s for s in report.steps}
    assert by_system["runtime"].status == "skipped"

    assert by_system["bedrock_gate_tokens"].status == "skipped"
    assert "bedrock_aips" not in by_system
    assert "bedrock_role" not in by_system
    assert by_system["metronome"].status == "skipped"
    # No billing row -> both "stripe"-system preview steps skip independently.
    stripe_steps = {s.action: s for s in report.steps if s.system == "stripe"}
    assert stripe_steps["delete_stripe_customer"].status == "skipped"
    assert stripe_steps["deauthorize_company_account"].status == "skipped"
    assert by_system["paperclip_row"].status == "skipped"
    assert by_system["billing_row"].status == "skipped"
    assert by_system["user_row"].status == "skipped"
    # Owner-partition sweeps and Clerk have no cheap existence check -- always would_delete.
    assert by_system["usage_counters"].status == "would_delete"
    assert by_system["messaging"].status == "would_delete"
    assert by_system["clerk_user"].status == "would_delete"


@pytest.mark.asyncio
async def test_preview_wipe_malformed_id_raises(wipe_mocks):
    with pytest.raises(MalformedUserIdError):
        await preview_wipe("not_a_user")
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
# Dataclass shape
# ---------------------------------------------------------------------------


def test_wipe_report_to_dict_shape():
    report = WipeReport(
        owner_id=OWNER_ID,
        dry_run=True,
        steps=[
            WipeStep(system="runtime", action="delete_workspace_runtime", status="would_delete", detail="x"),
            WipeStep(system="clerk_user", action="delete_user", status="failed", error="boom"),
        ],
    )
    d = report.to_dict()
    assert d == {
        "owner_id": OWNER_ID,
        "dry_run": True,
        "steps": [
            {
                "system": "runtime",
                "action": "delete_workspace_runtime",
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
        # WorkspaceRepo resolves its table via core.dynamodb.get_table (prefix
        # applied at call time); bedrock_gate_tokens takes the explicit env
        # override. Same wiring as tests/unit/billing/test_bedrock_gate_tokens.py.
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "test-gate-tokens")
        wipe_mocks.gate_tokens_delete.side_effect = real_delete_gate_token_rows
        real_repo = WorkspaceRepo()
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
    """A pending brief where it lives since the switch retired: the owner's tasks-table PENDING_BRIEF row."""
    repo = WorkspaceRepo()
    item = {"user_id": owner_id, "kind": "pending_brief", "brief_state": "pending", "owner_id": owner_id}
    repo._ws_table().put_item(
        Item={**workspace_repo._meta_item(item, owner_id, ts.PENDING_BRIEF_SK), **repo._pending_key(owner_id)}
    )


def _paperclip_keys(client) -> set[str]:
    """Old-table keys plus each surviving pending brief, named as before (``__pending_brief__:{owner}``)."""
    old = {i["user_id"]["S"] for i in client.scan(TableName="test-paperclip-companies")["Items"]}
    tasks = client.scan(TableName="test-tasks")["Items"] if "test-tasks" in client.list_tables()["TableNames"] else []
    briefs = {
        f"__pending_brief__:{i['pk']['S'].split('#', 1)[1]}" for i in tasks if i["sk"]["S"] == ts.PENDING_BRIEF_SK
    }
    return old | briefs


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

    async def _delete_user_service(owner_id, *, delete_profiles=False, report=None):
        surviving_at_ecs_teardown.extend(sorted(_gate_rows(real_ddb)))

    wipe_mocks.delete_workspace_runtime.side_effect = _delete_user_service

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert surviving_at_ecs_teardown == [], (
        f"gate-token rows still live when the containers row was destroyed: {surviving_at_ecs_teardown}"
    )
    systems = [s.system for s in report.steps]
    assert systems.index("bedrock_gate_tokens") < systems.index("runtime")


@pytest.mark.asyncio
async def test_deletes_a_gate_token_hash_the_up_front_snapshot_never_saw(wipe_mocks, real_ddb):
    """Gap 1, staleness. ``_read_identifiers`` reads the containers row
    eventually consistently AND before the Paperclip steps run, so a mint that
    landed just before the wipe — or during it — is simply not in the snapshot.
    The step re-reads strongly consistently and deletes the UNION.

    Union, not replacement: a swap that lost its race drops hashes from the
    tracked list while their rows stay live (``_swap_hash_pointer`` logs exactly
    that), so the snapshot can be the only thing still naming them."""
    for h in ("hash-current", "hash-old-1", "hash-old-2", "hash-minted-mid-wipe"):
        _put_gate_row(real_ddb, h)
    _put_gate_row(real_ddb, "hash-someone-else", owner_id="user_other")

    # 1st read = the stale up-front snapshot; 2nd = the gate-token step's
    # strongly-consistent re-read, by which time a mint has rotated the pointer
    # and dropped two old hashes from tracking.
    wipe_mocks.container_get.side_effect = [
        _container_row(),
        _container_row(
            bedrock_gate_token_hash="hash-minted-mid-wipe",
            bedrock_gate_superseded_hashes=["hash-current"],
        ),
    ]

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = {s.system: s for s in report.steps}["bedrock_gate_tokens"]
    assert step.status == "ok", step
    assert "deleted 4 of 4" in step.detail
    assert _gate_rows(real_ddb) == {"hash-someone-else"}


@pytest.mark.asyncio
async def test_final_sweep_deletes_a_hash_minted_after_the_gate_token_step(wipe_mocks, real_ddb):
    """Gap 1, the residual window. Nothing stops a mint landing between the
    gate-token step's re-read and the containers row's death; at that point the
    row is the only thing naming the new hash and the next instruction destroys
    it. ``delete_user_service`` hands back the DELETE's own ALL_OLD image, and
    the runtime step sweeps whatever it still named."""
    _put_gate_row(real_ddb, "hash-current")
    _put_gate_row(real_ddb, "hash-minted-after-the-step")
    _put_gate_row(real_ddb, "hash-someone-else", owner_id="user_other")

    async def _delete_user_service(owner_id, *, delete_profiles=False, report=None):
        return _container_row(
            bedrock_gate_token_hash="hash-minted-after-the-step",
            bedrock_gate_superseded_hashes=["hash-current"],
        )

    wipe_mocks.delete_workspace_runtime.side_effect = _delete_user_service

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert _gate_rows(real_ddb) == {"hash-someone-else"}
    ecs_step = {s.system: s for s in report.steps}["runtime"]
    assert ecs_step.status == "ok", ecs_step
    assert "swept 1 gate-token row(s)" in ecs_step.detail


@pytest.mark.asyncio
async def test_gate_token_step_is_a_clean_no_op_for_a_non_bedrock_owner(wipe_mocks):
    """chatgpt_oauth / byo_key owners never mint a gate token — their
    containers row carries neither attribute. That is a clean no-op, not a
    failure, and nothing is deleted."""
    row = _container_row()
    row.pop("bedrock_gate_token_hash")
    row.pop("bedrock_gate_superseded_hashes")
    wipe_mocks.container_get.return_value = row

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    step = {s.system: s for s in report.steps}["bedrock_gate_tokens"]
    assert step.status == "ok"
    assert step.detail == "no bedrock-gate token hash on containers row"
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
async def test_wipe_deletes_a_pending_brief_written_after_the_snapshot(wipe_mocks, real_ddb):
    """Gap 2, staleness. The snapshot is taken before the Paperclip steps run,
    so an onboarding submission that finishes during the wipe leaves
    ``ids.pending_brief`` None while its item sits in the table. Gating the
    delete on that snapshot recreated the exact residue this step removes; the
    delete is idempotent, so it is always attempted."""
    _put_pending_brief(real_ddb, "user_other")

    # The submission lands mid-wipe — hung off the container teardown, which is
    # simply a step known to run after the up-front snapshot and before this one.
    async def _teardown_that_races_a_submission(owner_id, *, delete_profiles=False, report=None):
        _put_pending_brief(real_ddb, OWNER_ID)

    wipe_mocks.delete_workspace_runtime.side_effect = _teardown_that_races_a_submission

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert _paperclip_keys(real_ddb) == {"__pending_brief__:user_other"}
    step = {s.system: s for s in report.steps}["paperclip_row"]
    assert step.status == "ok", step
    # Reported off what DynamoDB actually removed, not off the snapshot, which
    # said there was nothing there.
    assert "pending_brief row deleted" in step.detail


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


def _paperclip_and_aurora_gone():
    """The RDS Data API and the Paperclip admin client both raise: the server and its
    Aurora database are gone (Paperclip exit, task 8b.7)."""
    real_client = boto3.client

    def _client(service, *args, **kwargs):
        if service == "rds-data":
            raise RuntimeError("aurora is gone")
        return real_client(service, *args, **kwargs)

    stack = ExitStack()
    stack.enter_context(patch("boto3.client", side_effect=_client))
    return stack


@pytest.mark.asyncio
async def test_account_wipe_no_paperclip_steps(wipe_mocks):
    with _paperclip_and_aurora_gone():
        preview = await preview_wipe(OWNER_ID)
        report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    for r in (preview, report):
        assert not {s.system for s in r.steps} & {"paperclip_company", "paperclip_aurora_user"}
    assert [s for s in report.steps if s.status == "failed"] == []
    wipe_mocks.paperclip_row_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.user_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.clerk_delete.assert_awaited_once_with(OWNER_ID)


@pytest.mark.asyncio
async def test_wipe_account_cancels_a_spark_subscription_before_deleting_the_users_row(wipe_mocks, monkeypatch):
    import stripe

    order = []
    wipe_mocks.user_get.return_value = _user_row(os_subscription_id="sub_1", os_subscription_status="trialing")
    monkeypatch.setattr(stripe.Subscription, "cancel", lambda sub_id, **kw: order.append(("cancel", sub_id)))
    wipe_mocks.user_delete.side_effect = lambda owner_id: order.append(("delete", owner_id))
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")
    (step,) = [s for s in report.steps if s.system == "user_row"]
    assert step.status == "ok" and order == [("cancel", "sub_1"), ("delete", OWNER_ID)]


@pytest.mark.asyncio
async def test_wipe_account_keeps_the_users_row_when_the_spark_cancel_fails(wipe_mocks, monkeypatch):
    import stripe

    def down(*a, **kw):
        raise stripe.APIConnectionError("stripe down")

    wipe_mocks.user_get.return_value = _user_row(os_subscription_id="sub_1", os_subscription_status="active")
    monkeypatch.setattr(stripe.Subscription, "cancel", down)
    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")
    (step,) = [s for s in report.steps if s.system == "user_row"]
    assert step.status == "failed" and "stripe down" in step.error
    wipe_mocks.user_delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id", ["", "user_", "user_a/b", "../x", "user_a b", "org_456"])
async def test_a_malformed_id_is_refused_before_any_read_or_delete(wipe_mocks, owner_id):
    """Every step deletes by this id or a prefix built from it: anything but a Clerk user id is refused first."""
    with pytest.raises(MalformedUserIdError):
        await wipe_account(owner_id, actor_admin_id="clerk:user.deleted", clerk_user_deleted=True)
    wipe_mocks.user_get.assert_not_awaited()
    wipe_mocks.container_get.assert_not_awaited()
    wipe_mocks.delete_workspace_runtime.assert_not_awaited()
    wipe_mocks.user_delete.assert_not_awaited()
    wipe_mocks.messaging_delete_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_well_formed_user_id_proceeds(wipe_mocks):
    report = await wipe_account("user_2abcXYZ09", actor_admin_id="admin_1")
    assert report.steps and report.owner_id == "user_2abcXYZ09"
