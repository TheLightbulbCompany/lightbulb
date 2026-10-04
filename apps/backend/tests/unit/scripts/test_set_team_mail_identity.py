"""set_team_mail_identity: move a workspace's team mail to a new slug with a public address."""

import os
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from scripts import set_team_mail_identity as script  # noqa: E402

OWNER = "org_founders"
DOMAIN = "thelightbulb.company"
OLD = f"agents@isol8-2.{DOMAIN}"
NEW = f"agents@lightbulb.{DOMAIN}"
PUBLIC = f"agents@{DOMAIN}"


def _table(resource, name, key, sk=None, gsi=None):
    attrs = {key, *([sk] if sk else []), *([gsi] if gsi else [])}
    kwargs = {
        "TableName": f"test-{name}",
        "KeySchema": [{"AttributeName": key, "KeyType": "HASH"}]
        + ([{"AttributeName": sk, "KeyType": "RANGE"}] if sk else []),
        "AttributeDefinitions": [{"AttributeName": a, "AttributeType": "S"} for a in attrs],
        "BillingMode": "PAY_PER_REQUEST",
    }
    if gsi:
        kwargs["GlobalSecondaryIndexes"] = [
            {
                "IndexName": "by-owner",
                "KeySchema": [{"AttributeName": gsi, "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ]
    return resource.create_table(**kwargs)


@pytest.fixture
def tables():
    with mock_aws():
        r = boto3.resource("dynamodb", region_name="us-east-1")
        t = {
            "slugs": _table(r, "company-slugs", "slug", gsi="owner_id"),
            "addresses": _table(r, "agent-email-addresses", "address", gsi="owner_id"),
            "inbox": _table(r, "agent-mail-inbox", "address", sk="received_key"),
        }
        t["slugs"].put_item(Item={"slug": "isol8-2", "owner_id": OWNER, "created_at": "2026-01-01T00:00:00Z"})
        t["slugs"].put_item(Item={"slug": "other", "owner_id": "org_other", "created_at": "2026-01-01T00:00:00Z"})
        t["addresses"].put_item(
            Item={
                "address": OLD,
                "owner_id": OWNER,
                "verified_senders": ["a@x.com"],
                "send_to_anyone": False,
                "status": "active",
                "outbound_disabled": "complaint",
                "meetings": {"m1", "m2"},
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        for i in range(3):
            t["inbox"].put_item(Item={"address": OLD, "received_key": f"2026-10-0{i}#id{i}", "ttl": 100 + i})
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", r),
            patch.object(script.settings, "AGENT_EMAIL_DOMAIN", DOMAIN),
            patch("core.services.owner_facts.write_owner_facts", AsyncMock()) as facts,
        ):
            t["facts"] = facts
            yield t


def _state(t) -> dict:
    return {name: sorted(map(repr, t[name].scan()["Items"])) for name in ("slugs", "addresses", "inbox")}


async def _run(*extra: str) -> int:
    return await script.main(["--owner-id", OWNER, "--new-slug", "lightbulb", "--public-address", PUBLIC, *extra])


@pytest.mark.asyncio
async def test_full_run_moves_slug_row_and_inbox(tables):
    assert await _run() == 0
    slugs = {r["slug"]: r["owner_id"] for r in tables["slugs"].scan()["Items"]}
    assert slugs == {"lightbulb": OWNER, "other": "org_other"}
    rows = {r["address"]: r for r in tables["addresses"].scan()["Items"]}
    assert set(rows) == {NEW, PUBLIC}
    assert rows[PUBLIC] == {"address": PUBLIC, "alias_of": NEW, "owner_id": OWNER}
    new = rows[NEW]
    assert new["public_address"] == PUBLIC
    assert new["verified_senders"] == ["a@x.com"] and new["send_to_anyone"] is False
    assert new["outbound_disabled"] == "complaint" and new["meetings"] == {"m1", "m2"}
    inbox = tables["inbox"].scan()["Items"]
    assert {(r["address"], r["received_key"], int(r["ttl"])) for r in inbox} == {
        (NEW, f"2026-10-0{i}#id{i}", 100 + i) for i in range(3)
    }
    tables["facts"].assert_awaited_once_with(OWNER)
    from core.repositories import agent_email_repo
    from core.services import agent_email

    assert (await agent_email_repo.resolve(PUBLIC))["address"] == NEW
    assert await agent_email.team_address(OWNER) == NEW
    assert await agent_email.public_address(OWNER) == PUBLIC


@pytest.mark.asyncio
async def test_rerun_after_each_partial_step_ends_in_the_same_state(tables):
    assert await _run() == 0
    done = _state(tables)
    assert await _run() == 0  # a re-run of a finished move is a no-op
    assert _state(tables) == done


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["delete_item", "put_item"])
async def test_rerun_finishes_a_move_that_failed_midway(tables, fail_at):
    """A crash at the first delete (old slug row) or the first address write leaves a half move;
    the re-run completes it to the same end state as a clean run."""
    real = script.run_in_thread
    calls = {"n": 0}

    async def flaky(fn, *a, **kw):
        if fn.__name__ == fail_at and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("boom")
        return await real(fn, *a, **kw)

    with patch.object(script, "run_in_thread", flaky):
        assert await _run() == 1
    assert await _run() == 0
    rows = {r["address"] for r in tables["addresses"].scan()["Items"]}
    assert rows == {NEW, PUBLIC}
    assert {r["address"] for r in tables["inbox"].scan()["Items"]} == {NEW}


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(tables):
    before = _state(tables)
    assert await _run("--dry-run") == 0
    assert _state(tables) == before
    tables["facts"].assert_not_awaited()


@pytest.mark.asyncio
async def test_slug_held_by_another_owner_is_refused(tables):
    before = _state(tables)
    assert await script.main(["--owner-id", OWNER, "--new-slug", "other"]) == 2
    assert _state(tables) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("addr", ["agents@evil.com", f"hello@{DOMAIN}", f"agents@sub.{DOMAIN}"])
async def test_public_address_other_than_the_apex_team_address_is_refused(tables, addr):
    before = _state(tables)
    assert await script.main(["--owner-id", OWNER, "--new-slug", "lightbulb", "--public-address", addr]) == 2
    assert _state(tables) == before
