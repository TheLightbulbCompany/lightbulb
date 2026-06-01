"""Guard-logic tests for the orphan-member recovery script.

Only the pure ``plan_recovery`` decision function is unit-tested — the AWS
calls (RDS Data API + DynamoDB) are thin wrappers exercised manually with
``--apply``. The decision function is where the safety lives: refuse
bootstrap admins, refuse ambiguous email matches, refuse when there's
nothing to recover.
"""

import pytest

from scripts.recover_orphan_paperclip_member import BOOTSTRAP_EMAILS, plan_recovery


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
