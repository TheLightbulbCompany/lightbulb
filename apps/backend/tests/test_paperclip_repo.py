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

from core.repositories.paperclip_repo import (
    BootstrapClaimResult,
    FunnelClaimResult,
    PaperclipCompany,
    PaperclipRepo,
)

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
# Per-owner seed claim (provision_org's cross-instance fresh-create fence)
# ---------------------------------------------------------------------------


async def test_seed_claim_fresh_wins_then_second_caller_loses(repo):
    """First caller wins the put-if-absent; a concurrent second caller loses
    while the claim is non-stale — the fence that stops two backend
    instances from both running the full Paperclip seed."""
    assert await repo.try_acquire_seed_claim("u1", org_id="u1", claim_id="a") is True
    assert await repo.try_acquire_seed_claim("u1", org_id="u1", claim_id="b") is False


async def test_seed_claim_row_reads_back_as_bare_seeding_row(repo):
    """The claim row must be readable via get() — the loser polls it and the
    status readers surface it — so _item_to_company must tolerate the bare
    shape (status="seeding", company_id="", NO credential attributes)."""
    await repo.try_acquire_seed_claim("u1", org_id="org_a", claim_id="a")
    row = await repo.get("u1")
    assert row is not None
    assert row.status == "seeding"
    assert row.company_id == ""
    assert row.paperclip_user_id == ""
    # Credentials are ABSENT (not ""), same discipline as the failed marker.
    raw = repo._table().get_item(Key={"user_id": "u1"})["Item"]
    assert "paperclip_user_id" not in raw


async def test_seed_claim_never_overwrites_a_real_row(repo):
    """provisioning/active/failed rows are real state — never claimable nor
    stealable, even with a stale threshold of 0. Only a bare seeding claim
    can ever be replaced."""
    for status in ("provisioning", "active", "failed"):
        user = f"user_{status}"
        await repo.put(_make_company(user_id=user, status=status))
        won = await repo.try_acquire_seed_claim(user, org_id=f"org_{user}", claim_id="x", stale_after_seconds=0)
        assert won is False
        assert (await repo.get(user)).status == status  # untouched


async def test_seed_claim_steals_only_when_stale(repo):
    """A holder that crashed hard before any durable progress leaves a bare
    seeding row; once claimed_at is past the stale window it is re-claimable
    so the owner isn't stranded forever."""
    assert await repo.try_acquire_seed_claim("u1", org_id="u1", claim_id="a") is True
    # Non-stale → loses.
    assert await repo.try_acquire_seed_claim("u1", org_id="u1", claim_id="b", stale_after_seconds=120) is False
    # stale_after_seconds=0 → any existing claim is stale → steal succeeds.
    assert await repo.try_acquire_seed_claim("u1", org_id="u1", claim_id="b", stale_after_seconds=0) is True


async def test_seed_claim_visible_to_status_reader_but_not_signin_resolver(repo):
    """Unlike the org-create sentinel (synthetic PK, invisible everywhere),
    the seed claim IS the real row: the active-only sign-in resolver must
    skip it, while the status reader surfaces the in-flight state."""
    await repo.try_acquire_seed_claim("creator", org_id="org_x", claim_id="a")
    assert await repo.find_owner_row("org_x") is None  # not sign-in-able
    state = await repo.get_onboarding_state("org_x")
    assert state["teams_state"] == "seeding"


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


async def test_find_owner_row_hydrates_from_gsi_without_a_second_get(repo):
    """The by-org-id GSI is ProjectionType.ALL, so find_owner_row must build the
    row from the query item -- NOT do a second base-table get() -- keeping the
    hot /container/status org poll to a single physical read."""
    from unittest.mock import patch

    now = datetime.now(timezone.utc)
    await repo.put(_make_company(user_id="user_creator", org_id="org_acme", created_at=now))

    with patch.object(repo, "get", side_effect=AssertionError("find_owner_row must not call get()")) as spy:
        resolved = await repo.find_owner_row("org_acme")
    spy.assert_not_called()
    assert resolved is not None and resolved.user_id == "user_creator"


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
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    assert result is FunnelClaimResult.WON
    fetched = await repo.get("u1")
    assert fetched.funnel_state == "submitted"
    assert fetched.funnel_submitted_at is not None
    assert fetched.onboarding_submitted_by == "member_1"


