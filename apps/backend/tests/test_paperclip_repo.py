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

from core.repositories.paperclip_repo import FunnelClaimResult, PaperclipCompany, PaperclipRepo

TABLE_NAME = "test-paperclip-companies"


@pytest.fixture
def repo():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "scheduled_purge_at", "AttributeType": "S"},
                {"AttributeName": "org_id", "AttributeType": "S"},
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
                    # Used by get_org_company_id() to find any existing
                    # row for an org so members joining a known org
                    # reuse the shared company_id.
                    "IndexName": "by-org-id",
                    "KeySchema": [
                        {"AttributeName": "org_id", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        # Patch the shared dynamodb resource so the repo's get_table()
        # call resolves to this moto-backed resource.
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield PaperclipRepo(table_name=TABLE_NAME, region="us-east-1")


def _make_company(user_id="u1", status="active", **kwargs):
    now = datetime.now(timezone.utc)
    return PaperclipCompany(
        user_id=user_id,
        org_id=kwargs.get("org_id", f"org_{user_id}"),
        company_id=kwargs.get("company_id", f"co_{user_id}"),
        paperclip_user_id=kwargs.get("paperclip_user_id", f"pc_user_{user_id}"),
        paperclip_password_encrypted=kwargs.get("paperclip_password_encrypted", "enc_pwd"),
        service_token_encrypted="enc_token",
        status=status,
        created_at=kwargs.get("created_at", now),
        updated_at=kwargs.get("updated_at", now),
        last_error=kwargs.get("last_error"),
        scheduled_purge_at=kwargs.get("scheduled_purge_at"),
        seeded_agent_ids=kwargs.get("seeded_agent_ids"),
        onboarding_generation=kwargs.get("onboarding_generation"),
        funnel_state=kwargs.get("funnel_state"),
        funnel_submitted_at=kwargs.get("funnel_submitted_at"),
        funnel_completed_at=kwargs.get("funnel_completed_at"),
        onboarding_goal_id=kwargs.get("onboarding_goal_id"),
        onboarding_project_id=kwargs.get("onboarding_project_id"),
        bootstrap_state=kwargs.get("bootstrap_state"),
        bootstrap_dispatch_id=kwargs.get("bootstrap_dispatch_id"),
        bootstrap_dispatched_at=kwargs.get("bootstrap_dispatched_at"),
        bootstrap_delivered_at=kwargs.get("bootstrap_delivered_at"),
        bootstrap_attempts=kwargs.get("bootstrap_attempts"),
        bootstrap_last_error=kwargs.get("bootstrap_last_error"),
    )


async def test_seeded_agent_ids_round_trip(repo):
    await repo.put(_make_company(user_id="user_seed", seeded_agent_ids=["friday_id", "atlas_id"]))
    retrieved = await repo.get("user_seed")
    assert retrieved.seeded_agent_ids == ["friday_id", "atlas_id"]


async def test_seeded_agent_ids_absent_round_trips_as_none(repo):
    await repo.put(_make_company(user_id="user_noseed"))
    retrieved = await repo.get("user_noseed")
    assert retrieved.seeded_agent_ids is None


async def test_put_and_get_round_trips(repo):
    company = _make_company(user_id="user_123", org_id="org_42")
    await repo.put(company)
    retrieved = await repo.get("user_123")
    assert retrieved is not None
    assert retrieved.org_id == "org_42"
    assert retrieved.company_id == "co_user_123"
    assert retrieved.paperclip_user_id == "pc_user_user_123"
    assert retrieved.paperclip_password_encrypted == "enc_pwd"
    assert retrieved.status == "active"


async def test_get_returns_none_for_missing(repo):
    assert await repo.get("user_does_not_exist") is None


async def test_update_status_transitions_to_disabled(repo):
    await repo.put(_make_company(user_id="user_456"))
    purge_at = datetime(2026, 5, 27, tzinfo=timezone.utc)
    await repo.update_status("user_456", status="disabled", scheduled_purge_at=purge_at)
    retrieved = await repo.get("user_456")
    assert retrieved.status == "disabled"
    assert retrieved.scheduled_purge_at == purge_at


async def test_update_status_records_last_error(repo):
    await repo.put(_make_company(user_id="user_x"))
    await repo.update_status("user_x", status="failed", last_error="api timeout")
    retrieved = await repo.get("user_x")
    assert retrieved.status == "failed"
    assert retrieved.last_error == "api timeout"


async def test_update_status_not_if_status_skips_when_guarded(repo):
    """``not_if_status`` must SKIP the write (return False) when the row is
    already in that status — used so a losing concurrent provision attempt
    can't downgrade an already-``active`` row to ``failed``."""
    await repo.put(_make_company(user_id="user_active", status="active"))
    landed = await repo.update_status("user_active", status="failed", last_error="late error", not_if_status="active")
    assert landed is False
    retrieved = await repo.get("user_active")
    assert retrieved.status == "active"  # NOT downgraded
    assert retrieved.last_error is None


async def test_update_status_not_if_status_applies_when_not_guarded(repo):
    """``not_if_status`` allows the write (return True) when the row is in any
    OTHER status — e.g. a genuinely ``provisioning`` row may still fail."""
    await repo.put(_make_company(user_id="user_prov", status="provisioning"))
    landed = await repo.update_status("user_prov", status="failed", last_error="boom", not_if_status="active")
    assert landed is True
    retrieved = await repo.get("user_prov")
    assert retrieved.status == "failed"
    assert retrieved.last_error == "boom"


async def test_delete_removes_row(repo):
    await repo.put(_make_company(user_id="user_789"))
    await repo.delete("user_789")
    assert await repo.get("user_789") is None


async def test_scan_purge_due_returns_overdue_disabled(repo):
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=1)
    future = now + timedelta(days=10)
    # disabled and overdue -> should be returned
    await repo.put(_make_company(user_id="overdue", status="disabled", scheduled_purge_at=past))
    # disabled but not yet due -> should NOT be returned
    await repo.put(_make_company(user_id="not_yet", status="disabled", scheduled_purge_at=future))
    # active -> should NOT be returned (different status)
    await repo.put(_make_company(user_id="still_active", status="active"))
    due = await repo.scan_purge_due(now)
    user_ids = {c.user_id for c in due}
    assert user_ids == {"overdue"}


async def test_get_org_company_id_returns_shared_company_id(repo):
    """Two users in the same org both carry the same company_id and a
    lookup by org_id returns it (regardless of which row we hit).
    """
    shared_company_id = "co_shared_123"
    await repo.put(
        _make_company(
            user_id="user_a",
            org_id="org_alpha",
            company_id=shared_company_id,
        )
    )
    await repo.put(
        _make_company(
            user_id="user_b",
            org_id="org_alpha",
            company_id=shared_company_id,
        )
    )
    found = await repo.get_org_company_id("org_alpha")
    assert found == shared_company_id


async def test_get_org_company_id_returns_none_for_missing_org(repo):
    """An org with no provisioned rows yet returns None — caller will
    treat this as "first member, create company"."""
    found = await repo.get_org_company_id("org_does_not_exist")
    assert found is None


async def test_count_org_members_returns_zero_for_missing_org(repo):
    """No rows for an org should count as 0 — used by purge() to detect
    last-member archive eligibility."""
    assert await repo.count_org_members("org_phantom") == 0


async def test_count_org_members_counts_all_rows_for_org(repo):
    """All rows sharing an ``org_id`` are counted via the by-org-id GSI."""
    await repo.put(_make_company(user_id="u1", org_id="org_acme"))
    await repo.put(_make_company(user_id="u2", org_id="org_acme"))
    await repo.put(_make_company(user_id="u3", org_id="org_acme"))
    # Different org — must NOT be counted toward org_acme.
    await repo.put(_make_company(user_id="u4", org_id="org_other"))
    assert await repo.count_org_members("org_acme") == 3
    assert await repo.count_org_members("org_other") == 1


async def test_count_org_members_post_delete_reflects_remaining(repo):
    """After deleting a row, count drops accordingly. This is the
    contract purge() relies on (delete first, then count)."""
    await repo.put(_make_company(user_id="u1", org_id="org_acme"))
    await repo.put(_make_company(user_id="u2", org_id="org_acme"))
    assert await repo.count_org_members("org_acme") == 2
    await repo.delete("u1")
    assert await repo.count_org_members("org_acme") == 1
    await repo.delete("u2")
    assert await repo.count_org_members("org_acme") == 0


# ---------------------------------------------------------------------------
# Per-org create claim (strongly-consistent cross-instance guard)
# ---------------------------------------------------------------------------


async def test_org_create_claim_fresh_wins_then_second_caller_loses(repo):
    """First caller wins the fresh claim; a second caller (fresh holder) loses
    while the first holder is non-stale."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a") is True
    assert await repo.try_acquire_org_create_claim("org_x", holder="b") is False


async def test_org_create_claim_is_per_org(repo):
    """Claims are independent per org_id."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a") is True
    assert await repo.try_acquire_org_create_claim("org_y", holder="a") is True


