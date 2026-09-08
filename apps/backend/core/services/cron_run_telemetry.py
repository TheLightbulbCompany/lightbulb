"""Recover cron-run telemetry that the live-connection emit paths cannot see.

``agent_run_completed`` is emitted from two places today, and BOTH need a live
socket at the moment the run ends: the gateway reader loop
(``connection_pool._emit_run_completed``, alive only while a human is in that
owner's chat) and the Paperclip operator relay tap
(``operator_proxy._tap_run_completion_telemetry``, alive only while a Paperclip
adapter is connected). A container-native OpenClaw cron job has neither — it
fires inside the container on its own schedule, broadcasts its terminal frame
to nobody, and finishes unrecorded.

The container keeps its own durable record of exactly those runs. ``cron.runs``
(gateway method, namespace ``cron``, scope ``operator.read``, available since
``<=2026.7`` — verified at v2026.8.1-isol8.3 in
``src/gateway/methods/core-descriptors.ts`` and ``src/gateway/server-methods/
cron.ts``) returns the persisted history page
``{entries, total, offset, limit, hasMore, nextOffset}``, where each
``CronRunLogEntry`` carries ``ts`` / ``runId`` / ``sessionKey`` / ``status`` /
``completionStatus`` / ``runAtMs`` / ``durationMs``.

This module reads that history for every RUNNING container once an hour and
replays each recent entry through ``run_analytics.schedule_run_completed`` —
the same function both live paths call, so the event name, the property shape
and the dedup are identical by construction rather than by convention.

DEDUP. Emission is idempotent on ``owner:runId`` at three layers, which is why
this sweep is deliberately STATELESS — no watermark row, no cursor, nothing to
migrate or reconcile:

  1. ``capture_product_event``'s deterministic capture uuid,
     ``uuid5(ns, f"{owner}:{run_id}")`` — ALREADY implemented in
     ``run_analytics``. PostHog ingestion drops exact duplicate uuids, so a
     re-offer across processes, restarts, or the transient x2 backend of a
     rolling deploy collapses to one event. This is the load-bearing layer,
     and it is precisely why the sweep routes through the existing emitter
     instead of growing a second one.
  2. ``run_analytics._seen_run_ids`` — the in-process FIFO that catches a run
     seen by BOTH a live path and this sweep within the same process.
  3. ``_LOOKBACK_S`` — only recently-finished entries are offered, so an idle
     owner's ancient history is never re-walked.

An entry with no ``runId`` (Optional in the schema) is SKIPPED: without it
layer 1 has no key and every re-offer would land as a fresh event.
Under-counting those beats double-counting them.

The CloudWatch ``run.completed`` metric is deliberately NOT fired from here
(``emit_metric=False``). It has no durable dedup, and two backend tasks run
this worker during every rolling deploy. Its meaning stays "runs observed
live"; PostHog is the complete count.
"""

from __future__ import annotations

import logging
import time

from core.containers import get_ecs_manager
from core.gateway.operator_rpc import gateway_rpc_oneshot
from core.observability.metrics import put_metric
from core.repositories import container_repo

logger = logging.getLogger(__name__)

# Only entries finishing inside this window are offered. Kept wider than the
# pass interval so a slow or skipped tick leaves no hole; the overlap costs
# nothing because emission is idempotent (see the module docstring).
_LOOKBACK_S = 2 * 60 * 60

# CronRunsParamsSchema caps `limit` at 200. One page per owner: 200 cron runs
# inside two hours for a single owner is already pathological, and paging
# further would spend real time chasing runs we would rather under-count.
_PAGE_LIMIT = 200

# Per-owner budget. gateway_rpc_oneshot defaults to 10s on each of
# open/recv/rpc, which across the fleet would stall the shared worker loop.
_RPC_TIMEOUT_S = 5.0


