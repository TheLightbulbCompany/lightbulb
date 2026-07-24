"""RFC #1406 gap-1 probe: what a COLD provider change does to a RUNNING container.

change_provider's cold path (BYO involved on either side, or targeting
bedrock_claude) dispatches ``background_provision`` and answers
``restart: True`` (billing_service.py:1187-1196). background_provision's
FIRST act is the single-flight lease claim (container_provision.py:90-94),
and ``try_claim_provision``'s ConditionExpression admits only
no-row / error / stopped / stale-provisioning — ``running`` is NOT claimable
(container_repo.py:157-199).

These tests drive the REAL conditional write (moto) end-to-end through
change_provider to pin the resulting behavior:

  * RUNNING row  -> the dispatched provision loses the claim and stands down;
    ``provision_user_container`` is NEVER reached; the row is untouched; the
    caller was nevertheless told ``restart: True``. The new provider lands on
    the billing row only — the live container keeps its old task-def + config.
  * STOPPED row  -> the claim wins and the SAME dispatch applies the new
    provider via ``provision_user_container`` (the desired==0 branch is the
    only one that rewrites configs / re-registers the task-def).

The asymmetry is the finding: the cold provider change only actually applies
when the container happens to be down — for the always-on paid fleet (the
only population allowed to call change_provider) it silently no-ops.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import billing_service  # noqa: E402


@pytest.fixture
def containers_table():
    """Real DynamoDB engine (moto) for the containers table — the claim is a
    conditional write, and a hand mock would happily pass expressions
    DynamoDB rejects (same rationale as test_container_repo_provision_claim)."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-containers")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _billing_row():
    return {
        "owner_id": "user_cold",
        "subscription_status": "active",
        "stripe_subscription_id": "sub_1",
        "provider_choice": "bedrock_claude",
    }


async def _run_cold_change(created_tasks: list) -> dict:
    """Drive change_provider bedrock_claude -> byo_key (cold: involves_byo)."""
    return await billing_service.change_provider(
        owner_id="user_cold",
        clerk_user_id="user_cold",
        owner_type="personal",
        provider_choice="byo_key",
        byo_provider="anthropic",
    )


@pytest.fixture
def cold_change_env(containers_table):
    """Stub everything around the two functions under test.

    billing_repo is the SAME module object in billing_service and
    container_provision (both `from core.repositories import billing_repo`),
    so one patch serves change_provider's guard read AND
    background_provision's resolve_provider_choice read.
    """
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="svc-name")
    created: list = []
    metrics = MagicMock()
    row = _billing_row()

    async def _commit_choice(owner_id, *, provider_choice, byo_provider, connector_user_id=None):
        # Mirror billing_repo.set_provider_choice: the dispatched
        # background_provision re-reads the row and must see the NEW choice.
        row["provider_choice"] = provider_choice
        row["byo_provider"] = byo_provider

    with (
        patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value=row),
        ),
        patch.object(
            billing_service.billing_repo,
            "set_provider_choice",
            new=AsyncMock(side_effect=_commit_choice),
        ),
        # Lazy imports inside change_provider — patch at their source modules.
        patch("core.services.provision_gate.is_subscription_active", lambda a: True),
        patch(
            "core.services.key_service.get_secret_arn",
            new=AsyncMock(return_value="arn:aws:secretsmanager:us-east-1:1:secret:byok"),
        ),
        patch("core.services.owner_facts.write_owner_facts", new=AsyncMock()),
        patch.object(billing_service, "put_metric", new=MagicMock()),
        # Capture the dispatched coroutine instead of scheduling it, so the
        # test can await it deterministically.
        patch.object(
            billing_service.asyncio,
            "create_task",
            new=lambda coro: created.append(coro) or MagicMock(),
        ),
        # The provision executor: reached ONLY if the claim is won.
        patch("core.services.container_provision.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.container_provision._emit_provision_metric", new=metrics),
    ):
        yield {"ecs": fake_ecs, "created": created, "metrics": metrics, "table": containers_table}


@pytest.mark.asyncio
async def test_cold_provider_change_on_running_container_silently_stands_down(cold_change_env):
    env = cold_change_env
    env["table"].put_item(
        Item={"owner_id": "user_cold", "status": "running", "service_name": "openclaw-user_cold-abc"}
    )

    result = await _run_cold_change(env["created"])

    # The API told the caller a restart is coming.
    assert result == {
        "status": "ok",
        "provider_choice": "byo_key",
        "byo_provider": "anthropic",
        "restart": True,
    }

    # Actually run the dispatched background_provision to completion.
    assert len(env["created"]) == 1, "expected exactly one background_provision dispatch"
    await env["created"][0]

    # The claim lost against status="running": the provision executor was
    # NEVER reached — no config rewrite, no task-def re-register, no restart.
    env["ecs"].provision_user_container.assert_not_awaited()
    env["metrics"].assert_called_once_with("claim_lost")

    # The container row is untouched: still "running", no lease stamped, no
    # provisioning flip — so the status poll will never show the change either.
    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
    assert row["status"] == "running"
    assert "provision_claim_id" not in row
    assert "provision_claimed_at" not in row


@pytest.mark.asyncio
async def test_cold_provider_change_on_stopped_container_applies_new_provider(cold_change_env):
    """Control: the identical call against a STOPPED row wins the claim and
    reaches provision_user_container with the NEW provider — proving the
    running-row stand-down above is a state-dependent hole, not a stubbing
    artifact."""
    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "stopped"})

    result = await _run_cold_change(env["created"])
    assert result["restart"] is True

    assert len(env["created"]) == 1
    await env["created"][0]

    env["ecs"].provision_user_container.assert_awaited_once()
    kwargs = env["ecs"].provision_user_container.await_args.kwargs
    assert kwargs["provider_choice"] == "byo_key"
    assert kwargs["byo_provider"] == "anthropic"
    assert kwargs["claim_id"]  # the won lease's fencing token

    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
    assert row["status"] == "provisioning"
    assert row["provision_claim_id"] == kwargs["claim_id"]
