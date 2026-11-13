"""A shared Claude Code or Codex session's browser on its Mac (PRs #4046, #4051, #4053).

The Mac app identifies a CDP connection on its loopback port as one shared session and offers it
(`browser.cdp.offer`, node_broker). This runs on the task holding that Mac's socket. The session
is a run, `session-<sessionId>`, of its own person: it takes a `mac_browser` lease (first owner
wins; the first lease in loads the workspace sign-ins, the last one out saves them), the runtime
runs those sync steps over the ticketed Mac door (`browser.state` with a door), and then
the Mac opens the channel on its own agent Chrome for the session's client. The channel stays open
for the client's whole connection. While active the lease is renewed; 300 s after the last command
the Mac says `idle` and the lease is released (saving); the next command is held on the Mac while
`active` re-acquires it (loading), then `cdp_leased` lets it through. The session ending, or its
socket closing, releases. When the Mac's Browser is off, or the session chose the cloud, the channel
is relayed instead: a `use: "session"` door (mac_cdp) joins it to the runtime, which pumps it into
the workspace's cloud browser (`browser.attach`). Never log a ticket, a URL or a frame: owner ids and
exception class names only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
import weakref

from botocore.exceptions import ClientError

from core.config import settings
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import (
    container_rpc,
    live_events,
    local_sessions,
    mac_browser,
    mac_cdp,
    mac_live,
    session_screen,
)
from core.services.desktop_execution import DesktopError
from core.services.messaging import ApiProblem, make_live_event
from core.services.node_broker import CDP_COMMAND, CdpClosed, CommandNotAllowed, NodeCapacity

logger = logging.getLogger(__name__)

RUN_PREFIX = "session-"
AGENT_ID = "session"
NOT_SHARED = "Share this session to use the Lightbulb browser"
PLACES = ("mac", "cloud")
PLACE_ATTEMPTS = 3
PLACE_NOTE = "Applies to the session's next browser connection. To move an open one now, restart the lightbulb-browser MCP server (/mcp in Claude Code)."
CLOUD_MESSAGE = "The workspace's cloud browser isn't available right now. Try again in a minute."
CLOUD_BUSY = "This session already has a cloud browser connection; close it first."
MESSAGES = {"not_shared": NOT_SHARED, "cloud_unavailable": CLOUD_MESSAGE, "cloud_busy": CLOUD_BUSY}
# The Mac waits 150 s for the answer: the last attempt starts before this and an invoke can take
# 90 s (container_rpc's floor), so the worst case is 55 + 90 = 145 s, inside it with 5 s to spare.
OFFER_BUDGET_S = 55
DOOR_WAIT_S = 15  # the runtime dials the door right after "attached"
POLL_S = 2
RENEW_SECONDS = 120  # < mac_browser.LEASE_SECONDS
OPEN_SECONDS = 2 * RENEW_SECONDS
# A cloud session holds the bridge's lease this long after the Mac's `idle`: renewing before the lease
# lapses keeps the cloud generation (a lapsed one re-claims, which revokes the relay's connection). An
# idle AWS browser bills only its CPU; past this the lease lapses and the browser's idle stop applies.
CLOUD_IDLE_HOLD_S = 30 * 60
SYNC_TIMEOUT_S = 150  # the bridge caps one Mac sync at 120 s (MAC_SYNC_BUDGET_S)
_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_clock = time.time
_tasks: set[asyncio.Task] = set()
# One sign-in sync at a time per session upstream (two channels of one session, or a closing save
# racing the next load). A lock lives while someone holds or waits on it.
_sync_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_relays: dict[tuple[str, str], object] = {}  # (owner, run) -> the offered channel its door will relay
# (owner, run) -> its one cloud channel, from the offer to its close. The bridge runs a session as ONE
# run, so the cloud path takes one CDP connection per session; a second is refused, never overwritten.
_cloud: dict[tuple[str, str], object] = {}


def run_id(session_id: str) -> str:
    return RUN_PREFIX + session_id


def session_of(run: object) -> str | None:
    """The session id of a `session-<id>` run, else None."""
    sid = run[len(RUN_PREFIX) :] if isinstance(run, str) and run.startswith(RUN_PREFIX) else ""
    return sid if _SID.fullmatch(sid) else None


def relay(claims: dict):
    """The door's half (mac_cdp, `use: "session"`): the channel this ticket's session offered, keyed by
    its own workspace and run, so another workspace's ticket never finds it. Single claim: the first
    door takes it, so two doors can never join one channel."""
    return _relays.pop((claims["ownerId"], claims["runId"]), None)


def auth_for(owner: str, member: str, session_id: str) -> dict:
    """What `desktop_execution.authorize_run` answers for this session's run."""
    return {
        "ownerId": owner,
        "memberId": member,
        "runId": run_id(session_id),
        "agentId": AGENT_ID,
        "family": "session",
        "roomId": "",
    }


