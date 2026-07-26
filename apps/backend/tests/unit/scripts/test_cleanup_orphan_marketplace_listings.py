"""Tests for the pure planning core of cleanup_orphan_marketplace_listings.

The planner decides which marketplace listing rows to delete. The safety-critical
property is that it NEVER deletes a published or retired row (both resolve buyer
entitlements / back live deploys) — only dead taken_down rows and old orphan drafts.
"""

from __future__ import annotations

from scripts.cleanup_orphan_marketplace_listings import _plan_deletions, _s3_keys, _ts

# A fixed "now" so orphan-draft age math is deterministic. 2026-06-25T00:00:00Z.
NOW = _ts("2026-06-25T00:00:00+00:00")


def _row(
    listing_id,
    version,
    status,
    *,
    agent_id=None,
    published_at=None,
    created_at="2026-06-01T00:00:00+00:00",
    artifact_uploaded=True,
    seller_id="seller_x",
):
    return {
        "listing_id": listing_id,
        "version": version,
        "status": status,
        "agent_id": agent_id,
        "published_at": published_at,
        "created_at": created_at,
        "artifact_uploaded": artifact_uploaded,
        "seller_id": seller_id,
        "s3_prefix": f"listings/{listing_id}/v{version}/",
    }


def _deleted_ids(plan):
    return {(d["listing_id"], int(d["version"])) for d in plan["delete"]}


