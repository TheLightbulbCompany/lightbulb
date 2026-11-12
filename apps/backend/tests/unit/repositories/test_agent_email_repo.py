"""agent_email_repo: the workspace's one mailbox row, address-keyed with a by-owner GSI."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import agent_email_repo as repo  # noqa: E402


@pytest.fixture
def agent_email_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-agent-email-addresses",
            KeySchema=[{"AttributeName": "address", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "address", "AttributeType": "S"},
                {"AttributeName": "owner_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner",
                    "KeySchema": [{"AttributeName": "owner_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-agent-email-addresses")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_put_normalizes_and_get_by_address(agent_email_table):
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="Acme@Agents.Isol8.co",
        owner_id="user_1",
        verified_senders=["Owner@Example.com"],
    )

    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row is not None
    assert row["owner_id"] == "user_1"
    assert "agent_id" not in row
    assert row["verified_senders"] == ["owner@example.com"]
    assert "public" not in row
    assert row["status"] == "active"
    assert row["created_at"]


@pytest.mark.asyncio
async def test_put_mints_open_rows_and_the_owner_can_close_them(agent_email_table):
    """Owner ruling 2026-09-24: agent email is open by default in both directions."""
    await repo.put(address="acme@agents.isol8.co", owner_id="user_1", verified_senders=[])

    assert (await repo.get_by_address("acme@agents.isol8.co"))["send_to_anyone"] is True

    await repo.update_settings("acme@agents.isol8.co", send_to_anyone=False)
    assert (await repo.get_by_address("acme@agents.isol8.co"))["send_to_anyone"] is False


@pytest.mark.asyncio
async def test_get_by_address_missing_returns_none(agent_email_table):
    from core.repositories import agent_email_repo

    assert await agent_email_repo.get_by_address("nope@agents.isol8.co") is None


@pytest.mark.asyncio
async def test_get_by_address_is_case_insensitive_on_read(agent_email_table):
    """Stored address is already lowercase; the query casing must still normalize.

    Regression guard for dropping `normalize()` from `get_by_address`'s
    `Key=` construction -- a mutation that every other test here misses
    because they all query with an already-lowercase address.
    """
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )

    row = await agent_email_repo.get_by_address("AcMe@Agents.Isol8.CO")
    assert row is not None
    assert row["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_get_by_address_uses_consistent_read(agent_email_table, monkeypatch):
    """`ensure_team_address` reads the row right before and after a create; an
    eventual miss would report a failed mint. GetItem supports ConsistentRead."""
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )

    calls = []
    real_get_item = agent_email_table.get_item

    def spy_get_item(**kwargs):
        calls.append(kwargs)
        return real_get_item(**kwargs)

    class _TableSpy:
        def get_item(self, **kwargs):
            return spy_get_item(**kwargs)

    monkeypatch.setattr(agent_email_repo, "_get_table", lambda: _TableSpy())

    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row is not None
    assert len(calls) == 1
    assert calls[0].get("ConsistentRead") is True


@pytest.mark.asyncio
async def test_put_never_overwrites_an_existing_row(agent_email_table):
    """A create, not an upsert: a racing second create (same owner or not) must
    not reset the owner's settings."""
    from core.repositories import agent_email_repo

    first = await agent_email_repo.put(address="acme@agents.isol8.co", owner_id="user_1", verified_senders=[])
    await agent_email_repo.update_settings("acme@agents.isol8.co", send_to_anyone=False)
    second = await agent_email_repo.put(
        address="acme@agents.isol8.co", owner_id="user_1", verified_senders=["owner@example.com"]
    )

    assert first is not None
    assert second is None
    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row["send_to_anyone"] is False
    assert row["verified_senders"] == []


@pytest.mark.asyncio
async def test_put_rejects_and_returns_none_for_a_conflicting_owner(agent_email_table):
    """The conditional put must reject a different owner's write rather than
    last-writer-wins clobbering the existing row's owner_id."""
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )

    result = await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_2",
        verified_senders=["intruder@example.com"],
    )
    assert result is None

    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row["owner_id"] == "user_1"
    assert row["verified_senders"] == []


@pytest.mark.asyncio
async def test_query_by_owner_returns_only_that_owner(agent_email_table):
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="acme-atlas@agents.isol8.co",
        owner_id="user_1",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="other@agents.isol8.co",
        owner_id="user_2",
        verified_senders=[],
    )

    rows = await agent_email_repo.query_by_owner("user_1")
    assert {r["address"] for r in rows} == {
        "acme@agents.isol8.co",
        "acme-atlas@agents.isol8.co",
    }