async def test_org_create_claim_steals_when_stale(repo):
    """A holder that has gone silent past stale_after_seconds can be stolen,
    so a crash mid-create doesn't strand the org forever."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a") is True
    # Non-stale → loses.
    assert await repo.try_acquire_org_create_claim("org_x", holder="b", stale_after_seconds=120) is False
    # stale_after_seconds=0 → any existing claim is stale → steal succeeds.
    assert await repo.try_acquire_org_create_claim("org_x", holder="b", stale_after_seconds=0) is True


async def test_org_create_claim_release_allows_reacquire(repo):
    """Releasing on failure lets the next caller re-acquire a fresh claim."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a") is True
    await repo.release_org_create_claim("org_x", holder="a")
    assert await repo.try_acquire_org_create_claim("org_x", holder="b") is True


async def test_org_create_claim_release_only_by_holder(repo):
    """A non-holder cannot release the claim (guards against deleting a claim
    another caller has stolen)."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a") is True
    await repo.release_org_create_claim("org_x", holder="someone_else")
    # Still held by "a" → a fresh caller loses.
    assert await repo.try_acquire_org_create_claim("org_x", holder="b") is False


async def test_org_create_claim_sentinel_invisible_to_org_gsi_queries(repo):
    """The claim sentinel must NOT appear in any by-org-id GSI query — it omits
    org_id so it's never indexed. Otherwise it would corrupt find_org_owner /
    get_org_company_id / count_org_members / list_by_org_id."""
    await repo.try_acquire_org_create_claim("org_acme", holder="a")
    assert await repo.get_org_company_id("org_acme") is None
    assert await repo.count_org_members("org_acme") == 0
    assert await repo.list_by_org_id("org_acme") == []
    # A real row for the same org is still found normally.
    await repo.put(_make_company(user_id="u1", org_id="org_acme"))
    assert await repo.count_org_members("org_acme") == 1
    assert await repo.get_org_company_id("org_acme") is not None


# ---------------------------------------------------------------------------
# Onboarding: put/get round-trip (writer safety depends on this exact shape).
# ---------------------------------------------------------------------------


async def test_onboarding_fields_round_trip(repo):
    now = datetime.now(timezone.utc)
    await repo.put(
        _make_company(
            user_id="user_onb",
            onboarding_generation="01ONB",
            funnel_state="submitted",
            funnel_submitted_at=now,
            onboarding_goal_id="goal-1",
            onboarding_project_id="proj-1",
            bootstrap_state="dispatched",
            bootstrap_dispatch_id="disp-1",
            bootstrap_attempts=2,
        )
    )
    fetched = await repo.get("user_onb")
    assert fetched.onboarding_generation == "01ONB"
    assert fetched.funnel_state == "submitted"
    assert fetched.funnel_submitted_at == now
    assert fetched.onboarding_goal_id == "goal-1"
    assert fetched.onboarding_project_id == "proj-1"
    assert fetched.bootstrap_state == "dispatched"
    assert fetched.bootstrap_dispatch_id == "disp-1"
    assert fetched.bootstrap_attempts == 2


async def test_onboarding_fields_absent_round_trip_as_none(repo):
    await repo.put(_make_company(user_id="user_no_onb"))
    fetched = await repo.get("user_no_onb")
    assert fetched.onboarding_generation is None
    assert fetched.funnel_state is None
    assert fetched.bootstrap_state is None


async def test_put_without_onboarding_kwargs_wipes_previously_set_fields(repo):
    """Documents the exact hazard the writer-safety fix in
    paperclip_provisioning.py guards against: put() is a raw PutItem, so a
    PaperclipCompany built without onboarding fields SILENTLY WIPES them on
    a row that previously had them. This is why every put site in
    provisioning must read-merge or deliberately mint."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01ONB", funnel_state="complete"))
    assert (await repo.get("u1")).funnel_state == "complete"

    # A put that forgot to carry onboarding fields forward...
    await repo.put(_make_company(user_id="u1", status="provisioning"))
    # ...wipes them. (This is the exact bug the provisioning module's
    # _carry_forward_onboarding_fields helper exists to prevent at every
    # call site — see tests/test_paperclip_provisioning.py for the
    # end-to-end proof that provision_org's real puts do NOT do this.)
    fetched = await repo.get("u1")
    assert fetched.onboarding_generation is None
    assert fetched.funnel_state is None


