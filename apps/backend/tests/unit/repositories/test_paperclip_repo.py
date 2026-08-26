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
                    # Used by get_org_company_id() to find any existing
                    # row for an org so members joining a known org
                    # reuse the shared company_id.
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


async def test_update_status_seeding_claim_fence_blocks_stale_holder(repo):
    """A stale seed holder must not downgrade a NEWER holder's live seeding row.

    The bare seeding row carries the winner's ``seeding_claim_id``; after a
    stale steal it carries the NEW holder's id. The old holder's failure path
    (``require_seeding_claim_id`` = its now-superseded id) must SKIP."""
    # Holder "old" wins the bare claim.
    assert await repo.try_acquire_seed_claim("u_seed", org_id="u_seed", claim_id="old") is True
    # Holder "new" steals it (stale window 0) → row now carries claim_id "new".
    assert await repo.try_acquire_seed_claim("u_seed", org_id="u_seed", claim_id="new", stale_after_seconds=0) is True

    # The OLD holder's downgrade is fenced out → no-op, row stays seeding.
    landed = await repo.update_status(
        "u_seed", status="failed", last_error="old holder late error", require_seeding_claim_id="old"
    )
    assert landed is False
    assert (await repo.get("u_seed")).status == "seeding"

    # The current holder CAN downgrade with its own id.
    landed = await repo.update_status(
        "u_seed", status="failed", last_error="new holder gave up", require_seeding_claim_id="new"
    )
    assert landed is True
    assert (await repo.get("u_seed")).status == "failed"


async def test_update_status_seeding_claim_fence_allows_advanced_row(repo):
    """Once a row advances past the bare claim it drops ``seeding_claim_id``, so
    the fence's ``attribute_not_exists`` branch lets the write through — a real
    failure on a provisioning row must still be recordable."""
    await repo.put(_make_company(user_id="u_prov2", status="provisioning"))  # no seeding_claim_id
    landed = await repo.update_status(
        "u_prov2", status="failed", last_error="boom", require_seeding_claim_id="whatever"
    )
    assert landed is True
    assert (await repo.get("u_prov2")).status == "failed"


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
    """First caller wins the fresh claim (gets a token); a second caller (fresh
    holder) loses (None) while the first holder is non-stale."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a")  # truthy token
    assert await repo.try_acquire_org_create_claim("org_x", holder="b") is None


async def test_org_create_claim_is_per_org(repo):
    """Claims are independent per org_id."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a")
    assert await repo.try_acquire_org_create_claim("org_y", holder="a")


