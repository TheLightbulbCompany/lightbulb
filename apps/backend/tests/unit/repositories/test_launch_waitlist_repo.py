"""launch-waitlist repository — moto-backed, mirrors test_admin_actions_repo."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

TABLE = "test-launch-waitlist"


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "email", "AttributeType": "S"},
                {"AttributeName": "referral_code", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "referral-code-index",
                    "KeySchema": [{"AttributeName": "referral_code", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        from core.repositories import launch_waitlist_repo as repo

        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield repo


async def test_join_assigns_sequential_positions_and_codes(table):
    a = await table.join("A@Example.com ", referred_by=None, source="waitlist")
    b = await table.join("b@example.com", referred_by=None, source="waitlist")
    assert a["email"] == "a@example.com"
    assert (a["position"], b["position"]) == (1, 2)
    assert len(a["referral_code"]) == 8 and a["referral_code"] != b["referral_code"]
    assert a["source"] == "waitlist" and a["referrals_signed_up"] == 0


async def test_join_is_idempotent_per_email(table):
    first = await table.join("a@example.com", referred_by=None, source="waitlist")
    again = await table.join("A@EXAMPLE.COM", referred_by=None, source="signup")
    assert again["position"] == first["position"]
    assert again["referral_code"] == first["referral_code"]
    assert again["source"] == "waitlist"  # first write wins
    assert await table.count() == 1  # one person, not two


async def test_join_attributes_referral_and_counts_it(table):
    ref = await table.join("ref@example.com", referred_by=None, source="waitlist")
    new = await table.join("new@example.com", referred_by=ref["referral_code"], source="waitlist")
    assert new["referred_by"] == ref["referral_code"]
    assert (await table.get("ref@example.com"))["referrals_signed_up"] == 1


async def test_join_ignores_unknown_and_self_referral(table):
    row = await table.join("me@example.com", referred_by="NOPE1234", source="waitlist")
    assert "referred_by" not in row
    assert not await table.attribute("me@example.com", row["referral_code"])


async def test_attribute_only_once(table):
    ref = await table.join("ref@example.com", referred_by=None, source="waitlist")
    other = await table.join("other@example.com", referred_by=None, source="waitlist")
    me = await table.join("me@example.com", referred_by=None, source="signup")
    assert await table.attribute(me["email"], ref["referral_code"]) is True
    assert await table.attribute(me["email"], other["referral_code"]) is False
    assert (await table.get("me@example.com"))["referred_by"] == ref["referral_code"]
    assert (await table.get("ref@example.com"))["referrals_signed_up"] == 1
    assert (await table.get("other@example.com"))["referrals_signed_up"] == 0


async def test_get_by_referral_code(table):
    row = await table.join("a@example.com", referred_by=None, source="waitlist")
    assert (await table.get_by_referral_code(row["referral_code"]))["email"] == "a@example.com"
    assert await table.get_by_referral_code("ZZZZZZZZ") is None


async def test_claim_user_sets_once(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    await table.claim_user("a@example.com", "user_1")
    await table.claim_user("a@example.com", "user_2")
    assert (await table.get("a@example.com"))["user_id"] == "user_1"


async def test_mark_rewarded_is_cas(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    assert await table.mark_rewarded("a@example.com") is True
    assert await table.mark_rewarded("a@example.com") is False


async def test_add_counter_floors_at_zero(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    assert await table.add_counter("a@example.com", "free_months_banked", 2) == 2
    assert await table.add_counter("a@example.com", "free_months_banked", -1) == 1
    assert await table.add_counter("a@example.com", "free_months_banked", -5) == 1  # refused, unchanged


async def test_list_all_excludes_counter_row(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    await table.join("b@example.com", referred_by=None, source="waitlist")
    emails = {r["email"] for r in await table.list_all()}
    assert emails == {"a@example.com", "b@example.com"}


def test_normalize_email_rejects_garbage(table):
    with pytest.raises(ValueError):
        table.normalize_email("not-an-email")
