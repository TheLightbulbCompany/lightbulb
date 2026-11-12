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
    assert "text" not in item  # no body: no attribute, like the rows written before it was kept


@pytest.mark.asyncio
async def test_put_sent_keeps_the_body_cut_and_nudges_the_mail_panel(monkeypatch):
    from core.services import agent_email

    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.put_sent(
        address="a@b.c", message_id="<x@d>", owner_id="o", agent_id="a", to="t@x.y", subject="s", text="z" * 70_000
    )
    assert table.put_item.call_args.kwargs["Item"]["text"] == "z" * repo.MAX_TEXT_CHARS
    agent_email.mail_changed.assert_awaited_once_with("o")


@pytest.mark.asyncio
async def test_put_sent_never_raises(monkeypatch):
    table = MagicMock()
    table.put_item.side_effect = RuntimeError("ddb down")
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.put_sent(address="a@b.c", message_id="<x@d>", owner_id="o", agent_id="a", to="t@x.y", subject="s")
    from core.services import agent_email

    agent_email.mail_changed.assert_not_awaited()  # nothing stored, nothing to show


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


@pytest.mark.asyncio
async def test_list_messages_pages_past_sent_rows(monkeypatch):
    """A page of nothing but sent mail must not hide older inbound."""
    table = MagicMock()
    table.query.side_effect = [
        {"Items": [{"received_key": "z", "direction": "out"}], "LastEvaluatedKey": {"k": 1}},
        {"Items": [{"received_key": "a", "agent_id": "friday"}]},
    ]
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    rows = await repo.list_messages("agents@acme.isol8.co", agent_id="friday")
    assert [r["received_key"] for r in rows] == ["a"]
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


@pytest.mark.asyncio
async def test_list_messages_stops_after_five_pages(monkeypatch):
    table = MagicMock()
    table.query.return_value = {"Items": [{"received_key": "z", "direction": "out"}], "LastEvaluatedKey": {"k": 1}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.list_messages("agents@acme.isol8.co") == []
    assert table.query.call_count == 5


@pytest.mark.asyncio
async def test_list_page_returns_both_directions_and_a_cursor(monkeypatch):
    table = MagicMock()
    table.query.return_value = {
        "Items": [{"received_key": "c", "direction": "out"}, {"received_key": "b"}],
        "LastEvaluatedKey": {"received_key": "b"},
    }
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    rows, nxt = await repo.list_page("agents@acme.isol8.co", before="d", limit=2)
    assert [r["received_key"] for r in rows] == ["c", "b"] and nxt == "b"
    kw = table.query.call_args.kwargs
    assert kw["ScanIndexForward"] is False and kw["Limit"] == 2
    table.query.return_value = {"Items": [{"received_key": "a"}]}
    assert (await repo.list_page("agents@acme.isol8.co", before="b"))[1] is None


@pytest.mark.asyncio
async def test_count_received_after_counts_inbound_only_on_this_address_after_the_mark(monkeypatch):
    table = MagicMock()
    table.query.side_effect = [
        {"Items": [{"direction": "out"}] * 30 + [{}], "LastEvaluatedKey": {"k": 1}},  # one inbound under 30 sent
        {"Items": [{}, {"direction": "out"}]},
    ]
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.count_received_after("Agents@Acme.isol8.co", "2026-10-03T12:00:00+00:00", cap=10) == 2
    kw = table.query.call_args_list[0].kwargs
    address_cond, key_cond = kw["KeyConditionExpression"].get_expression()["values"]
    assert address_cond.get_expression()["values"][1] == "agents@acme.isol8.co"
    assert key_cond.get_expression()["operator"] == ">"
    assert key_cond.get_expression()["values"][1] == "2026-10-03T12:00:00+00:00"
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


@pytest.mark.asyncio
async def test_count_received_after_stops_paging_at_the_cap(monkeypatch):
    table = MagicMock()
    table.query.return_value = {"Items": [{}] * 50, "LastEvaluatedKey": {"k": 1}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.count_received_after("agents@acme.isol8.co", "t", cap=10) == 10
    assert table.query.call_count == 1


@pytest.mark.asyncio
async def test_put_message_writes_channel_only_when_given(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    kw = dict(
        address="a@b.c", owner_id="o", agent_id="a", message_id="m1", from_addr="+15551230000",
        subject="", text="hi", headers={}, attachments=[],
    )  # fmt: skip
    assert "channel" not in await repo.put_message(**kw)
    assert (await repo.put_message(**kw, channel="text"))["channel"] == "text"


@pytest.mark.asyncio
async def test_put_sent_writes_channel(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    kw = dict(address="a@b.c", message_id="<x@d>", owner_id="o", agent_id="a", to="+15551230000", subject="")
    await repo.put_sent(**kw)
    assert "channel" not in table.put_item.call_args.kwargs["Item"]
    await repo.put_sent(**kw, channel="text")
    assert table.put_item.call_args.kwargs["Item"]["channel"] == "text"


@pytest.mark.asyncio
async def test_put_message_stores_provider_ids_only_when_given(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    kw = dict(
        address="a@b.co",
        owner_id="o",
        agent_id="g",
        message_id="m1",
        from_addr="+1",
        subject="",
        text="t",
        headers={},
        attachments=[],
    )
    assert "provider" not in await repo.put_message(**kw)
    item = await repo.put_message(**kw, provider_ids={"space_id": "s1"})
    assert item["provider"] == {"space_id": "s1"}