async def test_claim_funnel_submission_submitted_by_can_differ_from_row_user_id(repo):
    """The row's PK (user_id) is the ORG CREATOR for an org owner; the
    submitting member can be a DIFFERENT org member entirely -- the whole
    point of this field (see paperclip_repo.claim_funnel_submission's
    docstring)."""
    await repo.put(_make_company(user_id="creator_1", onboarding_generation="01G", funnel_state="not_started"))
    result = await repo.claim_funnel_submission(
        "creator_1", onboarding_generation="01G", submitted_by="member_not_the_creator"
    )
    assert result is FunnelClaimResult.WON
    fetched = await repo.get("creator_1")
    assert fetched.user_id == "creator_1"
    assert fetched.onboarding_submitted_by == "member_not_the_creator"


async def test_revert_funnel_submission_clears_onboarding_submitted_by(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    fetched = await repo.get("u1")
    assert fetched.onboarding_submitted_by is None
    # A fresh claim by a DIFFERENT member can now win and re-stamp it.
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_2")
    assert result is FunnelClaimResult.WON
    assert (await repo.get("u1")).onboarding_submitted_by == "member_2"


async def test_claim_funnel_submission_persists_brief_text(repo):
    """The user's brief is stamped on the SAME CAS write as the claim, so a
    winning claim always carries it (never a separate write that could persist
    a brief for a claim that lost)."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    result = await repo.claim_funnel_submission(
        "u1",
        onboarding_generation="01G",
        submitted_by="member_1",
        brief_text="a social media agency for small restaurants",
    )
    assert result is FunnelClaimResult.WON
    fetched = await repo.get("u1")
    assert fetched.onboarding_brief_text == "a social media agency for small restaurants"
    # And it surfaces on the get_onboarding_state bridge dict the dispatcher reads.
    state = await repo.get_onboarding_state("u1")
    assert state["onboarding_brief_text"] == "a social media agency for small restaurants"


async def test_claim_funnel_submission_without_brief_text_leaves_field_absent(repo):
    """The "you pick" path supplies no brief -- the attribute is simply not
    set, and reads back as None (the builder then falls back to its default)."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    assert result is FunnelClaimResult.WON
    assert (await repo.get("u1")).onboarding_brief_text is None
    assert (await repo.get_onboarding_state("u1"))["onboarding_brief_text"] is None


async def test_revert_funnel_submission_clears_brief_text(repo):
    """A reverted claim must not leave a stale brief behind for the next
    submitter -- revert drops it alongside the other claim-scoped fields."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission(
        "u1", onboarding_generation="01G", submitted_by="member_1", brief_text="a coffee subscription box"
    )
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    assert (await repo.get("u1")).onboarding_brief_text is None


async def test_claim_funnel_submission_second_caller_gets_already_submitted(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
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
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="submitted"))
    real_get = repo.get
    flipped = {"done": False}

    async def flipping_get(user_id, *, consistent_read=False):
        if not flipped["done"]:
            flipped["done"] = True
            # The peer reverts its failed claim between our CAS failure and re-read.
            await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
        return await real_get(user_id, consistent_read=consistent_read)

    with patch.object(repo, "get", flipping_get):
        result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")

    assert result is FunnelClaimResult.WON
    assert (await repo.get("u1")).funnel_state == "submitted"


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
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    results = await asyncio.gather(
        repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1"),
        repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1"),
    )
    assert FunnelClaimResult.WON in results
    assert set(results) <= {FunnelClaimResult.WON, FunnelClaimResult.ALREADY_SUBMITTED}
    row = await repo.get("u1")
    assert row.funnel_state == "submitted"
    assert row.funnel_submitted_at is not None


async def test_get_onboarding_state_surfaces_failed_org_seed(repo):
    """COMPOSER P2: an org whose Paperclip seed FAILED has only a
    status="failed" owner row. get_onboarding_state must still resolve it (via
    include_inactive) so teams_state="failed" surfaces -- the active-only
    sign-in resolver would drop it and hide the Teams-degraded affordance."""
    await repo.put(
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


async def test_get_onboarding_state_surfaces_row_user_id_and_submitted_by(repo):
    """The bridge dict must carry the resolved row's OWN PK (user_id --
    the org creator for an org owner, distinct from owner_id/org_id) and
    onboarding_submitted_by, so a caller (bootstrap_dispatcher, threaded
    through readiness_state.resolve_onboarding_state) can address a write
    at the correct row and the correct gateway session without a second
    read."""
    await repo.put(
        _make_company(
            user_id="creator_2",
            org_id="org_z",
            seeded_agent_ids=["friday_id"],
            onboarding_generation="01G",
            funnel_state="not_started",
        )
    )
    await repo.claim_funnel_submission("creator_2", onboarding_generation="01G", submitted_by="member_x")

    state = await repo.get_onboarding_state("org_z")
    assert state["user_id"] == "creator_2"
    assert state["onboarding_submitted_by"] == "member_x"


async def test_get_onboarding_state_defaults_user_id_and_submitted_by_to_none(repo):
    state = await repo.get_onboarding_state("org_never_provisioned")
    assert state["user_id"] is None
    assert state["onboarding_submitted_by"] is None


async def test_find_owner_row_include_inactive_returns_failed_provisioner(repo):
    await repo.put(_make_company(user_id="creator", org_id="org_y", status="failed", seeded_agent_ids=["friday_id"]))
    # Default (sign-in) resolver hides the non-active row...
    assert await repo.find_owner_row("org_y") is None
    # ...but the status reader can still resolve it.
    row = await repo.find_owner_row("org_y", include_inactive=True)
    assert row is not None and row.status == "failed"


async def test_claim_funnel_submission_stale_generation_is_a_silent_no_op(repo):
    """A caller fencing on a SUPERSEDED generation (e.g. the company was
    reprovisioned underneath it) gets STALE_GENERATION, not
    ALREADY_SUBMITTED -- INV-4: superseded writes are a no-op, not a
    duplicate-submit report."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01OLD", submitted_by="member_1")
    assert result is FunnelClaimResult.STALE_GENERATION
    # The row itself is untouched.
    assert (await repo.get("u1")).funnel_state == "not_started"