# ---------------------------------------------------------------------------
# resolve_owner_row / find_owner_row
# ---------------------------------------------------------------------------


async def test_resolve_owner_row_personal_is_direct_get(repo):
    """A personal owner_id IS the row's PK — no GSI query needed."""
    await repo.put(_make_company(user_id="user_solo", org_id="user_solo", onboarding_generation="01G"))
    resolved = await repo.resolve_owner_row("user_solo")
    assert resolved is not None
    assert resolved.onboarding_generation == "01G"


async def test_resolve_owner_row_org_finds_creator_row_via_gsi(repo):
    """For an org, owner_id (org_xxx) is NOT the row's PK — the row
    provision_org wrote is keyed by the CREATOR's user_id. resolve_owner_row
    must find it via the by-org-id GSI (oldest active row), matching
    PaperclipProvisioning.find_org_owner's selection."""
    now = datetime.now(timezone.utc)
    await repo.put(
        _make_company(
            user_id="user_creator",
            org_id="org_acme",
            created_at=now,
            onboarding_generation="01CREATOR",
        )
    )
    # A member who joined later — must NOT be selected over the creator.
    await repo.put(
        _make_company(
            user_id="user_member",
            org_id="org_acme",
            created_at=now + timedelta(seconds=5),
        )
    )
    resolved = await repo.resolve_owner_row("org_acme")
    assert resolved is not None
    assert resolved.user_id == "user_creator"
    assert resolved.onboarding_generation == "01CREATOR"


