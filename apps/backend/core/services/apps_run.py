"""Resume a builder agent's turn that died in the middle of building an app.

A failed room turn is deliberately not retried for anyone else (``post_run_failure_message``), so
until now a dead build turn waited for a person (Demo Lab, 30 Oct 2026). Only an errored turn arms a
wake: a model's refusal that ends as completed prose waits for phase 2's stored request. Events here
only write the sparse ``build`` map and set ``due_at``; only the sweeper (``advance``) wakes. Wake 1
is a system line in the room; later ones are Paperclip issues, because a room wake replays the same
Hermes session and a refusal caused by its history would repeat.

``build``: source_at (epoch s of the last real source change), content (hash of that tree), builder (named at upload), due_at,
armed_at, stopped, resume (armed by a spent allowance), attempts.wake (reset by a changed tree),
day{date, wakes}, wake{n (never reset), via, at, sent, message_id|issue_id}.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, datetime, timedelta

from core.config import settings
from core.dynamodb import plain
from core.repositories import apps_repo, billing_repo, messaging_repo
from core.services import apps_room, apps_service, apps_source
from core.services.chat_gate import budget_block_reason
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner
from core.services.trial_cap import DAILY_CAP, INCLUDED_CAP

logger = logging.getLogger(__name__)

WAKE_DELAYS_S = (60, 300, 1800)  # before wake 1, 2, 3
_DELAY_WORDS = ("a minute", "5 minutes", "30 minutes")
MAX_WAKES = len(WAKE_DELAYS_S)
DAILY_WAKE_CEILING = 6  # per app and UTC day, whatever the plan: a changing tree can reset the count
BUILDING_S = 6 * 3600  # no real source change for this long: the app is not being built
HOLD_S = 2 * 60  # a run is open or the server is deploying: look again
LEASE_S = 90  # a sweeper that crashed mid-wake: the next one resends under the same key
ISSUE_HOLD_S = 2 * 3600  # ponytail: an issue whose run died may stay open; past this it counts as dead
_DAILY = frozenset({DAILY_CAP, INCLUDED_CAP})
_PLACEHOLDER = "agent"  # what routers/apps.py stores for a hook token with no agent header
_OPEN_ISSUE = frozenset({"backlog", "todo", "in_progress", "in_review"})


def _now() -> int:
    return int(time.time())


def _after_midnight(now: int) -> int:
    """After 00:00 UTC, spread so a whole fleet of capped builds does not wake at once."""
    today = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((today + timedelta(days=1)).timestamp()) + random.randint(0, 600)


def _phase(app: dict, rows: dict) -> str:
    """Whose move it is: finished (or closed), server, owner (approvals, or a first preview a person
    looks at), ready (new source on a live app) or open (the agent's)."""
    if app.get("state") == "destroyed" or app.get("destroy_requested_by") or app.get("rolled_back"):
        return "finished"
    if app.get("preview_pending") or app.get("publish_pending"):
        return "server"
    state, error = apps_service._state(app, rows)
    if state == "provisioning":
        return "server"
    if state == "awaiting_approval" and rows:  # no rows: the agent uploaded source and never ran `up`
        return "owner"
    if state == "failed" and (error or {}).get("code") in ("provisioning_failed", apps_service._REFUSAL_CODE):
        return "owner"
    if state == "ready":
        if not app.get("live_version"):
            return "owner"
        # Finished unless a source came after the last release (a rebuilt put-back serves an older source).
        sources = app.get("sources") or {}
        newest = max(sources, key=lambda s: sources[s].get("at") or "", default=None)
        released = max((v.get("at") or "" for v in app.get("versions") or []), default="")
        current = newest == app.get("live_source_id") or (sources.get(newest) or {}).get("at", "") <= released
        return "finished" if current else "ready"
    return "open"


async def _builders_app(owner_id: str, row: dict) -> dict | None:
    """The agent-built app in this turn's room, when the turn is its builder's. No guessing."""
    here = [
        a
        for a in await apps_repo.list_for_owner(owner_id)
        # The room it is built in: a started app's origin room hears its builder on other work too.
        if row.get("roomId") == apps_room.room_of(a)
        and a.get("actor_kind") == "agent"
        and a.get("state") != "destroyed"
    ]
    if not here:
        return None
    latest = max(here, key=lambda a: a.get("updated_at") or "")
    app = plain(await apps_repo.get(latest["app_id"], consistent_read=True) or {})
    if not app:
        return None
    builder = await apps_room._builder(app, row["roomId"], guess=False)
    return app if builder and builder["id"] == row.get("agentId") else None


def _wakes_today(build: dict, now: int) -> int:
    day = build.get("day") or {}
    return int(day.get("wakes") or 0) if day.get("date") == _date(now) else 0


def _date(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


async def _write(app: dict, **fields) -> dict | None:
    """``None``: a sweeper's wake or a new source landed since this copy was read, and that stands."""
    build = app.get("build") or {}
    return await apps_repo.patch(
        app["app_id"],
        {"build": {**build, **fields}},
        expect={"build.wake.n": (build.get("wake") or {}).get("n"), "build.source_at": build.get("source_at")},
    )


async def record_upload(app: dict, actor_id: str, actor_kind: str, *, files: dict[str, bytes]) -> None:
    """A source upload: name the builder (a real agent header, else the only agent with an open run in
    the room right now), and a changed tree restarts the count. The same tree restarts nothing, whatever
    its archive's bytes.
    Compare-and-set, so it cannot overwrite a sweeper's claim. Never raises: the upload has landed."""
    if not settings.APPS_BUILD_WAKE or app.get("actor_kind") != "agent":
        return
    try:
        content = await asyncio.to_thread(apps_source.content_hash, files)
        named = None
        if actor_kind == "agent":
            named = actor_id
            if actor_id == _PLACEHOLDER:
                running = await messaging_repo.list_open_runs_for_room(app["owner_id"], apps_room.room_of(app))
                agents = {r.get("agentId") for r in running}
                named = agents.pop() if len(agents) == 1 else None
        for _ in range(3):
            fresh = plain(await apps_repo.get(app["app_id"], consistent_read=True) or {})
            build = fresh.get("build") or {}
            builder = named or build.get("builder")
            is_new = content != build.get("content")
            if not is_new and builder == build.get("builder"):
                return
            new = {**build, "builder": builder}
            if is_new:
                new.update(
                    content=content, source_at=_now(), due_at=None, attempts={"wake": 0}, stopped=False, resume=False
                )
                if build.get("wake"):  # a claim in flight is over: the next wake takes the next key
                    new["wake"] = {**build["wake"], "sent": True}
            won = await apps_repo.patch(
                app["app_id"],
                {"build": new},
                expect={
                    "build.wake.n": (build.get("wake") or {}).get("n"),
                    "build.due_at": build.get("due_at"),
                    "build.source_at": build.get("source_at"),
                },
            )
            if won is not None:
                return
    except Exception:  # noqa: BLE001
        logger.warning("apps build: upload not recorded for %s", app.get("app_id"), exc_info=True)


async def on_turn_failed(owner_id: str, row: dict, *, budget_reason: str | None, agent_name: str) -> str | None:
    """Schedule the resume of a builder's errored turn on an app really being built. Returns the room
    line to show instead of today's failure note, or ``None`` when this is any other turn. Never raises."""
    if not settings.APPS_BUILD_WAKE or row.get("abortRequestedAt") or row.get("eventKind") == "room_event":
        return None
    try:
        now = _now()
        app = await _builders_app(owner_id, row)
        if app is None:
            return None
        build = app.get("build") or {}
        if now - int(build.get("source_at") or 0) > BUILDING_S or build.get("stopped"):
            return None
        if _phase(app, await apps_service._rows(app)) in ("finished", "owner"):
            return None
        if _wakes_today(build, now) >= DAILY_WAKE_CEILING:
            return None
        attempts = int((build.get("attempts") or {}).get("wake") or 0)
        if attempts >= MAX_WAKES:  # the sweeper posts the one stuck line
            await _write(app, due_at=now, armed_at=now)
            return None
        if budget_reason in _DAILY:
            due = _after_midnight(now)
            if await _write(app, due_at=due, armed_at=due, resume=True) is None:
                return None
            return f"{apps_service._display(app)} continues tomorrow; today's allowance is used."
        if budget_reason:
            return None
        if await _write(app, due_at=now + WAKE_DELAYS_S[attempts], armed_at=now) is None:
            return None  # today's note: this schedule did not land
        return f"{agent_name} lost its place on {apps_service._display(app)}. Trying again in {_DELAY_WORDS[attempts]}."
    except Exception:  # noqa: BLE001 — the failure note must still post
        logger.warning("apps build: failed turn not scheduled owner=%s", owner_id, exc_info=True)
        return None


async def on_run_ended(owner_id: str, row: dict) -> None:
    """The builder's turn ended without an error, or a person pressed Stop: nothing is left to retry.
    A completed turn never arms a wake. Never raises."""
    outcome = row.get("outcome")
    if not settings.APPS_BUILD_WAKE or outcome not in ("completed", "aborted"):
        return
    if outcome == "completed" and row.get("eventKind") == "room_event":
        return
    try:
        app = await _builders_app(owner_id, row)
        if app and (app.get("build") or {}).get("due_at") is not None:
            await _write(app, due_at=None)
    except Exception:  # noqa: BLE001 — the run is already closed
        logger.warning("apps build: run end not recorded owner=%s", owner_id, exc_info=True)


_post = apps_room.post_system_line


async def _wake_room(app: dict, builder: dict, n: int) -> dict:
    name = builder.get("displayName") or "Agent"
    message = await _post(
        app,
        f"apps-build:{app['app_id']}:{n}",
        f"{name}, your last turn on {apps_service._display(app)} stopped before it finished. Continue from "
        "where it stopped and post here when it's done. If you are waiting on a person, say what you need.",
        [{"agentId": builder["id"], "eventKind": "user_request"}],
    )
    if message is None:
        raise RuntimeError("room gone")
    return {"message_id": message["messageId"]}


async def _wake_issue(app: dict, builder: dict, n: int) -> dict:
    """A Paperclip issue runs in its own fresh session. The key replays a retry's issue (see ``ideas``)."""
    admin = shared_admin_client()
    display = apps_service._display(app)
    issue = await admin.create_issue(
        session_cookie=await get_admin_session_cookie(admin._http),
        company_id=await resolve_company_id_for_owner(app["owner_id"]),
        body={
            "title": f"Continue building {display}",
            "description": (
                f"Your earlier turns on the app `{app['name']}` stopped before it finished, and the room "
                "turn could not resume it. Run `lightbulb app status --json`, continue from where it "
                "stopped, and post in the room when it's done or if you need a person.\n\n"
                f"## From\nThe app's room: room `{apps_room.room_of(app)}`."
            ),
            "status": "todo",
            "assigneeAgentId": builder["id"],
            "idempotencyKey": f"apps-build:{app['app_id']}:{n}",
            "allowDuplicate": True,
        },
    )
    return {"issue_id": issue["id"]}


async def _issue_status(wake: dict, now: int) -> str | None:
    """``open``, ``done``, ``cancelled`` or ``None`` (no issue, or its run died) for the last wake's issue."""
    if wake.get("via") != "issue" or not wake.get("issue_id"):
        return None
    held = now - int(wake.get("at") or 0) <= ISSUE_HOLD_S
    try:
        admin = shared_admin_client()
        issue = await admin.get_issue(
            session_cookie=await get_admin_session_cookie(admin._http), issue_id=wake["issue_id"]
        )
    except Exception:  # noqa: BLE001 — Paperclip not answering reads as open while the hold lasts
        logger.info("apps build: issue status unavailable", exc_info=True)
        return "open" if held else None
    status = issue.get("status")
    if status in ("done", "cancelled"):
        return status
    return "open" if status in _OPEN_ISSUE and held else None


async def _hold(app: dict, build: dict, now: int) -> None:
    gone = now - int(build.get("armed_at") or 0) > BUILDING_S
    await _write(app, due_at=None if gone else now + HOLD_S)


async def _stuck(app: dict, build: dict, builder: dict | None) -> None:
    """One line, then nothing until a new source."""
    name = (builder or {}).get("displayName") or "an agent"
    n = (build.get("wake") or {}).get("n") or 0
    await _post(
        app,
        f"apps-build-stuck:{app['app_id']}:{n}",
        f"{apps_service._display(app)} needs a person. Try again in Canvas or ask {name}.",
    )
    await _write(app, due_at=None, stopped=True)


async def advance(app: dict) -> None:
    """One due build, from a fresh read: wait, settle, give up or wake. At most one message per wake:
    the compare-and-set on ``wake.n`` and ``due_at`` admits one sweeper, and the receipt key absorbs a resend."""
    now = _now()
    app = plain(await apps_repo.get(app["app_id"], consistent_read=True) or {})
    build = app.get("build") or {}
    due = build.get("due_at")
    if due is None or due > now:
        return
    if now - due > BUILDING_S:  # left over from a time the flag was off: not a fresh death
        await _write(app, due_at=None)
        return
    phase = _phase(app, await apps_service._rows(app))
    if phase in ("finished", "owner"):
        await _write(app, due_at=None)
        return
    room_id = apps_room.room_of(app)
    room = await messaging_repo.get_room(room_id) if room_id else None
    if not room or room.get("archivedAt") or room.get("deletedAt"):
        await _write(app, due_at=None)
        return
    wake = build.get("wake") or {}
    # Open runs in its conversation and its room: an older app opened mid-build still builds in its room.
    if phase == "server" or any(
        [await messaging_repo.list_open_runs_for_room(app["owner_id"], r) for r in apps_service._rooms(app)]
    ):
        await _hold(app, build, now)
        return
    issue = await _issue_status(wake, now)
    if issue == "open":
        await _hold(app, build, now)
        return
    if issue == "done":  # the fresh session finished without an error
        await _write(app, due_at=None)
        return
    if issue == "cancelled":  # a person said no: no more wakes until a new source
        await _write(app, due_at=None, stopped=True)
        return
    builder = await apps_room._builder(app, room_id, guess=False)
    attempts = int((build.get("attempts") or {}).get("wake") or 0)
    today = _wakes_today(build, now)
    unsent = bool(wake.get("n")) and not wake.get("sent")  # a sweeper died mid-wake: same key, same attempt
    if builder is None or (not unsent and (attempts >= MAX_WAKES or today >= DAILY_WAKE_CEILING)):
        await _stuck(app, build, builder)
        return
    reason = await budget_block_reason(app["owner_id"], await billing_repo.get_by_owner_id(app["owner_id"]))
    if reason in _DAILY:
        if now - int(build.get("source_at") or 0) > BUILDING_S:  # not one line a day while the owner stays capped
            await _write(app, due_at=None)
            return
        await _post(
            app,
            f"apps-build-budget:{app['app_id']}:{_date(now)}",
            f"{apps_service._display(app)} continues tomorrow; today's allowance is used.",
        )
        resume_at = _after_midnight(now)
        await _write(app, due_at=resume_at, armed_at=resume_at, resume=True)
        return
    if reason:
        await _write(app, due_at=None)
        return
    if unsent:
        n, via, at = int(wake["n"]), wake["via"], wake["at"]
    else:
        n, at = int(wake.get("n") or 0) + 1, now
        via = "room" if attempts == 0 else "issue"
        attempts += 1
        # One wake after a spent allowance: a second one would spend the new day's on a failing turn.
        today = DAILY_WAKE_CEILING if build.get("resume") else today + 1
    claim = {
        **build,
        "due_at": now + LEASE_S,
        "attempts": {"wake": attempts},
        "day": {"date": _date(now), "wakes": today},
        "resume": False,
        "wake": {"n": n, "via": via, "at": at},
    }
    won = await apps_repo.patch(
        app["app_id"],
        {"build": claim},
        expect={"build.wake.n": wake.get("n"), "build.due_at": due, "build.source_at": build.get("source_at")},
    )
    if won is None:
        return
    send = _wake_room if via == "room" else _wake_issue
    try:
        ref = await send(app, builder, n)
    except Exception as exc:  # noqa: BLE001 — the attempt is spent; the cap bounds a wake that cannot land
        logger.warning("apps build: wake %s of %s failed: %s", n, app["app_id"], type(exc).__name__)
        ref = {"failed": True}
    next_in = WAKE_DELAYS_S[attempts] if attempts < MAX_WAKES else WAKE_DELAYS_S[-1]
    await _write(
        {**app, "build": claim},
        due_at=now + next_in,
        wake={"n": n, "via": via, "at": at, "sent": True, **ref},
    )
