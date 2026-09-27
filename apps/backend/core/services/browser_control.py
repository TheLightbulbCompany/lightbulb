"""One workspace browser, shared like a screen.

People and agents use it at the same time: a person's clicks and keys go straight through the
live view and never take a lock. The only coordination is a soft yield in the agent's browser
hook (the bridge waits until the person has been quiet for a few seconds, then re-snapshots),
fed by `human_input_at`, which the panel posts while someone is using the stream.

DynamoDB still fences two things a person never sees: agent runs against each other (AWS admits
one automation connection per session, so one run holds a short lease) and saved-state
publication (a checkpoint commits only on the generation that took it). Saving is automatic:
at the end of every agent turn, ~10 s after a person's last input, and on Stop.

Lifetime is ours, not AWS's. AWS's `sessionTimeoutSeconds` is a TTL "regardless of ongoing
activity" (prod 2026-09-26: a session in active use died at exactly createdAt + 3600 s), so
sessions start at the 8 h maximum and the sweep saves and stops one after IDLE_STOP_SECONDS
with no agent run, no human input and nobody watching (the open panel's `human_view_at`
heartbeat). A session that ends anyway (the 8 h ceiling, an AWS error) is replaced by the next
agent acquire, human Start or watching panel, which restores the last save.

A cold start (session + restore) outlasts API Gateway's 30 s, so no request waits for one:
it runs on in the background and the caller polls ("starting").
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import websockets
from botocore.config import Config

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services import browser_extensions, browser_sessions, container_rpc

logger = logging.getLogger(__name__)

#: An agent run's hold on the automation connection, renewed on every browser tool call.
LEASE_SECONDS = 300
#: How long an agent acquire waits for another run's lease before 409. The bridge calls it
#: from its pre_tool_call hook, whose whole budget is 20 s (Hermes kills the turn's tools
#: after a 30 s hook), so the wait plus CLAIM_WAIT_SECONDS stays under that.
ACQUIRE_WAIT_SECONDS = 10
ACQUIRE_POLL_SECONDS = 1.0
#: How long a request waits on a claim before answering "starting" and leaving it running.
CLAIM_WAIT_SECONDS = 5
STARTING = {"status": "starting"}
TRANSITION_SECONDS = 300
#: AWS's maximum. A hard TTL, not an inactivity timeout: our own idle stop ends sessions.
SESSION_SECONDS = 28800
#: Save and stop a browser nobody (agent or person) has used for this long.
IDLE_STOP_SECONDS = 1800
#: Save this long after a person's last input in the live view.
AUTOSAVE_QUIET_SECONDS = 10
AUTOSAVE_SWEEP_SECONDS = 10


class BrowserError(Exception):
    """A safe, user-facing conflict; never wrap a credential-bearing SDK exception."""


async def _row(owner: str) -> dict:
    row = await container_repo.get_by_owner_id(owner, consistent=True)
    if not row or not row.get("browser_profile_id"):
        raise BrowserError("Browser is unavailable for this workspace.")
    state = await container_repo.get_browser_state(owner)
    if state and state.get("mode") == "transition" and state.get("operation_at", 0) + TRANSITION_SECONDS < time.time():
        recovered = {
            **state,
            "mode": "error",
            "error_code": "transition_expired",
            "error": "Browser operation expired. Restart restores the last completed save.",
        }
        if not await container_repo.compare_browser_state(owner, state, recovered):
            raise BrowserError("Browser control changed. Try again.")
        state = recovered
    return {**row, "browser_state": state}


async def _publish(owner: str, old: dict | None, new: dict) -> dict:
    if not await container_repo.compare_browser_state(owner, old, new):
        raise BrowserError("Browser control changed. Refresh and try again.")
    return new


async def _begin(owner: str, state: dict | None, operation: str = "save") -> dict:
    if state and state.get("mode") == "transition":
        raise BrowserError("Browser is changing control. Try again shortly.")
    return await _publish(
        owner,
        state,
        {
            **(state or {}),
            "generation": secrets.token_hex(32),
            "mode": "transition",
            "operation": operation,
            "operation_at": int(time.time()),
            "error": None,
            "error_code": None,
        },
    )


async def _owned(owner: str, row: dict, state: dict) -> dict | None:
    sid = state.get("session_id")
    if not sid:
        return None
    return await browser_sessions.owns_session(owner, sid, profile_id=row["browser_profile_id"], strict=True)


async def _stream(session_id: str, enabled: bool, token: str) -> None:
    await run_in_thread(
        browser_sessions._data_client().update_browser_stream,
        browserIdentifier=browser_sessions.BUILTIN_BROWSER_ID,
        sessionId=session_id,
        streamUpdate={"automationStreamUpdate": {"streamStatus": "ENABLED" if enabled else "DISABLED"}},
        clientToken=token,
    )


async def _cdp(session_id: str) -> str:
    return await run_in_thread(browser_sessions._presign_stream, session_id, "automation")


def _state_url(key: str, action: str) -> str:
    params = {"Bucket": settings.OWNER_STATE_BUCKET, "Key": key}
    if action == "put_object":
        params["ContentType"] = "application/json"
    return boto3.client(
        "s3", region_name=settings.AWS_REGION, config=Config(signature_version="s3v4")
    ).generate_presigned_url(
        action,
        Params=params,
        ExpiresIn=180,
    )


async def _checkpoint(owner: str, state: dict) -> dict:
    # The runtime cannot replace a committed object: each operation writes its own key.
    key = f"browser-state/{owner}/{state['generation']}.json"
    params = {
        "action": "save",
        "cdpUrl": await _cdp(state["session_id"]),
        "stateUrl": await run_in_thread(_state_url, key, "put_object"),
    }
    previous = state.get("saved_key")
    if previous:
        if not previous.startswith(f"browser-state/{owner}/"):
            raise BrowserError("Saved browser state is unavailable.")
        params["previousStateUrl"] = await run_in_thread(_state_url, previous, "get_object")
    result = await container_rpc.call(owner, "browser.state", params, timeout_s=180)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BrowserError("Could not save browser state. Try Done again.")
    return {"saved_key": key, "saved_at": datetime.now(timezone.utc).isoformat()}


async def _delete_snapshot(key: str) -> None:
    try:
        await run_in_thread(
            boto3.client("s3", region_name=settings.AWS_REGION).delete_object,
            Bucket=settings.OWNER_STATE_BUCKET,
            Key=key,
        )
    except Exception:
        pass  # Wipe also sweeps this backend-only prefix after stopping the runtime.


async def _discard_uncommitted(owner: str, state: dict) -> None:
    key = f"browser-state/{owner}/{state['generation']}.json"
    current = await container_repo.get_browser_state(owner) or {}
    if current.get("saved_key") != key:
        await _delete_snapshot(key)


async def _commit_checkpoint(owner: str, state: dict, saved: dict) -> dict:
    next_state = await _publish(owner, state, {**state, **saved})
    previous = state.get("saved_key")
    if previous and previous != saved["saved_key"] and previous.startswith(f"browser-state/{owner}/"):
        await _delete_snapshot(previous)
    return next_state


async def _restore(owner: str, state: dict) -> None:
    key = state.get("saved_key")
    if not key:
        return
    if not key.startswith(f"browser-state/{owner}/"):
        raise BrowserError("Saved browser state is unavailable.")
    result = await container_rpc.call(
        owner,
        "browser.state",
        {
            "action": "load",
            "cdpUrl": await _cdp(state["session_id"]),
            "stateUrl": await run_in_thread(_state_url, key, "get_object"),
        },
        timeout_s=180,
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BrowserError("Could not restore saved browser state. Try again.")


async def _fail(owner: str, state: dict, message: str) -> None:
    await container_repo.compare_browser_state(owner, state, {**state, "mode": "error", "error": message})


async def _retire(session_id: str) -> None:
    await run_in_thread(
        browser_sessions._data_client().stop_browser_session,
        browserIdentifier=browser_sessions.BUILTIN_BROWSER_ID,
        sessionId=session_id,
    )


def _mode(state: dict | None, now: float | None = None) -> str:
    """The effective mode. "human" is a pre-shared-screen row: it reads as idle."""
    state = state or {}
    mode = state.get("mode", "idle")
    if mode == "agent" and state.get("lease_until", 0) <= (time.time() if now is None else now):
        return "idle"
    return "idle" if mode == "human" else mode


def _free(state: dict) -> dict:
    """An idle state, minus every field the shared-screen model no longer carries."""
    dropped = ("controller_id", "agent_id", "lease_until", "autosave_failed_at", "operation", "ended_reason")
    return {k: v for k, v in state.items() if k not in dropped} | {"mode": "idle"}


async def _claim(owner: str, run_id: str | None, *, start_only: bool = False, agent_id: str | None = None) -> dict:
    """Make sure a live session exists; with `run_id`, also give that agent run the lease."""
    row = await _row(owner)
    old = row.get("browser_state")
    if not old:
        for sid in await run_in_thread(browser_sessions._ready_session_ids):
            if await browser_sessions.owns_session(owner, sid, profile_id=row["browser_profile_id"], strict=True):
                raise BrowserError(
                    "The previous browser session is still running. Wait for it to finish before starting a shared browser."
                )
    mode = _mode(old)
    # An errored browser recovers on the next agent action too, not only a person's Restart:
    # a dead session must never be a dead end for the agent.
    if mode == "transition":
        raise BrowserError("Browser is changing. Try again shortly.")
    if mode == "agent" and run_id and old.get("controller_id") != run_id:
        raise BrowserError("Another agent run is using the browser. Try again shortly.")
    owned = old and mode != "error" and await _owned(owner, row, old)
    if owned and start_only:
        return old
    if owned and mode == "agent":  # this run renewing its own lease
        return await _publish(owner, old, {**old, "lease_until": int(time.time()) + LEASE_SECONDS})
    state = await _begin(owner, old, "start")
    try:
        got = await _owned(owner, row, state)
        # Failed/abandoned operations may still hold old AWS capabilities. Recover into a
        # different session; an old stream update can then never mutate the replacement.
        if got and mode == "error":
            await _retire(state["session_id"])
            got = None
        if not got:
            start_token = state.get("start_token") or secrets.token_hex(32)
            state = await _publish(owner, state, {**state, "start_token": start_token})
            # Admin-managed (Connections -> Browser -> Extensions); fixed for the session's life.
            extensions = await browser_extensions.session_extensions(owner)
            result = await run_in_thread(
                browser_sessions._data_client().start_browser_session,
                browserIdentifier=browser_sessions.BUILTIN_BROWSER_ID,
                profileConfiguration={"profileIdentifier": row["browser_profile_id"]},
                name=f"workspace-{start_token[:16]}",
                clientToken=start_token,
                sessionTimeoutSeconds=SESSION_SECONDS,
                # The size of the panel a person last watched it in, so an agent-started
                # session already fits the panel that opens beside the chat.
                viewPort=await container_repo.get_browser_viewport(owner)
                or {"width": browser_sessions.REMOTE_WIDTH, "height": browser_sessions.REMOTE_HEIGHT},
                **({"extensions": extensions} if extensions else {}),
            )
            state = await _publish(owner, state, {**state, "session_id": result["sessionId"], "start_token": None})
            if not await _owned(owner, row, state):
                raise BrowserError("Browser is still starting. Try again shortly.")
            await _restore(owner, state)
        # Always on: a person's input goes through the live view whether or not an agent is
        # connected. A fresh token revokes any stale automation client of an older generation.
        await _stream(state["session_id"], True, state["generation"])
        if run_id:
            nxt = {
                **_free(state),
                "mode": "agent",
                "controller_id": run_id,
                "agent_id": agent_id,
                "lease_until": int(time.time()) + LEASE_SECONDS,
            }
        else:
            nxt = _free(state)
        return await _publish(owner, state, nxt)
    except BrowserError:
        await _fail(owner, state, "Browser could not start. Start again to recover the last saved state.")
        raise
    except Exception:
        await _fail(owner, state, "Browser could not start. Start again to recover the last saved state.")
        raise BrowserError("Browser could not start. Try again.") from None


_background: set[asyncio.Task] = set()


def _settled(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception():
        logger.warning("browser background claim failed: %s", type(task.exception()).__name__)


async def _claim_soon(owner: str, run_id: str | None, **kwargs) -> dict | None:
    """`_claim`, answered within CLAIM_WAIT_SECONDS: None means it is still running (a cold
    start) and finishes in the background. `_begin`'s compare-and-set is the fence, so two
    callers on two backend tasks never start two sessions."""
    task = asyncio.create_task(_claim(owner, run_id, **kwargs))
    _background.add(task)
    task.add_done_callback(_settled)
    done, _ = await asyncio.wait({task}, timeout=CLAIM_WAIT_SECONDS)
    return task.result() if done else None


async def start(owner: str, user: str) -> dict:
    # Old images retain v1 until the explicit image rollout. Never start a browser whose
    # runtime cannot checkpoint it, even if backend deployment happened first.
    health = await container_rpc.call(owner, "health")
    if not isinstance(health, dict) or health.get("browserProtocol") != 2:
        raise BrowserError("This workspace needs the browser update before it can start a shared session.")
    await _claim_soon(owner, None, start_only=True)
    return await view(owner, user)


async def watch(owner: str, user: str) -> dict:
    """The open, visible panel's heartbeat: someone is looking, so keep the browser running,
    and start it (exactly like Start) when none is. Stamped with server time, never the
    client's, so a late or replayed heartbeat cannot backdate or extend anything."""
    await container_repo.touch_browser_input(owner, time.time(), field="human_view_at")
    out = await view(owner, user)
    if out["active"] or out["mode"] == "transition":
        return out
    return await start(owner, user)


