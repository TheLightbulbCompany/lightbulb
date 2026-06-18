"""Poll-until-recovered helpers shared by SP2 FIS scenarios."""

import asyncio
import time
from collections.abc import Awaitable, Callable


async def poll_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout_s: float,
    interval_s: float = 3.0,
) -> float:
    """Return seconds-to-true, or raise TimeoutError. predicate is async."""
    start = time.monotonic()
    deadline = start + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return time.monotonic() - start
        await asyncio.sleep(interval_s)
    raise TimeoutError(f"did not recover within {timeout_s}s")


def wait_experiment_terminal(fis_client, experiment_id: str, *, timeout_s: float = 300.0) -> str:
    """Block until the FIS experiment reaches a terminal state; return that state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = fis_client.get_experiment(id=experiment_id)["experiment"]["state"]["status"]
        if state in ("completed", "stopped", "failed"):
            return state
        time.sleep(5)
    raise TimeoutError(f"experiment {experiment_id} not terminal within {timeout_s}s")


def wait_experiment_running(fis_client, experiment_id: str, *, timeout_s: float = 120.0) -> str:
    """Block until the FIS experiment is actually injecting the fault.

    ``start_experiment`` returns while the experiment is still ``pending`` /
    ``initiating`` — the fault action is NOT yet applied. A during-fault probe
    run immediately after start would sample BEFORE the outage window. So we
    poll ``get_experiment`` until the state advances to ``running`` (the fault
    is live). If the experiment reaches a terminal state first we return that
    too, so the caller can fail loudly instead of hanging on a missed window.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = fis_client.get_experiment(id=experiment_id)["experiment"]["state"]["status"]
        if state == "running":
            return state
        if state in ("completed", "stopped", "failed"):
            return state
        time.sleep(3)
    raise TimeoutError(f"experiment {experiment_id} not running within {timeout_s}s")
