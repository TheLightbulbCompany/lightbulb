"""One Temporal client per process (contracts-v2 §16). The FastAPI lifespan and the worker entrypoint
connect it; callers take it from `get_client()`, which raises instead of hanging when it is not up."""

import asyncio
import logging
from typing import Any

from temporalio.client import Client, WithStartWorkflowOperation, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from core import tasks_schema as ts
from core.config import settings
from core.temporal.types import RouterState, TaskInputRef, TaskStart, TaskStartReason, TriggerRef


logger = logging.getLogger(__name__)


class TemporalUnavailable(RuntimeError):
    """No connected Temporal client in this process (env master off, or the startup connect failed)."""


_client: Client | None = None


async def connect() -> Client:
    kwargs: dict = {"namespace": settings.TEMPORAL_NAMESPACE}
    if settings.TEMPORAL_API_KEY:
        kwargs |= {"api_key": settings.TEMPORAL_API_KEY, "tls": True}
    return await Client.connect(settings.TEMPORAL_ADDRESS, **kwargs)


def set_client(client: Client | None) -> None:
    global _client
    _client = client


def get_client() -> Client:
    from core.services import super_agent_faults  # dev fault hook; None in prod

    if super_agent_faults.fault("temporal_down") is not None:
        raise super_agent_faults.temporal_down_error()
    if _client is None:
        raise TemporalUnavailable("Temporal client is not connected")
    return _client


async def connect_and_set(timeout_s: float = 10) -> None:
    """Startup's background connect: bounded, and a failure only logs (get_client() keeps raising)."""
    try:
        async with asyncio.timeout(timeout_s):
            set_client(await connect())
    except Exception:
        logger.warning("temporal connect failed", exc_info=True)


# --- Helpers every lane uses to reach the Router or a task (wave-2 README; contracts §7, G24) ---------
# Workflows are named by string so callers never import workflow code.


def task_handle(ws: str, task_id: str) -> WorkflowHandle:
    return get_client().get_workflow_handle(ts.task_workflow_id(ws, task_id))


async def signal_router(ref: TriggerRef) -> None:
    """Signal-with-start the workspace's `RouterWorkflow` with one admitted trigger."""
    await get_client().start_workflow(
        "RouterWorkflow",
        RouterState(ws=ref.ws),
        id=ts.router_workflow_id(ref.ws),
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="trigger",
        start_signal_args=[ref],
    )


async def start_task_workflow(ws: str, task_id: str, reason: TaskStartReason) -> None:
    """Start the task's workflow; a running one is kept (a retried start is a no-op); a closed one restarts."""
    await get_client().start_workflow(
        "TaskWorkflow",
        TaskStart(ws, task_id, reason),
        id=ts.task_workflow_id(ws, task_id),
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )


async def signal_task_input(ws: str, task_id: str, ref: TaskInputRef) -> None:
    """Signal-with-start `input`: a task with no running workflow (migrated, or resolved) gets one."""
    await get_client().start_workflow(
        "TaskWorkflow",
        TaskStart(ws, task_id, "migrated"),
        id=ts.task_workflow_id(ws, task_id),
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        start_signal="input",
        start_signal_args=[ref],
    )


async def update_task(ws: str, task_id: str, update_name: str, arg: Any) -> Any:
    """Update-with-start: every action on a task goes through here, so migrated tasks need no special case."""
    op = WithStartWorkflowOperation(
        "TaskWorkflow",
        TaskStart(ws, task_id, "migrated"),
        id=ts.task_workflow_id(ws, task_id),
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    return await get_client().execute_update_with_start_workflow(update_name, arg, start_workflow_operation=op)