#: What the live view's display server grants a web client (dev, 2026-09-26).
VIEWPORT_MIN = (640, 480)
VIEWPORT_MAX = (1920, 1080)
VIEWPORT_TIMEOUT_S = 15


async def _fit_windows(session_id: str, width: int, height: int) -> None:
    """Size every browser window to the live view's display over the automation stream.

    The session's ``viewPort`` sizes Chrome's window once, at start, and nothing moves it after:
    the display has no window manager, so a window neither maximizes nor follows a display
    resize (both checked on dev 2026-09-26). Pages then lay out at the new size, which is what
    the agent's next snapshot sees."""
    async with websockets.connect(await _cdp(session_id), max_size=None, open_timeout=10) as ws:
        ids = itertools.count(1)

        async def call(method: str, params: dict | None = None) -> dict:
            call_id = next(ids)
            await ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == call_id:
                    if "error" in message:
                        raise BrowserError("Could not resize the browser.")
                    return message.get("result") or {}

        targets = (await call("Target.getTargets")).get("targetInfos", [])
        windows = set()
        for target in targets:
            if target.get("type") == "page":
                windows.add((await call("Browser.getWindowForTarget", {"targetId": target["targetId"]}))["windowId"])
        for window in windows:
            # A window asked for exactly the display's size comes back one pixel short (so does
            # AgentCore's own: 1919x1079 on 1920x1080), leaving a black line at the edge. One
            # pixel more is granted and falls off-screen.
            bounds = {"left": 0, "top": 0, "width": width + 1, "height": height + 1}
            await call("Browser.setWindowBounds", {"windowId": window, "bounds": bounds})


