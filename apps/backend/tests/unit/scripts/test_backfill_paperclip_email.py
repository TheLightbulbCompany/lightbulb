"""Selection logic for the paperclip_email backfill.

``plan`` is the whole risk surface: this backfill has a ONE-WAY DOOR (it must
run while Clerk and Aurora still agree), so writing a wrong address is worse
than writing none — a row that looks backfilled but holds an address Better
Auth does not have cements the very 401 the field exists to prevent.
"""

from unittest.mock import patch

from scripts.backfill_paperclip_email import main, plan


def _row(**over) -> dict:
    base = {
        "user_id": "user_x",
        "paperclip_user_id": "pc_x",
        "status": "active",
    }
    base.update(over)
    return base


class TestPlan:
    def test_row_without_a_stored_email_is_seeded_from_aurora(self):
        targets, mismatches, orphans = plan([_row()], {"pc_x": "alice@isol8.co"})
        assert targets == [("user_x", "alice@isol8.co")]
        assert (mismatches, orphans) == (0, 0)

    def test_already_backfilled_row_is_skipped(self):
        """Idempotent: a second run writes nothing."""
        rows = [_row(paperclip_email="alice@isol8.co")]
        targets, mismatches, _ = plan(rows, {"pc_x": "alice@isol8.co"})
        assert targets == []
        assert mismatches == 0

    def test_disagreeing_stored_email_is_reported_not_overwritten(self):
        """The stored value IS what the sign-in doors use. Overwriting it on a
        disagreement is the one-way door slamming; surface it for a human."""
        rows = [_row(paperclip_email="alice@isol8.co")]
        targets, mismatches, _ = plan(rows, {"pc_x": "alice@thelightbulb.company"})
        assert targets == []
        assert mismatches == 1

    def test_credential_less_row_is_skipped(self):
        """A seed claim / failed marker has no Better Auth account, so there is
        no address to record — the next provision attempt writes one."""
        rows = [_row(paperclip_user_id="", status="seeding")]
        targets, _, orphans = plan(rows, {"pc_x": "alice@isol8.co"})
        assert targets == []
        assert orphans == 0

    def test_row_with_no_aurora_user_is_an_orphan_not_a_guess(self):
        targets, _, orphans = plan([_row()], {})
        assert targets == []
        assert orphans == 1

    def test_empty_stored_email_is_treated_as_missing(self):
        targets, _, _ = plan([_row(paperclip_email="")], {"pc_x": "alice@isol8.co"})
        assert targets == [("user_x", "alice@isol8.co")]


class TestTableResolution:
    """The script must scan the table for the env it was ASKED for.

    Table naming reads DYNAMODB_TABLE_PREFIX, which defaults to "isol8-dev-";
    ENVIRONMENT does not affect it. An `--env prod` run that scanned the DEV
    table would report a confident all-clear for a fleet it never read — and
    here that all-clear is the green light to move a mailbox, which is a
    one-way door.

    `core.dynamodb` snapshots the prefix at import, which is why every import in
    the script is function-local. These tests run in a WARM process where it is
    already imported, so the mismatch guard is exercised for real.
    """

    def test_inherited_dev_prefix_cannot_win(self, monkeypatch):
        """OVERWRITE, not setdefault — a dev prefix already exported in the
        operator's shell must not silently redirect a prod run."""
        import os

        monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "isol8-dev-")
        monkeypatch.setattr("sys.argv", ["backfill", "--env", "prod", "--dry-run"])
        with patch("scripts.backfill_paperclip_email.asyncio.run", return_value=0):
            main()
        assert os.environ["DYNAMODB_TABLE_PREFIX"] == "isol8-prod-"

    def test_refuses_to_run_on_a_table_mismatch(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["backfill", "--env", "prod", "--dry-run"])
        with (
            patch("core.dynamodb.table_name", return_value="isol8-dev-paperclip-companies"),
            patch("scripts.backfill_paperclip_email.asyncio.run") as run_mock,
        ):
            rc = main()
        assert rc == 2
        run_mock.assert_not_called()

    def test_proceeds_when_the_table_resolves_correctly(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["backfill", "--env", "prod", "--dry-run"])
        with (
            patch("core.dynamodb.table_name", return_value="isol8-prod-paperclip-companies"),
            patch("scripts.backfill_paperclip_email.asyncio.run", return_value=0) as run_mock,
        ):
            rc = main()
        assert rc == 0
        run_mock.assert_called_once()


class TestWriteGuard:
    """``_write_email`` uses UpdateItem, which is an UPSERT.

    Without ``attribute_exists(user_id)`` a row deleted between the scan and the
    write (account wipe, purge sweep) is RESURRECTED holding only
    ``{user_id, paperclip_email}``. ``_item_to_company`` then KeyErrors on
    ``status``/``created_at`` for every ``get()`` — a hard 500 on /teams, the
    proxy and /container/status — and ``try_acquire_seed_claim``'s
    ``attribute_not_exists(user_id)`` can never succeed again, wedging the owner
    in the loser-wait 409 path permanently.
    """

    def _captured_kwargs(self) -> dict:
        import asyncio
        from unittest.mock import MagicMock

        from scripts import backfill_paperclip_email as mod

        table = MagicMock()
        captured: dict = {}

        def _update_item(**kwargs):
            captured.update(kwargs)

        table.update_item = _update_item

        async def _run_in_thread(fn, *a, **kw):
            return fn(*a, **kw)

        # _write_email imports get_table/run_in_thread INSIDE the function
        # (deliberately — every core.* import in this script is function-local),
        # so the patch target is core.dynamodb, not the script module.
        with (
            patch("core.dynamodb.get_table", MagicMock(return_value=table)),
            patch("core.dynamodb.run_in_thread", _run_in_thread),
        ):
            asyncio.run(mod._write_email("user_x", "alice@isol8.co"))
        return captured

    def test_the_write_refuses_to_resurrect_a_deleted_row(self):
        cond = self._captured_kwargs().get("ConditionExpression", "")
        assert "attribute_exists(user_id)" in cond

    def test_the_write_still_refuses_to_clobber_a_stored_address(self):
        cond = self._captured_kwargs().get("ConditionExpression", "")
        assert "attribute_not_exists(paperclip_email)" in cond
        assert "paperclip_email = :empty" in cond
