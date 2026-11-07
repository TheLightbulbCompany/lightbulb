"""Tests for paperclip_repo using moto's DynamoDB mock.

The repo is async (matches the pattern of other repos in
``core/repositories/``); ``asyncio_mode = "auto"`` in pyproject.toml means
``async def`` test functions are auto-marked, so no explicit
``@pytest.mark.asyncio`` decorator is needed.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core import tasks_schema as ts
from core.repositories.paperclip_repo import (
    FunnelClaimResult,
    PaperclipIdentity,
    PaperclipRepo,
    PaperclipWorkspace,
    from_meta_item,
)

TABLE_NAME = "test-paperclip-companies"


def create_companies_table(resource, name: str = TABLE_NAME):
    """The paperclip-companies table with the prod GSIs (database-stack.ts)."""
    return resource.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "scheduled_purge_at", "AttributeType": "S"},
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "company_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by-status-purge-at",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "scheduled_purge_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
            {
                # Used by org_company_ids() (the duplicate-company guard).
                "IndexName": "by-org-id",
                "KeySchema": [
                    {"AttributeName": "org_id", "KeyType": "HASH"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                # Mirrors the prod table (database-stack.ts). Load-bearing
                # for the seed-claim / failed-marker tests: DynamoDB
                # rejects any PutItem whose company_id is an empty STRING
                # once this index exists, so those writes must OMIT the
                # attribute — a table without this GSI cannot catch that.
                "IndexName": "by-company-id",
                "KeySchema": [
                    {"AttributeName": "company_id", "KeyType": "HASH"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["status"],
                },
            },
        ],
    )


def create_tasks_table(resource):
    """The `tasks` table as `tasks_schema` defines it (keys + GSIs, projection ALL like api-stack.ts)."""
    attrs = {"pk", "sk"} | {a for g in ts.GSIS for a in (g.pk_attr, g.sk_attr)}
    return resource.create_table(
        TableName="tasks",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
        GlobalSecondaryIndexes=[
            {
                "IndexName": g.name,
                "KeySchema": [
                    {"AttributeName": g.pk_attr, "KeyType": "HASH"},
                    {"AttributeName": g.sk_attr, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for g in ts.GSIS
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def raw_workspace(repo, org_id: str) -> dict | None:
    """The stored workspace row in snake_case, from whichever store the repo writes."""
    if repo._meta_on_tasks():
        item = repo._ws_table().get_item(Key=repo._ws_key(org_id)).get("Item")
        return from_meta_item(item) if item else None
    return repo._table().get_item(Key={"user_id": org_id}).get("Item")


@pytest.fixture(params=["paperclip", "tasks"])
def repo(request):
    """Every test runs against both META stores (wave1-R Task 6): the old table, and `tasks`."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        create_tasks_table(resource)
        create_companies_table(resource)
        # Patch the shared dynamodb resource so the repo's get_table()
        # call resolves to this moto-backed resource.
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
            patch.object(PaperclipRepo, "_meta_on_tasks", lambda self: request.param == "tasks"),
        ):
            yield PaperclipRepo(table_name=TABLE_NAME, region="us-east-1")


def _make_company(user_id="u1", status="active", **kwargs):
    """A WORKSPACE row keyed ``user_id`` (the key an org row carries under the
    ``user_id`` attribute)."""
    now = datetime.now(timezone.utc)
    return PaperclipWorkspace(
        org_id=kwargs.get("org_id", user_id),
        company_id=kwargs.get("company_id", f"co_{user_id}"),
        status=status,
        created_at=kwargs.get("created_at", now),
        updated_at=kwargs.get("updated_at", now),
        provisioner_user_id=kwargs.get("provisioner_user_id", ""),
        member_user_ids=kwargs.get("member_user_ids", set()),
        last_error=kwargs.get("last_error"),
        scheduled_purge_at=kwargs.get("scheduled_purge_at"),
        seeded_agent_ids=kwargs.get("seeded_agent_ids"),
        starter_agent_ids=kwargs.get("starter_agent_ids"),
        onboarding_generation=kwargs.get("onboarding_generation"),
        funnel_state=kwargs.get("funnel_state"),
        funnel_submitted_at=kwargs.get("funnel_submitted_at"),
        funnel_completed_at=kwargs.get("funnel_completed_at"),
        onboarding_goal_id=kwargs.get("onboarding_goal_id"),
        onboarding_project_id=kwargs.get("onboarding_project_id"),
    )


async def test_seeded_agent_ids_round_trip(repo):
    await repo.put_workspace(_make_company(user_id="user_seed", seeded_agent_ids=["friday_id", "atlas_id"]))
    retrieved = await repo.get_workspace("user_seed")
    assert retrieved.seeded_agent_ids == ["friday_id", "atlas_id"]


async def test_seeded_agent_ids_absent_round_trips_as_none(repo):
    await repo.put_workspace(_make_company(user_id="user_noseed"))
    retrieved = await repo.get_workspace("user_noseed")
    assert retrieved.seeded_agent_ids is None


async def test_put_condition_passes_when_generation_absent_or_matching(repo):
    """Fix round 1 (Important #2): put()'s optional condition — used only at the G2 mint site —
    is (attribute_not_exists(onboarding_generation) OR onboarding_generation = :g). It must
    succeed on a genuinely fresh row (attribute absent) and on a self-retry with the SAME
    generation it already holds."""
    company = _make_company(user_id="user_cond", onboarding_generation="gen-a")
    await repo.put_workspace(
        company,
        condition="attribute_not_exists(onboarding_generation) OR onboarding_generation = :g",
        condition_values={":g": "gen-a"},
    )
    assert (await repo.get_workspace("user_cond")).onboarding_generation == "gen-a"

    # Self-retry with the identical generation still satisfies the condition.
    await repo.put_workspace(
        company,
        condition="attribute_not_exists(onboarding_generation) OR onboarding_generation = :g",
        condition_values={":g": "gen-a"},
    )
    assert (await repo.get_workspace("user_cond")).onboarding_generation == "gen-a"