async def set_viewport(owner: str, width: int, height: int) -> dict:
    """Make the shared browser the size of the panel showing it, so pages read at normal size.

    Only while no agent run holds the browser: a run owns the one automation connection AWS
    admits (a second one is refused with 429, dev 2026-09-26), and its snapshots and clicks are
    laid out for the size it started with. The panel keeps the stream scaled meanwhile and asks
    again once the run releases. ``resized: False`` tells it not to resize the display either.
    The size is remembered either way: the next session starts at it."""
    if not (VIEWPORT_MIN[0] <= width <= VIEWPORT_MAX[0] and VIEWPORT_MIN[1] <= height <= VIEWPORT_MAX[1]):
        raise BrowserError("Unsupported browser size.")
    await container_repo.set_browser_viewport(owner, width, height)
    row = await _row(owner)
    state = row.get("browser_state") or {}
    if _mode(state) != "idle" or not state.get("session_id") or not await _owned(owner, row, state):
        return {"resized": False}
    await asyncio.wait_for(_fit_windows(state["session_id"], width, height), VIEWPORT_TIMEOUT_S)
    return {"resized": True}


async def record_input(owner: str) -> dict:
    """The panel's throttled "a person is using the stream" signal: the agent yields to it,
    and the autosave sweep saves after it goes quiet. It grants and blocks nothing."""
    await container_repo.touch_browser_input(owner, time.time())
    return {"ok": True}


