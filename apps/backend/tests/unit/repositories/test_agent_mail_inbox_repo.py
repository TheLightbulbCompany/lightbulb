"""agent_mail_inbox_repo: key shape, truncation, newest-first listing."""

from unittest.mock import MagicMock

import pytest

from core.repositories import agent_mail_inbox_repo as repo


def test_reply_key_roundtrip():
    key = repo.reply_key("friday@acme.isol8.co", "2026-08-21T10:00:00+00:00#m1")
    assert key == "friday@acme.isol8.co/2026-08-21T10:00:00+00:00#m1"
    assert repo.parse_reply_key(key) == ("friday@acme.isol8.co", "2026-08-21T10:00:00+00:00#m1")


def test_parse_reply_key_rejects_garbage():
    assert repo.parse_reply_key("") is None
    assert repo.parse_reply_key("no-slash") is None
    assert repo.parse_reply_key("/leading") is None


@pytest.mark.asyncio
async def test_put_message_truncates_text_and_sets_ttl(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    item = await repo.put_message(
        address="Friday@Acme.isol8.co",
        owner_id="user_1",
        agent_id="agent_1",
        message_id="m1",
        from_addr="noreply@github.com",
        subject="Verify",
        text="x" * 70_000,
        headers={"Message-ID": "<a@b>"},
        attachments=[],
        received_at="2026-08-21T10:00:00+00:00",
    )
    assert item["address"] == "friday@acme.isol8.co"
    assert item["received_key"] == "2026-08-21T10:00:00+00:00#m1"
    assert len(item["text"]) == 64_000
    assert item["ttl"] > 0
    table.put_item.assert_called_once()


@pytest.mark.asyncio
async def test_list_messages_queries_newest_first(monkeypatch):
    table = MagicMock()
    table.query.return_value = {"Items": [{"received_key": "b"}, {"received_key": "a"}]}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    rows = await repo.list_messages("friday@acme.isol8.co", since="2026-08-20T00:00:00+00:00", limit=5)
    assert [r["received_key"] for r in rows] == ["b", "a"]
    kwargs = table.query.call_args.kwargs
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 5


@pytest.mark.asyncio
async def test_find_by_message_id_queries_the_gsi_and_filters_by_address(monkeypatch):
    table = MagicMock()
    table.query.return_value = {
        "Items": [
            {"address": "other@acme.isol8.co", "message_id": "m1", "received_key": "2026#other"},
            {"address": "friday@acme.isol8.co", "message_id": "m1", "received_key": "2026#m"},
        ]
    }
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    item = await repo.find_by_message_id("Friday@Acme.isol8.co", "m1")
    assert item == {"address": "friday@acme.isol8.co", "message_id": "m1", "received_key": "2026#m"}
    kwargs = table.query.call_args.kwargs
    assert kwargs["IndexName"] == "by-message-id"


@pytest.mark.asyncio
async def test_find_by_message_id_returns_none_without_querying_when_empty(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.find_by_message_id("friday@acme.isol8.co", "") is None
    table.query.assert_not_called()
