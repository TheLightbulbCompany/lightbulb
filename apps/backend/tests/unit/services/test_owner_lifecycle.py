"""The guard over the owner-lifecycle lease.

The lease itself (conditional write, staleness takeover, fail-closed) is covered in
``tests/unit/repositories/test_container_repo_lifecycle_claim.py``. What is pinned here is the
guard's own contract: who waits for whom, and who may re-enter. It runs against moto rather
than a stubbed claim, because "serializes" is only true if the real conditional write is the
thing refusing the second caller.
"""

import asyncio
import os

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.owner_lifecycle import OwnerLifecycleBusyError, owner_lifecycle_guard  # noqa: E402
from core.repositories import container_repo  # noqa: E402


@pytest.fixture(autouse=True)
def dynamodb_table():
    moto_call_lock = asyncio.Lock()
    run_in_thread = container_repo.run_in_thread

    async def atomic_moto_call(func, *args, **kwargs):
        # Moto checks the condition and writes in separate steps; DynamoDB does both atomically.
        async with moto_call_lock:
            return await run_in_thread(func, *args, **kwargs)

    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
            patch.object(container_repo, "run_in_thread", atomic_moto_call),
        ):
            yield


@pytest.mark.asyncio
async def test_child_task_cannot_inherit_parent_owner_lock():
    """A spawned replacement/provision task must wait for its parent to release."""
    child_started = asyncio.Event()
    child_entered = asyncio.Event()

    async def child():
        child_started.set()
        async with owner_lifecycle_guard("org_one"):
            child_entered.set()

    async with owner_lifecycle_guard("org_one"):
        task = asyncio.create_task(child())
        await child_started.wait()
        await asyncio.sleep(0)
        assert not child_entered.is_set()

    await task
    assert child_entered.is_set()


@pytest.mark.asyncio
async def test_same_task_can_reenter_same_owner_lock():
    async with owner_lifecycle_guard("org_one"):
        async with owner_lifecycle_guard("org_one"):
            pass


@pytest.mark.asyncio
async def test_two_callers_for_one_owner_serialize():
    """The whole point: a connect finishing its writes and a disconnect tearing them down must
    not interleave. The second caller enters only once the first has left."""
    order: list[str] = []

    async def caller(name: str, hold: float) -> None:
        async with owner_lifecycle_guard("org_one"):
            order.append(f"{name}:in")
            await asyncio.sleep(hold)
            order.append(f"{name}:out")

    await asyncio.gather(caller("a", 0.05), caller("b", 0))

    assert order in (["a:in", "a:out", "b:in", "b:out"], ["b:in", "b:out", "a:in", "a:out"])


@pytest.mark.asyncio
async def test_different_owners_do_not_block_each_other():
    """One owner's slow credential write must not stall the rest of the fleet -- the lease is
    per-owner, so both sections are open at once."""
    both_in = asyncio.Event()
    entered = 0

    async def caller(owner_id: str) -> None:
        nonlocal entered
        async with owner_lifecycle_guard(owner_id):
            entered += 1
            if entered == 2:
                both_in.set()
            await asyncio.wait_for(both_in.wait(), timeout=2)

    await asyncio.gather(caller("org_one"), caller("org_two"))

    assert both_in.is_set()


@pytest.mark.asyncio
async def test_a_section_that_never_finishes_makes_the_next_caller_busy_rather_than_hanging():
    """Nothing has been written when the wait times out, so the caller answers "busy" and the
    owner retries -- a 500 would look like the write failed halfway, and hanging would hold a
    request worker for as long as the stuck section does.

    The contender is a separate TASK: the guard is re-entrant within one task by design.
    """
    from core.services import owner_lifecycle

    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with owner_lifecycle_guard("org_one"):
            holding.set()
            await release.wait()

    held = asyncio.create_task(holder())
    await holding.wait()
    try:
        with patch.object(owner_lifecycle, "OWNER_LIFECYCLE_TIMEOUT_S", 0.0):
            with pytest.raises(OwnerLifecycleBusyError):
                async with owner_lifecycle_guard("org_one"):
                    pass
    finally:
        release.set()
        await held


@pytest.mark.asyncio
async def test_the_lease_is_released_even_when_the_section_raises():
    async def boom():
        async with owner_lifecycle_guard("org_one"):
            raise RuntimeError("write failed")

    with pytest.raises(RuntimeError):
        await boom()

    async with owner_lifecycle_guard("org_one"):
        pass
