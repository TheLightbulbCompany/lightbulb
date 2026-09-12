"""backfill_workspace_ids: dry run by default (no CLI flag == no writes);
--apply is required to mutate the table."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts import backfill_workspace_ids as mod


def _table(rows: list[dict]) -> MagicMock:
    table = MagicMock()
    table.scan.return_value = {"Items": rows}
    return table


@pytest.mark.asyncio
async def test_default_mode_scans_and_reports_but_never_writes():
    table = _table([{"pk": "ROOM#r1", "sk": "META", "ownerId": "owner_1"}])
    with (
        patch.object(mod.repo, "_table", return_value=table),
        patch.object(mod.repo, "ensure_workspace_id", AsyncMock(return_value="ws-1")),
    ):
        assert await mod.main(apply=False) == 0
    table.update_item.assert_not_called()


@pytest.mark.asyncio
async def test_apply_writes_the_stamped_workspace_id():
    table = _table([{"pk": "ROOM#r1", "sk": "META", "ownerId": "owner_1"}])
    with (
        patch.object(mod.repo, "_table", return_value=table),
        patch.object(mod.repo, "ensure_workspace_id", AsyncMock(return_value="ws-1")),
    ):
        assert await mod.main(apply=True) == 0
    table.update_item.assert_called_once()
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"] == {":ws": "ws-1"}
