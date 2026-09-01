"""Selection logic for the agent-email domain re-key.

``plan`` is the whole risk surface. ``address`` is the PARTITION KEY, so a
wrong decision here is not a bad field on a good row — it is a second row at a
key that routes real inbound mail, or an agent left with no ``active`` address
at all (``resolve_from_address`` returns None and the agent cannot send).
"""

from unittest.mock import MagicMock, patch

from scripts.rekey_agent_email_domain import Plan, _new_item, main, plan, rekey

OLD = "isol8.co"
NEW = "thelightbulb.company"


def _row(**over) -> dict:
    base = {
        "address": "friday@acme.isol8.co",
        "owner_id": "user_x",
        "agent_id": "agent_1",
        "status": "active",
        "verified_senders": ["ben@enjoylull.com"],
        "created_at": "2026-01-01T00:00:00Z",
    }
    base.update(over)
    return base


class TestRekey:
    def test_swaps_only_the_domain_suffix(self):
        assert rekey("friday@acme.isol8.co", OLD, NEW) == "friday@acme.thelightbulb.company"

    def test_a_slug_containing_the_old_domain_is_not_mangled(self):
        """A plain str.replace would rewrite the slug too. The domain is the
        SUFFIX — the company slug is its own subdomain label."""
        assert rekey("friday@isol8-co.isol8.co", OLD, NEW) == "friday@isol8-co.thelightbulb.company"

    def test_an_address_not_on_the_old_domain_is_not_guessed_at(self):
        assert rekey("friday@acme.example.com", OLD, NEW) is None

    def test_a_non_address_is_refused(self):
        assert rekey("isol8.co", OLD, NEW) is None


class TestPlan:
    def test_active_row_is_moved(self):
        p = plan([_row()], OLD, NEW)
        assert [(r["address"], a) for r, a in p.moves] == [("friday@acme.isol8.co", "friday@acme.thelightbulb.company")]
        assert p.collisions == []

    def test_a_second_run_is_a_no_op(self):
        """Idempotent: after a completed run the old row is an alias and the
        new one is already on the target domain, so nothing moves."""
        rows = [
            _row(status="alias"),
            _row(address="friday@acme.thelightbulb.company"),
        ]
        p = plan(rows, OLD, NEW)
        assert p.moves == []
        assert (p.already_moved, p.aliases_left_alone) == (1, 1)

    def test_a_crash_between_the_two_writes_is_repaired_not_flagged(self):
        """New row written, old one not yet demoted — both ACTIVE, same owner
        and agent. That is this script's own interrupted run: re-plan it as a
        move so the demote finishes, do NOT call it a collision."""
        rows = [_row(), _row(address="friday@acme.thelightbulb.company")]
        p = plan(rows, OLD, NEW)
        assert len(p.moves) == 1
        assert p.collisions == []

    def test_another_owner_holding_the_new_key_is_a_collision_not_an_overwrite(self):
        """The new address is a partition key. Writing over someone else's row
        hands them another owner's inbound mail — refuse, leave the old row
        ACTIVE so the agent keeps working, and let a human decide."""
        rows = [_row(), _row(address="friday@acme.thelightbulb.company", owner_id="user_other")]
        p = plan(rows, OLD, NEW)
        assert p.moves == []
        assert len(p.collisions) == 1
        assert "user_other" in p.collisions[0][2]

    def test_same_owner_different_agent_is_also_a_collision(self):
        """The address derives from the agent NAME, so two same-owner agents
        sharing a name collide — the same case mint_agent_email_address
        refuses rather than handing one agent another's mailbox."""
        rows = [_row(), _row(address="friday@acme.thelightbulb.company", agent_id="agent_2")]
        p = plan(rows, OLD, NEW)
        assert p.moves == []
        assert len(p.collisions) == 1

    def test_alias_rows_are_left_alone(self):
        """A superseded row still routes inbound to an already-published
        address. It is not promoted and not moved."""
        p = plan([_row(status="alias")], OLD, NEW)
        assert p.moves == []
        assert p.aliases_left_alone == 1

    def test_an_active_row_on_an_unknown_domain_is_reported_not_guessed(self):
        p = plan([_row(address="friday@acme.example.com")], OLD, NEW)
        assert p.moves == []
        assert p.unrecognised == ["friday@acme.example.com"]


