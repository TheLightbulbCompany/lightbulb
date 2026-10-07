"""Guard-logic tests for the orphan-member recovery script.

Only the pure ``plan_recovery`` decision function is unit-tested — the AWS
calls (RDS Data API + DynamoDB) are thin wrappers exercised manually with
``--apply``. The decision function is where the safety lives: refuse
bootstrap admins, refuse ambiguous email matches, refuse when there's
nothing to recover.
"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.recover_orphan_paperclip_member import (
    BOOTSTRAP_EMAILS,
    _ddb_failed_rows,
    _secret_arn,
    plan_recovery,
)


def test_bootstrap_emails_is_the_single_shared_constant():
    """One definition, imported everywhere. Three copies of this pair existed;
    narrowing one of them would have left the others protecting the old set."""
    from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS as canonical

    assert BOOTSTRAP_EMAILS is canonical


def test_refuses_bootstrap_admin():
    with pytest.raises(SystemExit):
        plan_recovery(
            email=next(iter(BOOTSTRAP_EMAILS)),
            aurora_rows=[],
            ddb_failed_rows=[],
        )


def test_plans_delete_for_single_orphan():
    """Deleting a failed DDB row requires the explicit Clerk user_id (the row
    can't be correlated to the email)."""
    plan = plan_recovery(
        email="member@example.com",
        aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
        ddb_failed_rows=[{"user_id": "user_clerk_x", "status": "failed"}],
        member_user_id="user_clerk_x",
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
    share the org — only ``status="failed"`` rows are in scope, and only the
    named member's."""
    plan = plan_recovery(
        email="member@example.com",
        aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
        ddb_failed_rows=[
            {"user_id": "user_failed", "status": "failed"},
            {"user_id": "user_active", "status": "active"},
        ],
        member_user_id="user_failed",
    )
    assert plan.ddb_user_ids == ["user_failed"]


def test_refuses_any_failed_row_without_member_user_id():
    """A failed DDB row carries no email and can't be correlated to the target,
    so deleting one ALWAYS requires --user-id — even when it's the only failed
    row in scope (it might belong to an unrelated member; --owner-id could be
    mistyped). Refuse rather than guess."""
    with pytest.raises(SystemExit):
        plan_recovery(
            email="member@example.com",
            aurora_rows=[{"id": "usr_orphan", "email": "member@example.com"}],
            ddb_failed_rows=[{"user_id": "user_a", "status": "failed"}],
        )


def test_refuses_multiple_failed_rows_without_member_user_id():
    """Same refusal with multiple failed rows in scope."""
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


def test_secret_arn_paginates_secrets_manager():
    """The secret can be on a later list_secrets page — _secret_arn must use
    the paginator, or a busy account falsely reports 'not found' and blocks
    recovery (Codex P2)."""
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"SecretList": [{"Name": "other/secret", "ARN": "arn:other"}]},
        {"SecretList": [{"Name": "lightbulb-prod-paperclip-db-credentials-AbCd", "ARN": "arn:target"}]},
    ]
    fake_sm = MagicMock()
    fake_sm.get_paginator.return_value = paginator

    with patch("scripts.recover_orphan_paperclip_member.boto3.client", return_value=fake_sm):
        arn = _secret_arn("prod")

    assert arn == "arn:target"
    fake_sm.get_paginator.assert_called_once_with("list_secrets")


@pytest.mark.parametrize("membership_count", [1, 2])
def test_refuses_identity_with_existing_company_memberships(membership_count):
    with pytest.raises(SystemExit, match="membership"):
        plan_recovery(
            email="member@example.com",
            aurora_rows=[{"id": "pc_existing", "email": "member@example.com", "membership_count": membership_count}],
            ddb_failed_rows=[{"user_id": "new_clerk", "status": "failed"}],
            member_user_id="new_clerk",
        )


def test_apply_aborts_before_ddb_delete_if_identity_gains_membership(monkeypatch):
    import scripts.recover_orphan_paperclip_member as mod

    client = MagicMock()
    client.execute_statement.return_value = {"numberOfRecordsUpdated": 0}
    monkeypatch.setattr(mod, "_aurora", lambda env: (client, "db", "secret"))
    monkeypatch.setattr(mod, "_aurora_users", lambda *args: [{"id": "pc_existing", "email": "m@example.com"}])
    monkeypatch.setattr(mod, "_ddb_failed_rows", lambda *args: [{"user_id": "user_m", "status": "failed"}])
    monkeypatch.setattr(
        mod.sys, "argv", ["recovery", "--env", "dev", "--email", "m@example.com", "--user-id", "user_m", "--apply"]
    )
    ddb = MagicMock()
    monkeypatch.setattr(mod.boto3, "client", lambda *args, **kwargs: ddb)
    with pytest.raises(SystemExit, match="REFUSING"):
        mod.main()
    assert "NOT EXISTS" in client.execute_statement.call_args.kwargs["sql"]
    assert "company_memberships" in client.execute_statement.call_args.kwargs["sql"]
    ddb.delete_item.assert_not_called()