@pytest.mark.asyncio
async def test_update_settings_sets_only_supplied_fields(monkeypatch):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"address": "a@b.c", "send_to_anyone": True}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    out = await repo.update_settings("A@b.c", send_to_anyone=True)
    assert out["send_to_anyone"] is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "a@b.c"}
    assert "send_to_anyone" in kwargs["UpdateExpression"]
    assert "verified_senders" not in kwargs["UpdateExpression"]
    assert kwargs["ConditionExpression"] == "attribute_exists(address)"


@pytest.mark.asyncio
async def test_update_settings_none_removes_outbound_disabled(monkeypatch):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"address": "a@b.c"}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.update_settings("a@b.c", outbound_disabled=None)
    assert "REMOVE outbound_disabled" in table.update_item.call_args.kwargs["UpdateExpression"]


@pytest.mark.asyncio
async def test_update_settings_missing_row_returns_none(monkeypatch):
    table = MagicMock()
    table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.update_settings("a@b.c", send_to_anyone=False) is None


@pytest.mark.asyncio
async def test_put_has_no_public_field(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    item = await repo.put(address="a@b.c", owner_id="o", verified_senders=["O@x.y"])
    assert "public" not in item
    assert item["verified_senders"] == ["o@x.y"]


@pytest.mark.asyncio
async def test_add_meeting_is_an_idempotent_set_add(agent_email_table):
    await repo.put(address="agents@acme.isol8.co", owner_id="o", verified_senders=[])
    await repo.add_meeting("Agents@acme.isol8.co", "m1")
    await repo.add_meeting("agents@acme.isol8.co", "m1")
    await repo.add_meeting("agents@acme.isol8.co", "m2")
    assert (await repo.get_by_address("agents@acme.isol8.co"))["meetings"] == {"m1", "m2"}


@pytest.mark.asyncio
async def test_add_meeting_never_raises(agent_email_table):
    await repo.add_meeting("nobody@acme.isol8.co", "m1")  # no row: the condition fails, logged only
    assert await repo.get_by_address("nobody@acme.isol8.co") is None


# ---- the text line: a kind="text" row keyed by its E.164 number ----


async def _async(value):
    return value


async def test_record_text_line_puts_the_row_and_deletes_the_pending_row_atomically(monkeypatch):
    table = MagicMock()
    table.name = "t"
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    ok = await repo.record_text_line(
        number="+15551230000",
        owner_id="org_1",
        agent_id="agent_1",
        photon_resource_id="res_1",
        photon_user_id="usr_1",
        operation_id="op_1",
    )
    assert ok is True
    put, delete = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
    assert put["Put"]["ConditionExpression"] == "attribute_not_exists(address)"
    item = put["Put"]["Item"]
    assert item == {
        "address": "+15551230000",
        "owner_id": "org_1",
        "agent_id": "agent_1",
        "kind": "text",
        "status": "active",
        "created_at": item["created_at"],
        "photon_resource_id": "res_1",
        "photon_user_id": "usr_1",
        "human_threads": {},
        "member_phones": {},
        "phone_codes": {},
        "disclosed": {},
    }
    assert item["created_at"]
    assert delete["Delete"]["Key"] == {"address": "pending:org_1"}
    assert delete["Delete"]["ConditionExpression"] == "kind = :pending AND operation_id = :op"
    assert delete["Delete"]["ExpressionAttributeValues"] == {":pending": "text_pending", ":op": "op_1"}


async def test_record_text_line_is_false_when_the_pending_row_is_gone(monkeypatch):
    table = MagicMock()
    table.name = "t"
    table.meta.client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"
    )
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert (
        await repo.record_text_line(
            number="+15551230000",
            owner_id="o",
            agent_id="a",
            photon_resource_id="r",
            photon_user_id="u",
            operation_id="op_1",
        )
        is False
    )
    table.meta.client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "TransactWriteItems"
    )
    with pytest.raises(ClientError):
        await repo.record_text_line(
            number="+1", owner_id="o", agent_id="a", photon_resource_id="r", photon_user_id="u", operation_id="op_1"
        )


async def test_delete_all_for_owner_keeps_a_line_that_failed_to_release(monkeypatch):
    # release_line deletes the rows it releases; a line or pending row still present at the sweep is a failed
    # release, kept so a retried wipe can release the dedicated number instead of orphaning it at Photon.
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    rows = [
        {"address": "friday@acme.isol8.co", "owner_id": "o"},
        {"address": "+15551230000", "owner_id": "o", "kind": "text"},
        {"address": "pending:o", "owner_id": "o", "kind": "text_pending"},
    ]
    monkeypatch.setattr(repo, "query_by_owner", AsyncMock(return_value=rows))
    assert await repo.delete_all_for_owner("o") == ["friday@acme.isol8.co"]
    table.delete_item.assert_called_once_with(Key={"address": "friday@acme.isol8.co"})