async def test_put_condition_raises_when_a_different_generation_already_won(repo):
    """A peer already minted a DIFFERENT generation onto the row -- the conditioned put must
    fail (ClientError/ConditionalCheckFailedException), not silently clobber the winner."""
    from botocore.exceptions import ClientError

    await repo.put_workspace(_make_company(user_id="user_race", onboarding_generation="gen-winner"))

    losing_company = _make_company(user_id="user_race", company_id="co_loser", onboarding_generation="gen-loser")
    with pytest.raises(ClientError) as exc_info:
        await repo.put_workspace(
            losing_company,
            condition="attribute_not_exists(onboarding_generation) OR onboarding_generation = :g",
            condition_values={":g": "gen-loser"},
        )
    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    won = await repo.get_workspace("user_race")
    # On `tasks` both rows are one transaction: the failed META write moved no old-row attribute.
    assert (won.onboarding_generation, won.company_id) == ("gen-winner", "co_user_race")


async def test_starter_agent_ids_round_trip(repo):
    starter_ids = {
        "ceo": "agent-ceo",
        "engineering": "agent-eng",
        "finance": "agent-fin",
        "growth": "agent-growth",
        "analytics": "agent-analytics",
        "customer_operations": "agent-cx",
    }
    await repo.put_workspace(_make_company(user_id="user_starter", starter_agent_ids=starter_ids))

    assert (await repo.get_workspace("user_starter")).starter_agent_ids == starter_ids


async def test_legacy_seeded_row_reads_without_starter_ids(repo):
    await repo.put_workspace(_make_company(user_id="user_legacy", seeded_agent_ids=["friday_id", "atlas_id"]))

    retrieved = await repo.get_workspace("user_legacy")
    assert retrieved.seeded_agent_ids == ["friday_id", "atlas_id"]
    assert retrieved.starter_agent_ids is None


async def test_get_returns_none_for_missing(repo):
    assert await repo.get_workspace("user_does_not_exist") is None


async def test_update_status_transitions_to_disabled(repo):
    await repo.put_workspace(_make_company(user_id="user_456"))
    purge_at = datetime(2026, 5, 27, tzinfo=timezone.utc)
    await repo.update_status("user_456", status="disabled", scheduled_purge_at=purge_at)
    retrieved = await repo.get_workspace("user_456")
    assert retrieved.status == "disabled"
    assert retrieved.scheduled_purge_at == purge_at


async def test_update_status_records_last_error(repo):
    await repo.put_workspace(_make_company(user_id="user_x"))
    await repo.update_status("user_x", status="failed", last_error="api timeout")
    retrieved = await repo.get_workspace("user_x")
    assert retrieved.status == "failed"
    assert retrieved.last_error == "api timeout"


async def test_update_status_not_if_status_skips_when_guarded(repo):
    """``not_if_status`` must SKIP the write (return False) when the row is
    already in that status — used so a losing concurrent provision attempt
    can't downgrade an already-``active`` row to ``failed``."""
    await repo.put_workspace(_make_company(user_id="user_active", status="active"))
    landed = await repo.update_status("user_active", status="failed", last_error="late error", not_if_status="active")
    assert landed is False
    retrieved = await repo.get_workspace("user_active")
    assert retrieved.status == "active"  # NOT downgraded
    assert retrieved.last_error is None


async def test_update_status_not_if_status_applies_when_not_guarded(repo):
    """``not_if_status`` allows the write (return True) when the row is in any
    OTHER status — e.g. a genuinely ``provisioning`` row may still fail."""
    await repo.put_workspace(_make_company(user_id="user_prov", status="provisioning"))
    landed = await repo.update_status("user_prov", status="failed", last_error="boom", not_if_status="active")
    assert landed is True
    retrieved = await repo.get_workspace("user_prov")
    assert retrieved.status == "failed"
    assert retrieved.last_error == "boom"


async def test_delete_removes_row(repo):
    await repo.put_workspace(_make_company(user_id="user_789"))
    await repo.delete("user_789")
    assert await repo.get_workspace("user_789") is None


async def test_scan_purge_due_returns_overdue_disabled(repo):
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=1)
    future = now + timedelta(days=10)
    # disabled and overdue -> should be returned
    await repo.put_workspace(_make_company(user_id="overdue", status="disabled", scheduled_purge_at=past))
    # disabled but not yet due -> should NOT be returned
    await repo.put_workspace(_make_company(user_id="not_yet", status="disabled", scheduled_purge_at=future))
    # active -> should NOT be returned (different status)
    await repo.put_workspace(_make_company(user_id="still_active", status="active"))
    due = await repo.scan_purge_due(now)
    user_ids = {c.org_id for c in due}
    assert user_ids == {"overdue"}


# ---------------------------------------------------------------------------
# Per-owner seed claim (provision_org's cross-instance fresh-create fence)
# ---------------------------------------------------------------------------


async def test_seed_claim_fresh_wins_then_second_caller_loses(repo):
    """First caller wins the put-if-absent; a concurrent second caller loses
    while the claim is non-stale — the fence that stops two backend
    instances from both running the full Paperclip seed."""
    assert await repo.try_acquire_seed_claim("u1", claim_id="a") is True
    assert await repo.try_acquire_seed_claim("u1", claim_id="b") is False


async def test_seed_claim_steals_only_when_stale(repo):
    """A holder that crashed hard before any durable progress leaves a bare
    seeding row; once claimed_at is past the stale window it is re-claimable
    so the owner isn't stranded forever."""
    assert await repo.try_acquire_seed_claim("u1", claim_id="a") is True
    # Non-stale → loses.
    assert await repo.try_acquire_seed_claim("u1", claim_id="b", stale_after_seconds=120) is False
    # stale_after_seconds=0 → any existing claim is stale → steal succeeds.
    assert await repo.try_acquire_seed_claim("u1", claim_id="b", stale_after_seconds=0) is True


# ---------------------------------------------------------------------------
# Onboarding: put/get round-trip (writer safety depends on this exact shape).
# ---------------------------------------------------------------------------


async def test_onboarding_fields_round_trip(repo):
    now = datetime.now(timezone.utc)
    await repo.put_workspace(
        _make_company(
            user_id="user_onb",
            onboarding_generation="01ONB",
            funnel_state="submitted",
            funnel_submitted_at=now,
            onboarding_goal_id="goal-1",
            onboarding_project_id="proj-1",
        )
    )
    fetched = await repo.get_workspace("user_onb")
    assert fetched.onboarding_generation == "01ONB"
    assert fetched.funnel_state == "submitted"
    assert fetched.funnel_submitted_at == now
    assert fetched.onboarding_goal_id == "goal-1"
    assert fetched.onboarding_project_id == "proj-1"


