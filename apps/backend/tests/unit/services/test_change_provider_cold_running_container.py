"""RFC #1406 gap-1 / F1 probe: what a COLD provider change does on ECS.

Historical context: prior to the F1 fix, change_provider's cold path (BYO
involved on either side, or targeting bedrock_claude) dispatched
``dispatch_provision`` directly, and a RUNNING ECS row silently stood down
(``try_claim_provision`` doesn't admit "running"). The fix made the cold path
await ``stop_user_service`` first, then dispatch
``ecs_manager.drain_then_dispatch_provision``. Every RUNNING-row scenario this
file used to pin (stop-then-drain, the poll-claim race across the stop, revert
on a genuine ECS stop failure) is now dead: deletion PR-1 made
``runtime_transport.host_for`` unconditionally report ``"agentcore"``, so
``EcsManager._hosted_on_agentcore`` is ``True`` for ANY existing container row
regardless of its actual ``runtime_host`` or ``status`` — ``stop_user_service``
no-ops (never touches ECS, never flips the row) for every owner that already
has a row. Those tests were deleted with the ECS branch they pinned.

What remains genuinely exercises the create-from-scratch / already-claimable
paths, which don't depend on an ECS stop actually running:

  * STOPPED row  -> already claimable; the dispatch wins the claim and starts
    a provision execution with the new provider.
  * NO row / NO ECS service -> ``_hosted_on_agentcore`` is False for a
    nonexistent row, so the stop genuinely reaches ECS, hits
    ServiceNotFoundException (treated as idempotent), and the dispatch falls
    through to the create-from-scratch claim path — a subscribed-but-never-
    provisioned owner's provider change must not 500 after committing the
    choice.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers.ecs_manager import EcsManager  # noqa: E402
from core.containers import ecs_manager as ecs_manager_module  # noqa: E402
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
        "owner_id": "org_cold",
        "subscription_status": "active",
        "stripe_subscription_id": "sub_1",
        "provider_choice": "bedrock_claude",
    }


@asynccontextmanager
async def _noop_owner_lifecycle_guard(owner_id: str):
    yield


def _real_manager() -> EcsManager:
    """A real EcsManager (so ``reprovision_for_user``'s stop-then-drain logic
    genuinely runs and genuinely flips the container row to "stopped" via the
    real container_repo, against the moto table) with only the low-level ECS
    boto client swapped for a MagicMock — no network calls, no __init__
    dependency on ECS_CLUSTER_ARN/subnets/etc.

    ``list_tasks`` reports no task records so ``_wait_for_service_drained``
    (which verifies actual task termination) returns on its first poll — no
    real sleep.
    """
    manager = EcsManager.__new__(EcsManager)
    manager._ecs = MagicMock()
    manager._ecs.describe_services.return_value = {"services": [{"runningCount": 0, "pendingCount": 0}]}
    manager._ecs.list_tasks.return_value = {"taskArns": []}
    manager._cluster = "arn:aws:ecs:us-east-1:1:cluster/test"
    return manager


async def _run_cold_change(created_tasks: list) -> dict:
    """Drive change_provider bedrock_claude -> byo_key (cold: involves_byo)."""
    return await billing_service.change_provider(
        owner_id="org_cold",
        clerk_user_id="user_cold",
        owner_type="org",
        provider_choice="byo_key",
        byo_provider="anthropic",
    )


@pytest.fixture
def cold_change_env(containers_table):
    """Stub everything around the functions under test.

    billing_repo is the SAME module object in billing_service and
    container_provision (both `from core.repositories import billing_repo`),
    so one patch serves change_provider's guard read AND
    dispatch_provision's resolve_provider_choice read.
    """
    # The dispatched dispatch_provision funnels to provision_dispatch, whose
    # only side effect once the single-flight claim is won against the real
    # moto row is to spawn attempt 0 -- a provision_runner CHILD PROCESS of
    # the backend, not an in-process provision_user_container call and no
    # longer StartExecution. So the boundary these tests pin is _spawn_inline.
    # The NEW provider is applied by the runner off the (already-committed)
    # billing row; the test asserts the dispatch fired + the row committed the
    # new choice.
    fake_sfn = MagicMock()
    fake_spawn = MagicMock()
    fake_sfn.start_execution.return_value = {
        "executionArn": "arn:aws:states:us-east-1:1:execution/prov/exec-1",
        "startDate": 0,
    }
    real_manager = _real_manager()
    created: list = []
    row = _billing_row()
    _real_create_task = asyncio.create_task  # captured before the patch below shadows it

    async def _commit_choice(
        owner_id, *, provider_choice, byo_provider, connector_user_id=None, requires_reprovision=True
    ):
        # Mirror billing_repo.set_provider_choice: the dispatched
        # dispatch_provision re-reads the row and must see the NEW choice.
        # requires_reprovision accepted (the revert path passes it) but ignored —
        # the stub doesn't model the container-marking side effect.
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
        # stop_user_service is now @serialize_owner_lifecycle: it takes a
        # cross-process file lock under settings.EFS_MOUNT_PATH before running.
        # There is no EFS mount in the unit-test sandbox (real os.makedirs
        # there raises), so the guard is stubbed to a no-op — same spirit as
        # every other real-I/O collaborator this fixture stubs out.
        patch("core.services.owner_lifecycle.owner_lifecycle_guard", new=_noop_owner_lifecycle_guard),
        # change_provider's cold path now lazily imports get_ecs_manager from
        # core.containers, awaits .stop_user_service, and dispatches
        # .drain_then_dispatch_provision(owner_id) as a task — hand it a
        # real EcsManager (ECS client mocked, see _real_manager) so the
        # stop-first fix's stop -> drain -> dispatch sequence genuinely runs.
        patch("core.containers.get_ecs_manager", return_value=real_manager),
        # Capture BOTH dispatched tasks instead of scheduling them, so the
        # test can await them deterministically: billing_service dispatches
        # drain_then_dispatch_provision (created[0]) and, inside it,
        # ecs_manager dispatches dispatch_provision (created[1]). Both
        # modules do `import asyncio` at module scope — the SAME module
        # object — so one patch of asyncio.create_task covers both call
        # sites. Only those two named coroutines are intercepted; anything
        # else (e.g. a stray create_task from an un-stubbed collaborator)
        # falls through to the real create_task instead of silently vanishing
        # into `created` and corrupting its assertions.
        patch.object(
            ecs_manager_module.asyncio,
            "create_task",
            new=lambda coro: (
                (created.append(coro), MagicMock())[1]
                if coro.cr_code.co_name in ("drain_then_dispatch_provision", "dispatch_provision")
                else _real_create_task(coro)
            ),
        ),
        # The SFN dispatch boundary: start_execution is reached ONLY if the
        # single-flight claim is won against the real moto row. Kept because
        # Branch A (a stamped, still-owned claim) still hands off to the
        # machine — these tests assert it stays SILENT on the happy path.
        patch("core.services.provision_dispatch._client", return_value=fake_sfn),
        # THE dispatch boundary these tests pin. Post-inline-fast-path, a won
        # claim runs attempt 0 as a backend child process instead of calling
        # StartExecution, so _spawn_inline — not start_execution — is what
        # "a provision was dispatched" means. Recorded rather than executed:
        # the real one would fork a provision_runner subprocess.
        patch("core.services.provision_dispatch._spawn_inline", new=fake_spawn),
    ):
        yield {
            "sfn": fake_sfn,
            "spawn": fake_spawn,
            "manager": real_manager,
            "created": created,
            "table": containers_table,
            "row": row,
        }


@pytest.mark.asyncio
async def test_cold_provider_change_on_stopped_container_applies_new_provider(cold_change_env):
    """Control: the identical call against an already-STOPPED row also wins
    the claim and reaches provision_user_container with the NEW provider —
    proving the running-row fix above didn't regress the already-working
    stopped-row case."""
    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "org_cold", "status": "stopped"})

    result = await _run_cold_change(env["created"])
    assert result["restart"] is True

    assert len(env["created"]) == 1
    await env["created"][0]
    assert len(env["created"]) == 2
    await env["created"][1]

    env["spawn"].assert_called_once()

    row = env["table"].get_item(Key={"owner_id": "org_cold"}).get("Item")
    assert row["status"] == "provisioning"
    assert row["provision_claim_id"]
    assert result["provider_choice"] == "byo_key"


@pytest.mark.asyncio
async def test_cold_provider_change_with_no_container_still_provisions(cold_change_env):
    """A subscribed owner with NO container row and NO ECS service (e.g.
    post-checkout before the first /container/status provision, or a legacy
    owner falling back from the hot path) must not 500 after committing the
    provider choice: the stop treats service-not-found as idempotent success
    and the dispatch falls through to dispatch_provision's
    create-from-scratch path."""
    env = cold_change_env
    # No container row put_item — the table is empty for this owner.
    env["manager"]._ecs.update_service.side_effect = ClientError(
        {"Error": {"Code": "ServiceNotFoundException", "Message": "Service not found."}},
        "UpdateService",
    )
    env["manager"]._ecs.describe_services.return_value = {
        "services": [],
        "failures": [{"reason": "MISSING"}],
    }

    result = await _run_cold_change(env["created"])
    assert result["restart"] is True

    # Both dispatches still happen: drain_then_dispatch_provision, then
    # dispatch_provision from inside it.
    assert len(env["created"]) == 1
    await env["created"][0]
    assert len(env["created"]) == 2
    await env["created"][1]

    # The no-row claim path (attribute_not_exists) is won and an SFN execution
    # is dispatched — the container is created from scratch by the runner with
    # the NEW provider, exactly what the pre-F1 bare dispatch did for this case.
    env["spawn"].assert_called_once()

    row = env["table"].get_item(Key={"owner_id": "org_cold"}).get("Item")
    assert row is not None and row["status"] == "provisioning"
    assert result["provider_choice"] == "byo_key"
