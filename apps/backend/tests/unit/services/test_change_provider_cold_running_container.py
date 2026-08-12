"""RFC #1406 gap-1 / F1 probe: what a COLD provider change does to a RUNNING
container.

Prior to the F1 fix, change_provider's cold path (BYO involved on either
side, or targeting bedrock_claude) dispatched ``dispatch_provision``
directly. ``dispatch_provision``'s FIRST act is the single-flight lease
claim (container_provision.py:90-94), and ``try_claim_provision``'s
ConditionExpression admits only no-row / error / stopped / stale-provisioning
— ``running`` was NOT claimable (container_repo.py:157-199). So a RUNNING
container's provider switch silently stood down: the dispatched provision
lost the claim, ``provision_user_container`` was never reached, and the row
was left untouched — even though the caller was told ``restart: True``.

The fix (spec PR-11): the cold path now awaits ``stop_user_service`` (one
fast, fail-loud ECS call) and then dispatches
``ecs_manager.drain_then_dispatch_provision`` as a background task — it
waits for the drain (up to 90s, which is why it must not be awaited on the
request path) and then dispatches ``dispatch_provision``. That lands the
dispatch on the scale-from-zero branch (the only one that rewrites configs /
re-registers the task def) instead of racing a still-"running" row and
losing. The task deliberately does NOT re-stop: a status-poll wake can
legitimately claim the exposed "stopped" row first, and a second stop would
clobber its "provisioning" status back to claimable and let a second
executor start (see the poll-claim race test).

Post-SFN (spec §7 PR-10), ``dispatch_provision`` funnels to the durable
Step Functions executor (``provision_dispatch``): once the single-flight claim
is won against the row, its only side effect is ``StartExecution``; the actual
``provision_user_container`` runs in a SEPARATE one-shot ECS task
(``scripts.provision_runner``), out of process, which reads the (already
committed) provider off the billing row. So these tests drive the REAL
conditional write (moto) end-to-end through change_provider and pin the FIXED
behavior at the dispatch boundary (``_spawn_inline`` — post-inline-fast-path,
a won claim runs attempt 0 as a backend child process; ``start_execution`` is
now only Branch A's handoff, and must stay silent on these happy paths):

  * RUNNING row  -> stop-first flips the row to "stopped", the SAME dispatch
    then wins the claim and STARTS a provision execution (the runner applies
    the new provider) — no more silent stand-down.
  * STOPPED row  -> control. Already claimable; behaves the same as before —
    proving the running-row fix isn't a stubbing artifact.
  * NO row / NO ECS service -> nothing to stop; the dispatch falls through to
    the create-from-scratch claim path — a subscribed-but-never-provisioned
    owner's provider change must not 500 after committing the choice.
"""

import os
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
        "owner_id": "user_cold",
        "subscription_status": "active",
        "stripe_subscription_id": "sub_1",
        "provider_choice": "bedrock_claude",
    }


def _real_manager() -> EcsManager:
    """A real EcsManager (so ``reprovision_for_user``'s stop-then-drain logic
    genuinely runs and genuinely flips the container row to "stopped" via the
    real container_repo, against the moto table) with only the low-level ECS
    boto client swapped for a MagicMock — no network calls, no __init__
    dependency on ECS_CLUSTER_ARN/subnets/etc.

    ``describe_services`` reports an already-drained service so
    ``_wait_for_service_drained`` returns on its first poll — no real sleep.
    """
    manager = EcsManager.__new__(EcsManager)
    manager._ecs = MagicMock()
    manager._ecs.describe_services.return_value = {"services": [{"runningCount": 0, "pendingCount": 0}]}
    manager._cluster = "arn:aws:ecs:us-east-1:1:cluster/test"
    return manager


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
        # sites.
        patch.object(
            ecs_manager_module.asyncio,
            "create_task",
            new=lambda coro: created.append(coro) or MagicMock(),
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
async def test_cold_provider_change_on_running_container_reprovisions(cold_change_env):
    """F1 fix: a RUNNING row is stopped first, so the SAME dispatch wins the
    claim and applies the new provider — no more silent stand-down."""
    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "running", "service_name": "openclaw-user_cold-abc"})

    result = await _run_cold_change(env["created"])

    # The API told the caller a restart is coming.
    assert result == {
        "status": "ok",
        "provider_choice": "byo_key",
        "byo_provider": "anthropic",
        "restart": True,
    }

    # The stop was AWAITED on the request path (real container_repo write
    # against the moto table flipped the row to "stopped") — by the time
    # change_provider returned, the old-provider container was draining.
    env["manager"]._ecs.update_service.assert_called()
    assert env["manager"]._ecs.update_service.call_args.kwargs["desiredCount"] == 0
    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
    assert row["status"] == "stopped"

    # The drain-wait + provision dispatch ran OFF the request path: exactly
    # one drain_then_dispatch_provision task was dispatched. Run it — it
    # sees the drained service (NO second stop — see the poll-claim race
    # test below) and dispatches dispatch_provision (created[1]). Then
    # run that to completion.
    assert len(env["created"]) == 1, "expected exactly one drain-then-dispatch task"
    await env["created"][0]
    assert len(env["created"]) == 2, "expected the drain task to dispatch dispatch_provision"
    await env["created"][1]

    # Stop-first means the row was "stopped" by the time the claim ran: the
    # claim is WON and attempt 0 is dispatched (an inline runner subprocess)
    # — the runner applies the committed new provider off the billing row. The
    # fix's whole point: the running-row change is no longer a silent no-op.
    env["spawn"].assert_called_once()

    # The row reflects a genuinely-claimed reprovision, not a silent stand-down:
    # try_claim_provision stamped it "provisioning" with a real fencing token.
    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
    assert row["status"] == "provisioning"
    assert row["provision_claim_id"]
    # The new provider is committed on the billing row the runner reads.
    assert result["provider_choice"] == "byo_key"


