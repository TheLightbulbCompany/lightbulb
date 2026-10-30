"""The apps progress sweeper: a pending preview or publish lands without anyone reading status.

Until 30 Oct 2026 a deployment only advanced inside the builder's own ``up``/``status`` reads; with
the agent down, Demo Lab's first publish sat in "building" for 21 minutes. This lifespan task steps
every app with a pending deployment every ``PASS_S`` seconds. Admission (one instance per pass) is
``container_repo.try_claim_pass``; per-app exclusion is the lease inside ``apps_service._progress``.
Vercel webhooks are not used: account webhooks are Pro/Enterprise only and an app's token lives one hour,
so this poll is the mechanism, and the live event ``_patch`` publishes carries the change to Canvas.
"""

from __future__ import annotations

import asyncio
import logging

from core.dynamodb import plain
from core.repositories import apps_repo, container_repo
from core.services import apps_service

logger = logging.getLogger(__name__)

PASS_S = 30  # one pass per half minute across the fleet
TICK_S = 10  # how often each instance asks for the pass
MAX_APPS = 25  # per pass; a pending deployment needs 1-2 Vercel calls
STEP_S = 55  # one slow provider call must not hold the whole pass


async def _step(app: dict) -> None:
    rows, _ = await apps_service._progress(app)
    await apps_service._record_state(app, apps_service._state(app, rows)[0])


async def sweep_once() -> int:
    """Step every app with a pending deployment once. Returns how many were stepped."""
    stepped = 0
    for raw in await apps_repo.list_pending(limit=MAX_APPS):
        app = plain(raw)
        if app.get("state") == "destroyed":
            continue
        try:
            await asyncio.wait_for(_step(app), STEP_S)
            stepped += 1
        except Exception as exc:  # noqa: BLE001 — one app's provider trouble never stops the pass
            logger.warning("apps sweep: %s failed: %s", app.get("app_id"), type(exc).__name__)
    return stepped


async def run_apps_sweeper(stop_event: asyncio.Event) -> None:
    """The lifespan sweeper, shaped like ``memory_api.run_memory_sweeper``."""
    while not stop_event.is_set():
        try:
            if await container_repo.try_claim_pass("apps_progress", interval_s=PASS_S):
                await sweep_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("apps sweep failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_S)
        except asyncio.TimeoutError:
            pass