async def test_org_create_claim_steals_when_stale(repo):
    """A holder that has gone silent past stale_after_seconds can be stolen,
    so a crash mid-create doesn't strand the org forever."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a")
    # Non-stale → loses.
    assert await repo.try_acquire_org_create_claim("org_x", holder="b", stale_after_seconds=120) is None
    # stale_after_seconds=0 → any existing claim is stale → steal succeeds.
    assert await repo.try_acquire_org_create_claim("org_x", holder="b", stale_after_seconds=0)


async def test_org_create_claim_release_allows_reacquire(repo):
    """Releasing on failure (with the returned token) lets the next caller
    re-acquire a fresh claim."""
    token = await repo.try_acquire_org_create_claim("org_x", holder="a")
    assert token
    await repo.release_org_create_claim("org_x", token=token)
    assert await repo.try_acquire_org_create_claim("org_x", holder="b")


async def test_org_create_claim_release_wrong_token_is_noop(repo):
    """A release fenced on a WRONG token cannot delete the live claim."""
    assert await repo.try_acquire_org_create_claim("org_x", holder="a")
    await repo.release_org_create_claim("org_x", token="not-the-token")
    # Still held → a fresh caller loses.
    assert await repo.try_acquire_org_create_claim("org_x", holder="b") is None


async def test_org_create_claim_stale_straggler_cannot_release_newer_holder(repo):
    """ABA guard: the SAME identity re-polls and steals its own stale claim
    (fresh token), then the FIRST attempt's straggler tries to release with its
    now-superseded token — it must be a no-op, not a deletion that reopens the
    duplicate-create window."""
    tok_first = await repo.try_acquire_org_create_claim("org_x", holder="a")
    assert tok_first
    # Same identity 'a' re-polls after the first attempt went stale and steals
    # its own claim, minting a FRESH token.
    tok_second = await repo.try_acquire_org_create_claim("org_x", holder="a", stale_after_seconds=0)
    assert tok_second and tok_second != tok_first
    # The first attempt's straggler releases with its stale token → no-op.
    await repo.release_org_create_claim("org_x", token=tok_first)
    # The live (second) claim must still hold → a fresh caller loses.
    assert await repo.try_acquire_org_create_claim("org_x", holder="b") is None
    # The real holder can still release with its own token.
    await repo.release_org_create_claim("org_x", token=tok_second)
    assert await repo.try_acquire_org_create_claim("org_x", holder="b")


async def test_org_create_claim_legacy_holder_release_cannot_delete_tokenized_claim(repo):
    """Rolling-deploy safety: an OLD worker still runs the pre-token release
    (ConditionExpression ``holder = :h``). A tokenized claim stores NO ``holder``
    attribute (identity is under ``claimed_by``), so that legacy release can't
    match — it cannot delete a newer token-fenced claim for the same admin."""
    from core.repositories.paperclip_repo import _org_create_claim_key

    token = await repo.try_acquire_org_create_claim("org_x", holder="admin_a")
    assert token
    table = repo._table()
    key = _org_create_claim_key("org_x")
    # Emulate the OLD worker's holder-only release for the SAME admin id.
    with pytest.raises(table.meta.client.exceptions.ConditionalCheckFailedException):
        table.delete_item(
            Key={"user_id": key},
            ConditionExpression="holder = :h",
            ExpressionAttributeValues={":h": "admin_a"},
        )
    # The claim survived → a fresh caller still loses (no reopened create window).
    assert await repo.try_acquire_org_create_claim("org_x", holder="admin_b") is None


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
    shape (status="seeding", NO company_id, NO credential attributes —
    get() hydrates the absent company_id back to "")."""
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
    # call site — see tests/unit/services/test_paperclip_provisioning_moto.py for the
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
    assert await repo.mark_bootstrap_delivered("u1", onboarding_generation="01G", dispatch_id=dispatch_id) is True
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
    assert (
        await repo.mark_bootstrap_delivered("u1", onboarding_generation="01G", dispatch_id="stale-dispatch-id") is False
    )
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
    assert await repo.mark_bootstrap_delivered("u1", onboarding_generation="01OLD", dispatch_id=dispatch_id) is False
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
# mark_bootstrap_send_attempted / mark_bootstrap_sent — the two stamps between
# the claim and the terminal marks. They are what a later poll reads to tell a
# dead claimant that never sent from one that may already have.
# ---------------------------------------------------------------------------


async def _claimed(repo, generation="01G"):
    await repo.put(
        _make_company(
            user_id="u1", onboarding_generation=generation, funnel_state="submitted", bootstrap_state="not_started"
        )
    )
    _, dispatch_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation=generation)
    return dispatch_id