@pytest.mark.asyncio
async def test_cold_provider_change_on_stopped_container_applies_new_provider(cold_change_env):
    """Control: the identical call against an already-STOPPED row also wins
    the claim and reaches provision_user_container with the NEW provider —
    proving the running-row fix above didn't regress the already-working
    stopped-row case."""
    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "stopped"})

    result = await _run_cold_change(env["created"])
    assert result["restart"] is True

    assert len(env["created"]) == 1
    await env["created"][0]
    assert len(env["created"]) == 2
    await env["created"][1]

    env["spawn"].assert_called_once()

    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
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

    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
    assert row is not None and row["status"] == "provisioning"
    assert result["provider_choice"] == "byo_key"


@pytest.mark.asyncio
async def test_poll_claim_between_stop_and_drain_task_is_not_clobbered(cold_change_env):
    """Single-flight under the accepted status-poll race: if a poll wake
    claims the exposed "stopped" row BEFORE the drain task runs, the drain
    task must neither clobber the claim (no second stop → no update_status
    over "provisioning"; it is conditioned on the row existing, not on the
    lease, so being scoped does not save it) nor start a second executor — its
    own dispatched dispatch_provision loses the lease and stands down."""
    from core.repositories import container_repo

    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "running", "service_name": "openclaw-user_cold-abc"})

    await _run_cold_change(env["created"])
    assert len(env["created"]) == 1  # the drain task, not yet run

    # A status-poll wake lands NOW: real conditional claim on the stopped row.
    poll_claim = await container_repo.try_claim_provision("user_cold")
    assert poll_claim, "poll should win the claim on the exposed stopped row"

    # Run the drain task. It must NOT reset the row to "stopped", and its
    # dispatched dispatch_provision must LOSE the lease and stand down.
    await env["created"][0]
    assert len(env["created"]) == 2
    await env["created"][1]

    # The drain task's dispatch LOST the lease to the poll's claim: NOTHING
    # provisioned — no inline attempt spawned and no SFN execution started
    # (single-flight — exactly one executor). This is the fence this file
    # exists for; it now lands on _spawn_inline.
    env["spawn"].assert_not_called()
    env["sfn"].start_execution.assert_not_called()

    # The poll's claim survived untouched — still provisioning, same token.
    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
    assert row["status"] == "provisioning"
    assert row["provision_claim_id"] == poll_claim


@pytest.mark.asyncio
async def test_cold_provider_change_stop_failure_reverts_commit_and_retry_works(cold_change_env):
    """A GENUINE ECS stop failure (not service-not-found) after the provider
    commit must revert the billing row, so a retry re-runs the full switch
    instead of hitting the no-op fast-path and silently dispatching nothing
    — the running container would otherwise keep the old provider forever
    (running rows are neither poll-retryable nor claimable)."""
    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "running", "service_name": "openclaw-user_cold-abc"})
    env["manager"]._ecs.update_service.side_effect = ClientError(
        {"Error": {"Code": "ClusterNotFoundException", "Message": "throttled"}},
        "UpdateService",
    )

    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await _run_cold_change(env["created"])
    assert exc.value.code == "reprovision_stop_failed"

    # The commit was REVERTED — the shared row-mirror reflects the old
    # provider again — and no reprovision task was dispatched.
    account = await billing_service.billing_repo.get_by_owner_id("user_cold")
    assert account["provider_choice"] == "bedrock_claude"
    assert env["created"] == []

    # The stop was NOT applied (ECS itself failed), so the row must NOT be
    # re-stamped "stopped" — that would trigger a pointless restart cycle
    # of a genuinely-running container on a transient ECS throttle.
    row = env["table"].get_item(Key={"owner_id": "user_cold"}).get("Item")
    assert row["status"] == "running"

    # Retry once ECS recovers: NOT a noop — the full switch re-runs and the
    # provision executor applies the new provider.
    env["manager"]._ecs.update_service.side_effect = None
    result = await _run_cold_change(env["created"])
    assert result["status"] == "ok"
    assert result["restart"] is True

    assert len(env["created"]) == 1
    await env["created"][0]
    assert len(env["created"]) == 2
    await env["created"][1]

    env["spawn"].assert_called_once()
    assert result["provider_choice"] == "byo_key"