async def test_onboarding_fields_absent_round_trip_as_none(repo):
    await repo.put_workspace(_make_company(user_id="user_no_onb"))
    fetched = await repo.get_workspace("user_no_onb")
    assert fetched.onboarding_generation is None
    assert fetched.funnel_state is None


async def test_put_without_onboarding_kwargs_wipes_previously_set_fields(repo):
    """Documents the exact hazard the writer-safety fix in
    paperclip_provisioning.py guards against: put() is a raw PutItem, so a
    workspace built without onboarding fields SILENTLY WIPES them on
    a row that previously had them. This is why every put site in
    provisioning must read-merge or deliberately mint."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01ONB", funnel_state="complete"))
    assert (await repo.get_workspace("u1")).funnel_state == "complete"

    # A put that forgot to carry onboarding fields forward...
    await repo.put_workspace(_make_company(user_id="u1", status="provisioning"))
    # ...wipes them. (This is the exact bug the provisioning module's
    # _carry_forward_onboarding_fields helper exists to prevent at every
    # call site — see tests/unit/services/test_paperclip_provisioning_moto.py for the
    # end-to-end proof that provision_org's real puts do NOT do this.)
    fetched = await repo.get_workspace("u1")
    assert fetched.onboarding_generation is None
    assert fetched.funnel_state is None


# ---------------------------------------------------------------------------
# resolve_owner_row, members, identity rows, company lookups
# ---------------------------------------------------------------------------


async def test_workspace_round_trips_with_members_and_provisioner(repo):
    await repo.put_workspace(
        _make_company(user_id="org_acme", provisioner_user_id="user_a", member_user_ids={"user_a", "user_b"})
    )
    ws = await repo.get_workspace("org_acme")
    assert ws.org_id == "org_acme" and ws.company_id == "co_org_acme"
    assert ws.provisioner_user_id == "user_a"
    assert ws.member_user_ids == {"user_a", "user_b"}
    raw = raw_workspace(repo, "org_acme")
    # Company facts only: no credential ever lands on a workspace row.
    assert raw["org_id"] == "org_acme"
    assert not {"paperclip_user_id", "paperclip_password_encrypted", "service_token_encrypted"} & set(raw)


async def test_resolve_owner_row_is_one_get_of_the_org_row(repo):
    await repo.put_workspace(_make_company(user_id="org_acme", onboarding_generation="01G"))
    resolved = await repo.resolve_owner_row("org_acme")
    assert resolved is not None and resolved.onboarding_generation == "01G"


async def test_resolve_owner_row_hides_non_active_unless_asked(repo):
    await repo.put_workspace(_make_company(user_id="org_y", status="failed"))
    assert await repo.resolve_owner_row("org_y") is None
    row = await repo.resolve_owner_row("org_y", include_inactive=True)
    assert row is not None and row.status == "failed"


async def test_add_and_remove_member_edit_only_the_set(repo):
    await repo.put_workspace(_make_company(user_id="org_acme", member_user_ids={"user_a"}))
    await repo.add_member("org_acme", "user_b")
    await repo.add_member("org_acme", "user_b")  # idempotent
    assert (await repo.get_workspace("org_acme")).member_user_ids == {"user_a", "user_b"}
    await repo.remove_member("org_acme", "user_a")
    await repo.remove_member("org_acme", "user_a")  # idempotent
    assert (await repo.get_workspace("org_acme")).member_user_ids == {"user_b"}


async def test_member_writes_never_create_a_workspace_row(repo):
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        await repo.add_member("org_ghost", "user_a")
    await repo.remove_member("org_ghost", "user_a")  # no-op, no raise
    assert await repo.get_workspace("org_ghost") is None


async def test_company_lookups_resolve_the_active_workspace(repo):
    await repo.put_workspace(_make_company(user_id="org_acme", company_id="co_1", member_user_ids={"user_b", "user_a"}))
    await repo.put_workspace(_make_company(user_id="org_gone", company_id="co_2", status="disabled"))
    assert (await repo.find_workspace_by_company("co_1")).org_id == "org_acme"
    assert await repo.get_user_ids_for_company("co_1") == ["user_a", "user_b"]
    assert await repo.find_workspace_by_company("co_2") is None
    assert await repo.get_user_ids_for_company("co_2") == []
    assert await repo.org_company_ids("org_acme") == {"co_1"}
    # Sentinels carry no org_id and never show up.
    await repo.put_pending_brief("org_new", path="p", user_input=None, detail=None, brief_text=None, submitted_by="u")
    assert await repo.org_company_ids("org_new") == set()


async def test_identity_round_trips_and_names_no_company(repo):
    await repo.put_identity(
        PaperclipIdentity(
            user_id="user_a",
            paperclip_user_id="pc_a",
            paperclip_password_encrypted="enc",
            paperclip_email="alice@example.com",
        )
    )
    identity = await repo.get_identity("user_a")
    assert (identity.paperclip_user_id, identity.paperclip_email, identity.status) == (
        "pc_a",
        "alice@example.com",
        None,
    )
    raw = repo._table().get_item(Key={"user_id": "user_a"})["Item"]
    assert not {"company_id", "org_id", "status", "service_token_encrypted"} & set(raw)
    assert await repo.get_identity("user_missing") is None
    # An identity row is never mistaken for a workspace.
    assert await repo.get_workspace("user_a") is None


async def test_put_identity_settles_a_seed_claim(repo):
    assert await repo.try_acquire_seed_claim("user_a", claim_id="c1")
    assert (await repo.get_identity("user_a")).status == "seeding"
    await repo.put_identity(PaperclipIdentity(user_id="user_a", paperclip_user_id="pc_a", paperclip_email="a@x"))
    raw = repo._table().get_item(Key={"user_id": "user_a"})["Item"]
    assert "status" not in raw and "seeding_claim_id" not in raw


async def test_seed_claim_is_identity_only(repo):
    """The claim names no org and no company, so it never enters the by-org-id
    or by-company-id indexes."""
    assert await repo.try_acquire_seed_claim("user_a", claim_id="c1")
    raw = repo._table().get_item(Key={"user_id": "user_a"}, ConsistentRead=True)["Item"]
    assert raw["status"] == "seeding" and "org_id" not in raw and "company_id" not in raw


async def test_seed_claim_never_overwrites_a_confirmed_identity(repo):
    await repo.put_identity(PaperclipIdentity(user_id="user_a", paperclip_user_id="pc_a", paperclip_email="a@x"))
    assert not await repo.try_acquire_seed_claim("user_a", claim_id="x", stale_after_seconds=0)
    assert (await repo.get_identity("user_a")).paperclip_user_id == "pc_a"


async def test_fail_signup_claim_is_fenced_on_the_holder(repo):
    """A stale holder must not downgrade a NEWER holder's stolen claim."""
    assert await repo.try_acquire_seed_claim("u_seed", claim_id="old")
    assert await repo.try_acquire_seed_claim("u_seed", claim_id="new", stale_after_seconds=0)
    assert not await repo.fail_signup_claim("u_seed", reason="late", claim_id="old")
    assert (await repo.get_identity("u_seed")).status == "seeding"
    assert await repo.fail_signup_claim("u_seed", reason="gave up", claim_id="new")
    assert (await repo.get_identity("u_seed")).status == "failed"
    # ...and the credential-less failed claim is reclaimable.
    assert await repo.try_acquire_seed_claim("u_seed", claim_id="retry")


