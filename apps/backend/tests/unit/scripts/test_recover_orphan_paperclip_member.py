"""Guard-logic tests for the orphan-member recovery script.

Only the pure ``plan_recovery`` decision function is unit-tested — the AWS
calls (RDS Data API + DynamoDB) are thin wrappers exercised manually with
``--apply``. The decision function is where the safety lives: refuse
bootstrap admins, refuse ambiguous email matches, refuse when there's
nothing to recover.
"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.recover_orphan_paperclip_member import BOOTSTRAP_EMAILS, _ddb_failed_rows, plan_recovery


def test_refuses_bootstrap_admin():
    with pytest.raises(SystemExit):
        plan_recovery(
            email=next(iter(BOOTSTRAP_EMAILS)),
            aurora_rows=[],
            ddb_failed_rows=[],
        )


def test_plans_delete_for_single_orphan():
    plan = plan_recovery(
        email="member@example.com",
        aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
        ddb_failed_rows=[{"user_id": "user_clerk_x", "status": "failed"}],
    )
    assert plan.aurora_user_id == "usr_orphan"
    assert plan.ddb_user_ids == ["user_clerk_x"]


def test_refuses_ambiguous_email():
    with pytest.raises(SystemExit):
        plan_recovery(
            email="m@x.co",
            aurora_rows=[{"id": "a", "email": "m@x.co"}, {"id": "b", "email": "m@x.co"}],
            ddb_failed_rows=[],
        )


def test_no_aurora_match_is_safe_exit():
    with pytest.raises(SystemExit):
        plan_recovery(
            email="absent@x.co",
            aurora_rows=[{"id": "a", "email": "other@x.co"}],
            ddb_failed_rows=[],
        )


def test_only_failed_ddb_rows_are_planned_for_delete():
    """A member's recovery must not delete an active DDB row that happens to
    share the email — only ``status="failed"`` rows are in scope."""
    plan = plan_recovery(
        email="member@example.com",
        aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
        ddb_failed_rows=[
            {"user_id": "user_failed", "status": "failed"},
            {"user_id": "user_active", "status": "active"},
        ],
    )
    assert plan.ddb_user_ids == ["user_failed"]


def test_refuses_multiple_failed_rows_without_member_user_id():
    """Recovering ONE member must not wipe other members' failed rows in the
    same org. When more than one failed row is in scope and no member_user_id
    is given to disambiguate, refuse rather than delete them all."""
    with pytest.raises(SystemExit):
        plan_recovery(
            email="member@example.com",
            aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
            ddb_failed_rows=[
                {"user_id": "user_a", "status": "failed"},
                {"user_id": "user_b", "status": "failed"},
            ],
        )


def test_member_user_id_scopes_ddb_to_that_member():
    """With member_user_id supplied, the DDB deletion targets exactly that
    member's failed row — never the other failed rows in the org."""
    plan = plan_recovery(
        email="member@example.com",
        aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
        ddb_failed_rows=[
            {"user_id": "user_a", "status": "failed"},
            {"user_id": "user_b", "status": "failed"},
        ],
        member_user_id="user_b",
    )
    assert plan.ddb_user_ids == ["user_b"]


def test_member_user_id_not_failed_yields_no_ddb_delete():
    """If the named member has no failed row (e.g. already cleaned, or only an
    active row), the DDB side is a no-op — Aurora-only recovery."""
    plan = plan_recovery(
        email="member@example.com",
        aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
        ddb_failed_rows=[{"user_id": "user_a", "status": "failed"}],
        member_user_id="user_other",
    )
    assert plan.ddb_user_ids == []


def test_ddb_failed_rows_paginates_across_pages():
    """The DDB scan MUST follow LastEvaluatedKey — a failed row on page 2 must
    be seen, or the disambiguation guard could be bypassed (Codex P2)."""

    def _item(uid, status, org):
        return {"user_id": {"S": uid}, "status": {"S": status}, "org_id": {"S": org}}

    fake_ddb = MagicMock()
    fake_ddb.scan.side_effect = [
        {"Items": [_item("user_a", "failed", "org_x")], "LastEvaluatedKey": {"user_id": {"S": "user_a"}}},
        {"Items": [_item("user_b", "failed", "org_x"), _item("user_c", "active", "org_x")]},
    ]

    with patch("scripts.recover_orphan_paperclip_member.boto3.client", return_value=fake_ddb):
        rows = _ddb_failed_rows("prod", owner_id="org_x")

    # Both failed rows across both pages are returned; the active row is dropped.
    assert {r["user_id"] for r in rows} == {"user_a", "user_b"}
    # Second scan used the ExclusiveStartKey from page 1.
    assert fake_ddb.scan.call_count == 2
    assert fake_ddb.scan.call_args_list[1].kwargs.get("ExclusiveStartKey") == {"user_id": {"S": "user_a"}}