@pytest.mark.asyncio
async def test_stop_failure_reverts_legacy_row_to_default_not_stuck(cold_change_env):
    """A LEGACY billing row carries no provider_choice (resolves to
    bedrock_claude). On a genuine stop failure after the commit, the revert
    must restore the effective default rather than skip — otherwise the row
    keeps the newly-committed provider and a retry hits the no-op fast path,
    stranding the still-running container on the old provider forever."""
    env = cold_change_env
    del env["row"]["provider_choice"]  # legacy: never chose a provider
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "running", "service_name": "openclaw-user_cold-abc"})
    env["manager"]._ecs.update_service.side_effect = ClientError(
        {"Error": {"Code": "ClusterNotFoundException", "Message": "throttled"}},
        "UpdateService",
    )

    with pytest.raises(billing_service.ChangeProviderError):
        await _run_cold_change(env["created"])

    # Reverted to the resolved legacy default -- NOT left on the requested
    # byo_key (which would no-op the retry), NOT left absent (repo rejects None).
    account = await billing_service.billing_repo.get_by_owner_id("user_cold")
    assert account["provider_choice"] == "bedrock_claude"
    assert env["created"] == []  # nothing dispatched


@pytest.mark.asyncio
async def test_cold_change_fences_out_in_flight_poll_wake_claim(cold_change_env):
    """A poll wake that claimed BEFORE the provider commit resolved the OLD
    provider; without a fence it would keep passing its leased checkpoints
    (the stop's row write preserves its claim) and finish with the old
    config while our delayed dispatch stands down. The cold path now
    takeover_provision's after the stop — same deliberate-door fencing as
    conversion //recover/admin — so the stale run dies at its next
    checkpoint and OUR dispatch applies the new provider."""
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded

    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "stopped"})
    poll_claim = await container_repo.try_claim_provision("user_cold")
    assert poll_claim, "poll wake should win the claim on the stopped row"

    result = await _run_cold_change(env["created"])
    assert result["restart"] is True

    # The in-flight poll run is SUPERSEDED: its next checkpoint raises
    # instead of letting it write the pre-commit provider's config.
    with pytest.raises(ProvisionSuperseded):
        await container_repo.update_container_leased("user_cold", poll_claim, {"substatus": "poison"})

    # Our own dispatch proceeds end-to-end with the NEW provider.
    assert len(env["created"]) == 1
    await env["created"][0]
    assert len(env["created"]) == 2
    await env["created"][1]
    env["spawn"].assert_called_once()
    assert result["provider_choice"] == "byo_key"


@pytest.mark.asyncio
async def test_stop_row_write_failure_also_reverts_commit(cold_change_env):
    """The revert must cover the WHOLE stop path, not just the wrapped ECS
    call: stop_user_service's row writes after desiredCount=0 (update_status /
    stamp_stop_reason) propagate raw DDB exceptions, and skipping the revert
    for those would leave the provider row committed with no dispatch and no
    convergence path."""
    env = cold_change_env
    env["table"].put_item(Item={"owner_id": "user_cold", "status": "running", "service_name": "openclaw-user_cold-abc"})

    update_status_mock = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "ddb blip"}},
            "UpdateItem",
        )
    )
    with patch("core.repositories.container_repo.update_status", new=update_status_mock):
        with pytest.raises(billing_service.ChangeProviderError) as exc:
            await _run_cold_change(env["created"])
    assert exc.value.code == "reprovision_stop_failed"

    # Reverted + nothing dispatched — same contract as the ECS-failure case.
    account = await billing_service.billing_repo.get_by_owner_id("user_cold")
    assert account["provider_choice"] == "bedrock_claude"
    assert env["created"] == []

    # The ECS stop DID land in this shape, so the handler attempted the
    # best-effort "stopped" re-stamp (a second update_status call — here it
    # fails too, which is tolerated; /recover remains the backstop). The
    # ECS-failure case must NOT re-stamp — pinned by the running-row status
    # assert in the retry-works test above.
    assert update_status_mock.call_count == 2