async def acquire_agent(owner: str, run_id: str, agent_id: str | None = None) -> dict:
    # Only another RUN can hold the browser (one automation connection per session); a person
    # never does. Wait briefly for that run's turn to release it.
    # A start or save in flight answers "starting" at once: the bridge polls (202).
    deadline = time.monotonic() + ACQUIRE_WAIT_SECONDS
    while time.monotonic() < deadline:
        state = await container_repo.get_browser_state(owner)
        mode = _mode(state)
        if mode == "transition" and float(state.get("operation_at") or 0) + TRANSITION_SECONDS > time.time():
            return STARTING  # an expired one falls through: _claim recovers it
        if mode == "agent" and (state or {}).get("controller_id") != run_id:
            await asyncio.sleep(ACQUIRE_POLL_SECONDS)
            continue
        break
    state = await _claim_soon(owner, run_id, agent_id=agent_id)
    if state is None:
        return STARTING
    return {
        "generation": state["generation"],
        "cdpUrl": await _cdp(state["session_id"]),
        "humanInputAt": await container_repo.get_browser_input_at(owner),
    }


async def _save(owner: str, old: dict, *, stop: bool = False, reason: str | None = None) -> dict:
    """Checkpoint the live session, commit it, and leave the browser idle (or stopped)."""
    state = await _begin(owner, old)
    try:
        if stop:
            # Revoke a connected agent's automation client first: the checkpoint needs the one
            # connection AWS admits.
            await _stream(state["session_id"], False, secrets.token_hex(32))
        await _stream(state["session_id"], True, state["generation"])
        saved = await _checkpoint(owner, state)
        # Commit the save before stop. A stale RPC can upload its own object, but cannot
        # move this pointer or stop the session now owned by another controller.
        state = await _commit_checkpoint(owner, state, saved)
        if stop:
            await _retire(state["session_id"])
        return await _publish(
            owner,
            state,
            {**_free(state), "session_id": None if stop else state["session_id"], "ended_reason": reason},
        )
    except Exception:
        await _discard_uncommitted(owner, state)
        if (await container_repo.get_browser_state(owner)) == state:
            await container_repo.compare_browser_state(owner, state, old)
        raise BrowserError("Could not save the browser. Try again.") from None