async def sweep_cron_runs() -> None:
    """Offer every recently-finished cron run on every RUNNING container.

    Per-owner try/except so one unreachable container never starves the rest.
    Only ``running`` rows are visited — a scaled-to-zero trial box is left
    asleep rather than woken for telemetry. Never raises.

    ponytail: sequential over the fleet under a 5s-per-owner budget. At ~40
    running containers that is a worst case of a few minutes once an hour on
    the shared worker (same shape as the neighbouring `_rollup_pass`). Bound
    it with a semaphore + gather if the fleet outgrows that.
    """
    from core.services import run_analytics  # local: keeps the worker import light

    ecs = get_ecs_manager()
    cutoff_ms = int((time.time() - _LOOKBACK_S) * 1000)
    offered = 0
    for row in await container_repo.get_by_status("running"):
        owner_id = row.get("owner_id")
        if not owner_id:
            continue
        try:
            # Re-resolve the task IP every pass: the row is long-lived but the
            # IP is ephemeral, and a cached dead IP is a known prod outage
            # (see ecs_manager.discover_ip).
            container, ip = await ecs.resolve_running_container(owner_id)
            if container is None or not ip:
                continue
            page = await gateway_rpc_oneshot(
                ip,
                owner_id=owner_id,
                token=container["gateway_token"],
                method="cron.runs",
                # scope "all" is refused for a cron-SCOPED caller; the backend
                # operator device carries no cron caller scope, so it is
                # allowed. "skipped" entries never executed, so they are not
                # runs and are filtered server-side.
                params={"scope": "all", "limit": _PAGE_LIMIT, "statuses": ["ok", "error"]},
                open_timeout=_RPC_TIMEOUT_S,
                recv_timeout=_RPC_TIMEOUT_S,
                rpc_timeout=_RPC_TIMEOUT_S,
            )
            offered += _offer_page(owner_id, page, cutoff_ms, run_analytics)
        except Exception:
            logger.warning("cron-run telemetry sweep failed for owner %s", owner_id, exc_info=True)
    put_metric("run.cron_sweep.offered", value=offered)
    if offered:
        logger.info("cron-run telemetry sweep emitted %d run(s)", offered)


def _offer_page(owner_id: str, page: dict, cutoff_ms: int, run_analytics) -> int:
    """Replay the in-window entries of one page through the shared emitter.

    Returns how many actually produced an emit (the emitter returns None for
    an already-seen run, or when the PostHog write key is unset).
    """
    entries = page.get("entries") if isinstance(page, dict) else None
    offered = 0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts")
        # cron.runs history sorts newest-first by default, so the first
        # out-of-window entry ends the page.
        if not isinstance(ts, (int, float)) or ts < cutoff_ms:
            break
        if not entry.get("runId"):
            # No idempotency key -> no durable dedup. Skip rather than risk a
            # fresh event on every re-offer.
            continue
        task = run_analytics.schedule_run_completed(
            owner_id=owner_id,
            parsed=_parsed_for(entry),
            payload=_payload_for(entry),
            phase="error" if _failed(entry) else "end",
            emit_metric=False,
        )
        if task is not None:
            offered += 1
    return offered


def _failed(entry: dict) -> bool:
    """``status`` is ok|error|skipped; ``completionStatus`` succeeded|failed|unknown."""
    return entry.get("status") == "error" or entry.get("completionStatus") == "failed"


def _parsed_for(entry: dict) -> dict:
    """The ``_parse_session_key`` shape the emitter expects.

    ``source`` is PINNED to "cron" rather than read off the session key: a job
    with ``target: "current"`` reuses the triggering conversation's key, which
    parses as webchat and would misclassify an unattended run as
    human-initiated. The origin is not in doubt here — it came out of
    ``cron.runs``.
    """
    from core.gateway.connection_pool import _parse_session_key  # local: heavy module

    parsed = _parse_session_key(entry.get("sessionKey") or "")
    return {"agent_id": parsed.get("agent_id"), "source": "cron", "cron_id": entry.get("jobId")}


def _payload_for(entry: dict) -> dict:
    """The lifecycle-frame shape the emitter reads: ``runId`` + ``data``.

    ``runAtMs`` + ``durationMs`` reconstruct the same ``duration_ms`` the live
    paths compute from the lifecycle frame's own startedAt/endedAt.
    """
    data: dict = {}
    started, duration = entry.get("runAtMs"), entry.get("durationMs")
    if isinstance(started, (int, float)) and isinstance(duration, (int, float)):
        data["startedAt"] = started
        data["endedAt"] = started + duration
    return {"runId": entry.get("runId"), "data": data}
