# apps/backend/tests/unit/core/repositories/test_ops_cache_repo.py
import time
from unittest.mock import MagicMock, patch

import pytest

from core.repositories import ops_cache_repo


@pytest.mark.asyncio
async def test_put_then_get_roundtrip():
    store: dict = {}

    fake_table = MagicMock()

    def _put_item(Item):
        store[Item["cache_key"]] = Item

    def _get_item(Key):
        item = store.get(Key["cache_key"])
        return {"Item": item} if item else {}

    fake_table.put_item.side_effect = _put_item
    fake_table.get_item.side_effect = _get_item

    with patch.object(ops_cache_repo, "_get_table", return_value=fake_table):
        before = int(time.time())
        put = await ops_cache_repo.put("fleet#dev", {"running": 3}, ttl_seconds=30)
        assert put["value"] == {"running": 3}
        assert put["ttl"] >= before + 30

        got = await ops_cache_repo.get("fleet#dev")
        assert got is not None
        assert got["value"] == {"running": 3}

        missing = await ops_cache_repo.get("nope")
        assert missing is None