async def test_fail_signup_claim_never_touches_a_confirmed_identity(repo):
    await repo.put_identity(PaperclipIdentity(user_id="user_a", paperclip_user_id="pc_a", paperclip_email="a@x"))
    assert not await repo.fail_signup_claim("user_a", reason="boom", claim_id=None)
    assert (await repo.get_identity("user_a")).status is None


async def test_failed_marker_is_a_company_less_workspace_row(repo):
    assert await repo.put_failed_marker_if_absent(org_id="org_x", reason="boom")
    ws = await repo.get_workspace("org_x")
    assert ws.status == "failed" and ws.company_id == "" and ws.last_error == "boom"
    # Never overwrites a real row.
    assert not await repo.put_failed_marker_if_absent(org_id="org_x", reason="again")


async def test_remove_starter_agent_id_removes_only_matching_entry(repo):
    await repo.put_workspace(
        _make_company(
            user_id="user_owner",
            org_id="org_acme",
            starter_agent_ids={"ceo": "agent-ceo", "finance": "agent-fin"},
        )
    )

    await repo.remove_starter_agent_id("org_acme", "agent-fin")

    assert (await repo.get_workspace("org_acme")).starter_agent_ids == {"ceo": "agent-ceo"}


async def test_remove_unknown_starter_agent_id_is_a_no_op(repo):
    starter_ids = {"ceo": "agent-ceo", "finance": "agent-fin"}
    await repo.put_workspace(_make_company(user_id="user_owner", starter_agent_ids=starter_ids))

    await repo.remove_starter_agent_id("user_owner", "agent-other")

    assert (await repo.get_workspace("user_owner")).starter_agent_ids == starter_ids


async def test_resolve_owner_row_returns_none_for_unprovisioned_org(repo):
    assert await repo.resolve_owner_row("org_ghost") is None


# ---------------------------------------------------------------------------
# claim_funnel_submission / record_onboarding_ids / revert_funnel_submission
# ---------------------------------------------------------------------------