async def test_send_attempted_then_sent_walks_the_ladder(repo):
    dispatch_id = await _claimed(repo)
    assert await repo.mark_bootstrap_send_attempted("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    attempted = await repo.get("u1")
    # Stamp 2 records the intent WITHOUT resolving the state — the row is
    # still `dispatched`, because nothing is known about the outcome yet.
    assert attempted.bootstrap_state == "dispatched"
    assert attempted.bootstrap_send_attempted_at is not None
    assert attempted.bootstrap_sent_at is None

    assert await repo.mark_bootstrap_sent("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    sent = await repo.get("u1")
    assert sent.bootstrap_state == "sent"
    assert sent.bootstrap_sent_at is not None
    assert sent.funnel_state == "submitted"  # completion, not delivery, completes the funnel


async def test_sent_returns_false_when_the_row_moved_on_under_the_claim(repo):
    """The ack landed but the row was already settled by a stale-claim
    recovery. The caller must be able to TELL — reading the swallowed
    guard-fail as success is what let a delivered kickoff sit in `failed`
    with no warning and an admin reset waiting to double-deliver."""
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_send_attempted("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    await repo.mark_bootstrap_failed(
        "u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="assumed dead", only_if_unacked=True
    )

    assert not await repo.mark_bootstrap_sent("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    settled = await repo.get("u1")
    assert settled.bootstrap_state == "failed"  # not silently re-opened
    # ...but the DELIVERY is still recorded against that row. It is the one
    # fact no other writer can reconstruct, and it turns the recorded reason's
    # "may double-deliver" into something an operator can act on.
    assert settled.bootstrap_sent_at is not None


async def test_a_late_ack_on_a_superseded_generation_annotates_nothing(repo):
    """Nothing left to annotate: a reprovision minted a new incarnation, so the
    old attempt's delivery is not a fact about THIS row."""
    dispatch_id = await _claimed(repo)
    await repo.put(_make_company(user_id="u1", onboarding_generation="01NEW", funnel_state="not_started"))

    assert not await repo.mark_bootstrap_sent("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    fresh = await repo.get("u1")
    assert fresh.bootstrap_sent_at is None
    assert fresh.onboarding_generation == "01NEW"


async def test_send_attempted_returns_false_when_the_claim_moved_on(repo):
    """The caller must not send when it no longer holds the claim."""
    await _claimed(repo)
    assert not await repo.mark_bootstrap_send_attempted(
        "u1", onboarding_generation="01G", dispatch_id="someone-elses-claim"
    )
    assert (await repo.get("u1")).bootstrap_send_attempted_at is None


async def test_terminal_marks_accept_a_sent_row(repo):
    """`sent` is a live post-claim state: the completion observer's
    delivered/failed writes sit on top of it unchanged."""
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_sent("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    await repo.mark_bootstrap_delivered("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    delivered = await repo.get("u1")
    assert delivered.bootstrap_state == "delivered"
    assert delivered.funnel_state == "complete"


async def test_terminal_failure_accepts_a_sent_row(repo):
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_sent("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    await repo.mark_bootstrap_failed("u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="chat.aborted")
    failed = await repo.get("u1")
    assert failed.bootstrap_state == "failed"
    assert failed.bootstrap_last_error == "chat.aborted"


async def test_rearm_refuses_a_claim_that_has_already_attempted_a_send(repo):
    """THE double-send race: a claimant judged dead may be merely SLOW and
    stamp its write-ahead between a recovery poll's read and its re-arm. The
    re-arm must be fenced on the STAMP, not just on state+dispatch_id — a
    re-armed row is immediately re-claimable, so a landed re-arm here means
    Friday runs twice."""
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_send_attempted("u1", onboarding_generation="01G", dispatch_id=dispatch_id)

    await repo.rearm_bootstrap_dispatch(
        "u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="assumed dead"
    )
    still = await repo.get("u1")
    assert still.bootstrap_state == "dispatched"  # untouched — the live claimant keeps it
    assert still.bootstrap_last_error is None
    # And the row stays unclaimable, so the recovering poll cannot send either.
    result, _ = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    assert result is BootstrapClaimResult.NOT_READY


async def test_only_if_unacked_refuses_to_fail_a_row_that_acked_in_the_window(repo):
    """The same race on the other branch: settling an ACKED row `failed` would
    mislabel a delivered kickoff and invite an admin reset into a second one."""
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_send_attempted("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    await repo.mark_bootstrap_sent("u1", onboarding_generation="01G", dispatch_id=dispatch_id)

    await repo.mark_bootstrap_failed(
        "u1",
        onboarding_generation="01G",
        dispatch_id=dispatch_id,
        reason="assumed dead mid-send",
        only_if_unacked=True,
    )
    assert (await repo.get("u1")).bootstrap_state == "sent"  # untouched

    # Without the flag (the completion observer's call) a terminal chat.error
    # still resolves that same row — the narrowing is opt-in, not a new rule.
    await repo.mark_bootstrap_failed("u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="chat.error")
    assert (await repo.get("u1")).bootstrap_state == "failed"


async def test_only_if_unacked_still_settles_a_genuinely_unacked_claim(repo):
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_send_attempted("u1", onboarding_generation="01G", dispatch_id=dispatch_id)

    await repo.mark_bootstrap_failed(
        "u1",
        onboarding_generation="01G",
        dispatch_id=dispatch_id,
        reason="ack never recorded",
        only_if_unacked=True,
    )
    settled = await repo.get("u1")
    assert settled.bootstrap_state == "failed"
    assert settled.bootstrap_last_error == "ack never recorded"


async def test_a_new_claim_clears_the_previous_attempts_stamps(repo):
    """A re-claimed row must start a CLEAN per-attempt record: a leftover
    send_attempted stamp would make the NEXT dead claim look ambiguous (never
    re-armed) when it provably sent nothing.

    Reached via the admin lever, since that is the only path that returns an
    ATTEMPTED row to not_started — the automatic re-arm is fenced against
    exactly this stamp (see test_rearm_refuses_a_claim_that_has_already_...)."""
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_send_attempted("u1", onboarding_generation="01G", dispatch_id=dispatch_id)
    assert await repo.reset_bootstrap_for_retry("u1", onboarding_generation="01G", expected_dispatch_id=dispatch_id)
    # Cleared by the reset itself, not merely by the next claim: between the two
    # an operator reading "send attempted at ..." on a not_started row would
    # misread what is in flight.
    assert (await repo.get("u1")).bootstrap_send_attempted_at is None

    _, second_id = await repo.claim_bootstrap_dispatch("u1", onboarding_generation="01G")
    reclaimed = await repo.get("u1")
    assert second_id != dispatch_id
    assert reclaimed.bootstrap_send_attempted_at is None
    assert reclaimed.bootstrap_sent_at is None


# ---------------------------------------------------------------------------
# reset_bootstrap_for_retry
# ---------------------------------------------------------------------------


async def test_reset_bootstrap_for_retry_recovers_an_expired_dispatched_claim(repo):
    """The admin lever's stuck-'dispatched' case (the row's claimant died).
    Fenced on the observed dispatch_id, so a claim that landed in between is
    never stomped."""
    dispatch_id = await _claimed(repo)
    await repo.mark_bootstrap_send_attempted("u1", onboarding_generation="01G", dispatch_id=dispatch_id)

    assert not await repo.reset_bootstrap_for_retry(
        "u1", onboarding_generation="01G", expected_dispatch_id="a-different-claim"
    )
    assert (await repo.get("u1")).bootstrap_state == "dispatched"

    assert await repo.reset_bootstrap_for_retry("u1", onboarding_generation="01G", expected_dispatch_id=dispatch_id)
    reset = await repo.get("u1")
    assert reset.bootstrap_state == "not_started"
    assert reset.bootstrap_attempts == 0
    assert reset.funnel_state == "submitted"


async def test_reset_bootstrap_for_retry_without_a_dispatch_id_still_only_takes_failed(repo):
    """The no-fence form is the original failed-only lever, unchanged."""
    dispatch_id = await _claimed(repo)
    assert not await repo.reset_bootstrap_for_retry("u1", onboarding_generation="01G")
    assert (await repo.get("u1")).bootstrap_state == "dispatched"
    await repo.mark_bootstrap_failed("u1", onboarding_generation="01G", dispatch_id=dispatch_id, reason="boom")
    assert await repo.reset_bootstrap_for_retry("u1", onboarding_generation="01G")
    assert (await repo.get("u1")).bootstrap_state == "not_started"


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
# first_contact claim / settle / attempts
#
# Once-only is inferred SERVER-SIDE from this state, never from a
# client-supplied `purpose` label, so an agent cannot skip the guard by
# mislabelling its send.
# ---------------------------------------------------------------------------


async def test_claim_first_contact_wins_fresh(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G"))
    assert await repo.claim_first_contact("u1", onboarding_generation="01G") is True
    assert (await repo.get("u1")).first_contact_state == "sending"


async def test_claim_first_contact_exactly_once(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G"))
    first = await repo.claim_first_contact("u1", onboarding_generation="01G")
    second = await repo.claim_first_contact("u1", onboarding_generation="01G")
    assert first is True
    assert second is False


async def test_claim_first_contact_is_generation_fenced(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G"))
    assert await repo.claim_first_contact("u1", onboarding_generation="WRONG") is False


async def test_settle_first_contact_marks_sent(repo):
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G"))
    await repo.claim_first_contact("u1", onboarding_generation="01G")
    await repo.settle_first_contact("u1", onboarding_generation="01G", state="sent")
    assert (await repo.get("u1")).first_contact_state == "sent"


async def test_failed_first_contact_is_reclaimable(repo):
    """A failed mint or SES send must not make the first failure terminal."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G"))
    await repo.claim_first_contact("u1", onboarding_generation="01G")
    await repo.settle_first_contact("u1", onboarding_generation="01G", state="failed", reason="ses_send_failed")
    assert await repo.claim_first_contact("u1", onboarding_generation="01G") is True


async def test_sent_first_contact_is_not_reclaimable(repo):
    """`sent` is terminal -- reclaiming it would double-send."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G"))
    await repo.claim_first_contact("u1", onboarding_generation="01G")
    await repo.settle_first_contact("u1", onboarding_generation="01G", state="sent")
    assert await repo.claim_first_contact("u1", onboarding_generation="01G") is False


async def test_first_contact_attempts_are_bounded(repo):
    """The counter is the SOLE bound on the nudge loop -- nudge runs
    re-register the completion callback, so without it this is infinite."""
    await repo.put(_make_company(user_id="u1", onboarding_generation="01G"))
    for _ in range(3):
        assert await repo.bump_first_contact_attempts("u1", onboarding_generation="01G", limit=3) is True
    assert await repo.bump_first_contact_attempts("u1", onboarding_generation="01G", limit=3) is False


async def test_onboarding_state_projects_first_contact_and_friday(repo):
    """send_to_owner reads these from the SAME single read compute_status does."""
    await repo.put(
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


# --- Pending brief (intake before the workspace can receive it) --------------


async def test_pending_brief_round_trip(repo):
    landed = await repo.put_pending_brief(
        "user_abc",
        path="grow",
        user_input="acme.com",
        detail=None,
        brief_text="work on acme.com",
        submitted_by="member_zed",
    )
    assert landed is True

    item = await repo.get_pending_brief("user_abc")
    assert item["owner_id"] == "user_abc"
    assert item["path"] == "grow"
    assert item["input"] == "acme.com"
    assert item["brief_text"] == "work on acme.com"
    assert item["submitted_by"] == "member_zed"
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


async def test_pending_brief_is_invisible_to_the_org_gsi(repo):
    """The sentinel item deliberately carries no org_id, so it can never be
    mistaken for a company row by find_owner_row / get_org_company_id."""
    await repo.put(_make_company(user_id="u1", org_id="org_x", onboarding_generation="01G"))
    await repo.put_pending_brief(
        "org_x", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="u1"
    )

    rows = await repo.list_by_org_id("org_x")
    assert [r["user_id"] for r in rows] == ["u1"]


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
    await repo.put(_make_company(user_id="u1", org_id="u1", onboarding_generation="01G"))
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
    await repo.put(_make_company(user_id="u1", org_id="u1", onboarding_generation="01G", funnel_state="complete"))
    await repo.put_pending_brief(
        "u1", path="new", user_input="an idea", detail=None, brief_text="build an idea", submitted_by="u1"
    )

    state = await repo.get_onboarding_state("u1")
    assert state["pending_brief"] is False
    assert state["funnel_state"] == "complete"


async def test_onboarding_state_still_reports_a_pending_brief_under_a_stuck_claim(repo):
    """funnel_state "submitted" with nothing seeded behind it is the stuck-claim
    case -- the drain has to stay able to see the brief and recover it."""
    await repo.put(_make_company(user_id="u1", org_id="u1", onboarding_generation="01G", funnel_state="submitted"))
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


async def test_get_org_company_id_skips_bare_seed_claim_rows(repo):
    """A bare seed-claim row (try_acquire_seed_claim) sits in the by-org-id GSI
    with NO company_id. A member joining an org that already has a company must
    still resolve it — the claim must never shadow the real row, whatever
    order the GSI hands them back in."""
    assert await repo.try_acquire_seed_claim("user_member", org_id="org_alpha", claim_id="c1")
    assert await repo.try_acquire_seed_claim("user_member_2", org_id="org_alpha", claim_id="c2")
    await repo.put(_make_company(user_id="user_owner", org_id="org_alpha", company_id="co_shared"))
    assert await repo.get_org_company_id("org_alpha") == "co_shared"
    # Claims alone (owner still seeding, no company yet) resolve to None.
    assert await repo.try_acquire_seed_claim("user_solo", org_id="org_beta", claim_id="c3")
    assert await repo.get_org_company_id("org_beta") is None


async def test_seed_claim_reclaims_credential_less_failed_marker(repo):
    """A failed marker with no paperclip_user_id (pre-sign-up failure) is
    reclaimed atomically — its retry re-runs the sign-up, so concurrent
    retries must be fenced exactly like a fresh row."""
    assert await repo.put_failed_marker_if_absent(
        user_id="user_m", org_id="org_alpha", reason="boom", company_id="co_1"
    )
    assert await repo.try_acquire_seed_claim("user_m", org_id="org_alpha", claim_id="c1")
    raw = repo._table().get_item(Key={"user_id": "user_m"}, ConsistentRead=True)["Item"]
    assert raw["status"] == "seeding" and raw["seeding_claim_id"] == "c1"
    assert "paperclip_user_id" not in raw
    # Now a bare live claim: a second claimant loses.
    assert not await repo.try_acquire_seed_claim("user_m", org_id="org_alpha", claim_id="c2")


async def test_seed_claim_leaves_failed_row_with_credentials_alone(repo):
    """A failed row that carries paperclip_user_id is adopted (signed in), not
    re-signed-up — no fence needed, and it must never be overwritten."""
    row = _make_company(user_id="user_k", org_id="org_alpha", company_id="co_1")
    row.status = "failed"
    await repo.put(row)
    assert not await repo.try_acquire_seed_claim("user_k", org_id="org_alpha", claim_id="c1")
    after = await repo.get("user_k", consistent_read=True)
    assert after.status == "failed" and after.paperclip_user_id == row.paperclip_user_id


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
    assert await repo.try_acquire_seed_claim("user_legacy", org_id="org_alpha", claim_id="c1")
    raw = repo._table().get_item(Key={"user_id": "user_legacy"}, ConsistentRead=True)["Item"]
    assert raw["status"] == "seeding" and raw["seeding_claim_id"] == "c1"
