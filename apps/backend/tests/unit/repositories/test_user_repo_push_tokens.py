"""Unit tests for user_repo push-token scoped map writes."""

import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_set_push_token_scoped_update():
    fake_table = MagicMock()
    with patch("core.repositories.user_repo._get_table", return_value=fake_table):
        from core.repositories import user_repo

        entry = {"token": "ExponentPushToken[abc]", "platform": "ios", "updatedAt": "2026-07-10T00:00:00Z"}
        await user_repo.set_push_token("user_1", "dev_1", entry)

    # Two calls: init-map-if-absent, then scoped set of the single key.
    assert fake_table.update_item.call_count == 2
    init_kwargs = fake_table.update_item.call_args_list[0].kwargs
    set_kwargs = fake_table.update_item.call_args_list[1].kwargs
    assert init_kwargs["Key"] == {"user_id": "user_1"}
    assert init_kwargs["UpdateExpression"] == "SET #pt = if_not_exists(#pt, :empty)"
    assert set_kwargs["UpdateExpression"] == "SET #pt.#did = :entry"
    assert set_kwargs["ExpressionAttributeNames"] == {"#pt": "push_tokens", "#did": "dev_1"}
    assert set_kwargs["ExpressionAttributeValues"] == {":entry": entry}


@pytest.mark.asyncio
async def test_clear_push_token_scoped_remove():
    fake_table = MagicMock()
    with patch("core.repositories.user_repo._get_table", return_value=fake_table):
        from core.repositories import user_repo

        await user_repo.clear_push_token("user_1", "dev_1")

    fake_table.update_item.assert_called_once()
    kwargs = fake_table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "REMOVE #pt.#did"
    assert kwargs["ExpressionAttributeNames"] == {"#pt": "push_tokens", "#did": "dev_1"}
