"""A13 — the layers: after A1–A12 every object is a ``tasks`` row, the legacy room-run path wrote nothing, and
the Router only ever recorded decisions (spec 09 §8.3; wave2-README: the wave-2 partial, ``paperclip.call=0``
waits for spec 03's remaining moves).

Three reads, each a module function a test can stand in for:
- ``ctx.tasks_rows``: every task id A2 left in ``ctx.state["taskIds"]`` has its task row, an attempt, a
  subscription and a routed trigger.
- ``legacy_rows``: the dev messaging table has no ``RUN#`` row under ``OWNER#{ws}`` and no ``FOLLOWUP#`` row under
  any room the run used (``roomId`` + the DM rooms) -- the legacy room-run path never ran for this workspace,
  except in an app's conversation room (room meta ``app``, "App · ..."): #4876 builds the app there on the
  legacy room-run path by design, so those rows are reported as ``appRuns`` and do not fail the check.
- ``router_runs``: every ``router_turn`` batch in the ``router:{ws}`` workflow's history, read back from the bridge
  (``runtime_runs.get_run``): its decisions are Router tools only. The bridge records ``recentTools`` for task runs
  only, so for a Router run the decisions ARE its tool log (``lightbulb_router`` tools record decisions and nothing
  else; the run limits enable only that toolset and read-only memory). When a Router run does carry tool names,
  they are checked against the same allowlist.
"""

from __future__ import annotations

import os
from typing import Any

from core import tasks_schema as ts
from scripts.super_agent.acceptance.ctx import Result

#: ``lightbulb_router`` (apps/infra/hermes/tests/test_router_tools.py) plus the read-only memory toolset.
ROUTER_TOOLS = frozenset(
    {"start_task", "pass_through", "reply", "ask", "ignore", "follow_link", "propose_automation"}
    | {"memory", "search", "recall"}
    | {"tool_describe", "tool_search"}  # Hermes's built-in tool discovery: reads the toolset, acts on nothing
)
DEV_TEMPORAL = {"TEMPORAL_ADDRESS": "lightbulb-dev.nv5xx.tmprl.cloud:7233", "TEMPORAL_NAMESPACE": "lightbulb-dev.nv5xx"}


async def is_app_room(room_id: str) -> bool:
    """An app's conversation room (``app_conversation._create``: room meta ``app``)."""
    from core.repositories import messaging_repo

    return bool(((await messaging_repo.get_room(room_id)) or {}).get("app"))


async def legacy_rows(ws: str, room_ids: list[str], since: str | None = None) -> dict[str, list[str]]:
    """``{"runs": [sk...], "appRuns": [sk...], "followUps": [room/sk...]}`` from ``lightbulb-dev-messaging`` (7-day TTL rows). A
    ``RUN#`` row counts when it is in one of ``room_ids`` or was written at/after ``since`` (this run): an older
    row from another room is stale history (gate runs 2-4: one ended row of 08:54Z failed every later run)."""
    from boto3.dynamodb.conditions import Key

    from core.dynamodb import get_table, run_in_thread

    table = get_table("messaging")

    async def query(pk: str, prefix: str) -> list[dict]:
        out: list[dict] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix)}
        while True:
            page = await run_in_thread(table.query, **kwargs)
            out += page.get("Items", [])
            if not page.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def this_run(item: dict) -> bool:
        when = item.get("updatedAt") or item.get("createdAt")
        return item.get("roomId") in room_ids or not since or not when or when >= since

    items = [item for item in await query(f"OWNER#{ws}", "RUN#") if this_run(item)]
    app_rooms = {room for room in {i.get("roomId") for i in items} if room and await is_app_room(room)}
    runs = [i["sk"] for i in items if i.get("roomId") not in app_rooms]
    app_runs = [i["sk"] for i in items if i.get("roomId") in app_rooms]
    follow_ups = [f"{room}/{i['sk']}" for room in room_ids for i in await query(f"ROOM#{room}", "FOLLOWUP#")]
    return {"runs": runs, "appRuns": app_runs, "followUps": follow_ups}


async def dev_temporal_client():
    """The Temporal client on the dev namespace (key from Secrets Manager unless the settings already hold one)."""
    from core.config import settings
    from core.temporal import client as temporal_client
    from scripts.multiparty_e2e.runner import _secret

    for name, value in DEV_TEMPORAL.items():
        if getattr(settings, name) in ("", "localhost:7233", "default"):
            setattr(settings, name, os.environ.get(name, value))
    if not settings.TEMPORAL_API_KEY:
        settings.TEMPORAL_API_KEY = os.environ.get("TEMPORAL_API_KEY") or _secret("lightbulb/dev/temporal_api_key")
    assert settings.TEMPORAL_NAMESPACE.startswith("lightbulb-dev."), "refusing: not the dev Temporal namespace"
    return await temporal_client.connect()


