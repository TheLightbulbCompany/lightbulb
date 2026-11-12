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

AWS streams the live view to ONE viewer. Everyone else watches live frames: their heartbeat
(`watch(frames=True)`) stamps `human_frames_at` and tells the bridge, which screencasts each
browsing run's tab to its room; their clicks and keys (`send_input`) go to that tab through the
bridge, which holds the one automation connection while an agent browses.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from botocore.exceptions import ClientError

from core.billing import bedrock_aip
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
        browserIdentifier=await run_in_thread(browser_sessions.browser_of, session_id),
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


def _base_key(owner: str, state: dict) -> str:
    return f"browser-state/{owner}/{state['generation']}.base.json"


async def _checkpoint(owner: str, state: dict) -> dict:
    # The runtime cannot replace a committed object: each operation writes its own key.
    key = f"browser-state/{owner}/{state['generation']}.json"
    base_out = _base_key(owner, state)
    params = {
        "action": "save",
        "cdpUrl": await _cdp(state["session_id"]),
        "stateUrl": await run_in_thread(_state_url, key, "put_object"),
        # The session's own export, kept as the base its next save is measured against: per
        # cookie domain and origin, the session wins only where it changed since, so a sign-in
        # a paired Mac saved meanwhile (the bridge's Mac sync) is not overwritten.
        "baseOutUrl": await run_in_thread(_state_url, base_out, "put_object"),
    }
    previous = state.get("saved_key")
    if previous:
        if not previous.startswith(f"browser-state/{owner}/"):
            raise BrowserError("Saved browser state is unavailable.")
        params["previousStateUrl"] = await run_in_thread(_state_url, previous, "get_object")
    base = state.get("base_key")
    if base and base.startswith(f"browser-state/{owner}/"):
        params["baseStateUrl"] = await run_in_thread(_state_url, base, "get_object")
    result = await container_rpc.call(owner, "browser.state", params, timeout_s=180)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BrowserError("Could not save browser state. Try Done again.")
    await _save_extension_storage(owner, state, key)
    return {
        "saved_key": key,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        # An image without the three-way merge writes no base: its next save starts from none.
        "base_key": base_out if result.get("base") is True else None,
        # What the live session now holds: a later saved_key it did not write (a paired Mac's
        # publish) is news it has not loaded (at the next claim). A session that was already stale has
        # merged that news into this save without loading it, so it stays stale.
        "live_key": state.get("live_key") if _stale(state) else key,
    }


# ---- each installed extension's own chrome.storage ------------------------------------------
# AWS's browser keeps no Chrome profile, so every session installs every extension afresh and
# its settings and sign-ins would be lost. The backend reads each Web Store extension's
# chrome.storage.local/sync over the automation stream at every save, beside the page state
# (``<generation>.extensions.json``), and writes it back at the next start before the tabs
# come back. An extension-origin page (its manifest.json, opened in the background) reaches
# the storage whether or not the extension's service worker is awake. Not covered: ZIP uploads
# without a store key (their id is not known to us), an extension page's localStorage, and
# toolbar pins (Chrome prefs, not reachable over CDP).

EXT_STORAGE_MAX_BYTES = 5 * 1024 * 1024
EXT_STORAGE_TIMEOUT_S = 5
_EXT_READY_JS = (
    "for (let i = 0; i < 40 && !(self.chrome && chrome.storage); i++) await new Promise((r) => setTimeout(r, 100));"
)
_EXT_READ_JS = (
    "(async () => { " + _EXT_READY_JS + " return JSON.stringify({local: await chrome.storage.local.get(null), "
    "sync: await chrome.storage.sync.get(null)}); })()"
)


def _ext_key(key: str) -> str:
    return key.removesuffix(".json") + ".extensions.json"


async def _stored_extension_ids(owner: str) -> list[str]:
    return [
        e["slug"]
        for e in await container_repo.get_browser_extensions(owner) or []
        if browser_extensions.is_webstore_id(e["slug"])
    ]


async def _read_snapshot(key: str) -> dict:
    try:
        got = await run_in_thread(
            boto3.client("s3", region_name=settings.AWS_REGION).get_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key
        )
        return json.loads(got["Body"].read())
    except Exception:
        return {}


