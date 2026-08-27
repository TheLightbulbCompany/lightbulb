"""select_rows: SELECT/WITH only, formatRecordsAs=JSON parsed, secret ARN cached per env."""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import paperclip_sql  # noqa: E402


@pytest.mark.asyncio
async def test_rejects_non_select():
    with pytest.raises(ValueError):
        await paperclip_sql.select_rows("DELETE FROM routines", env="test")


@pytest.mark.asyncio
async def test_parses_formatted_records_and_caches_secret_arn(monkeypatch):
    paperclip_sql._secret_arn_cache.clear()
    sm = MagicMock()
    sm.get_paginator.return_value.paginate.return_value = [
        {"SecretList": [{"Name": "isol8-test-paperclip-db-credentials-abc", "ARN": "arn:secret:1"}]}
    ]
    rds = MagicMock()
    rds.execute_statement.return_value = {"formattedRecords": '[{"n": 2, "name": "Lightbulb AI"}]'}
    with (
        patch("core.services.paperclip_sql.secretsmanager_client", return_value=sm),
        patch("core.services.paperclip_sql.rds_data_client", return_value=rds),
    ):
        rows = await paperclip_sql.select_rows("  select count(*) as n from companies", env="test")
        rows2 = await paperclip_sql.select_rows("WITH x AS (select 1) select * from x", env="test")

    assert rows == [{"n": 2, "name": "Lightbulb AI"}]
    assert rows2 == rows
    assert sm.get_paginator.call_count == 1  # cached after the first lookup
    kwargs = rds.execute_statement.call_args.kwargs
    assert kwargs["resourceArn"].endswith(":cluster:isol8-test-paperclip-db")
    assert kwargs["secretArn"] == "arn:secret:1"
    assert kwargs["database"] == "paperclip"
    assert kwargs["formatRecordsAs"] == "JSON"


@pytest.mark.asyncio
async def test_empty_result_is_empty_list():
    paperclip_sql._secret_arn_cache["test"] = "arn:secret:cached"
    rds = MagicMock()
    rds.execute_statement.return_value = {}
    with patch("core.services.paperclip_sql.rds_data_client", return_value=rds):
        assert await paperclip_sql.select_rows("select 1", env="test") == []