async def router_runs(ws: str, front: str | None = None) -> list[dict[str, Any]]:
    """One entry per ``router_turn`` batch in the Router workflow's history: ``{batchId, lane, at, status,
    decisions, tools}`` (``at``: when the batch was scheduled, UTC ISO). A batch of scripted starts only
    (``ref.start``, an automation fire) has no bridge run by design and is left out. Temporal: the dev namespace, key from Secrets Manager ``lightbulb/dev/temporal_api_key`` unless the
    ``TEMPORAL_*`` settings are already set. ponytail: reads the current run's history only; a Router that has
    continued-as-new hides earlier batches -- read ``first_execution_run_id`` chains if that ever matters."""
    from temporalio.converter import DataConverter

    from core.repositories import tasks_repo
    from core.services import agent_registry, runtime_runs
    from core.temporal.types import RouterTurnIn

    client = await dev_temporal_client()
    handle = client.get_workflow_handle(ts.router_workflow_id(ws))
    batches: list[tuple[str, RouterTurnIn]] = []
    async for event in handle.fetch_history_events():
        if not event.HasField("activity_task_scheduled_event_attributes"):
            continue
        attrs = event.activity_task_scheduled_event_attributes
        if attrs.activity_type.name != "router_turn":
            continue
        try:
            (inp,) = await DataConverter.default.decode(list(attrs.input.payloads), [RouterTurnIn])
        except KeyError:  # a batch recorded by an older worker lacks a field the type now has: skip it
            continue
        batches.append((event.event_time.ToDatetime().strftime("%Y-%m-%dT%H:%M:%S"), inp))
    # The front agent A1 saw, else the registry's CEO (as the Router does under the switch): this script runs with
    # the env master off, where front_agent_id falls back to the Paperclip row (gate run 2: KeyError 'created_at').
    front = front or next(a["id"] for a in await agent_registry.roster(ws) if a.get("role") == "ceo")
    out = []
    for at, inp in batches:
        trigs = [await tasks_repo.get_trigger(ws, t) for t in inp.trigger_ids]
        if trigs and all(isinstance(((t or {}).get("ref") or {}).get("start"), dict) for t in trigs):
            continue  # deterministic start: the Router applied `ref.start` without a model run
        trig = trigs[0] if trigs else None
        identity = runtime_runs.RunIdentity(
            ts.router_run_id(ws, inp.batch_id),
            ws,
            inp.batch_id,
            1,
            front,
            mode="router",
            lane=inp.lane,
            room=ts.router_room_key(ts.trigger_scope(trig)) if trig else None,  # the batch's conversation
        )
        try:
            status = await runtime_runs.get_run(ws, identity)
            entry = {
                "status": status.status,
                "decisions": [d.get("kind") for d in status.decisions],
                "tools": [t.get("name") for t in status.recent_tools if isinstance(t, dict)],
            }
        except Exception as exc:  # noqa: BLE001 -- a run the bridge no longer holds is evidence, not a crash
            entry = {"status": f"unavailable: {type(exc).__name__}: {exc}", "decisions": [], "tools": []}
        out.append({"batchId": inp.batch_id, "lane": inp.lane, "at": at, **entry})
    return out


def check_rows(rows: list[dict], task_ids: list[str], sub_ids: list[str] | None = None) -> dict[str, Any]:
    """Every task A2 made is a tasks row with its attempt, subscription and routed trigger."""
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r.get("type", ""), []).append(r)
    tasks = {t["taskId"]: t for t in by_type.get("task", [])}
    attempts = {a["taskId"] for a in by_type.get("attempt", [])}
    subs = {s["taskId"] for s in by_type.get("subscription", [])}
    routed = {t["triggerId"] for t in by_type.get("trigger", []) if t.get("routedTo")}
    missing = {
        "task": [t for t in task_ids if t not in tasks],
        "attempt": [t for t in task_ids if t not in attempts],
        "subscription": [
            t for t in (sub_ids if sub_ids is not None else task_ids) if t not in subs
        ],  # one owner per thread
        "routedTrigger": [t for t in task_ids if t in tasks and tasks[t].get("triggerId") not in routed],
    }
    return {"missing": missing, "ok": bool(task_ids) and not any(missing.values())}


def check_router(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Router runs completed and called nothing outside the Router toolset."""
    outside = [
        {"batchId": r["batchId"], "names": sorted(set(r["decisions"] + r["tools"]) - ROUTER_TOOLS)}
        for r in runs
        if set(r["decisions"] + r["tools"]) - ROUTER_TOOLS
    ]
    not_completed = [r["batchId"] for r in runs if r["status"] != "completed"]
    return {"outside": outside, "notCompleted": not_completed, "ok": bool(runs) and not outside and not not_completed}


async def a13(ctx) -> Result:
    task_ids = list(ctx.state.get("taskIds") or [])
    rooms = [v for k, v in ctx.state.items() if k == "roomId" or k.endswith("DmRoomId")]
    rows_check = check_rows(
        await ctx.tasks_rows(ctx.ws),
        task_ids,
        [ctx.state["firstStartedTaskId"]] if ctx.state.get("firstStartedTaskId") else None,
    )
    since = getattr(ctx, "started_at", None)  # this run only: older batches and rows are other runs' history
    legacy = await legacy_rows(ctx.ws, rooms, since)
    try:
        runs = [
            r
            for r in await router_runs(ctx.ws, ctx.state.get("frontAgentId"))
            if not since or not r.get("at") or r["at"] >= since
        ]
        router = check_router(runs)
    except Exception as exc:  # noqa: BLE001 -- Temporal or the bridge unreachable is a failed check with its reason
        runs, router = [], {"outside": [], "notCompleted": [], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    checks = {
        "every_object_is_a_tasks_row": rows_check["ok"],
        "no_legacy_run_rows": not legacy["runs"],
        "no_legacy_followup_rows": not legacy["followUps"],
        "router_ran_only_router_tools": router["ok"],
    }
    return Result(
        all(checks.values()),
        {
            "taskIds": task_ids,
            "rooms": rooms,
            "missing": rows_check["missing"],
            "legacy": legacy,
            "routerRuns": runs,
            "routerOutside": router["outside"],
            "routerNotCompleted": router["notCompleted"],
            **({"routerError": router["error"]} if router.get("error") else {}),
            "paperclipCall": "not checked in wave 2 (spec 03 follow-up): partial by design",
            **checks,
        },
    )