async def _in_extension(call, ext_id: str, expression: str):
    """Evaluate ``expression`` in a background page of the extension's own origin."""
    target = (
        await call("Target.createTarget", {"url": f"chrome-extension://{ext_id}/manifest.json", "background": True})
    )["targetId"]
    try:
        page = (await call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        for attempt in range(3):  # the first context can be the initial about:blank
            try:
                got = await call(
                    "Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, page
                )
                if "exceptionDetails" not in got:
                    return (got.get("result") or {}).get("value")
            except BrowserError:
                pass
            await asyncio.sleep(0.3)
        return None
    finally:
        await call("Target.closeTarget", {"targetId": target})


async def _save_extension_storage(owner: str, state: dict, key: str) -> None:
    """Never fails the save: an extension that can't be read keeps what the last save had."""
    ids = await _stored_extension_ids(owner)
    previous = state.get("saved_key")
    before = (
        await _read_snapshot(_ext_key(previous)) if previous and previous.startswith(f"browser-state/{owner}/") else {}
    )
    saved = {i: before[i] for i in ids if i in before}  # a removed extension's data goes here
    if ids:
        try:
            async with _devtools(state["session_id"], "Could not read an extension's storage.") as call:
                for ext_id in ids:
                    try:
                        value = await asyncio.wait_for(_in_extension(call, ext_id, _EXT_READ_JS), EXT_STORAGE_TIMEOUT_S)
                    except Exception:  # noqa: BLE001
                        value = None
                    if not value:
                        logger.info("browser extension storage unreadable owner=%s ext=%s", owner, ext_id)
                    elif len(value) > EXT_STORAGE_MAX_BYTES:
                        logger.warning("browser extension storage over cap owner=%s ext=%s", owner, ext_id)
                    else:
                        saved[ext_id] = json.loads(value)
        except Exception as exc:  # noqa: BLE001 -- e.g. 429 while an agent run holds the stream
            logger.info("browser extension storage save skipped owner=%s: %s", owner, type(exc).__name__)
    await run_in_thread(
        boto3.client("s3", region_name=settings.AWS_REGION).put_object,
        Bucket=settings.OWNER_STATE_BUCKET,
        Key=_ext_key(key),
        Body=json.dumps(saved).encode(),
        ContentType="application/json",
    )


async def _restore_extension_storage(owner: str, state: dict, key: str) -> None:
    """Put each still-installed extension's saved storage back. Never fails the start."""
    saved = await _read_snapshot(_ext_key(key))
    ids = [i for i in await _stored_extension_ids(owner) if i in saved]
    if not ids:
        return
    try:
        async with _devtools(state["session_id"], "Could not restore an extension's storage.") as call:
            for ext_id in ids:
                data = json.dumps(saved[ext_id])
                expression = (
                    f"(async (d) => {{ {_EXT_READY_JS} await chrome.storage.local.set(d.local || {{}}); "
                    "try { await chrome.storage.sync.set(d.sync || {}); } catch (e) {} return 'ok'; })"
                    f"({data})"
                )
                try:
                    ok = await asyncio.wait_for(_in_extension(call, ext_id, expression), EXT_STORAGE_TIMEOUT_S)
                except Exception:  # noqa: BLE001
                    ok = None
                if ok != "ok":
                    logger.info("browser extension storage not restored owner=%s ext=%s", owner, ext_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("browser extension storage restore skipped owner=%s: %s", owner, type(exc).__name__)


async def _delete_snapshot(key: str) -> None:
    for each in (key, _ext_key(key)):
        try:
            await run_in_thread(
                boto3.client("s3", region_name=settings.AWS_REGION).delete_object,
                Bucket=settings.OWNER_STATE_BUCKET,
                Key=each,
            )
        except Exception:
            pass  # Wipe also sweeps this backend-only prefix after stopping the runtime.


async def _discard_uncommitted(owner: str, state: dict) -> None:
    key = f"browser-state/{owner}/{state['generation']}.json"
    current = await container_repo.get_browser_state(owner) or {}
    if current.get("saved_key") != key:
        await _delete_snapshot(key)
    if current.get("base_key") != _base_key(owner, state):
        await _delete_snapshot(_base_key(owner, state))


async def _commit_checkpoint(owner: str, state: dict, saved: dict) -> dict:
    next_state = await _publish(owner, state, {**state, **saved})
    for field in ("saved_key", "base_key"):
        previous = state.get(field)
        if previous and previous != next_state.get(field) and previous.startswith(f"browser-state/{owner}/"):
            await _delete_snapshot(previous)
    return next_state


async def _restore(owner: str, state: dict) -> str | None:
    """Load the last save into the new session; return the key of its merge base (the
    session's own export right after the load, which an image with the three-way merge
    writes), or None when there was nothing to load or the image wrote no base."""
    key = state.get("saved_key")
    base = _base_key(owner, state)
    if not key:
        # Nothing to restore, but the session still needs its own base, or its first save
        # would overwrite every cookie domain it holds (a paired Mac's included).
        try:
            result = await container_rpc.call(
                owner,
                "browser.state",
                {
                    "action": "base",
                    "cdpUrl": await _cdp(state["session_id"]),
                    "stateUrl": await run_in_thread(_state_url, base, "put_object"),
                },
                timeout_s=60,
            )
        except Exception:  # noqa: BLE001 -- an older image: no base, and the Mac stays gated
            return None
        return base if isinstance(result, dict) and result.get("base") is True else None
    if not key.startswith(f"browser-state/{owner}/"):
        raise BrowserError("Saved browser state is unavailable.")
    # Before the tabs come back, so each extension starts its pages with its own data.
    await _restore_extension_storage(owner, state, key)
    result = await container_rpc.call(
        owner,
        "browser.state",
        {
            "action": "load",
            "cdpUrl": await _cdp(state["session_id"]),
            "stateUrl": await run_in_thread(_state_url, key, "get_object"),
            "baseOutUrl": await run_in_thread(_state_url, base, "put_object"),
        },
        timeout_s=180,
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BrowserError("Could not restore saved browser state. Try again.")
    return base if result.get("base") is True else None


def _stale(state: dict) -> bool:
    """Another writer (a paired Mac) published since this live session last saved or loaded."""
    return bool(state.get("saved_key")) and state.get("live_key") != state["saved_key"]


async def _fail(owner: str, state: dict, message: str) -> None:
    await container_repo.compare_browser_state(owner, state, {**state, "mode": "error", "error": message})


#: Owners whose ``owner-browser`` policy this process has rewritten for ``BROWSER_ID``.
_GRANTED: set[str] = set()


async def _grant_browser(owner: str, profile_id: str) -> None:
    """Before a session starts on the VPC browser, make sure the owner's role names it: the
    runtime calls ``InvokeBrowser`` (computer use) under that role, and a role written before
    the move names only the built-in browser (a provision or roll rewrites it too). Once per
    owner per process; a failure costs computer use, never the browser."""
    if bedrock_aip.BROWSER_ID == bedrock_aip.BUILTIN_BROWSER_ID or owner in _GRANTED:
        return
    try:
        await bedrock_aip.ensure_owner_browser_policy(owner, profile_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner browser policy refresh failed for %s: %s", owner, type(exc).__name__)
        return
    _GRANTED.add(owner)


async def _retire(session_id: str) -> None:
    await run_in_thread(
        browser_sessions._data_client().stop_browser_session,
        browserIdentifier=await run_in_thread(browser_sessions.browser_of, session_id),
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
    mode = _mode(old)
    # An errored browser recovers on the next agent action too, not only a person's Restart:
    # a dead session must never be a dead end for the agent.
    if mode == "transition":
        if start_only:
            # A start (the open panel's heartbeat, another person's click) or a save is already
            # in flight: this Start has nothing to add, and the view says which it is.
            return old
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
            # Admin-managed extensions + Chrome policies; fixed for the session's life.
            # The helper's token names this session (helper_nonce below): it dies with it. From
            # the start token, so a retried start (same clientToken, same AWS session) agrees.
            helper_nonce = start_token[16:]
            session_config = await browser_extensions.session_config(owner, helper_nonce)
            await _grant_browser(owner, row["browser_profile_id"])
            result = await run_in_thread(
                browser_sessions._data_client().start_browser_session,
                browserIdentifier=browser_sessions.BROWSER_ID,
                profileConfiguration={"profileIdentifier": row["browser_profile_id"]},
                name=f"workspace-{start_token[:16]}",
                clientToken=start_token,
                sessionTimeoutSeconds=SESSION_SECONDS,
                # The size of the panel a person last watched it in, so an agent-started
                # session already fits the panel that opens beside the chat.
                viewPort=await container_repo.get_browser_viewport(owner)
                or {"width": browser_sessions.REMOTE_WIDTH, "height": browser_sessions.REMOTE_HEIGHT},
                **session_config,
            )
            state = await _publish(
                owner,
                state,
                {**state, "session_id": result["sessionId"], "start_token": None, "helper_nonce": helper_nonce},
            )
            if not await _owned(owner, row, state):
                raise BrowserError("Browser is still starting. Try again shortly.")
            restored = {"base_key": await _restore(owner, state), "live_key": state.get("saved_key")}
        else:
            restored = {}
        # Always on: a person's input goes through the live view whether or not an agent is
        # connected. A fresh token revokes any stale automation client of an older generation.
        await _stream(state["session_id"], True, state["generation"])
        if run_id and not restored and _stale(state):
            # Save the cloud session's changes before loading the Mac's latest save. A failed
            # save has not changed the browser, so retry at the next run's acquire. A failed
            # load may have cleared cookies/storage; let the claim replace that session.
            try:
                state = await _commit_checkpoint(owner, state, await _checkpoint(owner, state))
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser catch-up save failed for %s: %s", owner, type(exc).__name__)
                await _discard_uncommitted(owner, state)
                restored = {"live_key": None}
            else:
                restored = {"base_key": await _restore(owner, state), "live_key": state["saved_key"]}
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
        published = await _publish(owner, state, {**nxt, **restored})
        stale = state.get("base_key")
        if (
            "base_key" in restored
            and stale
            and stale != restored["base_key"]
            and stale.startswith(f"browser-state/{owner}/")
        ):
            await _delete_snapshot(stale)
        return published
    except BrowserError:
        await _discard_uncommitted(owner, state)
        await _fail(owner, state, "Browser could not start. Start again to recover the last saved state.")
        raise
    except Exception:
        await _discard_uncommitted(owner, state)
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
    await _claim_soon(owner, None, start_only=True)
    return await view(owner, user)


#: A frame watcher's heartbeat keeps the bridge casting this long (they beat every ~20 s).
FRAMES_TTL_SECONDS = 45
FRAMES_RPC_TIMEOUT_S = 5


async def watch(owner: str, user: str, frames: bool = False) -> dict:
    """The open, visible panel's heartbeat: someone is looking, so keep the browser running,
    and start it (exactly like Start) when none is -- unless a person stopped it: their Stop
    stands, however many panels are still open, until someone presses Start or an agent needs
    the browser. Stamped with server time, never the client's, so a late or replayed heartbeat
    cannot backdate or extend anything.

    ``frames``: this viewer does not have the live view (someone else holds its one slot), so
    the bridge casts the browsing runs' tabs to their rooms while it keeps asking."""
    now = time.time()
    await container_repo.touch_browser_input(owner, now, field="human_view_at")
    if frames:
        await container_repo.touch_browser_input(owner, now, field="human_frames_at")
    out = await view(owner, user)
    if frames and out["mode"] == "agent":
        # Only while an agent browses: the bridge is live then, so this never wakes a microVM.
        try:
            await asyncio.wait_for(
                container_rpc.call(owner, "browser.frames", {"ttl": FRAMES_TTL_SECONDS}), FRAMES_RPC_TIMEOUT_S
            )
        except Exception as exc:  # the next agent acquire carries framesAt anyway
            logger.info("browser frames ask failed for %s: %s", owner, type(exc).__name__)
    if out["active"] or out["mode"] == "transition" or out.get("endedReason") == "stopped":
        return out
    return await start(owner, user)


#: What the live view's display server grants a web client (dev, 2026-09-26).
VIEWPORT_MIN = (640, 480)
VIEWPORT_MAX = (1920, 1080)
VIEWPORT_TIMEOUT_S = 15


@contextlib.asynccontextmanager
async def _devtools(session_id: str, failure: str):
    """One short CDP connection over the session's automation stream. `call` answers a
    command's result; `session` addresses an attached target (flat mode)."""
    async with websockets.connect(await _cdp(session_id), max_size=None, open_timeout=10) as ws:
        ids = itertools.count(1)

        async def call(method: str, params: dict | None = None, session: str | None = None) -> dict:
            call_id = next(ids)
            message = {"id": call_id, "method": method, "params": params or {}}
            await ws.send(json.dumps(message | ({"sessionId": session} if session else {})))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == call_id:
                    if "error" in message:
                        raise BrowserError(failure)
                    return message.get("result") or {}

        yield call


async def _fit_windows(session_id: str, width: int, height: int) -> None:
    """Size every browser window to the live view's display over the automation stream.

    The session's ``viewPort`` sizes Chrome's window once, at start, and nothing moves it after:
    the display has no window manager, so a window neither maximizes nor follows a display
    resize (both checked on dev 2026-09-26). Pages then lay out at the new size, which is what
    the agent's next snapshot sees."""
    async with _devtools(session_id, "Could not resize the browser.") as call:
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


AGENT_BUSY = "An agent is using the browser. Try again in a moment."
FILL_TIMEOUT_S = 10
#: Runs in the page: the visible sign-in form's username (the last text-like field before the
#: password, as the helper's logins.js finds it) and password. Returns which exist; `focus`
#: focuses one and selects its text so the typed value replaces it. Never sees a secret.
_FIELDS_JS = """(focus) => {
  const shown = (el) => el.offsetParent !== null && !el.disabled && !el.readOnly;
  const password = [...document.querySelectorAll('input[type="password"]')].find(shown);
  const scope = password?.form || document;
  const texts = [...scope.querySelectorAll('input[type="email"], input[type="text"], input[type="tel"], input:not([type])')]
    .filter(shown).filter((el) => password && el.compareDocumentPosition(password) & Node.DOCUMENT_POSITION_FOLLOWING);
  const fields = {username: texts.at(-1), password};
  const el = fields[focus];
  if (el) { el.focus(); el.select(); }
  return {username: !!fields.username, password: !!password, visible: document.visibilityState === "visible", focused: document.hasFocus()};
}"""


async def _type_login(owner: str, session_id: str, login: dict) -> None:
    from core.services import website_logins

    async with _devtools(session_id, "Couldn't fill the login. Try again.") as call:

        async def fields(page: str, focus: str | None = None) -> dict:
            got = await call(
                "Runtime.evaluate",
                {"expression": f"({_FIELDS_JS})({json.dumps(focus)})", "returnByValue": True},
                page,
            )
            return (got.get("result") or {}).get("value") or {}

        # The active tab: the visible web page, the focused one if several windows show one.
        pages = []
        for target in (await call("Target.getTargets")).get("targetInfos", []):
            if target.get("type") == "page" and target.get("url", "").startswith(("http://", "https://")):
                page = (await call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))[
                    "sessionId"
                ]
                found = await fields(page)
                if found.get("visible"):
                    pages.append((not found.get("focused"), target["url"], page, found))
        if not pages:
            raise BrowserError("Open the sign-in page in the browser first.")
        _, url, page, found = min(pages, key=lambda p: p[0])
        # The tab's real address, from the browser -- never the page's or the helper's word.
        try:
            origin = website_logins.normalize_origin(url)
        except website_logins.LoginError:
            origin = None
        if origin != login["origin"]:
            raise BrowserError(f"That login is for {website_logins.host_of(login['origin'])}, not this page.")
        if not found.get("password"):
            raise BrowserError("There's no sign-in form on this page.")
        secret = await website_logins.resolve_login(owner, login["id"], agent_id="browser")
        if not secret:
            raise BrowserError("That login is no longer saved.")
        # insertText is a real input event, so React-style forms see the value.
        for field in ("username", "password") if found.get("username") else ("password",):
            await fields(page, field)
            await call("Input.insertText", {"text": secret[field]}, page)


async def fill_login(owner: str, login_id: str) -> dict:
    """A person's "Fill" in the shared browser: the backend types the saved login into the
    active tab over the automation stream, only if that tab is on the login's own origin. The
    password goes browser-ward only as keystrokes, never in a response."""
    from core.services import website_logins

    row = await _row(owner)
    state = row.get("browser_state") or {}
    if _mode(state) == "agent":
        raise BrowserError(AGENT_BUSY)
    if _mode(state) != "idle" or not await _owned(owner, row, state):
        raise BrowserError("The browser isn't ready. Try again in a moment.")
    login = next((x for x in await website_logins.list_logins(owner) if x["id"] == login_id), None)
    if not login:
        raise BrowserError("That login is no longer saved.")
    try:
        await asyncio.wait_for(_type_login(owner, state["session_id"], login), FILL_TIMEOUT_S)
    except websockets.exceptions.InvalidStatus as exc:
        # AWS admits one automation connection per session: 429 = a run holds it.
        if exc.response.status_code == 429:
            raise BrowserError(AGENT_BUSY) from None
        raise
    return {"ok": True}


# ---- website sign-ins: which sites the saved state is signed in to, and signing one out ------
# A site is a registrable domain (website_logins.site_of). The saved state is the snapshot at
# ``saved_key``: {"cookies": [{name, value, domain, path, expires, httpOnly, ...}],
# "origins": [{"origin", "localStorage": [...]}]}. Cookie values are never logged or returned.

SIGN_OUT_TIMEOUT_S = 15
SIGN_OUT_FAILED = "Couldn't sign out in the running browser. Try again."
#: What a site's pages keep beside cookies. Cleared for the live session; of these the saved
#: state only carries localStorage.
_SITE_STORAGE = "local_storage,session_storage,indexeddb,service_workers,cache_storage"


def _cookie_site(cookie: object) -> str | None:
    from core.services import website_logins

    if isinstance(cookie, dict) and isinstance(cookie.get("domain"), str):
        return website_logins.site_of(cookie["domain"])
    return None


def _origin_site(origin: object) -> str | None:
    from core.services import website_logins

    return website_logins.site_of(origin) if isinstance(origin, str) else None


def _sites(snapshot: dict, now: float) -> set[str]:
    """Sites the snapshot holds a sign-in for. ponytail: "signed in" is "an unexpired httpOnly
    cookie" -- a session cookie is nearly always httpOnly, while consent and analytics cookies
    (set from page script) are not, so those sites stay off the list. A site that keeps its
    session only in localStorage or a script-readable cookie is missed; a tracker with an
    httpOnly cookie is listed. Upgrade path: a per-site allow/deny list, or a live probe."""
    found = set()
    for cookie in snapshot.get("cookies") or []:
        site = _cookie_site(cookie)
        expires = cookie.get("expires") if site else None
        expired = isinstance(expires, (int, float)) and 0 < expires < now  # <= 0: a session cookie
        if site and cookie.get("httpOnly") is True and not expired:
            found.add(site)
    return found


async def signed_in_sites(owner: str) -> set[str]:
    """From the last save, not the live session: a save follows every agent turn and a person's
    input within seconds, and the session admits only one automation connection."""
    key = (await container_repo.get_browser_state(owner) or {}).get("saved_key")
    if not key or not key.startswith(f"browser-state/{owner}/"):
        return set()
    return _sites(await _read_snapshot(key), time.time())


def _without_site(snapshot: dict, site: str) -> dict:
    """The snapshot minus one site's cookies and localStorage; everything else untouched."""
    return {
        **snapshot,
        "cookies": [c for c in snapshot["cookies"] if _cookie_site(c) != site],
        "origins": [
            o for o in snapshot["origins"] if not (isinstance(o, dict) and _origin_site(o.get("origin")) == site)
        ],
    }


async def _saved_snapshot(owner: str, state: dict) -> dict | None:
    """The committed save, or None when there is none. One that cannot be read or is not a
    snapshot raises: guessing "empty" would sign every site out."""
    previous = state.get("saved_key")
    if not previous:
        return None
    if not previous.startswith(f"browser-state/{owner}/"):
        raise BrowserError("Saved browser state is unavailable.")
    got = await run_in_thread(
        boto3.client("s3", region_name=settings.AWS_REGION).get_object, Bucket=settings.OWNER_STATE_BUCKET, Key=previous
    )
    snapshot = json.loads(await run_in_thread(got["Body"].read))
    if not (
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("cookies"), list)
        and isinstance(snapshot.get("origins"), list)
    ):
        raise BrowserError("Saved browser state is unavailable.")
    return snapshot


async def _sign_out_snapshot(owner: str, state: dict, snapshot: dict, site: str) -> dict:
    """Write the save without ``site`` under this operation's own key (a committed object is
    never rewritten); answers the pointer fields to commit."""
    previous = state["saved_key"]
    s3 = boto3.client("s3", region_name=settings.AWS_REGION)
    key = f"browser-state/{owner}/{state['generation']}.json"
    await run_in_thread(
        s3.put_object,
        Bucket=settings.OWNER_STATE_BUCKET,
        Key=key,
        Body=json.dumps(_without_site(snapshot, site), separators=(",", ":")).encode(),
        ContentType="application/json",
    )
    # Each extension's own storage rides beside the save (_ext_key): carry it to the new key.
    # Only a confirmed-missing source may be skipped: dropping it deletes it for good.
    try:
        await run_in_thread(
            s3.copy_object,
            Bucket=settings.OWNER_STATE_BUCKET,
            Key=_ext_key(key),
            CopySource={"Bucket": settings.OWNER_STATE_BUCKET, "Key": _ext_key(previous)},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
            raise
    return {
        "saved_key": key,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        # The live session was signed out of the site too, so it holds this save exactly as
        # much as it held the last one; a stale session (a Mac published since) stays stale.
        "live_key": state.get("live_key") if _stale(state) else key,
    }


async def _sign_out_live(session_id: str, site: str, saved_origins: set[str]) -> None:
    """Remove the site's cookies and storage from the running browser and reload its open
    tabs. Without this the session's next autosave would publish the sign-in again: a save
    keeps the session's version of every cookie domain it changed since its base."""
    async with _devtools(session_id, SIGN_OUT_FAILED) as call:
        targets = [t for t in (await call("Target.getTargets")).get("targetInfos", []) if t.get("type") == "page"]
        scratch = None
        if not targets:  # Network.deleteCookies needs a page; the browser has none open
            scratch = (await call("Target.createTarget", {"url": "about:blank", "background": True}))["targetId"]
        try:
            sessions = {
                t["targetId"]: (await call("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}))[
                    "sessionId"
                ]
                for t in targets or [{"targetId": scratch}]
            }
            page = next(iter(sessions.values()))
            ours = [c for c in (await call("Storage.getCookies")).get("cookies", []) if _cookie_site(c) == site]
            for cookie in ours:
                params = {k: cookie[k] for k in ("name", "domain", "path", "partitionKey") if cookie.get(k) is not None}
                await call("Network.deleteCookies", params, page)
            # A cookie still there with the value it had was not deleted (one a page set again
            # has a new value, and the reload below signs that page out).
            before = {(c.get("name"), c.get("domain"), c.get("path"), c.get("value")) for c in ours}
            after = (await call("Storage.getCookies")).get("cookies", [])
            if any((c.get("name"), c.get("domain"), c.get("path"), c.get("value")) in before for c in after):
                raise BrowserError(SIGN_OUT_FAILED)
            open_tabs = [t for t in targets if _origin_site(t.get("url")) == site]
            origins = saved_origins | {f"https://{c['domain'].lstrip('.')}" for c in ours}
            for tab in open_tabs:
                parts = tab["url"].split("/")
                origins.add("/".join(parts[:3]))
            for origin in sorted(origins):
                with contextlib.suppress(BrowserError):  # one origin's storage; the cookies are gone
                    await call("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": _SITE_STORAGE}, page)
            for tab in open_tabs:
                with contextlib.suppress(BrowserError):
                    await call("Page.reload", {}, sessions[tab["targetId"]])
        finally:
            if scratch:
                with contextlib.suppress(BrowserError):
                    await call("Target.closeTarget", {"targetId": scratch})


async def sign_out(owner: str, site: str, *, actor: str) -> None:
    """Sign the workspace browser out of one site: its cookies and localStorage leave the saved
    state (so the next start, cloud or Mac, loads without them) and the running session.

    One fenced operation, like a save: `_begin` moves the pointer to "transition", so no agent
    acquire, autosave or Mac publish runs beside it. The live session goes first: if that fails
    nothing is committed and nothing has changed. While an agent run holds the browser this
    refuses (AGENT_BUSY): the run owns the one automation connection AWS admits, and editing
    the save alone would be undone by that run's turn-end save whenever the site refreshed its
    cookies meanwhile."""
    from core.services import website_logins

    # Never a public suffix or a bare word: every cookie match below is "this cookie's own site
    # is `site`", so a real site can only ever match itself and its subdomains.
    if not website_logins.is_site(site):
        raise BrowserError("Enter a site like example.com.")
    row = await container_repo.get_by_owner_id(owner, consistent=True)
    old = await container_repo.get_browser_state(owner)
    if not row or not old:
        return  # no browser has ever run here: nothing is signed in
    mode = _mode(old)
    if mode == "transition":
        raise BrowserError("Browser is changing. Try again shortly.")
    if mode == "agent":
        raise BrowserError(AGENT_BUSY)
    # An errored session is replaced by the next start, which restores the save: not "live".
    live = bool(
        mode == "idle" and row.get("browser_profile_id") and old.get("session_id") and await _owned(owner, row, old)
    )
    if not live and not old.get("saved_key"):
        return
    state = await _begin(owner, old, "save")
    try:
        snapshot = await _saved_snapshot(owner, state)
        if live:
            saved_origins = {
                o["origin"]
                for o in (snapshot or {}).get("origins", [])
                if isinstance(o, dict) and _origin_site(o.get("origin")) == site
            }
            try:
                await asyncio.wait_for(_sign_out_live(state["session_id"], site, saved_origins), SIGN_OUT_TIMEOUT_S)
            except websockets.exceptions.InvalidStatus as exc:
                # AWS admits one automation connection per session: 429 = a run holds it.
                raise BrowserError(AGENT_BUSY if exc.response.status_code == 429 else SIGN_OUT_FAILED) from None
        saved = await _sign_out_snapshot(owner, state, snapshot, site) if snapshot is not None else {}
        # Back to what it was (idle, stopped or errored), on the operation's generation.
        committed = await _publish(owner, state, {**old, "generation": state["generation"], **saved})
        if saved:
            await _delete_snapshot(old["saved_key"])
    except Exception as exc:
        await _discard_uncommitted(owner, state)
        if (await container_repo.get_browser_state(owner)) == state:
            await container_repo.compare_browser_state(owner, state, old)
        if isinstance(exc, BrowserError):
            raise
        logger.warning("browser sign-out failed for %s: %s", owner, type(exc).__name__)
        raise BrowserError("Couldn't sign out. Try again.") from None
    logger.info(
        "browser.sign_out owner=%s site=%s live=%s saved=%s actor=%s",
        owner,
        site,
        live,
        bool(committed.get("saved_key")),
        actor,
    )


async def record_input(owner: str) -> dict:
    """The panel's throttled "a person is using the stream" signal: the agent yields to it,
    and the autosave sweep saves after it goes quiet. It grants and blocks nothing."""
    await container_repo.touch_browser_input(owner, time.time())
    return {"ok": True}


INPUT_TIMEOUT_S = 10
NOT_CASTING = "That page isn't live here any more. Take live control to use the browser."


async def send_input(owner: str, event: dict) -> dict:
    """A watcher's click, scroll or keys on an agent's live frames: typed into that run's tab by
    the bridge (which holds the one automation connection while an agent browses). Counts as a
    person's input, so the agent yields to it like to the live view."""
    await container_repo.touch_browser_input(owner, time.time())
    if _mode(await container_repo.get_browser_state(owner)) != "agent":
        raise BrowserError(NOT_CASTING)
    try:
        await asyncio.wait_for(container_rpc.call(owner, "browser.input", event), INPUT_TIMEOUT_S)
    except RuntimeError:  # the bridge answered {ok:false}: that tab is not being cast
        raise BrowserError(NOT_CASTING) from None
    return {"ok": True}


async def acquire_agent(owner: str, run_id: str, agent_id: str | None = None) -> dict:
    # Only another RUN can hold the browser (one automation connection per session); a person
    # never does. Wait briefly for that run's turn to release it. Task VMs share this one lease per
    # workspace (contracts §14.11): another VM's run gets the busy answer and the model retries.
    # ponytail: busy-retry, not FIFO -- a queue of waiting runs if retries starve one.
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
        # Someone without the live view is watching: the bridge casts this run's tab.
        "framesAt": await container_repo.get_browser_input_at(owner, "human_frames_at"),
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


_restarts: set[asyncio.Task] = set()


async def restart_soon(owner: str) -> bool:
    """Save, stop and start the running browser in the background, so a new extension loads
    (extensions are fixed at session start). False -- nothing restarts -- when no session is
    running (the next start loads it) or an agent run is using it (its turn is not cut off)."""
    row = await _row(owner)
    old = row.get("browser_state") or {}
    if _mode(old) != "idle" or not old.get("session_id") or not await _owned(owner, row, old):
        return False

    async def run() -> None:
        try:
            await _save(owner, old, stop=True, reason="restart")
            await _claim(owner, None, start_only=True)
        except Exception as exc:  # the panel shows the stopped/errored browser; Start recovers
            logger.warning("browser restart failed for %s: %s", owner, type(exc).__name__)

    task = asyncio.create_task(run())
    _restarts.add(task)
    task.add_done_callback(_restarts.discard)
    return True


async def release_agent(owner: str, run_id: str, generation: str) -> dict:
    row = await _row(owner)
    old = row.get("browser_state") or {}
    if old.get("generation") != generation or old.get("controller_id") != run_id or old.get("mode") != "agent":
        raise BrowserError("Browser control changed. Try again.")
    if not await _owned(owner, row, old):
        raise BrowserError("The browser session expired. Its last completed save is available on restart.")
    await _save(owner, old)
    return {"ok": True}


async def release_for_run(owner: str, run_id: str) -> bool:
    """Free the lease `run_id` holds -- its task VM was stopped (or died) before the turn's own
    release. False, and nothing changes, when that run is not the controller."""
    state = await container_repo.get_browser_state(owner) or {}
    if _mode(state) != "agent" or state.get("controller_id") != run_id:
        return False
    try:
        await release_agent(owner, run_id, state["generation"])
    except BrowserError:
        return False  # changed hands, or the session expired: nothing of this run's left to free
    return True


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
    if not state:  # never started
        return {**browser_sessions._INACTIVE, "mode": "idle", "generation": None}
    got = await _owned(owner, row, state)
    mode = _mode(state)
    out = {
        **browser_sessions._INACTIVE,
        "sessionId": state.get("session_id"),
        "generation": state["generation"],
        "mode": mode,
        # The agent using it, so the panel can name it; nobody else ever "holds" it.
        "agentId": state.get("agent_id") if mode == "agent" else None,
        "savedAt": state.get("saved_at"),
        "error": state.get("error"),
        # Why the browser is not running: "stopped" (a person's Stop), "idle" (our stop),
        # "restart" (a new extension) or "expired" (it ended at AWS).
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