async def test_claim_funnel_submission_wins_fresh(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    assert result is FunnelClaimResult.WON
    fetched = await repo.get_workspace("u1")
    assert fetched.funnel_state == "submitted"
    assert fetched.funnel_submitted_at is not None
    assert fetched.onboarding_submitted_by == "member_1"


async def test_claim_funnel_submission_submitted_by_can_differ_from_row_user_id(repo):
    """The row's PK (user_id) is the ORG CREATOR for an org owner; the
    submitting member can be a DIFFERENT org member entirely -- the whole
    point of this field (see paperclip_repo.claim_funnel_submission's
    docstring)."""
    await repo.put_workspace(
        _make_company(user_id="creator_1", onboarding_generation="01G", funnel_state="not_started")
    )
    result = await repo.claim_funnel_submission(
        "creator_1", onboarding_generation="01G", submitted_by="member_not_the_creator"
    )
    assert result is FunnelClaimResult.WON
    fetched = await repo.get_workspace("creator_1")
    assert fetched.org_id == "creator_1"
    assert fetched.onboarding_submitted_by == "member_not_the_creator"


async def test_revert_funnel_submission_clears_onboarding_submitted_by(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    fetched = await repo.get_workspace("u1")
    assert fetched.onboarding_submitted_by is None
    # A fresh claim by a DIFFERENT member can now win and re-stamp it.
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_2")
    assert result is FunnelClaimResult.WON
    assert (await repo.get_workspace("u1")).onboarding_submitted_by == "member_2"


async def test_claim_funnel_submission_persists_brief_text(repo):
    """The user's brief is stamped on the SAME CAS write as the claim, so a
    winning claim always carries it (never a separate write that could persist
    a brief for a claim that lost)."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    result = await repo.claim_funnel_submission(
        "u1",
        onboarding_generation="01G",
        submitted_by="member_1",
        brief_text="a social media agency for small restaurants",
    )
    assert result is FunnelClaimResult.WON
    fetched = await repo.get_workspace("u1")
    assert fetched.onboarding_brief_text == "a social media agency for small restaurants"
    # And it surfaces on the get_onboarding_state bridge dict the dispatcher reads.
    state = await repo.get_onboarding_state("u1")
    assert state["onboarding_brief_text"] == "a social media agency for small restaurants"


async def test_claim_funnel_submission_without_brief_text_leaves_field_absent(repo):
    """The "you pick" path supplies no brief -- the attribute is simply not
    set, and reads back as None (the builder then falls back to its default)."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    assert result is FunnelClaimResult.WON
    assert (await repo.get_workspace("u1")).onboarding_brief_text is None
    assert (await repo.get_onboarding_state("u1"))["onboarding_brief_text"] is None


async def test_revert_funnel_submission_clears_brief_text(repo):
    """A reverted claim must not leave a stale brief behind for the next
    submitter -- revert drops it alongside the other claim-scoped fields."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission(
        "u1", onboarding_generation="01G", submitted_by="member_1", brief_text="a coffee subscription box"
    )
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    assert (await repo.get_workspace("u1")).onboarding_brief_text is None


async def test_claim_funnel_submission_second_caller_gets_already_submitted(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    assert (
        await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
        is FunnelClaimResult.WON
    )
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    assert result is FunnelClaimResult.ALREADY_SUBMITTED


async def test_claim_funnel_submission_retries_when_peer_reverts(repo):
    """Revert-race regression: a peer WON then reverted its claim (failed
    downstream seed) back to ``not_started`` on the SAME generation. A racing
    caller whose first CAS lost must RETRY and win -- NOT be mislabeled
    ALREADY_SUBMITTED, which would strand its client believing onboarding is
    done while the company has no goal/project.

    The row starts ``submitted`` so the first CAS fails; the patched re-read
    simulates the peer's revert (flips the real row back to ``not_started``)
    so the bounded retry's next CAS wins.
    """
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="submitted"))
    real_get = repo.get_workspace
    flipped = {"done": False}

    async def flipping_get(user_id, *, consistent_read=False):
        if not flipped["done"]:
            flipped["done"] = True
            # The peer reverts its failed claim between our CAS failure and re-read.
            await repo.put_workspace(
                _make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started")
            )
        return await real_get(user_id, consistent_read=consistent_read)

    with patch.object(repo, "get_workspace", flipping_get):
        result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")

    assert result is FunnelClaimResult.WON
    assert (await repo.get_workspace("u1")).funnel_state == "submitted"


async def test_claim_funnel_submission_concurrent_leaves_row_consistent(repo):
    """Two tabs submitting at once — the CAS write itself is the
    serialization point (no separate claim item, no stale-steal window).

    On real DynamoDB the conditional writes serialize, so exactly one caller
    WONs and the other reads back ALREADY_SUBMITTED. moto does NOT lock
    truly-parallel conditional writes (each `run_in_thread` call reads,
    checks, and writes on its own thread with no cross-thread atomicity), so
    under a warm thread pool BOTH can observe `funnel_state == not_started`
    and report WON. That's a moto fidelity gap, not a repo bug -- the
    deterministic CAS-rejection guarantee is pinned by
    ``test_claim_funnel_submission_second_caller_gets_already_submitted``
    above. Here we assert only the invariants moto DOES model faithfully:
    at least one caller wins, and the row converges to a single consistent
    ``submitted`` state (no lost update, never stuck at not_started)."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    results = await asyncio.gather(
        repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1"),
        repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1"),
    )
    assert FunnelClaimResult.WON in results
    assert set(results) <= {FunnelClaimResult.WON, FunnelClaimResult.ALREADY_SUBMITTED}
    row = await repo.get_workspace("u1")
    assert row.funnel_state == "submitted"
    assert row.funnel_submitted_at is not None


async def test_get_onboarding_state_surfaces_failed_org_seed(repo):
    """COMPOSER P2: an org whose Paperclip seed FAILED has only a
    status="failed" owner row. get_onboarding_state must still resolve it (via
    include_inactive) so teams_state="failed" surfaces -- the active-only
    sign-in resolver would drop it and hide the Teams-degraded affordance."""
    await repo.put_workspace(
        _make_company(
            user_id="creator",
            org_id="org_x",
            status="failed",
            seeded_agent_ids=["friday_id"],
            onboarding_generation="01G",
            funnel_state="not_started",
        )
    )
    state = await repo.get_onboarding_state("org_x")
    assert state["teams_state"] == "failed"
    assert state["onboarding_generation"] == "01G"


async def test_get_onboarding_state_defaults_user_id_and_submitted_by_to_none(repo):
    state = await repo.get_onboarding_state("org_never_provisioned")
    assert state["user_id"] is None
    assert state["onboarding_submitted_by"] is None


async def test_claim_funnel_submission_stale_generation_is_a_silent_no_op(repo):
    """A caller fencing on a SUPERSEDED generation (e.g. the company was
    reprovisioned underneath it) gets STALE_GENERATION, not
    ALREADY_SUBMITTED -- INV-4: superseded writes are a no-op, not a
    duplicate-submit report."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01OLD", submitted_by="member_1")
    assert result is FunnelClaimResult.STALE_GENERATION
    # The row itself is untouched.
    assert (await repo.get_workspace("u1")).funnel_state == "not_started"


async def test_claim_funnel_submission_missing_row_is_stale_generation(repo):
    result = await repo.claim_funnel_submission(
        "user_never_provisioned", onboarding_generation="01G", submitted_by="member_1"
    )
    assert result is FunnelClaimResult.STALE_GENERATION


async def test_record_onboarding_ids_then_read_back(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    await repo.record_onboarding_ids("u1", onboarding_generation="01G", goal_id="goal-1", project_id="proj-1")
    fetched = await repo.get_workspace("u1")
    assert fetched.onboarding_goal_id == "goal-1"
    assert fetched.onboarding_project_id == "proj-1"


async def test_record_onboarding_ids_without_claim_raises(repo):
    """record_onboarding_ids requires funnel_state=="submitted" -- calling it
    against a still-not_started row must fail loudly, not fabricate state."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    with pytest.raises(repo._table().meta.client.exceptions.ConditionalCheckFailedException):
        await repo.record_onboarding_ids("u1", onboarding_generation="01G", goal_id="goal-1", project_id="proj-1")


async def test_revert_funnel_submission_allows_reclaim(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    fetched = await repo.get_workspace("u1")
    assert fetched.funnel_state == "not_started"
    assert fetched.funnel_submitted_at is None
    # A fresh claim can now win again.
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    assert result is FunnelClaimResult.WON


async def test_revert_funnel_submission_is_a_noop_against_a_superseded_generation(repo):
    """A caller that stalled, got reprovisioned out from under it (new
    generation minted), and only then tries to revert its OWN old claim must
    not touch the new generation's fresh row."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01OLD", funnel_state="submitted"))
    # Company reprovisioned: new generation, fresh not_started state.
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))
    await repo.revert_funnel_submission("u1", onboarding_generation="01OLD")
    fetched = await repo.get_workspace("u1")
    assert fetched.onboarding_generation == "01NEW"
    assert fetched.funnel_state == "not_started"


async def test_revert_funnel_submission_does_not_downgrade_complete(repo):
    """A best-effort revert fenced on funnel_state=="submitted" must never
    downgrade a funnel that has already advanced to "complete" (e.g. a
    concurrent bootstrap dispatch observed chat.final between this caller's
    failure and its revert call)."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="complete"))
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    assert (await repo.get_workspace("u1")).funnel_state == "complete"


# ---------------------------------------------------------------------------
# first_contact claim / settle / attempts
#
# Once-only is inferred SERVER-SIDE from this state, never from a
# client-supplied `purpose` label, so an agent cannot skip the guard by
# mislabelling its send.
# ---------------------------------------------------------------------------


async def test_claim_first_contact_wins_fresh(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G"))
    assert await repo.claim_first_contact("u1", onboarding_generation="01G") is True
    assert (await repo.get_workspace("u1")).first_contact_state == "sending"


async def test_claim_first_contact_exactly_once(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G"))
    first = await repo.claim_first_contact("u1", onboarding_generation="01G")
    second = await repo.claim_first_contact("u1", onboarding_generation="01G")
    assert first is True
    assert second is False


async def test_claim_first_contact_is_generation_fenced(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G"))
    assert await repo.claim_first_contact("u1", onboarding_generation="WRONG") is False


async def test_settle_first_contact_marks_sent(repo):
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G"))
    await repo.claim_first_contact("u1", onboarding_generation="01G")
    await repo.settle_first_contact("u1", onboarding_generation="01G", state="sent")
    assert (await repo.get_workspace("u1")).first_contact_state == "sent"


async def test_failed_first_contact_is_reclaimable(repo):
    """A failed mint or SES send must not make the first failure terminal."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G"))
    await repo.claim_first_contact("u1", onboarding_generation="01G")
    await repo.settle_first_contact("u1", onboarding_generation="01G", state="failed", reason="ses_send_failed")
    assert await repo.claim_first_contact("u1", onboarding_generation="01G") is True


async def test_sent_first_contact_is_not_reclaimable(repo):
    """`sent` is terminal -- reclaiming it would double-send."""
    await repo.put_workspace(_make_company(user_id="u1", onboarding_generation="01G"))
    await repo.claim_first_contact("u1", onboarding_generation="01G")
    await repo.settle_first_contact("u1", onboarding_generation="01G", state="sent")
    assert await repo.claim_first_contact("u1", onboarding_generation="01G") is False


async def test_onboarding_state_projects_first_contact_and_friday(repo):
    """send_to_owner reads these from the SAME single read compute_status does."""
    await repo.put_workspace(
        _make_company(
            user_id="u1",
            org_id="u1",
            onboarding_generation="01G",
            seeded_agent_ids=["agent_friday", "agent_atlas"],
        )
    )
    state = await repo.get_onboarding_state("u1")
    assert state["first_contact_state"] == "not_started"
    assert state["first_contact_attempts"] == 0
    # Friday is seeded FIRST in provision_org, so she is index 0.
    assert state["friday_agent_id"] == "agent_friday"


async def test_onboarding_state_prefers_starter_ceo_over_legacy_friday(repo):
    await repo.put_workspace(
        _make_company(
            user_id="u1",
            org_id="u1",
            onboarding_generation="01G",
            seeded_agent_ids=["agent_friday", "agent_atlas"],
            starter_agent_ids={"ceo": "agent_ceo"},
        )
    )

    assert (await repo.get_onboarding_state("u1"))["friday_agent_id"] == "agent_ceo"


# --- Pending brief (intake before the workspace can receive it) --------------


async def test_pending_brief_round_trip(repo):
    landed = await repo.put_pending_brief(
        "user_abc",
        path="grow",
        user_input="acme.com",
        detail=None,
        brief_text="work on acme.com",
        submitted_by="member_zed",
        target_agent_id="agent_engineering",
    )
    assert landed is True

    item = await repo.get_pending_brief("user_abc")
    assert item["owner_id"] == "user_abc"
    assert item["path"] == "grow"
    assert item["input"] == "acme.com"
    assert item["brief_text"] == "work on acme.com"
    assert item["submitted_by"] == "member_zed"
    assert item["target_agent_id"] == "agent_engineering"
    # Absent, not "" -- "no detail" must not come back as a blank string a
    # later reader would quote at Friday.
    assert "detail" not in item

    await repo.delete_pending_brief("user_abc")
    assert await repo.get_pending_brief("user_abc") is None


async def test_pending_brief_is_put_if_absent(repo):
    """One-shot per COMPANY even before the owner row exists: a second org
    member submitting concurrently must lose, not overwrite."""
    assert await repo.put_pending_brief(
        "org_x", path="new", user_input="first idea", detail=None, brief_text="build first idea", submitted_by="m1"
    )
    assert not await repo.put_pending_brief(
        "org_x", path="new", user_input="second idea", detail=None, brief_text="build second idea", submitted_by="m2"
    )

    item = await repo.get_pending_brief("org_x")
    assert item["input"] == "first idea"
    assert item["submitted_by"] == "m1"


async def test_pending_brief_overwrite_true_bypasses_the_still_pending_guard(repo):
    """overwrite=True backs the launch-waitlist "edit your idea while held"
    flow -- it must replace a STILL-PENDING item, not just a failed one,
    which the ordinary put-if-absent guard never allows."""
    await repo.put_pending_brief(
        "user_abc", path="new", user_input="first idea", detail=None, brief_text="build first", submitted_by="user_abc"
    )
    item = await repo.get_pending_brief("user_abc")
    assert item["brief_state"] == "pending"  # still pending -- what the ordinary guard checks

    # The ordinary put-if-absent guard still refuses it...
    assert not await repo.put_pending_brief(
        "user_abc",
        path="new",
        user_input="second idea",
        detail=None,
        brief_text="build second",
        submitted_by="user_abc",
    )
    assert (await repo.get_pending_brief("user_abc"))["input"] == "first idea"  # unchanged

    # ...but overwrite=True replaces it against the real table.
    assert await repo.put_pending_brief(
        "user_abc",
        path="new",
        user_input="edited idea",
        detail=None,
        brief_text="build edited idea",
        submitted_by="user_abc",
        overwrite=True,
    )
    item = await repo.get_pending_brief("user_abc")
    assert item["input"] == "edited idea"
    assert item["brief_text"] == "build edited idea"
    assert item["brief_state"] == "pending"
    assert item["attempts"] == 0


async def test_onboarding_state_reports_a_pending_brief_before_the_row_exists(repo):
    """The first-run case: the funnel is answered while provisioning is still
    running, so there is no owner row yet. compute_status turns this into
    funnel_state "submitted" so the funnel stops being shown."""
    await repo.put_pending_brief(
        "user_abc", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="user_abc"
    )

    state = await repo.get_onboarding_state("user_abc")
    assert state["pending_brief"] is True
    # The RAW funnel_state stays not_started -- bootstrap_dispatcher reads this
    # dict too, and waking Friday before the drain has seeded her goal and
    # project would hand her a company with nothing to work on.
    assert state["funnel_state"] == "not_started"


async def test_onboarding_state_reports_a_pending_brief_against_an_unclaimed_row(repo):
    await repo.put_workspace(_make_company(user_id="u1", org_id="u1", onboarding_generation="01G"))
    await repo.put_pending_brief(
        "u1", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="u1"
    )

    state = await repo.get_onboarding_state("u1")
    assert state["pending_brief"] is True


async def test_onboarding_state_stops_reading_only_once_the_funnel_is_COMPLETE(repo):
    """The steady-state fleet polls this every ~3s, so the read is gated -- but
    on "complete", not on "still not_started". The tighter gate looked cheaper
    and was wrong: a claim that landed and then died mid-seed leaves the row at
    "submitted" forever, and gating there made the still-undrained brief
    invisible to the drain permanently."""
    await repo.put_workspace(
        _make_company(user_id="u1", org_id="u1", onboarding_generation="01G", funnel_state="complete")
    )
    await repo.put_pending_brief(
        "u1", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="u1"
    )

    state = await repo.get_onboarding_state("u1")
    assert state["pending_brief"] is False
    assert state["funnel_state"] == "complete"


async def test_onboarding_state_still_reports_a_pending_brief_under_a_stuck_claim(repo):
    """funnel_state "submitted" with nothing seeded behind it is the stuck-claim
    case -- the drain has to stay able to see the brief and recover it."""
    await repo.put_workspace(
        _make_company(user_id="u1", org_id="u1", onboarding_generation="01G", funnel_state="submitted")
    )
    await repo.put_pending_brief(
        "u1", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="u1"
    )

    assert (await repo.get_onboarding_state("u1"))["pending_brief"] is True


# --- Giving up, and letting the user try again -------------------------------


async def test_a_failed_brief_stops_dismissing_the_funnel(repo):
    """Exhausting the drain's budget must hand the funnel BACK. If a failed
    item kept reporting `pending_brief`, compute_status would keep synthesising
    funnel_state "submitted" and the user would sit in an empty chat forever
    with no way to resubmit."""
    await repo.put_pending_brief(
        "user_abc", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="user_abc"
    )
    assert (await repo.get_onboarding_state("user_abc"))["pending_brief"] is True

    await repo.fail_pending_brief("user_abc", reason="paperclip exploded")

    assert (await repo.get_onboarding_state("user_abc"))["pending_brief"] is False
    item = await repo.get_pending_brief("user_abc")
    # Kept, not deleted: `last_error` is what the next operator reads when
    # asked why this owner never got Friday.
    assert item["brief_state"] == "failed"
    assert item["last_error"] == "paperclip exploded"


async def test_a_resubmit_overwrites_a_failed_brief(repo):
    """The funnel is a committed front door with no skip -- if the put-if-absent
    guard also refused a failed item, the user would submit into a void."""
    await repo.put_pending_brief(
        "user_abc", path="new", user_input="first idea", detail=None, brief_text="build first", submitted_by="user_abc"
    )
    await repo.fail_pending_brief("user_abc", reason="gave up")

    assert await repo.put_pending_brief(
        "user_abc",
        path="new",
        user_input="second idea",
        detail=None,
        brief_text="build second",
        submitted_by="user_abc",
    )
    item = await repo.get_pending_brief("user_abc")
    assert item["input"] == "second idea"
    assert item["brief_state"] == "pending"
    assert item["attempts"] == 0


async def test_failures_are_counted_on_the_item_not_in_memory(repo):
    """The process that fails is often not the process that retries -- a
    rolling deploy, a different task picking up the next poll."""
    await repo.put_pending_brief(
        "user_abc", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="user_abc"
    )

    assert await repo.record_pending_brief_failure("user_abc", reason="boom") == 1
    assert await repo.record_pending_brief_failure("user_abc", reason="boom again") == 2
    item = await repo.get_pending_brief("user_abc")
    assert item["last_error"] == "boom again"
    # Still drainable: counting a failure is not giving up.
    assert item["brief_state"] == "pending"


async def test_failure_record_on_a_deleted_item_does_not_resurrect_it(repo):
    """An unconditional update_item against a concurrently-deleted item would
    resurrect it as a partial (attempts + last_error, no brief_state) that
    put_pending_brief's guard rejects forever -- the owner could never file
    a brief again."""
    await repo.put_pending_brief(
        "user_abc", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="user_abc"
    )
    await repo.delete_pending_brief("user_abc")

    assert await repo.record_pending_brief_failure("user_abc", reason="boom") == 0
    assert await repo.get_pending_brief("user_abc") is None

    assert await repo.put_pending_brief(
        "user_abc", path="new", user_input="next idea", detail=None, brief_text="build next", submitted_by="user_abc"
    )


async def test_fail_pending_brief_on_a_deleted_item_is_a_noop(repo):
    await repo.put_pending_brief(
        "user_abc", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="user_abc"
    )
    await repo.delete_pending_brief("user_abc")

    await repo.fail_pending_brief("user_abc", reason="gave up")

    assert await repo.get_pending_brief("user_abc") is None


async def test_seed_claim_reclaims_legacy_failed_marker_with_empty_paperclip_user_id(repo):
    """Older failed markers stored ``paperclip_user_id = ""`` explicitly.
    ``attribute_not_exists`` is FALSE for an attribute that exists as "", so
    the reclaim condition must accept "" too — otherwise the Python pre-check
    calls the row credential-less, the conditional put fails, and every retry
    is wedged in the loser wait."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    repo._table().put_item(
        Item={
            "user_id": "user_legacy",
            "org_id": "org_alpha",
            "status": "failed",
            "paperclip_user_id": "",
            "created_at": now,
            "updated_at": now,
        }
    )
    assert await repo.try_acquire_seed_claim("user_legacy", claim_id="c1")
    raw = repo._table().get_item(Key={"user_id": "user_legacy"}, ConsistentRead=True)["Item"]
    assert raw["status"] == "seeding" and raw["seeding_claim_id"] == "c1"


async def test_signup_credentials_converge_regardless_of_which_claim_stages_them(repo):
    """No more leases: two concurrent provision runs (the original claim holder
    and whoever won a later takeover) must both succeed and converge on the
    SAME staged email/password, rather than the loser hitting a
    ConditionalCheckFailedException. ``if_not_exists`` + read-back is the
    convergence primitive — first writer's values win, every caller reads
    them back regardless of which claim_id is doing the staging."""
    await repo.try_acquire_seed_claim("user_new", claim_id="first")
    assert await repo.stage_signup_credentials(
        "user_new", email="first@example.com", password_encrypted="first-secret"
    ) == ("first@example.com", "first-secret")
    await repo.try_acquire_seed_claim("user_new", claim_id="second", stale_after_seconds=0)
    # The ORIGINAL claim staging again (e.g. a slow holder that wasn't really
    # dead) must still succeed and read back the converged values.
    assert await repo.stage_signup_credentials(
        "user_new", email="stale@example.com", password_encrypted="stale-secret"
    ) == ("first@example.com", "first-secret")
    assert await repo.stage_signup_credentials(
        "user_new", email="new@example.com", password_encrypted="new-secret"
    ) == ("first@example.com", "first-secret")


async def test_seed_takeover_does_not_erase_credentials_staged_after_its_read(repo, monkeypatch):
    import core.repositories.paperclip_repo as mod

    await repo.try_acquire_seed_claim("user_race", claim_id="original")
    run_in_thread = mod.run_in_thread
    staged = False

    async def stage_before_takeover_write(func, *args, **kwargs):
        nonlocal staged
        if not staged and ":prev" in kwargs.get("ConditionExpression", ""):
            staged = True
            await repo.stage_signup_credentials(
                "user_race", email="original@example.com", password_encrypted="original-secret"
            )
        return await run_in_thread(func, *args, **kwargs)

    monkeypatch.setattr(mod, "run_in_thread", stage_before_takeover_write)
    won = await repo.try_acquire_seed_claim("user_race", claim_id="takeover", stale_after_seconds=0)
    assert staged
    # A credential CAS may defer this takeover; its next read must keep the
    # original secret just as an atomic in-place takeover would.
    if not won:
        assert await repo.try_acquire_seed_claim("user_race", claim_id="takeover", stale_after_seconds=0)
    assert await repo.stage_signup_credentials(
        "user_race", email="replacement@example.com", password_encrypted="replacement-secret"
    ) == ("original@example.com", "original-secret")


async def test_paused_automation_record_survives_a_whole_item_put_and_clears_per_id(repo):
    await repo.put_workspace(_make_company(user_id="org_pause"))
    assert await repo.record_paused_automation("org_pause", agent_ids=["a1"], routine_ids=["r1", "r2"], token="t")
    ws = await repo.get_workspace("org_pause")
    assert ws.automation_paused_at and ws.automation_paused_agent_ids == {"a1"}
    assert ws.automation_paused_routine_versions == {} and ws.automation_pause_token == "t"
    await repo.put_workspace(ws)  # a carried-forward put must keep the record AND the empty map
    assert await repo.record_routine_version("org_pause", token="t", routine_id="r1", updated_at="v1")
    assert await repo.pause_token_current("org_pause", "t")
    assert await repo.mark_pause_complete("org_pause", token="t")
    ws = await repo.get_workspace("org_pause")
    assert ws.automation_paused_routine_ids == {"r1", "r2"}
    assert ws.automation_paused_routine_versions == {"r1": "v1"} and ws.automation_pause_complete_at

    # A newer pause takes the fence: the old token can no longer write.
    assert await repo.record_paused_automation("org_pause", agent_ids=[], routine_ids=[], token="t2")
    assert (await repo.get_workspace("org_pause")).automation_pause_complete_at is None
    assert not await repo.record_routine_version("org_pause", token="t", routine_id="r2", updated_at="v2")
    assert not await repo.mark_pause_complete("org_pause", token="t")

    # The resume's clear drops the fence: the running pause sees it gone.
    await repo.clear_paused_automation("org_pause", agent_ids=["a1"], routine_ids=["r1"])
    assert not await repo.pause_token_current("org_pause", "t2")
    ws = await repo.get_workspace("org_pause")
    assert ws.automation_paused_at is None and ws.automation_paused_agent_ids is None
    assert ws.automation_paused_routine_versions is None
    assert ws.automation_paused_routine_ids == {"r2"}
    # Never resurrects a deleted row.
    assert not await repo.record_paused_automation("org_gone", agent_ids=["a"], routine_ids=[], token="t")
    assert await repo.get_workspace("org_gone") is None


# ---------------------------------------------------------------------------
# Old per-agent Paperclip API keys (agent_keys map; delete only)
# ---------------------------------------------------------------------------


async def test_delete_agent_key_drops_an_old_key_idempotently(repo):
    await repo.put_workspace(_make_company("org_k"))
    repo._table().update_item(
        Key={"user_id": "org_k"}, UpdateExpression="SET agent_keys = :m", ExpressionAttributeValues={":m": {"a1": "x"}}
    )
    await repo.delete_agent_key("org_k", "a1")
    assert repo._table().get_item(Key={"user_id": "org_k"})["Item"]["agent_keys"] == {}
    await repo.delete_agent_key("org_k", "a1")  # idempotent


async def test_put_workspace_still_clears_fields_the_snapshot_does_not_carry(repo):
    await repo.put_workspace(_make_company("org_k", funnel_state="submitted"))
    await repo.put_workspace(_make_company("org_k"))

    item = raw_workspace(repo, "org_k")
    assert "funnel_state" not in item