class TestStaleSenders:
    """``verified_senders`` is seeded from the owner's Clerk address and is what
    ``sender_allowed`` checks. After the human mailbox move it points at an
    address the owner no longer sends from, so the agent stops waking."""

    def test_a_sender_on_the_old_domain_is_reported(self):
        p = plan([_row(verified_senders=["admin@isol8.co"])], OLD, NEW)
        assert p.stale_senders == [("friday@acme.isol8.co", ["admin@isol8.co"])]

    def test_reporting_does_not_stop_or_change_the_move(self):
        p = plan([_row(verified_senders=["admin@isol8.co"])], OLD, NEW)
        assert len(p.moves) == 1
        row, _ = p.moves[0]
        assert row["verified_senders"] == ["admin@isol8.co"]

    def test_a_bare_domain_entry_is_reported_too(self):
        """`sender_allowed` accepts an ``@domain`` entry; one on the old domain
        is just as dead as an exact address."""
        p = plan([_row(verified_senders=["@isol8.co"])], OLD, NEW)
        assert p.stale_senders[0][1] == ["@isol8.co"]

    def test_an_external_sender_is_not_touched(self):
        p = plan([_row(verified_senders=["ben@enjoylull.com"])], OLD, NEW)
        assert p.stale_senders == []

    def test_rows_already_on_the_new_domain_are_still_checked(self):
        """The stale-sender problem outlives the re-key — a re-run must still
        surface it."""
        rows = [_row(address="friday@acme.thelightbulb.company", verified_senders=["admin@isol8.co"])]
        p = plan(rows, OLD, NEW)
        assert p.moves == []
        assert len(p.stale_senders) == 1


class TestNewItem:
    """The new row is the owner's state at a new key — not a fresh mint.

    ``agent_email_repo.put`` cannot be used for this: it always stamps a new
    ``created_at`` (flipping the earliest-``created_at`` tie-break
    ``owner_facts._lookup_email_address`` sorts on) and knows nothing about
    ``send_to_anyone`` / ``outbound_disabled`` / ``public``.
    """

    def test_owner_settings_survive_the_move(self):
        row = _row(send_to_anyone=True, outbound_disabled="complaint", public=True)
        item = _new_item(row, "friday@acme.thelightbulb.company")
        assert item["send_to_anyone"] is True
        assert item["outbound_disabled"] == "complaint"
        assert item["public"] is True
        assert item["verified_senders"] == ["ben@enjoylull.com"]

    def test_created_at_is_preserved_not_restamped(self):
        item = _new_item(_row(), "friday@acme.thelightbulb.company")
        assert item["created_at"] == "2026-01-01T00:00:00Z"

    def test_absent_optional_fields_are_not_invented(self):
        item = _new_item(_row(), "friday@acme.thelightbulb.company")
        assert "send_to_anyone" not in item
        assert "outbound_disabled" not in item

    def test_the_new_row_is_active(self):
        item = _new_item(_row(status="active"), "friday@acme.thelightbulb.company")
        assert item["status"] == "active"
        assert item["address"] == "friday@acme.thelightbulb.company"


class TestWriteOrderAndGuards:
    """Put the new row FIRST, then demote.

    The reverse order leaves a window with no ``active`` row for the agent,
    which is ``resolve_from_address`` returning None — the agent cannot send at
    all. Put-first leaves two active rows on a crash, both routing to the same
    agent, which the next run repairs.
    """

    def _captured(self, put_raises=None):
        import asyncio

        from scripts import rekey_agent_email_domain as mod

        calls: list[tuple[str, dict]] = []
        table = MagicMock()

        def _put(**kw):
            calls.append(("put", kw))
            if put_raises:
                raise put_raises

        table.put_item = _put
        table.update_item = lambda **kw: calls.append(("update", kw))

        async def _run_in_thread(fn, *a, **kw):
            return fn(*a, **kw)

        with (
            patch("core.dynamodb.get_table", MagicMock(return_value=table)),
            patch("core.dynamodb.run_in_thread", _run_in_thread),
        ):
            reason = asyncio.run(mod._write_move(_row(), "friday@acme.thelightbulb.company"))
        return calls, reason

    def test_the_new_row_is_written_before_the_old_is_demoted(self):
        calls, reason = self._captured()
        assert [c[0] for c in calls] == ["put", "update"]
        assert reason is None

    def test_the_put_cannot_overwrite_another_owners_row(self):
        calls, _ = self._captured()
        cond = calls[0][1]["ConditionExpression"]
        assert "attribute_not_exists(address)" in cond
        assert "owner_id = :owner" in cond

    def test_the_demote_sets_alias_and_cannot_resurrect_a_deleted_row(self):
        calls, _ = self._captured()
        _, kw = calls[1]
        assert kw["ExpressionAttributeValues"][":alias"] == "alias"
        assert kw["ConditionExpression"] == "attribute_exists(address)"
        # `status` is a DynamoDB reserved word — an inline SET status = :alias
        # is a ValidationException at runtime, which no unit test that stubs
        # the table would otherwise catch.
        assert kw["ExpressionAttributeNames"] == {"#s": "status"}
        assert "#s" in kw["UpdateExpression"]

    def test_a_lost_put_leaves_the_old_row_active(self):
        """No demote on a failed put: the agent keeps a working address."""
        from botocore.exceptions import ClientError

        err = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        calls, reason = self._captured(put_raises=err)
        assert [c[0] for c in calls] == ["put"]
        assert reason and "another owner" in reason


