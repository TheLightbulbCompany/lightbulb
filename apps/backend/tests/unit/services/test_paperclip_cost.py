"""Per-agent managed cost -> Paperclip cost events, with sub-cent carry (moto-backed repo)."""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from core.repositories.paperclip_repo import PaperclipRepo
from core.services import paperclip_cost

TABLE = "test-paperclip-companies"
OWNER, AGENT = "org_1", "agent-uuid"
OCT = "2026-10-05T12:00:00+00:00"
NOV = "2026-11-02T00:00:00+00:00"


@pytest.fixture
def repo():
    with mock_aws():
        res = boto3.resource("dynamodb", region_name="us-east-1")
        res.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", ""), patch("core.dynamodb._dynamodb_resource", res):
            r = PaperclipRepo(table_name=TABLE)
            row = {
                "user_id": OWNER,
                "org_id": OWNER,
                "company_id": "co1",
                "status": "active",
                "created_at": "2026-10-01T00:00:00+00:00",
                "updated_at": "2026-10-01T00:00:00+00:00",
            }
            res.Table(TABLE).put_item(Item=row)  # company_id and the cost remainder stay on the old row
            from core import tasks_schema as ts

            r._ws_table().put_item(Item=r._ws_item(row, OWNER, ts.META_SK))  # META moved to tasks (copy_workspace_meta)
            yield r


@pytest.fixture
def admin():
    client = AsyncMock()
    client._http = object()
    with (
        patch.object(paperclip_cost, "shared_admin_client", return_value=client),
        patch.object(paperclip_cost, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(paperclip_cost, "invalidate_admin_session"),
    ):
        yield client


async def _rec(repo, micro, at=OCT):
    return await paperclip_cost.record_cost(
        owner_id=OWNER, agent_id=AGENT, cost_microcents=micro, model="m", occurred_at=at, repo=repo
    )


async def test_subcent_accrues_without_posting(repo, admin):
    assert await _rec(repo, 600) == 0
    admin.post_cost_event.assert_not_awaited()


async def test_crossing_a_cent_posts_whole_cents_and_keeps_remainder(repo, admin):
    for _ in range(16):  # 16 x 600 = 9600 < 10000
        assert await _rec(repo, 600) == 0
    assert await _rec(repo, 600) == 1  # 10200 -> 1 cent, 200 left
    admin.post_cost_event.assert_awaited_once()
    kw = admin.post_cost_event.await_args.kwargs
    assert (kw["company_id"], kw["agent_id"], kw["cost_cents"], kw["occurred_at"]) == ("co1", AGENT, 1, OCT)
    row = repo._table().get_item(Key={"user_id": OWNER})["Item"]
    assert row["agent_cost_remainder"][f"{AGENT}#2026-10"] == 200


async def test_multi_cent_single_call_and_month_keys_separate(repo, admin):
    assert await _rec(repo, 25_000) == 2
    assert await _rec(repo, 9_000, at=NOV) == 0  # new month: own bucket, not pushed over by October's 5_000
    rem = repo._table().get_item(Key={"user_id": OWNER})["Item"]["agent_cost_remainder"]
    assert rem[f"{AGENT}#2026-10"] == 5_000 and rem[f"{AGENT}#2026-11"] == 9_000


async def test_post_failure_refunds_the_claim(repo, admin):
    admin.post_cost_event.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await _rec(repo, 25_000)
    rem = repo._table().get_item(Key={"user_id": OWNER})["Item"]["agent_cost_remainder"]
    assert rem[f"{AGENT}#2026-10"] == 25_000  # nothing lost; next call retries


async def test_no_company_or_no_cost_is_a_noop(repo, admin):
    assert (
        await paperclip_cost.record_cost(
            owner_id="nobody", agent_id=AGENT, cost_microcents=50_000, model="m", occurred_at=OCT, repo=repo
        )
        == 0
    )
    assert await _rec(repo, 0) == 0
    assert await _rec(repo, -5) == 0
    admin.post_cost_event.assert_not_awaited()


async def test_old_month_keys_are_pruned(repo, admin):
    repo._table().update_item(
        Key={"user_id": OWNER},
        UpdateExpression="SET agent_cost_remainder = :m",
        ExpressionAttributeValues={":m": {f"{AGENT}#2026-01": 10, f"{AGENT}#2026-09": 10}},
    )
    await _rec(repo, 100)
    rem = repo._table().get_item(Key={"user_id": OWNER})["Item"]["agent_cost_remainder"]
    assert f"{AGENT}#2026-01" not in rem and f"{AGENT}#2026-09" in rem
