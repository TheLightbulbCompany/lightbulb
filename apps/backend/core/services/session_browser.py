"""A shared Claude Code or Codex session's browser (spec docs/superpowers/specs/2026-10-11-session-browser-design.md).

The Mac app identifies a CDP connection on its loopback port as one shared session and offers it
(`browser.cdp.offer`, node_broker). This runs on the task holding that Mac's socket. The session
is a run, `session-<sessionId>`, of its own person: it takes a `mac_browser` lease (first owner
wins; the first lease in loads the workspace sign-ins, the last one out saves them), the runtime
runs those sync steps over the ticketed Mac door (`browser.state` with a door, spike 3), and then
the Mac opens the channel on its own agent Chrome for the session's client. The channel stays open
for the client's whole connection. While active the lease is renewed; 300 s after the last command
the Mac says `idle` and the lease is released (saving); the next command is held on the Mac while
`active` re-acquires it (loading), then `cdp_leased` lets it through. The session ending, or its
socket closing, releases. Never log a ticket, a URL or a frame: owner ids and exception class names
only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import weakref

from botocore.exceptions import ClientError

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import container_rpc, live_events, local_sessions, mac_browser, mac_live, session_screen
from core.services.desktop_execution import DesktopError
from core.services.messaging import ApiProblem, make_live_event
from core.services.node_broker import CDP_COMMAND, CdpClosed, CommandNotAllowed, NodeCapacity

logger = logging.getLogger(__name__)

RUN_PREFIX = "session-"
AGENT_ID = "session"
NOT_SHARED = "Share this session to use the Lightbulb browser"
MESSAGES = {"not_shared": NOT_SHARED}
OFFER_BUDGET_S = 60  # the Mac waits 150 s for the answer
POLL_S = 2
RENEW_SECONDS = 120  # < mac_browser.LEASE_SECONDS
OPEN_SECONDS = 2 * RENEW_SECONDS
SYNC_TIMEOUT_S = 150  # the bridge caps one Mac sync at 120 s (MAC_SYNC_BUDGET_S)
_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_clock = time.time
_tasks: set[asyncio.Task] = set()
# One sign-in sync at a time per session upstream (two channels of one session, or a closing save
# racing the next load). A lock lives while someone holds or waits on it.
_sync_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def run_id(session_id: str) -> str:
    return RUN_PREFIX + session_id


def session_of(run: object) -> str | None:
    """The session id of a `session-<id>` run, else None."""
    sid = run[len(RUN_PREFIX) :] if isinstance(run, str) and run.startswith(RUN_PREFIX) else ""
    return sid if _SID.fullmatch(sid) else None


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
    with its sync (Decision 8: a `[save, load]`, so the Mac's own changes are saved before the load),
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


async def _open_row(auth, session_id: str, *, owner_only: bool) -> dict:
    """The session's row for a person still in the workspace who may read it (`owner_only`: may type
    into it, `may_control`), while its browser is open."""
    row, _ = await local_sessions.authorize_session(auth, None, session_id, owner_only=owner_only)
    await session_screen._still_a_member(row, auth.user_id)
    if local_sessions.browser_open(row) is None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "This session's browser is not open.")
    return row


async def watch(auth, session_id: str) -> None:
    """The Browser tab's ~20 s beat: this session's browser frames for the next mac_live.WANTED_S.
    Any member who may read the session; frames are stored nowhere."""
    row = await _open_row(auth, session_id, owner_only=False)
    await store.run_in_thread(
        store._table().update_item,
        Key={"pk": repo.session_pk(row["ownerId"], row["sessionId"]), "sk": "META"},
        UpdateExpression="SET browserWatchedAt = :now",
        ConditionExpression="attribute_exists(pk)",
        ExpressionAttributeValues={":now": int(_clock())},
    )


async def input_target(auth, session_id: str) -> dict:
    """The session a person drives the browser of: they may type into it, are still in the
    workspace, and its browser is open."""
    return await _open_row(auth, session_id, owner_only=True)


async def may_type(owner: str, user: str, run: str) -> bool:
    """Re-checked where the Mac's socket is (mac_live.input): this person may still type into it."""
    sid = session_of(run)
    row = await repo.get_session(owner, sid) if sid else None
    if not row or row.get("unshared") or not local_sessions.may_control(row, user):
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
            want = bool(row) and now - float((row or {}).get("browserWatchedAt") or 0) < mac_live.WANTED_S
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
    workspace = await store.ensure_workspace_id(owner)
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


async def serve(owner: str, user: str, device: str, node, channel, session_id: str, mac_browser_on: bool) -> None:
    """One offered channel, from the offer to its close. Never raises; every offer ends in
    `cdp_accept` or `cdp_close`, and a refusal reaches the session's own CDP client as the close's
    message. The channel closes before the lease is released, so a refused session cannot drive the
    Mac's Chrome through the closing save."""
    auth = auth_for(owner, user, session_id)
    leased: dict = {"generation": None}
    try:
        await shared_row(owner, user, device, session_id)
        if not mac_browser_on or CDP_COMMAND not in node.commands:
            raise _err("browser_off")
        await _serve_mac(auth, device, node, channel, leased)
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