async def test_drop_member_phones_removes_only_that_members_phones(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    phones = {"+15550001111": "other", "+15551234567": "me", "+15559990000": "me"}
    await repo.drop_member_phones({"address": "+15551230000", "member_phones": phones}, "me")
    calls = table.update_item.call_args_list
    assert sorted(c.kwargs["ExpressionAttributeNames"]["#p"] for c in calls) == ["+15551234567", "+15559990000"]
    assert all(c.kwargs["UpdateExpression"] == "REMOVE member_phones.#p" for c in calls)


async def test_set_disclosed_records_the_first_time_only(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.set_disclosed("+1555", "+15559998888")
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "+1555"}
    assert kwargs["UpdateExpression"] == "SET disclosed.#c = if_not_exists(disclosed.#c, :iso)"
    assert kwargs["ExpressionAttributeNames"] == {"#c": "+15559998888"}
    assert kwargs["ConditionExpression"] == "attribute_exists(address)"


async def test_text_line_for_owner_filters_kind(monkeypatch):
    rows = [{"address": "agents@x.co", "owner_id": "o"}, {"address": "+1555", "owner_id": "o", "kind": "text"}]
    monkeypatch.setattr(repo, "query_by_owner", lambda owner_id: _async(rows))
    assert (await repo.text_line_for_owner("o"))["address"] == "+1555"
    monkeypatch.setattr(repo, "query_by_owner", lambda owner_id: _async(rows[:1]))
    assert await repo.text_line_for_owner("o") is None


async def test_set_thread_mode_human_sets_and_agent_removes(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.set_thread_mode("+1555", "+15559998888", "human")
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "+1555"}
    assert kwargs["UpdateExpression"] == "SET human_threads.#c = :iso"
    assert kwargs["ExpressionAttributeNames"] == {"#c": "+15559998888"}
    assert kwargs["ExpressionAttributeValues"][":iso"]
    assert kwargs["ConditionExpression"] == "attribute_exists(address)"
    await repo.set_thread_mode("+1555", "a@b.co", "agent")
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "REMOVE human_threads.#c"
    assert kwargs["ExpressionAttributeNames"] == {"#c": "a@b.co"}
    assert "ExpressionAttributeValues" not in kwargs


async def test_set_business_phone(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.set_business_phone("+1555", "+15550001111")
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "SET business_phone = :p"
    assert kwargs["ExpressionAttributeValues"] == {":p": "+15550001111"}


async def test_delete_text_line_is_conditional_on_kind(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.delete_text_line("+1555")
    kwargs = table.delete_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "+1555"}
    assert kwargs["ConditionExpression"] == "kind = :text"
    assert kwargs["ExpressionAttributeValues"] == {":text": "text"}


async def test_delete_text_line_swallows_a_mail_row_or_missing(monkeypatch):
    table = MagicMock()
    table.delete_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem")
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.delete_text_line("agents@x.co")
    table.delete_item.side_effect = ClientError({"Error": {"Code": "InternalServerError"}}, "DeleteItem")
    with pytest.raises(ClientError):
        await repo.delete_text_line("+1555")


async def test_put_pending_line_item_and_condition(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.put_pending_line(owner_id="org_1", agent_id="a", operation_id="op_1") is True
    kwargs = table.put_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "attribute_not_exists(address)"
    item = kwargs["Item"]
    assert item["address"] == "pending:org_1" and item["kind"] == "text_pending"
    assert (item["owner_id"], item["agent_id"], item["operation_id"]) == ("org_1", "a", "op_1")
    assert item["created_at"]
    table.put_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    assert await repo.put_pending_line(owner_id="org_1", agent_id="a", operation_id="op_2") is False


async def test_pending_line_for_owner_filters_kind(monkeypatch):
    row = {"address": "pending:o", "kind": "text_pending"}
    monkeypatch.setattr(repo, "get_by_address", lambda a: _async(row if a == "pending:o" else None))
    assert await repo.pending_line_for_owner("o") == row
    monkeypatch.setattr(repo, "get_by_address", lambda a: _async({"address": a, "kind": "text"}))
    assert await repo.pending_line_for_owner("o") is None


async def test_pending_page_token_is_set_or_cleared_under_the_operation_fence(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.set_pending_page_token("org_1", operation_id="op_1", page_token="t.11") is True
    kw = table.update_item.call_args.kwargs
    assert kw["Key"] == {"address": "pending:org_1"} and kw["UpdateExpression"] == "SET page_token = :t"
    assert kw["ConditionExpression"] == "kind = :pending AND operation_id = :op"
    assert kw["ExpressionAttributeValues"] == {":pending": "text_pending", ":op": "op_1", ":t": "t.11"}
    assert await repo.set_pending_page_token("org_1", operation_id="op_1", page_token=None) is True
    kw = table.update_item.call_args.kwargs
    assert kw["UpdateExpression"] == "REMOVE page_token" and ":t" not in kw["ExpressionAttributeValues"]
    table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    assert await repo.set_pending_page_token("org_1", operation_id="op_2", page_token="t.11") is False


async def test_pending_delete_is_fenced_on_the_operation(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.delete_pending_line("o", operation_id="op_1") is True
    kwargs = table.delete_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "pending:o"}
    assert kwargs["ConditionExpression"] == "kind = :pending AND operation_id = :op"
    assert kwargs["ExpressionAttributeValues"] == {":pending": "text_pending", ":op": "op_1"}
    # A newer request replaced the row (or another reader removed it): the condition fails, nothing is deleted.
    table.delete_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem")
    assert await repo.delete_pending_line("o", operation_id="op_1") is False


async def test_set_member_phone_sets_and_removes(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.set_member_phone("+1555", "+15559998888", "user_1")
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "+1555"}
    assert kwargs["UpdateExpression"] == "SET member_phones.#p = :uid"
    assert kwargs["ExpressionAttributeNames"] == {"#p": "+15559998888"}
    assert kwargs["ExpressionAttributeValues"] == {":uid": "user_1"}
    assert kwargs["ConditionExpression"] == "attribute_exists(address)"  # proof wins: no holder check
    await repo.set_member_phone("+1555", "+15559998888", None)
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "REMOVE member_phones.#p"
    assert kwargs["ConditionExpression"] == "attribute_exists(address)"
    assert "ExpressionAttributeValues" not in kwargs


_CCF = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


async def test_put_phone_code_sets_the_code_and_prunes_expired(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.put_phone_code("+1555", "123456", "user_1", 1000, prune=["111111", "123456"]) is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "SET phone_codes.#c = :v REMOVE phone_codes.#e0"
    assert kwargs["ConditionExpression"] == "attribute_exists(address) AND attribute_not_exists(phone_codes.#c)"
    assert kwargs["ExpressionAttributeNames"] == {"#c": "123456", "#e0": "111111"}
    assert kwargs["ExpressionAttributeValues"] == {":v": {"user_id": "user_1", "exp": 1000}}
    table.update_item.side_effect = _CCF
    assert await repo.put_phone_code("+1555", "123456", "user_1", 1000, prune=[]) is False


async def test_bind_member_phone_is_single_use_and_drops_other_phones(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    ok = await repo.bind_member_phone(
        "+1555", code="123456", phone="+1999", user_id="u1", now=500, drop=["+1888", "+1999"]
    )
    assert ok is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "SET member_phones.#p = :uid REMOVE phone_codes.#c, member_phones.#d0"
    assert kwargs["ConditionExpression"] == "phone_codes.#c.user_id = :uid AND phone_codes.#c.exp > :now"
    assert kwargs["ExpressionAttributeNames"] == {"#c": "123456", "#p": "+1999", "#d0": "+1888"}
    assert kwargs["ExpressionAttributeValues"] == {":uid": "u1", ":now": 500}
    table.update_item.side_effect = _CCF
    assert await repo.bind_member_phone("+1555", code="1", phone="+1999", user_id="u1", now=500, drop=[]) is False


@pytest.mark.asyncio
async def test_phone_code_round_trip_on_dynamodb(agent_email_table):
    await repo.put_pending_line(owner_id="o", agent_id="a", operation_id="op")
    assert await repo.record_text_line(
        number="+1555", owner_id="o", agent_id="a", photon_resource_id="r", photon_user_id="u", operation_id="op"
    )
    assert await repo.pending_line_for_owner("o") is None
    await repo.set_member_phone("+1555", "+1888", "u1")
    assert await repo.put_phone_code("+1555", "000001", "u1", 100, prune=[]) is True
    assert await repo.put_phone_code("+1555", "000001", "u2", 100, prune=[]) is False  # live collision
    assert await repo.put_phone_code("+1555", "000002", "u1", 900, prune=["000001"]) is True
    assert await repo.bind_member_phone("+1555", code="000002", phone="+1999", user_id="u2", now=50, drop=[]) is False
    assert await repo.bind_member_phone("+1555", code="000002", phone="+1999", user_id="u1", now=950, drop=[]) is False
    assert (
        await repo.bind_member_phone("+1555", code="000002", phone="+1999", user_id="u1", now=50, drop=["+1888"])
        is True
    )
    row = await repo.get_by_address("+1555")
    assert row["member_phones"] == {"+1999": "u1"} and row["phone_codes"] == {}
    assert await repo.bind_member_phone("+1555", code="000002", phone="+1999", user_id="u1", now=50, drop=[]) is False
