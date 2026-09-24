"""One-off migration: one workspace row per org + identity-only human rows,
plus the Clerk-vs-Paperclip membership drift reconcile. Delete with the script."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

import scripts.migrate_paperclip_org_rows as mig

TABLE = "paperclip-companies"
NOW = "2026-09-01T00:00:00+00:00"
LATER = "2026-09-02T00:00:00+00:00"


@pytest.fixture
def table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "org_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-org-id",
                    "KeySchema": [{"AttributeName": "org_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        with patch("core.dynamodb._table_prefix", ""), patch("core.dynamodb._dynamodb_resource", resource):
            yield t


def _legacy(user_id, org_id="org_a", company_id="co_a", status="active", created_at=NOW, **extra):
    item = {
        "user_id": user_id,
        "org_id": org_id,
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
        "paperclip_user_id": f"pc_{user_id}",
        "paperclip_password_encrypted": "enc",
        "paperclip_email": f"{user_id}@example.com",
        "service_token_encrypted": "svc",
    }
    if company_id:
        item["company_id"] = company_id
    item.update(extra)
    return item


def _seed(table, *items):
    for it in items:
        table.put_item(Item=it)


def _dump(table):
    return sorted(table.scan()["Items"], key=lambda it: it["user_id"])


# --- plan_migration (pure) --------------------------------------------------


def test_plan_picks_the_marked_owner_row_and_active_members():
    items = [
        _legacy("user_member", created_at=NOW),
        _legacy(
            "user_owner",
            created_at=LATER,
            starter_agent_ids={"ceo": "a1"},
            onboarding_generation="01G",
            funnel_state="complete",
        ),
        _legacy("user_gone", status="disabled"),
        {"user_id": "__org_create_claim__:org_a", "kind": "org_create_claim"},
    ]
    plan = mig.plan_migration(items)
    [op] = plan.orgs
    assert op.org_id == "org_a" and op.company_id == "co_a" and not op.existing
    assert op.item["provisioner_user_id"] == "user_owner"
    assert op.item["starter_agent_ids"] == {"ceo": "a1"} and op.item["funnel_state"] == "complete"
    assert op.item["member_user_ids"] == {"user_member", "user_owner"}
    assert not {"paperclip_password_encrypted", "service_token_encrypted"} & set(op.item)
    assert set(plan.strips) == {"user_member", "user_owner", "user_gone"}
    assert "org_id" in plan.strips["user_owner"] and "service_token_encrypted" in plan.strips["user_owner"]
    assert "paperclip_email" not in plan.strips["user_owner"]


def test_plan_keeps_claim_state_on_an_unconfirmed_signup():
    claim = {
        "user_id": "user_new",
        "org_id": "org_a",
        "status": "seeding",
        "seeding_claim_id": "c1",
        "seeding_claimed_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    plan = mig.plan_migration([_legacy("user_owner"), claim])
    assert plan.strips["user_new"] == ["org_id"]


def test_plan_aborts_listing_every_mismatch():
    items = [
        _legacy("u1", org_id="org_a", company_id="co_1"),
        _legacy("u2", org_id="org_a", company_id="co_2"),
        _legacy("u3", org_id="org_b", company_id=None, status="failed"),
        _legacy("u4", org_id="user_personal", company_id="co_p"),
    ]
    with pytest.raises(mig.MigrationAbort) as exc:
        mig.plan_migration(items)
    msg = str(exc.value)
    assert "org_a: 2 distinct" in msg and "org_b: 0 distinct" in msg and "user_personal" in msg


def test_plan_aborts_when_an_existing_workspace_row_names_another_company():
    existing = {"user_id": "org_a", "org_id": "org_a", "company_id": "co_other", "status": "active"}
    with pytest.raises(mig.MigrationAbort, match="co_other"):
        mig.plan_migration([existing, _legacy("u1")])


# --- apply ------------------------------------------------------------------


async def test_apply_writes_the_workspace_row_strips_humans_and_is_idempotent(table):
    _seed(table, _legacy("user_owner", starter_agent_ids={"ceo": "a1"}), _legacy("user_member", created_at=LATER))
    await mig.apply_migration(mig.plan_migration(await mig.scan_items()))

    rows = {it["user_id"]: it for it in _dump(table)}
    ws = rows["org_a"]
    assert ws["company_id"] == "co_a" and ws["provisioner_user_id"] == "user_owner"
    assert ws["member_user_ids"] == {"user_owner", "user_member"}
    assert set(rows["user_member"]) == mig.IDENTITY_FIELDS

    before = _dump(table)
    plan = mig.plan_migration(await mig.scan_items())
    assert plan.orgs == [] and plan.strips == {}
    await mig.apply_migration(plan)
    assert _dump(table) == before


async def test_apply_replaces_a_company_less_failed_marker_but_never_a_real_row(table):
    _seed(table, {"user_id": "org_a", "org_id": "org_a", "status": "failed", "created_at": NOW, "updated_at": NOW})
    _seed(table, _legacy("user_owner"))
    await mig.apply_migration(mig.plan_migration(await mig.scan_items()))
    assert table.get_item(Key={"user_id": "org_a"})["Item"]["company_id"] == "co_a"


# --- drift ------------------------------------------------------------------


def _admin(members):
    return SimpleNamespace(list_members=AsyncMock(return_value={"members": members}))


def _clerk(rosters):
    async def list_members(org_id, *, strict=False):
        assert strict, "drift must read Clerk strictly"
        value = rosters[org_id]
        if isinstance(value, Exception):
            raise value
        return [{"user_id": uid, "email": f"{uid}@example.com"} for uid in value]

    return list_members


async def test_drift_report_classifies_missing_extra_protected_unknown():
    members = [
        {"principalId": "pc_keep"},
        {"principalId": "pc_extra"},
        {"principalId": "pc_admin", "user": {"email": "admin@isol8.co"}},
        {"principalId": "pc_vendor"},
        {"principalId": "pc_nobody"},
    ]
    p2u = {"pc_keep": "user_keep", "pc_extra": "user_extra", "pc_vendor": "user_vendor", "pc_admin": "user_admin"}
    rosters = {"org_a": ["user_keep", "user_missing"], "org_vendor": ["user_vendor"]}
    with (
        patch.object(mig.clerk_admin, "list_organization_members", _clerk(rosters)),
        patch.object(mig.settings, "VENDOR_ORG_ID", "org_vendor"),
        patch.object(mig, "BOOTSTRAP_EMAILS", frozenset({"admin@isol8.co"})),
    ):
        [d] = await mig.compute_drift(
            [("org_a", "co_a")], principal_to_user=p2u, identity_emails={}, admin=_admin(members), cookie="c"
        )
    assert d.missing == {"user_missing"}
    assert d.extra == {"user_extra"}
    assert d.protected == {"user_admin", "user_vendor"}
    assert d.unknown == {"pc_nobody"}


async def test_drift_never_archives_when_the_clerk_roster_reads_empty():
    with (
        patch.object(mig.clerk_admin, "list_organization_members", _clerk({"org_a": []})),
        patch.object(mig.settings, "VENDOR_ORG_ID", ""),
    ):
        [d] = await mig.compute_drift(
            [("org_a", "co_a")],
            principal_to_user={"pc_x": "user_x"},
            identity_emails={},
            admin=_admin([{"principalId": "pc_x"}]),
            cookie="c",
        )
    assert d.clerk_empty and d.extra == set()


async def test_a_clerk_read_failure_aborts_the_drift():
    with (
        patch.object(mig.clerk_admin, "list_organization_members", _clerk({"org_a": RuntimeError("clerk 503")})),
        patch.object(mig.settings, "VENDOR_ORG_ID", ""),
        pytest.raises(RuntimeError, match="clerk 503"),
    ):
        await mig.compute_drift(
            [("org_a", "co_a")], principal_to_user={}, identity_emails={}, admin=_admin([]), cookie="c"
        )


async def test_apply_drift_joins_missing_and_archives_only_true_extras(table):
    _seed(
        table,
        {
            "user_id": "org_a",
            "org_id": "org_a",
            "company_id": "co_a",
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
            "member_user_ids": {"user_keep", "user_missing", "user_stale"},
        },
    )
    drift = mig.Drift(
        org_id="org_a",
        company_id="co_a",
        clerk={"user_keep", "user_missing"},
        missing={"user_missing"},
        extra={"user_extra"},
        protected={"user_admin", "user_vendor"},
        unknown={"pc_nobody"},
        in_company={"user_keep", "user_extra", "user_vendor"},
        emails={"user_missing": "m@example.com"},
    )
    from core.repositories.paperclip_repo import PaperclipRepo

    provisioning = SimpleNamespace(_repo=PaperclipRepo(table_name=TABLE), archive_member=AsyncMock())
    join = AsyncMock()
    with patch("core.services.user_provision._ensure_paperclip_for_member", join):
        await mig.apply_drift([drift], provisioning=provisioning)

    join.assert_awaited_once_with(org_id="org_a", member_user_id="user_missing", member_email="m@example.com")
    provisioning.archive_member.assert_awaited_once_with(user_id="user_extra", org_id="org_a")
    # The recorded membership mirrors the company for everyone Clerk keeps; the
    # stale entry goes (the join re-adds the missing member itself).
    members = table.get_item(Key={"user_id": "org_a"})["Item"]["member_user_ids"]
    assert members == {"user_keep"}


# --- main -------------------------------------------------------------------


async def _run_main(argv, rosters):
    provisioning = SimpleNamespace(_admin=SimpleNamespace(_http=None, list_members=AsyncMock(return_value=[])))
    with (
        patch(
            "core.services.paperclip_provisioning.build_paperclip_provisioning", AsyncMock(return_value=provisioning)
        ),
        patch("core.services.paperclip_provisioning.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.paperclip_admin_session.get_admin_session_cookie", AsyncMock(return_value="c")),
        patch.object(mig.clerk_admin, "list_organization_members", _clerk(rosters)),
        patch.object(mig.settings, "VENDOR_ORG_ID", ""),
    ):
        return await mig.main(argv)


async def test_dry_run_is_the_default_and_writes_nothing(table, capsys):
    _seed(table, _legacy("user_owner"))
    before = _dump(table)
    assert await _run_main([], {"org_a": ["user_owner"]}) == 0
    assert _dump(table) == before
    out = capsys.readouterr().out
    assert "DRIFT REPORT" in out and "DRY RUN" in out and "org_a: company=co_a" in out


async def test_a_mismatch_aborts_before_any_write(table):
    _seed(table, _legacy("u1", company_id="co_1"), _legacy("u2", company_id="co_2"))
    before = _dump(table)
    with pytest.raises(mig.MigrationAbort):
        await _run_main(["--apply"], {"org_a": ["u1", "u2"]})
    assert _dump(table) == before