async def test_claim_funnel_submission_missing_row_is_stale_generation(repo):
    result = await repo.claim_funnel_submission(
        "user_never_provisioned", onboarding_generation="01G", submitted_by="member_1"
    )
    assert result is FunnelClaimResult.STALE_GENERATION


async def test_record_onboarding_ids_then_read_back(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G", funnel_state="not_started"))
    await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
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
    await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
    await repo.revert_funnel_submission("u1", onboarding_generation="01G")
    fetched = await repo.get("u1")
    assert fetched.funnel_state == "not_started"
    assert fetched.funnel_submitted_at is None
    # A fresh claim can now win again.
    result = await repo.claim_funnel_submission("u1", onboarding_generation="01G", submitted_by="member_1")
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


# ---------------------------------------------------------------------------
# claim_bootstrap_dispatch / mark_bootstrap_delivered / mark_bootstrap_failed
# ---------------------------------------------------------------------------


async def test_claim_bootstrap_dispatch_wins_fresh(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    result, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    assert result is BootstrapClaimResult.WON
    assert dispatch_id
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "dispatched"
    assert fetched.bootstrap_dispatch_id == dispatch_id
    assert fetched.bootstrap_dispatched_at is not None
    assert fetched.bootstrap_attempts == 1


async def test_claim_bootstrap_dispatch_exactly_once(repo):
    """INV-5: only ONE caller ever wins the claim for a given generation --
    a second attempt (a racing /status poll, a duplicate /brief dispatch
    hook) must not re-dispatch."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    first, first_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    second, second_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    assert first is BootstrapClaimResult.WON
    assert second is BootstrapClaimResult.NOT_READY
    assert second_id is None
    fetched = await repo.get("u1")
    assert fetched.bootstrap_dispatch_id == first_id
    assert fetched.bootstrap_attempts == 1


async def test_claim_bootstrap_dispatch_not_ready_when_funnel_not_submitted(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="not_started", bootstrap_state="not_started"
        )
    )
    result, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    assert result is BootstrapClaimResult.NOT_READY
    assert dispatch_id is None
    assert (await repo.get("u1")).bootstrap_state == "not_started"


async def test_claim_bootstrap_dispatch_stale_generation_is_a_silent_no_op(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01NEW", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    result, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01OLD")
    assert result is BootstrapClaimResult.STALE_GENERATION
    assert dispatch_id is None
    assert (await repo.get("u1")).bootstrap_state == "not_started"


async def test_claim_bootstrap_dispatch_missing_row_is_stale_generation(repo):
    result, dispatch_id = await repo.claim_bootstrap_dispatch("user_never_provisioned", onboarding_generation="01G")
    assert result is BootstrapClaimResult.STALE_GENERATION
    assert dispatch_id is None


async def test_mark_bootstrap_delivered_completes_funnel(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    await repo.mark_bootstrap_delivered("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "delivered"
    assert fetched.bootstrap_delivered_at is not None
    assert fetched.funnel_state == "complete"
    assert fetched.funnel_completed_at is not None


async def test_mark_bootstrap_delivered_fenced_on_dispatch_id(repo):
    """A completion observer for a SUPERSEDED dispatch attempt (e.g. a
    delayed/duplicate chat.final delivered after a retry already re-claimed
    the row under a fresh dispatch_id) must not resolve the LATER attempt."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    await repo.mark_bootstrap_delivered("u1", onboarding_generation="01G", dispatch_id="stale-dispatch-id")
    fetched = await repo.get("u1")
    # No-op: still not_started, funnel still submitted.
    assert fetched.bootstrap_state == "not_started"
    assert fetched.funnel_state == "submitted"


async def test_mark_bootstrap_delivered_is_a_noop_against_a_superseded_generation(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01OLD", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01OLD")
    # Company reprovisioned mid-flight: new generation, fresh not_started state.
    await repo.put(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))
    await repo.mark_bootstrap_delivered("u1", onboarding_generation="01OLD", dispatch_id=dispatch_id)
    fetched = await repo.get("u1")
    assert fetched.onboarding_generation == "01NEW"
    assert fetched.funnel_state == "not_started"


async def test_mark_bootstrap_failed_records_error(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    await repo.mark_bootstrap_failed(
        "u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="container unreachable"
    )
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "failed"
    assert fetched.bootstrap_last_error == "container unreachable"
    # A failed dispatch does NOT complete the funnel.
    assert fetched.funnel_state == "submitted"


async def test_mark_bootstrap_failed_fenced_on_dispatch_id(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    await repo.mark_bootstrap_failed("u1", onboarding_generation="01G", dispatch_id="stale-dispatch-id", reason="boom")
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "not_started"
    assert fetched.bootstrap_last_error is None


# ---------------------------------------------------------------------------
# rearm_bootstrap_dispatch (transient-failure retry path)
# ---------------------------------------------------------------------------


async def test_rearm_bootstrap_dispatch_returns_row_to_not_started(repo):
    """The TRANSIENT counterpart to mark_bootstrap_failed: state back to
    not_started so the next poll re-dispatches, attempts KEPT (they bound the
    total dispatches), reason recorded, funnel untouched (still submitted —
    that's what makes the row re-dispatchable)."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    await repo.rearm_bootstrap_dispatch(
        "u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="[Errno 111] Connect call failed"
    )
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "not_started"
    assert fetched.bootstrap_attempts == 1  # kept, not reset
    assert fetched.bootstrap_last_error == "[Errno 111] Connect call failed"
    assert fetched.funnel_state == "submitted"


async def test_rearm_then_next_claim_wins_and_increments_attempts(repo):
    """The whole point of the re-arm: a later /container/status poll's claim
    must WIN again on the same generation, minting a fresh dispatch_id and
    counting the new attempt."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    first, first_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    await repo.rearm_bootstrap_dispatch("u1", onboarding_generation="01G", dispatch_id=first_id, reason="boot race")
    second, second_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    assert first is BootstrapClaimResult.WON
    assert second is BootstrapClaimResult.WON
    assert second_id != first_id
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "dispatched"
    assert fetched.bootstrap_dispatch_id == second_id
    assert fetched.bootstrap_attempts == 2


async def test_rearm_bootstrap_dispatch_fenced_on_dispatch_id(repo):
    """A stale re-arm from a superseded dispatch attempt must not yank a
    LATER attempt's in-flight dispatched state back to not_started."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    await repo.rearm_bootstrap_dispatch(
        "u1", onboarding_generation="01G", dispatch_id="stale-dispatch-id", reason="boom"
    )
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "dispatched"  # untouched
    assert fetched.bootstrap_dispatch_id == dispatch_id
    assert fetched.bootstrap_last_error is None


async def test_rearm_bootstrap_dispatch_is_a_noop_against_a_superseded_generation(repo):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01OLD", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01OLD")
    # Company reprovisioned mid-flight: new generation, fresh not_started state.
    await repo.put(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))
    await repo.rearm_bootstrap_dispatch("u1", onboarding_generation="01OLD", dispatch_id=dispatch_id, reason="boom")
    fetched = await repo.get("u1")
    assert fetched.onboarding_generation == "01NEW"
    assert fetched.bootstrap_state is None  # the fresh row untouched


async def test_rearm_bootstrap_dispatch_never_downgrades_a_resolved_dispatch(repo):
    """A dispatch that already resolved (delivered) is fenced on
    bootstrap_state=="dispatched" — a late transient classifier from the same
    dispatch (e.g. a raced completion) must not un-deliver it."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    await repo.mark_bootstrap_delivered("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    await repo.rearm_bootstrap_dispatch("u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="late")
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "delivered"
    assert fetched.funnel_state == "complete"


# ---------------------------------------------------------------------------
# reset_bootstrap_for_retry
# ---------------------------------------------------------------------------


async def test_reset_bootstrap_for_retry_lands_on_failed_row(repo):
    """Admin retry re-arms a failed kickoff: bootstrap_state back to
    not_started, attempts zeroed (so MAX_BOOTSTRAP_ATTEMPTS re-arms),
    dispatch_id REMOVEd -- while bootstrap_last_error is kept as history
    and the funnel stays submitted for the next dispatch claim."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    await repo.mark_bootstrap_failed(
        "u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="container unreachable"
    )

    ok = await repo.reset_bootstrap_for_retry("u1", onboarding_generation="01G")

    assert ok is True
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "not_started"
    assert fetched.bootstrap_attempts == 0
    assert fetched.bootstrap_dispatch_id is None
    assert fetched.bootstrap_last_error == "container unreachable"
    assert fetched.funnel_state == "submitted"


async def test_reset_bootstrap_for_retry_returns_false_on_condition_fail(repo):
    """The fence: only a genuinely failed bootstrap on the SAME generation
    the operator read may be reset -- a mid-flight dispatch or a superseded
    generation is a no-op (False)."""
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation="01G", funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")

    # Still dispatched (not failed): must not yank a live kickoff.
    assert await repo.reset_bootstrap_for_retry("u1", onboarding_generation="01G") is False
    assert (await repo.get("u1")).bootstrap_state == "dispatched"

    await repo.mark_bootstrap_failed("u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="boom")

    # Failed, but the operator is holding a stale generation: no-op.
    assert await repo.reset_bootstrap_for_retry("u1", onboarding_generation="01STALE") is False
    fetched = await repo.get("u1")
    assert fetched.bootstrap_state == "failed"
    assert fetched.bootstrap_attempts == 1
    assert fetched.bootstrap_dispatch_id == dispatch_id


# ---------------------------------------------------------------------------
# Budget fence stamp (core/services/paperclip_budget_fence.py)
# ---------------------------------------------------------------------------


async def test_set_budget_fence_state_round_trips(repo):
    await repo.put(_make_company(user_id="u1"))
    assert (await repo.get("u1")).budget_fence_state is None

    await repo.set_budget_fence_state("u1", state="fenced", reason="trial_daily_cap")
    fetched = await repo.get("u1")
    assert fetched.budget_fence_state == "fenced"
    assert fetched.budget_fence_reason == "trial_daily_cap"
    assert fetched.budget_fence_updated_at is not None

    await repo.set_budget_fence_state("u1", state="open", reason=None)
    assert (await repo.get("u1")).budget_fence_state == "open"


async def test_set_budget_fence_state_preserves_onboarding_fields(repo):
    """Targeted UpdateItem, not a whole-item put -- the stamp must never
    clobber the generation-fenced onboarding attributes."""
    await repo.put(
        _make_company(
            user_id="u1",
            onboarding_generation="01G",
            funnel_state="submitted",
            bootstrap_state="dispatched",
        )
    )
    await repo.set_budget_fence_state("u1", state="fenced", reason="credits_exhausted")
    fetched = await repo.get("u1")
    assert fetched.onboarding_generation == "01G"
    assert fetched.funnel_state == "submitted"
    assert fetched.bootstrap_state == "dispatched"


async def test_set_budget_fence_state_on_missing_row_is_a_noop(repo):
    """A row deleted under a reconcile must not be resurrected as a
    fence-only stub."""
    await repo.set_budget_fence_state("ghost", state="fenced", reason="trial_daily_cap")
    assert await repo.get("ghost") is None


async def test_put_does_not_carry_the_fence_stamp(repo):
    """Deliberate: a reprovision re-put drops the stamp back to "open", the
    safe direction -- the next reconcile re-derives and re-applies."""
    await repo.put(_make_company(user_id="u1"))
    await repo.set_budget_fence_state("u1", state="fenced", reason="trial_daily_cap")
    await repo.put(_make_company(user_id="u1"))
    assert (await repo.get("u1")).budget_fence_state is None