def live_shared(row: dict | None) -> bool:
    """The session is still shared and running: its browser may be shown and driven."""
    return bool(row) and not row.get("unshared") and row.get("endedAt") is None and row.get("removedAt") is None


def _err(code: str, status: int = 409) -> DesktopError:
    return DesktopError(code, status, MESSAGES.get(code) or mac_browser.MESSAGES.get(code, mac_browser.GENERIC_MESSAGE))


async def shared_row(owner: str, user: str, device: str, session_id: str) -> dict:
    """The session's row while it is this Mac's, its person's, running and shared; else not_shared."""
    row = await repo.get_session(owner, session_id)
    if (
        row is None
        or row.get("humanUserId") != user
        or row.get("deviceId") != device
        or row.get("unshared")
        or row.get("endedAt") is not None
        or row.get("removedAt") is not None
    ):
        raise _err("not_shared", 403)
    try:
        await local_sessions._enrolled(owner, user, device)  # sharing still on for this Mac
    except ApiProblem:
        raise _err("not_shared", 403) from None
    return row


async def set_place(owner: str, session_id: str, where: str) -> dict:
    """`use_browser`: where this session's NEXT browser connection goes; an open one stays where it is.
    Kept on the row (it survives re-registration) for the session's life."""
    if where not in PLACES:
        raise ApiProblem("INVALID_REQUEST", "where must be mac or cloud.")
    # Fenced by the row's `revision` (as local_sessions_repo.admit_command is), so a registration that read
    # the old place and then puts the whole META row cannot silently revert this write.
    for _ in range(PLACE_ATTEMPTS):
        row = await repo.get_session(owner, session_id)
        if not live_shared(row):
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
        try:
            await store.run_in_thread(
                store._table().update_item,
                Key={"pk": repo.session_pk(owner, session_id), "sk": "META"},
                UpdateExpression="SET browserPlace = :w, revision = :new",
                ConditionExpression="revision = :rev",
                ExpressionAttributeValues={":w": where, ":new": str(uuid.uuid4()), ":rev": row["revision"]},
            )
            return {"sessionId": session_id, "browser": where, "note": PLACE_NOTE}
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "The session changed while switching; try again.", retryable=True)


async def place_by_native(owner: str, user: str, native_id: str, where: str) -> dict:
    """`use_browser` from the engine's `lightbulb` MCP: the person's own shared, running session whose
    native conversation (a Claude session id, a Codex thread id) is the calling one."""
    ids = [row["sessionId"] for row in await repo.list_user_sessions(owner, user)]
    # ponytail: reads every session of the person; a nativeConversationId index if anyone has thousands
    rows = await repo.get_sessions(owner, ids) if ids else []
    match = next((r for r in rows if r and r.get("nativeConversationId") == native_id and live_shared(r)), None)
    if match is None:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", NOT_SHARED)
    try:
        await local_sessions._enrolled(owner, user, match["deviceId"])  # sharing still on for that Mac
    except ApiProblem:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", NOT_SHARED) from None
    return await set_place(owner, match["sessionId"], where)


