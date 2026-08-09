"""Tests for the ONE-SHOT orphan sweep (scripts/sweep_wipe_orphans.py).

DELETE ALONGSIDE THE SCRIPT once it has been run. Kept only so the sweep is
not run against prod on trust: the org guard below is the difference between
cleaning a historical backlog and deleting every live Teams org's slug,
mailbox and integrations.
"""

from unittest.mock import AsyncMock, patch

import pytest

# Imports of core.* happen INSIDE sweep_orphans (the table-prefix guard must
# assert before core.config binds table names), so patch targets are the
# source modules, not attributes on this script.
from scripts.sweep_wipe_orphans import sweep_orphans

# from every table you could enumerate it from. This is that one-shot.


@pytest.mark.asyncio
async def test_sweep_orphans_dry_run_reports_without_deleting():

    integrations_delete = AsyncMock()
    email_delete = AsyncMock()
    slug_delete = AsyncMock()

    with (
        patch("core.repositories.user_repo.list_all", new=AsyncMock(return_value=[{"user_id": "live_1"}])),
        patch(
            "core.repositories.integrations_repo.scan_all",
            new=AsyncMock(return_value=[{"owner_id": "dead_1", "provider": "slack"}]),
        ),
        patch("core.repositories.integrations_repo.delete", new=integrations_delete),
        patch(
            "core.repositories.agent_email_repo.scan_all",
            new=AsyncMock(return_value=[{"owner_id": "dead_1", "address": "a@x.co"}]),
        ),
        patch("core.repositories.agent_email_repo.delete_by_address", new=email_delete),
        patch(
            "core.repositories.company_slug_repo.scan_all",
            new=AsyncMock(return_value=[{"owner_id": "dead_1", "slug": "acme"}]),
        ),
        patch("core.repositories.company_slug_repo.delete_for_owner", new=slug_delete),
    ):
        report = await sweep_orphans(dry_run=True)

    assert report.dry_run is True
    assert {s.status for s in report.steps} == {"would_delete"}
    # A dry run that deletes is the worst possible bug in a sweep.
    integrations_delete.assert_not_awaited()
    email_delete.assert_not_awaited()
    slug_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_orphans_never_touches_a_live_owner():
    """The whole safety property. An owner still in the users table is alive,
    and its credential rows must survive the sweep untouched."""

    integrations_delete = AsyncMock()

    with (
        patch(
            "core.repositories.user_repo.list_all",
            new=AsyncMock(return_value=[{"user_id": "live_1"}, {"user_id": "live_2"}]),
        ),
        patch(
            "core.repositories.integrations_repo.scan_all",
            new=AsyncMock(
                return_value=[
                    {"owner_id": "live_1", "provider": "slack"},
                    {"owner_id": "dead_9", "provider": "github"},
                    {"owner_id": "live_2", "provider": "notion"},
                ]
            ),
        ),
        patch("core.repositories.integrations_repo.delete", new=integrations_delete),
        patch("core.repositories.agent_email_repo.scan_all", new=AsyncMock(return_value=[])),
        patch("core.repositories.company_slug_repo.scan_all", new=AsyncMock(return_value=[])),
    ):
        report = await sweep_orphans(dry_run=False)

    integrations_delete.assert_awaited_once_with("dead_9", "github")
    step = next(s for s in report.steps if s.system == "integrations")
    assert step.status == "ok"
    assert "dead_9" in step.detail and "live_1" not in step.detail


@pytest.mark.asyncio
async def test_sweep_orphans_reports_empty_tables_as_skipped():

    with (
        patch("core.repositories.user_repo.list_all", new=AsyncMock(return_value=[{"user_id": "live_1"}])),
        patch("core.repositories.integrations_repo.scan_all", new=AsyncMock(return_value=[])),
        patch("core.repositories.agent_email_repo.scan_all", new=AsyncMock(return_value=[])),
        patch("core.repositories.company_slug_repo.scan_all", new=AsyncMock(return_value=[])),
    ):
        report = await sweep_orphans(dry_run=False)

    assert {s.status for s in report.steps} == {"skipped"}


@pytest.mark.asyncio
async def test_sweep_never_touches_a_live_orgs_rows():
    """The bug this guard exists for, and it was nearly run against prod.

    `_live_owner_ids` reads the users table, which the Clerk user.created
    webhook populates with `user_...` ids ONLY — no path writes an `org_...`
    id there. But company-slugs, agent-email-addresses and integrations are
    keyed by the fleet-wide owner_id convention, which for a Teams account IS
    the org id. Diffing org-keyed rows against a personal-only liveness set
    marks every LIVE org as orphaned: releasing a paying org's slug for
    someone else to claim, deleting Friday's still-in-use mailbox, and
    dropping its integrations.
    """

    slug_delete = AsyncMock()
    email_delete = AsyncMock()
    integrations_delete = AsyncMock()

    with (
        patch("core.repositories.user_repo.list_all", new=AsyncMock(return_value=[{"user_id": "user_live"}])),
        patch(
            "core.repositories.company_slug_repo.scan_all",
            new=AsyncMock(return_value=[{"owner_id": "org_live_paying", "slug": "acme"}]),
        ),
        patch("core.repositories.company_slug_repo.delete_for_owner", new=slug_delete),
        patch(
            "core.repositories.agent_email_repo.scan_all",
            new=AsyncMock(return_value=[{"owner_id": "org_live_paying", "address": "friday@acme.isol8.co"}]),
        ),
        patch("core.repositories.agent_email_repo.delete_by_address", new=email_delete),
        patch(
            "core.repositories.integrations_repo.scan_all",
            new=AsyncMock(return_value=[{"owner_id": "org_live_paying", "provider": "slack"}]),
        ),
        patch("core.repositories.integrations_repo.delete", new=integrations_delete),
    ):
        report = await sweep_orphans(dry_run=False)

    slug_delete.assert_not_awaited()
    email_delete.assert_not_awaited()
    integrations_delete.assert_not_awaited()
    assert all(s.status == "skipped" for s in report.steps), report.steps
    # And it says so, rather than silently reporting "no orphans".
    assert all("org-keyed" in s.detail for s in report.steps), report.steps


@pytest.mark.asyncio
async def test_one_failing_row_does_not_strand_the_rest_of_the_table():
    """Matches owner_secret_sweep's per-row tolerance. A single bad row used to
    abort the loop, leaving every orphan after it undeleted behind one opaque
    'failed' with no record of what did or did not go."""

    integrations_delete = AsyncMock(side_effect=[RuntimeError("ddb blip"), None, None])

    with (
        patch("core.repositories.user_repo.list_all", new=AsyncMock(return_value=[{"user_id": "user_live"}])),
        patch(
            "core.repositories.integrations_repo.scan_all",
            new=AsyncMock(
                return_value=[
                    {"owner_id": "user_dead1", "provider": "a"},
                    {"owner_id": "user_dead2", "provider": "b"},
                    {"owner_id": "user_dead3", "provider": "c"},
                ]
            ),
        ),
        patch("core.repositories.integrations_repo.delete", new=integrations_delete),
        patch("core.repositories.agent_email_repo.scan_all", new=AsyncMock(return_value=[])),
        patch("core.repositories.company_slug_repo.scan_all", new=AsyncMock(return_value=[])),
    ):
        report = await sweep_orphans(dry_run=False)

    assert integrations_delete.await_count == 3
    step = next(s for s in report.steps if s.system == "integrations")
    assert "deleted 2/3" in step.detail and "1 failed" in step.detail