async def stop(owner: str, user: str, generation: str | None = None) -> dict:
    """Any member's Stop: save, then end the session (an agent mid-turn is disconnected)."""
    row = await _row(owner)
    old = row.get("browser_state") or {}
    if _mode(old) in ("transition", "error") or not old.get("session_id"):
        raise BrowserError("The browser is not running.")
    if not await _owned(owner, row, old):
        raise BrowserError("The browser session expired. Its last completed save is available on restart.")
    await _save(owner, old, stop=True)
    return await view(owner, user)


async def release_agent(owner: str, run_id: str, generation: str) -> dict:
    row = await _row(owner)
    old = row.get("browser_state") or {}
    if old.get("generation") != generation or old.get("controller_id") != run_id or old.get("mode") != "agent":
        raise BrowserError("Browser control changed. Try again.")
    if not await _owned(owner, row, old):
        raise BrowserError("The browser session expired. Its last completed save is available on restart.")
    await _save(owner, old)
    return {"ok": True}


def _epoch(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def _last_used(state: dict, input_at: float | None, view_at: float | None = None) -> float:
    """The latest agent or human activity. operation_at covers starts and turn-end releases;
    lease_until covers a run that died mid-turn without releasing; view_at is the open panel's
    heartbeat, so a watched browser never idle-stops and an unwatched one stops
    IDLE_STOP_SECONDS after the last look."""
    # float(): DynamoDB hands back Decimal, and `float - Decimal` raises (it killed the sweep).
    lease = float(state.get("lease_until") or 0)
    return max(
        float(input_at or 0),
        float(view_at or 0),
        float(state.get("operation_at") or 0),
        (lease - LEASE_SECONDS) if lease else 0.0,
    )


async def _idle_stop(owner: str, state: dict) -> None:
    """Save, then stop. If the save fails (or the browser is errored) stop anyway: the last
    completed save stands, and an idle session must not bill for up to 8 h."""
    if _mode(state) == "idle":
        try:
            await _save(owner, state, stop=True, reason="idle")
            return
        except Exception as exc:
            logger.warning("browser idle save failed for %s: %s", owner, type(exc).__name__)
    current = await container_repo.get_browser_state(owner)
    if not current or current.get("session_id") != state["session_id"] or _mode(current) not in ("idle", "error"):
        return
    try:
        await _retire(state["session_id"])
    except Exception as exc:  # already gone at AWS is the common case
        logger.info("browser idle stop for %s: %s", owner, type(exc).__name__)
    await container_repo.compare_browser_state(
        owner,
        current,
        {**_free(current), "session_id": None, "ended_reason": "idle", "error": None, "error_code": None},
    )


async def autosave_once(*, now: float | None = None) -> int:
    """Save every idle browser a person used and then left quiet for AUTOSAVE_QUIET_SECONDS,
    and save-then-stop every browser nobody has used for IDLE_STOP_SECONDS.

    A browser an agent is driving is skipped: its turn-end release saves, with the one
    automation connection the checkpoint needs. A failed save is not retried until new input.
    """
    now = time.time() if now is None else now
    saved = 0
    for row in await container_repo.get_by_status("running"):
        owner = row.get("owner_id")
        if not isinstance(owner, str) or not row.get("browser_profile_id"):
            continue
        state = await container_repo.get_browser_state(owner) or {}
        input_at = await container_repo.get_browser_input_at(owner)
        if (
            _mode(state, now) in ("idle", "error")
            and state.get("session_id")
            and now - _last_used(state, input_at, await container_repo.get_browser_input_at(owner, "human_view_at"))
            >= IDLE_STOP_SECONDS
        ):
            try:
                await _idle_stop(owner, state)
            except Exception as exc:
                logger.warning("browser idle stop failed for %s: %s", owner, type(exc).__name__)
            continue
        if (
            _mode(state, now) != "idle"
            or not state.get("session_id")
            or not input_at
            or now - input_at < AUTOSAVE_QUIET_SECONDS
            or input_at <= _epoch(state.get("saved_at"))
            or input_at <= float(state.get("autosave_failed_at", 0))
        ):
            continue
        try:
            await _save(owner, state)
            saved += 1
        except Exception as exc:
            # Class name only: a checkpoint failure can carry a signed URL.
            logger.warning("browser autosave failed for %s: %s", owner, type(exc).__name__)
            current = await container_repo.get_browser_state(owner)
            if current and _mode(current, now) == "idle":
                await container_repo.compare_browser_state(
                    owner, current, {**current, "autosave_failed_at": Decimal(str(input_at))}
                )
    return saved


async def run_autosave_sweeper(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            if await container_repo.try_claim_pass("browser_autosave", interval_s=AUTOSAVE_SWEEP_SECONDS):
                await autosave_once()
        except Exception as exc:
            logger.warning("browser autosave sweep failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=AUTOSAVE_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass


async def view(owner: str, user: str) -> dict:
    row = await _row(owner)
    state = row.get("browser_state")
    if not state:
        # Preserve existing v1 discovery during staged image rollout.
        return {**await browser_sessions.live_view(owner), "mode": "idle", "generation": None}
    got = await _owned(owner, row, state)
    mode = _mode(state)
    out = {
        **browser_sessions._INACTIVE,
        "sessionId": state.get("session_id"),
        "generation": state["generation"],
        "mode": mode,
        # The agent run using it, so the panel can name the agent; nobody else ever "holds" it.
        "controllerId": state.get("controller_id") if mode == "agent" else None,
        "agentId": state.get("agent_id") if mode == "agent" else None,
        "savedAt": state.get("saved_at"),
        "error": state.get("error"),
        # Why the browser is not running: "idle" (our stop) or "expired" (it ended at AWS).
        "endedReason": state.get("ended_reason"),
        "idleStopSeconds": IDLE_STOP_SECONDS,
        "operation": state.get("operation") if mode == "transition" else None,
    }
    if got:
        out.update(await browser_sessions.session_view(state["session_id"], got))
    elif state.get("session_id") and mode != "transition":
        out["endedReason"] = "expired"
    return out


async def teardown(owner: str, row: dict, *, delete_state: bool = False) -> None:
    """Stop the billed browser at runtime teardown, retaining the last completed save."""
    old = await container_repo.get_browser_state(owner)
    if not old:
        return
    state = {**old, "generation": secrets.token_hex(32), "mode": "transition", "operation_at": int(time.time())}
    if not await container_repo.compare_browser_state(owner, old, state):
        raise BrowserError("Browser changed during teardown.")
    try:
        if row.get("browser_profile_id") and await _owned(owner, row, state):
            if not delete_state:
                # Revoke any in-flight automation connection before the checkpoint client
                # attaches: AgentCore admits only one automation connection per session.
                await _stream(state["session_id"], False, secrets.token_hex(32))
                await _stream(state["session_id"], True, state["generation"])
                saved = await _checkpoint(owner, state)
                state = await _commit_checkpoint(owner, state, saved)
            await _retire(state["session_id"])
        await _publish(owner, state, {**_free(state), "session_id": None})
    except Exception:
        await _discard_uncommitted(owner, state)
        if (await container_repo.get_browser_state(owner)) == state:
            await container_repo.compare_browser_state(
                owner,
                state,
                {
                    **state,
                    "mode": _mode(old),
                    "error": "Could not save browser before stopping it.",
                },
            )
        raise BrowserError("Could not save browser before stopping it.") from None
