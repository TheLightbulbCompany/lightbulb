import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import billing_repo  # noqa: E402
from core.repositories.paperclip_repo import PaperclipRepo  # noqa: E402


def _two_page_table():
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"owner_id": "a"}], "LastEvaluatedKey": {"owner_id": "a"}},
        {"Items": [{"owner_id": "b"}]},
    ]
    return table


@pytest.mark.asyncio
async def test_billing_scan_all_paginates():
    table = _two_page_table()
    with patch("core.repositories.billing_repo._get_table", return_value=table):
        rows = await billing_repo.scan_all()
    assert [r["owner_id"] for r in rows] == ["a", "b"]
    assert table.scan.call_args_list[1].kwargs == {"ExclusiveStartKey": {"owner_id": "a"}}


@pytest.mark.asyncio
async def test_paperclip_scan_all_items_paginates():
    table = _two_page_table()
    repo = PaperclipRepo(table_name="paperclip-companies")
    with patch.object(PaperclipRepo, "_table", return_value=table):
        rows = await repo.scan_all_items()
    assert [r["owner_id"] for r in rows] == ["a", "b"]