def test_taken_down_rows_are_deleted():
    rows = [
        _row("live", 1, "published", agent_id="agent_b", published_at="2026-06-25T00:00:00+00:00"),
        _row("old", 1, "taken_down", agent_id="agent_a"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert _deleted_ids(plan) == {("old", 1)}


def test_cross_uuid_dupe_old_taken_down_deleted_new_kept():
    # Echo-shaped: an old listing was taken down (its own agent UUID) and a fresh
    # one republished under a new agent UUID. Only the taken_down one goes.
    rows = [
        _row("echo_new", 1, "published", agent_id="agent_6f23", published_at="2026-06-25T00:00:00+00:00"),
        _row("echo_old", 1, "taken_down", agent_id="agent_4407"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert _deleted_ids(plan) == {("echo_old", 1)}


def test_multiple_published_per_agent_are_all_kept():
    # Two published listings for one agent is anomalous, but this cleaner never
    # hard-deletes a published row (it may carry live entitlements) — removing a
    # genuine duplicate is a takedown decision, not a cleanup.
    rows = [
        _row("dup_a", 1, "published", agent_id="agent_z", published_at="2026-06-10T00:00:00+00:00"),
        _row("dup_b", 1, "published", agent_id="agent_z", published_at="2026-06-20T00:00:00+00:00"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert plan["delete"] == []


def test_retired_predecessor_of_keeper_is_kept():
    # A normal version chain: v1 retired by the atomic flip, v2 published.
    # Both belong to the KEPT listing_id — neither is deleted (entitlements).
    rows = [
        _row("keeper", 1, "retired", agent_id="agent_k"),
        _row("keeper", 2, "published", agent_id="agent_k", published_at="2026-06-22T00:00:00+00:00"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert plan["delete"] == []


def test_second_published_listing_and_its_retired_rows_are_never_deleted():
    # Even a second (duplicate) published listing for the same agent is left
    # untouched — published AND retired rows are protected, so a buyer who
    # installed from either listing keeps a resolvable deploy.
    rows = [
        _row("winner", 1, "published", agent_id="agent_w", published_at="2026-06-24T00:00:00+00:00"),
        _row("loser", 1, "retired", agent_id="agent_w"),
        _row("loser", 2, "published", agent_id="agent_w", published_at="2026-06-10T00:00:00+00:00"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert plan["delete"] == []


def test_taken_down_is_only_thing_removed_in_mixed_statuses():
    # Invariant smoke test: across all statuses for one agent, only the
    # taken_down row leaves; published / retired / review / recent-draft stay.
    rows = [
        _row("L", 4, "published", agent_id="a", published_at="2026-06-24T00:00:00+00:00"),
        _row("L", 3, "retired", agent_id="a"),
        _row("L", 2, "taken_down", agent_id="a"),
        _row("L", 1, "review", agent_id="a"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert _deleted_ids(plan) == {("L", 2)}
    assert all(d["status"] == "taken_down" for d in plan["delete"])


def test_v0_slug_sentinels_are_ignored():
    rows = [
        _row("live", 1, "published", agent_id="agent_b", published_at="2026-06-25T00:00:00+00:00"),
        _row("live", 0, "published", agent_id=None),  # slug sentinel
        _row("orphan-slug", 0, "draft", agent_id=None),  # dangling sentinel
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert plan["delete"] == []  # sentinels never touched here


def test_orphan_draft_old_and_unuploaded_is_deleted():
    rows = [
        _row("stale_draft", 1, "draft", artifact_uploaded=False, created_at="2026-04-01T00:00:00+00:00"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert _deleted_ids(plan) == {("stale_draft", 1)}


def test_recent_draft_is_kept():
    rows = [
        _row("fresh_draft", 1, "draft", artifact_uploaded=False, created_at="2026-06-24T00:00:00+00:00"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert plan["delete"] == []


def test_uploaded_draft_is_not_an_orphan():
    # Has an artifact (artifact_uploaded True) → seller's in-progress work, keep it
    # even when old.
    rows = [
        _row("wip_draft", 1, "draft", artifact_uploaded=True, created_at="2026-01-01T00:00:00+00:00"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert plan["delete"] == []


def test_review_row_is_kept():
    rows = [_row("pending", 1, "review", agent_id="agent_r")]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert plan["delete"] == []


def test_full_inventory_mixed():
    rows = [
        # agent_a: live published + 2 dead taken_down predecessors
        _row("a_live", 1, "published", agent_id="agent_a", published_at="2026-06-24T00:00:00+00:00"),
        _row("a_dead1", 1, "taken_down", agent_id="agent_a_old1"),
        _row("a_dead2", 1, "taken_down", agent_id="agent_a_old2"),
        # agent_b: keeper chain (retired v1 + published v2) — both kept
        _row("b_keeper", 1, "retired", agent_id="agent_b"),
        _row("b_keeper", 2, "published", agent_id="agent_b", published_at="2026-06-20T00:00:00+00:00"),
        # slug sentinels — ignored
        _row("a_live", 0, "published", agent_id=None),
        _row("b_keeper", 0, "published", agent_id=None),
        # an old orphan draft
        _row("junk", 1, "draft", artifact_uploaded=False, created_at="2026-01-01T00:00:00+00:00"),
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert _deleted_ids(plan) == {("a_dead1", 1), ("a_dead2", 1), ("junk", 1)}
    # keepers intact
    kept = {(r["listing_id"], int(r["version"])) for r in rows} - _deleted_ids(plan)
    assert ("a_live", 1) in kept
    assert ("b_keeper", 1) in kept
    assert ("b_keeper", 2) in kept


def test_decimal_version_handled():
    # boto3 resource returns numbers as Decimal — int() must work.
    from decimal import Decimal

    rows = [
        _row("live", Decimal(1), "published", agent_id="agent_b", published_at="2026-06-25T00:00:00+00:00"),
        _row("dead", Decimal(1), "taken_down", agent_id="agent_a"),
        _row("live", Decimal(0), "published", agent_id=None),  # sentinel
    ]
    plan = _plan_deletions(rows, max_age_days=30, now=NOW)
    assert _deleted_ids(plan) == {("dead", 1)}


def test_s3_keys_uses_prefix_then_falls_back():
    with_prefix = {"listing_id": "x", "version": 2, "s3_prefix": "listings/x/v2/"}
    assert _s3_keys(with_prefix) == [
        "listings/x/v2/workspace.tar.gz",
        "listings/x/v2/manifest.json",
        "listings/x/v2/openclaw-slice.json",
    ]
    without_prefix = {"listing_id": "y", "version": 3}
    assert _s3_keys(without_prefix)[0] == "listings/y/v3/workspace.tar.gz"


def test_ts_parses_iso_epoch_and_blank():
    assert _ts("2026-06-25T00:00:00+00:00") > 0
    assert _ts("1750000000") == 1750000000.0
    assert _ts(None) == 0.0
    assert _ts("") == 0.0
    assert _ts("not-a-date") == 0.0
