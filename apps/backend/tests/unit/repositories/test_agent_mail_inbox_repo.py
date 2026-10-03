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
    rows = await repo.list_messages("agents@acme.isol8.co", since="2026-08-20T00:00:00+00:00", limit=1)
    assert [r["received_key"] for r in rows] == ["b"]
    assert table.query.call_args.kwargs["ScanIndexForward"] is False


@pytest.mark.asyncio
async def test_list_messages_skips_sent_mail_and_filters_by_agent(monkeypatch):
    table = MagicMock()
    table.query.return_value = {
        "Items": [
            {"received_key": "c", "agent_id": "friday", "direction": "out"},
            {"received_key": "b", "agent_id": "ceo"},
            {"received_key": "a", "agent_id": "friday"},
        ]
    }
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert [r["received_key"] for r in await repo.list_messages("agents@acme.isol8.co")] == ["b", "a"]
    assert [r["received_key"] for r in await repo.list_messages("agents@acme.isol8.co", agent_id="friday")] == ["a"]


@pytest.mark.asyncio
async def test_put_sent_records_the_writer_under_the_rfc_message_id(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.put_sent(
        address="Agents@Acme.isol8.co", message_id="<x@d>", owner_id="o", agent_id="friday", to="t@x.y", subject="s"
    )
    item = table.put_item.call_args.kwargs["Item"]
    assert item["address"] == "agents@acme.isol8.co"
    assert (item["direction"], item["message_id"], item["agent_id"]) == ("out", "<x@d>", "friday")
    assert item["received_key"].endswith("#<x@d>") and item["ttl"] > 0


@pytest.mark.asyncio
async def test_put_sent_never_raises(monkeypatch):
    table = MagicMock()
    table.put_item.side_effect = RuntimeError("ddb down")
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.put_sent(address="a@b.c", message_id="<x@d>", owner_id="o", agent_id="a", to="t@x.y", subject="s")


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