async def test_resolve_owner_row_org_skips_non_active_rows(repo):
    """A provisioning/failed/disabled row is not sign-in-able and must never
    be picked over a genuinely active one, even if it's older."""
    now = datetime.now(timezone.utc)
    await repo.put(_make_company(user_id="user_failed", org_id="org_acme", status="failed", created_at=now))
    await repo.put(
        _make_company(
            user_id="user_active",
            org_id="org_acme",
            status="active",
            created_at=now + timedelta(seconds=5),
            onboarding_generation="01ACTIVE",
        )
    )
    resolved = await repo.resolve_owner_row("org_acme")
    assert resolved is not None
    assert resolved.user_id == "user_active"


async def test_resolve_owner_row_returns_none_for_unprovisioned_org(repo):
    assert await repo.resolve_owner_row("org_ghost") is None


# ---------------------------------------------------------------------------
# claim_funnel_submission / record_onboarding_ids / revert_funnel_submission
# ---------------------------------------------------------------------------


async def test_claim_funnel_submission_wins_fresh(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G")
    assert result is FunnelClaimResult.WON
    fetched = await repo.get("u1")
    assert fetched.funnel_state == "submitted"
    assert fetched.funnel_submitted_at is not None


async def test_claim_funnel_submission_second_caller_gets_already_submitted(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    assert await repo.claim_funnel_submission("u1", onboarding_generation="01G") is FunnelClaimResult.WON
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G")
    assert result is FunnelClaimResult.ALREADY_SUBMITTED


async def test_claim_funnel_submission_concurrent_exactly_one_wins(repo):
    """Two tabs submitting at once — the CAS write itself is the
    serialization point (no separate claim item, no stale-steal window)."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    results = await asyncio.gather(
        repo.claim_funnel_submission("u1", onboarding_generation="01G"),
        repo.claim_funnel_submission("u1", onboarding_generation="01G"),
    )
    assert set(results) == {FunnelClaimResult.WON, FunnelClaimResult.ALREADY_SUBMITTED}
    assert (await repo.get("u1")).funnel_state == "submitted"


async def test_claim_funnel_submission_stale_generation_is_a_silent_no_op(repo):
    """A caller fencing on a SUPERSEDED generation (e.g. the company was
    reprovisioned underneath it) gets STALE_GENERATION, not
    ALREADY_SUBMITTED -- INV-4: superseded writes are a no-op, not a
    duplicate-submit report."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01OLD")
    assert result is FunnelClaimResult.STALE_GENERATION
    # The row itself is untouched.
    assert (await repo.get("u1")).funnel_state == "not_started"


async def test_claim_funnel_submission_missing_row_is_stale_generation(repo):
    result = await repo.claim_funnel_submission("user_never_provisioned", onboarding_generation="01G")
    assert result is FunnelClaimResult.STALE_GENERATION


async def test_record_onboarding_ids_then_read_back(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission("u1", onboarding_generation="01G")
    await repo.record_onboarding_ids("u1", onboarding_generation="01G", goal_id="goal-1", project_id="proj-1")
    fetched = await repo.get("u1")
    assert fetched.onboarding_goal_id == "goal-1"
    assert fetched.onboarding_project_id == "proj-1"


async def test_record_onboarding_ids_without_claim_raises(repo):
    """record_onboarding_ids requires funnel_state=="submitted" -- calling it
    against a still-not_started row must fail loudly, not fabricate state."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    with pytest.raises(repo._table().meta.client.exceptions.ConditionalCheckFailedException):
        await repo.record_onboarding_ids("u1", onboarding_generation="01G", goal_id="goal-1", project_id="proj-1")


async def test_revert_funnel_submission_allows_reclaim(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission("u1", onboarding_generation="01G")
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    fetched = await repo.get("u1")
    assert fetched.funnel_state == "not_started"
    assert fetched.funnel_submitted_at is None
    # A fresh claim can now win again.
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G")
    assert result is FunnelClaimResult.WON


async def test_revert_funnel_submission_is_a_noop_against_a_superseded_generation(repo):
    """A caller that stalled, got reprovisioned out from under it (new
    generation minted), and only then tries to revert its OWN old claim must
    not touch the new generation's fresh row."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01OLD", funnel_state="submitted"))
    # Company reprovisioned: new generation, fresh not_started state.
    await repo.put(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))
    await repo.revert_funnel_submission("u1", onboarding_generation="01OLD")
    fetched = await repo.get("u1")
    assert fetched.onboarding_generation == "01NEW"
    assert fetched.funnel_state == "not_started"


async def test_revert_funnel_submission_does_not_downgrade_complete(repo):
    """A best-effort revert fenced on funnel_state=="submitted" must never
    downgrade a funnel that has already advanced to "complete" (e.g. a
    concurrent bootstrap dispatch observed chat.final between this caller's
    failure and its revert call)."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="complete"))
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    assert (await repo.get("u1")).funnel_state == "complete"