async def _mark_open(owner: str, human: str, session_id: str, where: str | None, *, announce: bool) -> None:
    """`browserOpen` on the session row (local_sessions.browser_open): the Browser tab shows while it is live."""
    key = {"pk": repo.session_pk(owner, session_id), "sk": "META"}
    if where:
        update = {
            "UpdateExpression": "SET browserOpen = :b",
            "ExpressionAttributeValues": {":b": {"where": where, "until": int(_clock()) + OPEN_SECONDS}},
        }
    else:
        update = {"UpdateExpression": "REMOVE browserOpen"}
    try:
        await store.run_in_thread(
            store._table().update_item, Key=key, ConditionExpression="attribute_exists(pk)", **update
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("session browser mark failed error=%s", type(exc).__name__)
        return
    except Exception as exc:  # noqa: BLE001 - the mark is UI-only; it never closes a working connection
        logger.warning("session browser mark failed error=%s", type(exc).__name__)
        return
    if announce:
        from core.services import local_session_relay  # it imports local_sessions

        await local_session_relay.publish_resources_changed(owner, human)


async def _sync(auth: dict, device: str, answer: dict) -> None:
    """Run a claimed sign-in sync in the runtime over the door `answer` carries, then report it.
    Never raises: a failed sync is reported failed (mac_browser defers a load, keeps a save's base)."""
    sync = answer["sync"]
    upstream = f"mac:{device}:{auth['runId']}"
    ok = False
    lock = _sync_locks.setdefault(upstream, asyncio.Lock())
    async with lock:
        if answer.get("cdpUrl"):
            try:
                result = await container_rpc.call(
                    auth["ownerId"],
                    "browser.state",
                    {
                        "doorUrl": answer["cdpUrl"],
                        "generation": answer["generation"],
                        "upstream": upstream,
                        "steps": sync["steps"],
                    },
                    timeout_s=SYNC_TIMEOUT_S,
                )
                ok = isinstance(result, dict) and result.get("ok") is True
            except Exception as exc:  # noqa: BLE001 -- the class only
                logger.warning("session browser sync failed owner=%s error=%s", auth["ownerId"], type(exc).__name__)
        with contextlib.suppress(Exception):
            await mac_browser.synced(auth, device, str(sync["syncId"]), ok)


async def _acquire(auth: dict, device: str, deadline: float) -> dict:
    """mac_browser.acquire until it stops answering STARTING (a cold Chrome, another bridge's sync)."""
    while True:
        budget = max(1.0, min(mac_browser.ACQUIRE_BUDGET_S, deadline - _clock()))
        answer = await mac_browser.acquire(auth, device, budget_s=budget)
        if answer != mac_browser.STARTING:
            return answer
        if _clock() + POLL_S >= deadline:
            raise _err("starting")
        await asyncio.sleep(POLL_S)


async def _release(auth: dict, device: str, generation: str) -> None:
    """Drop the lease; the last one out saves the Mac's sign-ins. Never raises."""
    try:
        answer = await mac_browser.release(auth, device, generation)
        if answer.get("sync"):
            await _sync(auth, device, answer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session browser release failed owner=%s error=%s", auth["ownerId"], type(exc).__name__)


async def _signal(channel) -> str | None:
    """The channel's next signal, "closed" once it is closed (a full queue cannot lose that), or
    None after RENEW_SECONDS."""
    get = asyncio.ensure_future(channel.signals.get())
    closed = asyncio.ensure_future(channel.closed.wait())
    try:
        await asyncio.wait({get, closed}, timeout=RENEW_SECONDS, return_when=asyncio.FIRST_COMPLETED)
    finally:
        closed.cancel()
        if not get.done():
            get.cancel()
    if channel.closed.is_set():
        return "closed"
    return get.result() if get.done() and not get.cancelled() else None


async def _renew(auth: dict, device: str, leased: dict) -> None:
    """The timer's renewal. A refusal or STARTING is logged and the connection stays. A refusal
    counts the lease as gone (it lapses on its own), so the next `active` re-acquires it with its sync."""
    try:
        answer = await mac_browser.acquire(auth, device)
    except DesktopError as exc:
        logger.warning("session browser renew refused owner=%s code=%s", auth["ownerId"], exc.code)
        leased["generation"] = None
        return
    if answer == mac_browser.STARTING:
        logger.info("session browser renew starting owner=%s", auth["ownerId"])
        return
    leased["generation"] = answer["generation"]
    if answer.get("sync"):
        await _sync(auth, device, answer)


async def _lease_loop(auth: dict, device: str, node, channel, leased: dict) -> None:
    """While the Mac has the channel open (the whole client connection). Each signal, and every
    RENEW_SECONDS, re-checks that the session is shared (DesktopError ends the connection) and renews
    the `browserOpen` mark. Leased: renew the lease, running any sync it hands back. `idle` (the Mac's
    300 s with no command): release it, which saves. `active` (the Mac holds a command): re-acquire
    with its sync (a `[save, load]`, so the Mac's own changes are saved before the load),
    then answer `cdp_leased`; a refusal answers its code and the connection stays, idle.
    `leased["generation"]` is the lease held now (None: released)."""
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    while True:
        signal = await _signal(channel)
        if signal == "closed":
            return
        await shared_row(owner, user, device, sid)
        if signal == "idle" and leased["generation"]:
            generation, leased["generation"] = leased["generation"], None
            await _release(auth, device, generation)
        elif signal == "active" and leased["generation"]:
            node.cdp_leased(channel, True)  # the lease never went: nothing to wait for
        elif signal == "active":
            try:
                answer = await _acquire(auth, device, _clock() + OFFER_BUDGET_S)
                if answer.get("sync"):
                    await _sync(auth, device, answer)
            except DesktopError as exc:
                exc = mac_browser._with_message(exc)
                node.cdp_leased(channel, False, exc.code, exc.message)
            else:
                leased["generation"] = answer["generation"]
                node.cdp_leased(channel, True)
        elif signal is None and leased["generation"]:
            await _renew(auth, device, leased)
        await _mark_open(owner, user, sid, "mac", announce=False)


async def _hold_mac(auth: dict, device: str, node, channel, leased: dict) -> None:
    """While the Mac has the channel open (the whole client connection): frames while watched and
    watcher input, idle or not (the tab stays); and the lease loop."""
    owner, sid = auth["ownerId"], session_of(auth["runId"])
    side = (
        [asyncio.create_task(_watch_mac(owner, sid, node, channel)), asyncio.create_task(_publish(owner, sid, channel))]
        if node.can_cast
        else []
    )
    try:
        with mac_live.live(owner, auth["runId"], node, channel):
            await _lease_loop(auth, device, node, channel, leased)
    finally:
        for task in side:
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def may_type(owner: str, user: str, run: str) -> bool:
    """Re-checked where the Mac's socket is (mac_live.input): this person may still type into it."""
    sid = session_of(run)
    row = await repo.get_session(owner, sid) if sid else None
    if not live_shared(row) or not local_sessions.may_control(row, user):
        return False
    try:
        await session_screen._still_a_member(row, user)
    except ApiProblem:
        return False
    return True


async def _watch_mac(owner: str, session_id: str, node, channel) -> None:
    """The thumb-tier cast while someone has the Browser tab open, re-set every SET_EVERY_S so the
    Mac's 15 s TTL keeps it alive; off otherwise (mac_live's rhythm). Never the view tier: a 480 px
    frame fits the session frames budget."""
    sent, sent_at = False, 0.0
    while True:
        try:
            row = await repo.get_session(owner, session_id)
        except Exception as exc:  # noqa: BLE001 -- a DynamoDB blip: keep the last state
            logger.warning("session browser watch read failed owner=%s error=%s", owner, type(exc).__name__)
        else:
            now = _clock()
            want = live_shared(row) and now - float(row.get("browserWatchedAt") or 0) < mac_live.WANTED_S
            if want != sent or (want and now - sent_at >= mac_live.SET_EVERY_S):
                try:
                    node.cast_set(channel, False, want)
                    sent, sent_at = want, now
                except NodeCapacity:
                    pass  # the node socket is backed up: next tick
                except (CdpClosed, CommandNotAllowed):
                    return
        await asyncio.sleep(mac_live.FRAME_POLL_S)


async def _publish(owner: str, session_id: str, channel) -> None:
    """Each cast frame to the session's frames channel, `workspace.session.screen` kind "image",
    within the session frames budget (its own key). Stored nowhere."""
    publisher = live_events.get_live_event_publisher()
    workspace = None
    while True:
        await channel.cast_ready.wait()
        channel.cast_ready.clear()  # clear, THEN pop (node_broker's consumer contract)
        frame = channel.cast.pop("thumb", None)
        channel.cast.pop("view", None)
        if frame is None or not session_screen._spend(
            f"{session_id}#browser", len(frame["jpegBase64"]) * 3 // 4, _clock()
        ):
            continue
        payload = {
            "sessionId": session_id,
            "kind": "image",
            "data": frame["jpegBase64"],
            "w": frame["w"],
            "h": frame["h"],
            "seq": frame["seq"],
        }
        try:
            # right before the publish: a thumb pending across an unshare/end/remove is dropped
            if not live_shared(await repo.get_session(owner, session_id)):
                continue
            workspace = workspace or await store.ensure_workspace_id(owner)
            await publisher.publish_session_frames(workspace, make_live_event("workspace.session.screen", payload))
        except Exception as exc:  # noqa: BLE001 -- skip it; the next frame follows
            logger.info("session browser frame dropped owner=%s error=%s", owner, type(exc).__name__)


async def _serve_mac(auth: dict, device: str, node, channel, leased: dict) -> None:
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    answer = await _acquire(auth, device, _clock() + OFFER_BUDGET_S)
    leased["generation"] = answer["generation"]
    if answer.get("sync"):
        await _sync(auth, device, answer)
    channel.local = True  # set before the open: none of its CDP frames is ours
    await node.cdp_accept(channel, {"profileId": answer["profileId"]})
    await _mark_open(owner, user, sid, "mac", announce=True)
    try:
        await _hold_mac(auth, device, node, channel, leased)
    finally:
        await _mark_open(owner, user, sid, None, announce=True)


async def _native_target(auth: dict, device: str, node) -> dict:
    """The Mac's live socket here, with no browser grant needed: a relay drives nothing on the Mac."""
    from core.services import connection_service

    try:
        return await connection_service.resolve_desktop_target(
            auth["ownerId"], auth["memberId"], device, node.generation, None
        )
    except connection_service.DesktopTargetError:
        raise _err("cloud_unavailable") from None


async def _attach_until(auth: dict, params) -> bool:
    """`browser.attach` until it answers "attached" (True), polling while the cloud browser starts
    (`params()` is called per attempt), within OFFER_BUDGET_S; False on any other answer or error."""
    deadline = _clock() + OFFER_BUDGET_S
    while True:
        try:
            answer = await container_rpc.call(auth["ownerId"], "browser.attach", params())
        except Exception as exc:  # noqa: BLE001 -- the runtime did not answer; the class only
            logger.warning("session browser attach failed owner=%s error=%s", auth["ownerId"], type(exc).__name__)
            return False
        status = answer.get("status") if isinstance(answer, dict) else None
        if status == "attached":
            return True
        if status != "starting" or _clock() + POLL_S >= deadline:
            return False
        await asyncio.sleep(POLL_S)


async def _attach(auth: dict, target: dict) -> None:
    """Hand the runtime a door to the session's channel: a fresh single-use ticket per attempt (one
    lives 60 s). The runtime is woken first (`health`): a cold one answers its first invoke after
    ~55 s and its bridge refreshes for up to 20 s more, so a ticket minted before the wake would be
    stale when the door is dialed. cloud_unavailable if the runtime or the cloud browser does not take it."""
    try:
        await container_rpc.call(auth["ownerId"], "health", {})
    except Exception as exc:  # noqa: BLE001 -- the class only
        logger.warning("session browser wake failed owner=%s error=%s", auth["ownerId"], type(exc).__name__)
        raise _err("cloud_unavailable") from None
    workspace = await store.ensure_workspace_id(auth["ownerId"])

    def params() -> dict:
        ticket = mac_cdp.mint(target=target, profile_id="cloud", auth=auth, use="session")
        return {
            "runId": auth["runId"],
            "doorUrl": mac_cdp.door_url(target, ticket),
            "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
            "frameChannel": f"/ws/{workspace}/session-frames/{session_of(auth['runId'])}",
        }

    if not await _attach_until(auth, params):
        raise _err("cloud_unavailable")


async def _stop_cast(owner: str, run: str) -> None:
    """Stop the runtime's cast of the session's tab now (`browser.frames` with ttl 0): its frames go
    straight from the runtime to AppSync, so nothing here can drop one. Never raises."""
    try:
        await container_rpc.call(owner, "browser.frames", {"runId": run, "ttl": 0})
    except Exception as exc:  # noqa: BLE001 -- the class only; the cast lapses by its TTL anyway
        logger.info("session browser stop-cast failed owner=%s error=%s", owner, type(exc).__name__)


async def _watch_cloud(owner: str, session_id: str, run: str) -> None:
    """While someone has the Browser tab open, ask the runtime to cast the session's tab every
    SET_EVERY_S (`browser.frames` with its run); the cast stops by itself after. Once the session
    is not shared (unshared, ended, removed) the cast is stopped at once and so is this."""
    while True:
        try:
            row = await repo.get_session(owner, session_id)
        except Exception as exc:  # noqa: BLE001 -- a DynamoDB blip: the next beat tries again
            logger.info("session browser watch read failed owner=%s error=%s", owner, type(exc).__name__)
        else:
            if not live_shared(row):
                await _stop_cast(owner, run)
                return
            if _clock() - float(row.get("browserWatchedAt") or 0) < mac_live.WANTED_S:
                try:
                    await container_rpc.call(owner, "browser.frames", {"runId": run, "ttl": mac_live.WANTED_S})
                except Exception as exc:  # noqa: BLE001 -- the next beat tries again
                    logger.info("session browser frames ask failed owner=%s error=%s", owner, type(exc).__name__)
        await asyncio.sleep(mac_live.SET_EVERY_S)


async def _hold_cloud(auth: dict, device: str, node, channel) -> None:
    """While the door relays (the whole client connection): each signal, and every RENEW_SECONDS,
    re-checks that the session is shared (DesktopError ends the connection, and with it the door)
    and renews the `browserOpen` mark. The timer renews the bridge's lease (`browser.attach` renew,
    which also keeps the runtime awake: AgentCore parks a microVM no invoke reaches, and the door is
    outbound) until CLOUD_IDLE_HOLD_S after the Mac's `idle`, then only wakes it (`health`). Renewing
    before the lease lapses keeps the generation, so the relay's connection survives the quiet.
    `active` ends the quiet and renews the lease before the held commands run."""
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    renew = lambda: {"runId": auth["runId"], "renew": True}  # noqa: E731
    quiet_since: float | None = None  # the Mac's last `idle`, None while commands flow
    watcher = asyncio.create_task(_watch_cloud(owner, sid, auth["runId"]))
    try:
        while True:
            signal = await _signal(channel)
            if signal == "closed":
                return
            try:
                await shared_row(owner, user, device, sid)
            except DesktopError:
                await _stop_cast(owner, auth["runId"])  # no frame of an unshared session goes out
                raise
            if signal == "active":
                quiet_since = None
                ok = await _attach_until(auth, renew)
                node.cdp_leased(channel, ok, *(() if ok else ("cloud_unavailable", CLOUD_MESSAGE)))
            elif signal == "idle":
                quiet_since = _clock() if quiet_since is None else quiet_since
            elif signal is None and (quiet_since is None or _clock() - quiet_since < CLOUD_IDLE_HOLD_S):
                if not await _attach_until(auth, renew):
                    logger.info("session browser cloud renew failed owner=%s", owner)
            elif signal is None:
                try:
                    await container_rpc.call(owner, "health", {})
                except Exception as exc:  # noqa: BLE001 -- the next tick tries again
                    logger.info("session browser keep-awake failed owner=%s error=%s", owner, type(exc).__name__)
            await _mark_open(owner, user, sid, "cloud", announce=False)
    finally:
        watcher.cancel()


async def _serve_cloud(auth: dict, device: str, node, channel) -> None:
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    target = await _native_target(auth, device, node)
    key = (owner, auth["runId"])
    if key in _cloud:  # no await between this check and the claim below
        raise _err("cloud_busy")
    _cloud[key] = _relays[key] = channel
    try:
        await _attach(auth, target)
        deadline = _clock() + DOOR_WAIT_S
        while key in _relays:  # the door claims the channel ...
            if _clock() >= deadline:
                raise _err("cloud_unavailable")
            await asyncio.sleep(0.1)
        # ... and opens it on the Mac (its cdp_accept); announce only then. A refused or timed-out
        # open ends the channel, which raises CdpClosed here.
        await asyncio.shield(channel.opened)
        await _mark_open(owner, user, sid, "cloud", announce=True)
        try:
            await _hold_cloud(auth, device, node, channel)
        finally:
            await _mark_open(owner, user, sid, None, announce=True)
    finally:
        if _relays.get(key) is channel:  # no door took it
            del _relays[key]
        if _cloud.get(key) is channel:
            del _cloud[key]


async def serve(owner: str, user: str, device: str, node, channel, session_id: str, mac_browser_on: bool) -> None:
    """One offered channel, from the offer to its close. Never raises; every offer ends in
    `cdp_accept` or `cdp_close`, and a refusal reaches the session's own CDP client as the close's
    message. The channel closes before the lease is released, so a refused session cannot drive the
    Mac's Chrome through the closing save."""
    auth = auth_for(owner, user, session_id)
    leased: dict = {"generation": None}
    try:
        row = await shared_row(owner, user, device, session_id)
        if mac_browser_on and CDP_COMMAND in node.commands and row.get("browserPlace") != "cloud":
            await _serve_mac(auth, device, node, channel, leased)
        else:
            await _serve_cloud(auth, device, node, channel)
    except DesktopError as exc:
        exc = mac_browser._with_message(exc)
        node.cdp_close(channel, exc.code, exc.message)
    except (CdpClosed, NodeCapacity):
        node.cdp_close(channel)
    except asyncio.CancelledError:
        node.cdp_close(channel)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("session browser failed owner=%s error=%s", owner, type(exc).__name__)
        node.cdp_close(channel, "CLOSED", mac_browser.GENERIC_MESSAGE)
    finally:
        # ponytail: a cancelled task still awaits this release's save, and nothing drains _tasks on
        # shutdown (the lease expires on its own); drain them in the lifespan if that ever matters.
        if leased["generation"]:  # idle already released it
            await _release(auth, device, leased["generation"])


def start(owner: str, user: str, device: str, node, channel, session_id: str, mac_browser_on: bool) -> None:
    """node_broker's `on_offer`: serve it in the background (the relay reader never waits). Never
    raises into the relay reader."""
    try:
        task = asyncio.create_task(serve(owner, user, device, node, channel, session_id, mac_browser_on))
    except Exception as exc:  # noqa: BLE001
        logger.warning("session browser start failed owner=%s error=%s", owner, type(exc).__name__)
        node.cdp_close(channel, "CLOSED", mac_browser.GENERIC_MESSAGE)
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