class TestDryRunAndExitCodes:
    def _rc(self, rows, dry_run):
        import asyncio

        from scripts import rekey_agent_email_domain as mod

        async def _scan():
            return rows

        with patch.object(mod, "_scan", _scan), patch.object(mod, "_write_move", return_value=None):
            return asyncio.run(mod._run(dry_run))

    def test_dry_run_writes_nothing(self):
        import asyncio

        from scripts import rekey_agent_email_domain as mod

        async def _scan():
            return [_row()]

        write = MagicMock()
        with patch.object(mod, "_scan", _scan), patch.object(mod, "_write_move", write):
            asyncio.run(mod._run(True))
        write.assert_not_called()

    def test_a_dry_run_that_would_collide_exits_non_zero(self):
        """The operator has to learn this BEFORE the flip, not from a failed
        real run."""
        rows = [_row(), _row(address="friday@acme.thelightbulb.company", owner_id="user_other")]
        assert self._rc(rows, dry_run=True) == 1

    def test_a_clean_dry_run_exits_zero(self):
        assert self._rc([_row()], dry_run=True) == 0

    def test_verify_fails_while_an_active_row_is_left_on_the_old_domain(self):
        """The scan is re-read after the writes, so a write that silently did
        not land still fails the run. Here _write_move is stubbed to a no-op
        success, so the second scan still shows the old row active."""
        assert self._rc([_row()], dry_run=False) == 1


class TestTableResolution:
    """The script must touch the table it was ASKED for. DYNAMODB_TABLE_PREFIX
    defaults to "lightbulb-dev-" and ENVIRONMENT does not affect it, so an
    `--env prod` run on the dev table would report a clean verify for a fleet
    it never read — and that clean verify is the green light to repoint SES."""

    def test_inherited_dev_prefix_cannot_win(self, monkeypatch):
        import os

        monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "lightbulb-dev-")
        monkeypatch.setattr("sys.argv", ["rekey", "--env", "prod", "--dry-run"])
        with patch("scripts.rekey_agent_email_domain.asyncio.run", return_value=0):
            main()
        assert os.environ["DYNAMODB_TABLE_PREFIX"] == "lightbulb-prod-"

    def test_refuses_to_run_on_a_table_mismatch(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["rekey", "--env", "prod", "--dry-run"])
        with (
            patch("core.dynamodb.table_name", return_value="lightbulb-dev-agent-email-addresses"),
            patch("scripts.rekey_agent_email_domain.asyncio.run") as run_mock,
        ):
            rc = main()
        assert rc == 2
        run_mock.assert_not_called()

    def test_proceeds_when_the_table_resolves_correctly(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["rekey", "--env", "prod", "--dry-run"])
        with (
            patch("core.dynamodb.table_name", return_value="lightbulb-prod-agent-email-addresses"),
            patch("scripts.rekey_agent_email_domain.asyncio.run", return_value=0) as run_mock,
        ):
            rc = main()
        assert rc == 0
        run_mock.assert_called_once()


def test_plan_dataclass_defaults_are_not_shared():
    """field(default_factory=...) — a bare [] default would share one list
    across every Plan and silently accumulate across runs in the same process."""
    a, b = Plan(), Plan()
    a.moves.append(({}, "x"))
    assert b.moves == []
