"""A paired Mac's agent Chrome, leased to a run (spec §6).

The bridge `acquire`s the Mac for a room or call run and gets a ticketed CDP door
(`mac_cdp`). The backend decides and fences the workspace sign-in sync (first owner wins,
`load_id`), but it never runs it: it answers sync steps with presigned URLs, the bridge runs them
with its own `browser_state.py` over the run's pipe (one sync engine), and reports `synced`.
Unpair and member removal fence the row and wipe the Mac's profile (`clear_device`).

One row per native Mac device, `__macchrome__#<device>` in the containers table (a reserved key
every scan skips). Every row write is conditional on the `load_id` it read.

Never log a cookie, a URL, a key or a ticket: owner ids and exception class names only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services import browser_control, desktop_execution, mac_cdp, task_address
from core.services.desktop_execution import DesktopError
from core.services.node_broker import BUSY_MESSAGE, CAST_COMMAND

logger = logging.getLogger(__name__)

ROW_PREFIX = "__macchrome__#"
CDP_COMMAND = "browser.cdp.v1"
WIPE_COMMAND = "browser.wipe.v1"
ENSURE_TIMEOUT_S = 27  # >= the Mac's own worst launch (LAUNCH_TIMEOUT 10 + START_TIMEOUT 15)
WIPE_TIMEOUT_S = 20
#: The bridge reaches acquire through API Gateway (30 s cap): acquire answers inside its caller's
#: budget, at most this. A cold `ensure` keeps running in the background and acquire answers
#: STARTING before it claims anything, so no claim is written that nobody receives.
ACQUIRE_BUDGET_S = 25
DECIDE_RESERVE_S = 4  # the claim, the ticket and the device name after the budget check
FORWARD_TIMEOUT_S = 28  # < API Gateway's 30 s: an origin never outlives its caller
LEASE_SECONDS = 300
CLAIM_SECONDS = 180  # the bridge's sync must report back within this
DEFER_SECONDS = 60
CLEAR_SECONDS = 15
BUSY_RETRY_SECONDS = 1  # a cloud start/save holds the pointer: retry the publish this often, twice
STATE_URL_SECONDS = 180  # browser_control._state_url's presign lifetime
STARTING = {"status": "starting"}
NATIVE_CLIENT = "openclaw-macos"
INTERNAL_PATH = "/api/v1/desktop-execution/internal"
WIPE_PATH = "/api/v1/desktop-execution/internal/mac-wipe"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_clock = time.time

MESSAGES = {
    "desktop_disabled": "Agents can't use a Mac's browser in this workspace yet.",
    "desktop_forbidden": "Only the Mac of the person who asked can be used for this.",
    "desktop_generation_lost": "That Mac is offline or just reconnected. Try again in a moment.",
    "desktop_update_required": "Update the Lightbulb desktop app on that Mac to let agents use its browser.",
    "browser_off": (
        "Browser access on that Mac was turned off. "
        "Its owner can turn it back on in the Lightbulb desktop app's Settings."
    ),
    # Unpaired, or its member removed from the workspace.
    "browser_access_ended": "Browser access on that Mac was turned off.",
    # A channel cap or byte budget on the Mac's socket (the door's 4503).
    "mac_browser_busy": BUSY_MESSAGE,
    "chrome_missing": "Google Chrome isn't installed on that Mac. Install it there, or use the cloud browser.",
    "chrome_start_failed": "Chrome on that Mac didn't start. Try again in a moment.",
    "browser_profile_held_by_another_workspace": (
        "This Mac's agent browser holds another workspace's sign-ins. "
        "Unpair it from that workspace first, or use the cloud browser."
    ),
    "starting": "The Mac's browser is loading the workspace's sign-ins; retry this browser action in a few seconds.",
    "no_mac": "Your Mac isn't connected. Open the Lightbulb desktop app on it, or use the cloud browser.",
    "choose_mac": (
        "More than one of your Macs is connected. Say which one (lightbulb_desktop devices), "
        "then use_browser with its deviceId."
    ),
}
SAVE_FAILED_NOTE = "Couldn't save the sign-ins from the Mac browser this time."
#: Any other code (a peer or dependency failure) still reaches the agent with a message.
GENERIC_MESSAGE = "The Mac's browser isn't available right now. Try again, or use the cloud browser."


def _err(code: str, status: int = 409) -> DesktopError:
    return DesktopError(code, status, MESSAGES.get(code, GENERIC_MESSAGE))


def _with_message(exc: DesktopError) -> DesktopError:
    if not exc.message:
        exc.message = MESSAGES.get(exc.code, GENERIC_MESSAGE)
    return exc


async def _held(holder: str | None) -> DesktopError:
    """Held elsewhere, naming the holding workspace (the Mac's owner pairs it to both)."""
    name = None
    if holder:
        with contextlib.suppress(Exception):
            from core.services.clerk_admin import get_organization

            name = ((await get_organization(holder)) or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return _err("browser_profile_held_by_another_workspace")
    name = name.strip()[:64]
    return DesktopError(
        "browser_profile_held_by_another_workspace",
        409,
        f"This Mac's agent browser holds the sign-ins of the {name} workspace. "
        f"Unpair it from {name} first, or use the cloud browser.",
    )


def _meter(kinds, result: str) -> None:
    for kind in kinds:
        with contextlib.suppress(Exception):
            put_metric("mac_browser.sync", dimensions={"kind": kind, "result": result})


class LoadSuperseded(Exception):
    """Another claim or fence changed the row's load_id since it was read."""


# ---- rows -----------------------------------------------------------------------------------


async def _row(device: str) -> dict:
    return await container_repo.get_by_owner_id(ROW_PREFIX + device, consistent=True) or {}


async def _write(device: str, row: dict, changes: dict, *, fence: bool = False) -> str:
    """Put the whole row, conditional on the load_id read. `fence` starts a new load_id."""
    seen = row.get("load_id")
    item = {**row, **changes, "owner_id": ROW_PREFIX + device}
    item["load_id"] = secrets.token_hex(16) if fence else (seen or secrets.token_hex(16))
    condition = (
        {"ConditionExpression": "load_id = :seen", "ExpressionAttributeValues": {":seen": seen}}
        if seen
        else {"ConditionExpression": "attribute_not_exists(load_id)"}
    )
    try:
        await asyncio.to_thread(container_repo._get_table().put_item, Item=item, **condition)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise LoadSuperseded() from None
        raise
    row.clear()
    row.update(item)
    return item["load_id"]


def _claim_live(row: dict, now: float) -> bool:
    return bool(row.get("loading_owner")) and float(row.get("loading_expires") or 0) > now


async def _still_paired(owner: str, device: str) -> bool:
    row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
    entry = (row.get("paired_devices") or {}).get(device)
    return isinstance(entry, dict) and not entry.get("revoking")


async def _row_holder(row: dict, device: str, now: float) -> str | None:
    """An unexpired claim's owner, else a still-paired loaded owner, else None."""
    if _claim_live(row, now):
        return row["loading_owner"]
    owner = row.get("loaded_owner")
    return owner if owner is not None and await _still_paired(owner, device) else None


_CLEARED = {"loading_owner": None, "loading_expires": None, "pending": None}


def _fence_changes(row: dict) -> tuple[dict, list]:
    """A revocation or stale-holder fence, and the keys it may delete. The profile stays dirty
    (`wipe_profile`) until the Mac reports a new profile id: a failed or offline wipe must never
    let another workspace go Fresh on the old sign-ins. `pending.saved_out` is left orphaned: a
    save in flight may still publish it (a deleted key published would lose the workspace save)."""
    pending = row.get("pending") or {}
    changes = _CLEARED | {
        "loaded_owner": None,
        "base_key": None,
        "synced_key": None,
        "leases": {},
        "wipe_profile": row.get("profile_id") or row.get("wipe_profile"),
    }
    return changes, [row.get("base_key"), pending.get("base_out"), pending.get("scratch")]


# ---- devices --------------------------------------------------------------------------------


def _native_of(row: dict, device_id: str) -> str | None:
    """The pair's native (Mac app) device id, for either device of a reciprocal pair."""
    from core.services.desktop_pair import paired_physical_mac

    devices = row.get("paired_devices") or {}
    entry = devices.get(device_id)
    pair = paired_physical_mac(row, entry.get("userId"), device_id) if isinstance(entry, dict) else None
    if pair is None:
        return None
    native = next(p for p in pair if p.get("clientId") == NATIVE_CLIENT)
    return next((key for key, value in devices.items() if value == native), None)


async def _ready(owner: str, device: str) -> dict | None:
    """The device's live ready row: generation, declared commands and member, or None."""
    from core.services.connection_service import ConnectionService

    connections = ConnectionService()
    got = await asyncio.to_thread(
        connections._client.get_item,
        TableName=connections.table_name,
        Key={"connectionId": {"S": connections.desktop_ready_id(owner, device)}},
        ConsistentRead=True,
    )
    item = got.get("Item") or {}
    generation = item.get("generation", {}).get("S", "")
    if not generation or int(item.get("ttl", {}).get("N", "0")) <= int(_clock()):
        return None
    return {
        "generation": generation,
        "commands": {v["S"] for v in item.get("commands", {}).get("L", []) if "S" in v},
        "userId": item.get("userId", {}).get("S", ""),
    }


async def _candidates(auth: dict, row: dict) -> list[tuple[str, str]]:
    """Every ready native Mac this run may use (the requester's), with its ready generation."""
    found = []
    for key, entry in (row.get("paired_devices") or {}).items():
        if (
            not isinstance(entry, dict)
            or entry.get("clientId") != NATIVE_CLIENT
            or entry.get("revoking")
            or _native_of(row, key) != key
            or entry.get("userId") != auth["memberId"]
        ):
            continue
        ready = await _ready(auth["ownerId"], key)
        if ready is not None:
            found.append((key, ready["generation"]))
    return found


async def _pick(auth: dict, device_id: str | None) -> tuple[str, str]:
    """(native device, ready generation). The generation always comes from the ready row, so a
    Mac that slept and reconnected is picked up by the next acquire."""
    owner = auth["ownerId"]
    row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
    if device_id:
        native = _native_of(row, device_id)
        if native is None or row["paired_devices"][native].get("revoking"):
            raise _err("browser_access_ended", 403)  # unpaired, or its member removed
        if row["paired_devices"][native].get("userId") != auth["memberId"]:
            raise _err("desktop_forbidden", 403)
        ready = await _ready(owner, native)
        if ready is None:
            raise _err("desktop_generation_lost")
        return native, ready["generation"]
    found = await _candidates(auth, row)
    if not found:
        raise _err("no_mac")
    if len(found) > 1:
        raise _err("choose_mac")
    return found[0]


async def _target(auth: dict, native: str, generation: str) -> dict:
    try:
        target = await desktop_execution._resolve(
            auth, {"deviceId": native, "expectedConnectionGeneration": generation, "command": CDP_COMMAND}
        )
    except DesktopError as exc:
        if exc.code in {"desktop_not_ready", "desktop_generation_stale"}:
            raise _err("desktop_generation_lost") from None
        if exc.code != "desktop_forbidden":
            raise _with_message(exc)
    else:
        if CAST_COMMAND not in target["commands"]:
            raise _err("desktop_update_required")
        return target
    # The command is not effective: tell an old app from a switched-off Browser (spec §4.1).
    entry = (
        (await container_repo.get_by_owner_id(auth["ownerId"], consistent=True) or {}).get("paired_devices") or {}
    ).get(native)
    if not isinstance(entry, dict) or entry.get("revoking"):
        raise _err("browser_access_ended", 403)
    commands = ((await _ready(auth["ownerId"], native)) or {}).get("commands", set())
    if WIPE_COMMAND not in commands:
        raise _err("desktop_update_required")
    if CDP_COMMAND not in commands:
        raise _err("browser_off")
    raise _err("desktop_forbidden", 403)


async def _invoke(target: dict, command: str, params: dict, seconds: float) -> dict:
    """One node invoke; a transport failure reads as a failed result (its class name only)."""
    try:
        return await asyncio.wait_for(
            desktop_execution._dispatch(
                target,
                {"command": command, "params": params, "executionDeadlineMs": int(seconds * 1000)},
                str(uuid.uuid4()),
            ),
            seconds + 2,
        )
    except DesktopError as exc:
        raise _with_message(exc)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": {"code": type(exc).__name__}}


async def _forward_mac(address: str, body: dict, path: str = INTERNAL_PATH, timeout: float = FORWARD_TIMEOUT_S) -> dict:
    """Forward to the task holding the Mac's socket. Unlike desktop_execution._forward it waits
    past `ensure`, and keeps the holder's message. The address comes from a ready row."""
    import httpx

    from core.config import settings

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                address + path, json=body, headers={"x-lightbulb-ws-internal": settings.WS_INTERNAL_SECRET}
            )
    except httpx.HTTPError:
        raise _err("desktop_peer_unavailable", 503) from None
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        detail = detail if isinstance(detail, dict) else {}
        code = detail.get("code") if isinstance(detail.get("code"), str) else "desktop_peer_unavailable"
        message = detail.get("message") if isinstance(detail.get("message"), str) else None
        raise DesktopError(code, response.status_code, message or MESSAGES.get(code, GENERIC_MESSAGE))
    return response.json()


# ---- background work ------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()
_ensures: dict[tuple, asyncio.Task] = {}  # (device, generation) -> the launch in flight
_locks: dict[str, asyncio.Lock] = {}  # ponytail: one per device ever seen, never evicted


def _done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("mac browser background task failed: %s", type(task.exception()).__name__)


def _spawn(device: str, make) -> None:
    """Run `make()` after the request, serialized per device in this task."""

    async def run():
        async with _locks.setdefault(device, asyncio.Lock()):
            await make()

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_done)


async def _delete(*keys) -> None:
    for key in keys:
        if key:
            await browser_control._delete_snapshot(key)


async def _url(key: str, action: str) -> str:
    return await asyncio.to_thread(browser_control._state_url, key, action)


# ---- acquire --------------------------------------------------------------------------------


def _saved_key(owner: str, state: dict | None) -> str | None:
    key = (state or {}).get("saved_key")
    if key and not key.startswith(f"browser-state/{owner}/"):
        raise _err("browser_state_unavailable")
    return key or None


def _new_key(owner: str, kind: str) -> str:
    return f"browser-state/{owner}/{kind}-{secrets.token_hex(16)}.json"


async def _save_step(owner: str, row: dict, saved: str | None, saved_out: str, base_out: str) -> dict:
    step = {"action": "save"}
    if saved:
        step["previousStateUrl"] = await _url(saved, "get_object")
    step |= {
        "baseStateUrl": await _url(row["base_key"], "get_object"),
        "stateUrl": await _url(saved_out, "put_object"),
        "baseOutUrl": await _url(base_out, "put_object"),
    }
    return step


async def _decide(owner: str, run_id: str, native: str, profile_id: str, target: dict) -> dict:
    """Claim, relabel or lease the row: STARTING, or {"sync": <sync or None>}."""
    for _ in range(3):
        now = _clock()
        row = await _row(native)
        if _claim_live(row, now):
            if row["loading_owner"] != owner:
                raise await _held(row["loading_owner"])
            return STARTING  # this workspace's sync is running in some bridge
        holder = await _row_holder(row, native, now)
        if holder not in (None, owner):
            raise await _held(holder)
        if row.get("wipe_profile") and row["wipe_profile"] == profile_id:
            # A fenced profile the Mac has not wiped yet (offline, or the wipe failed): held.
            with contextlib.suppress(Exception):  # a wipe that never lands shows up here
                put_metric("mac_browser.dirty_hold", dimensions={})
            _spawn(native, lambda: _stale_wipe(target))
            return STARTING
        loaded = row.get("loaded_owner")
        if loaded not in (None, owner) and row.get("profile_id") == profile_id:
            # A stale holder's sign-ins are still in this profile: fence, wipe, retry later.
            changes, keys = _fence_changes(row)
            try:
                await _write(native, row, changes, fence=True)
            except LoadSuperseded:
                continue
            await _delete(*keys)
            _spawn(native, lambda: _stale_wipe(target))
            return STARTING
        # This run already holds a live lease: a renewal (every browser call acquires), not a start.
        # A lease that expired mid-run (no browser call for LEASE_SECONDS) counts as a start again,
        # so that run's next call gets a full [save, load] sync.
        renewing = float((row.get("leases") or {}).get(run_id) or 0) > now
        leases = {k: v for k, v in (row.get("leases") or {}).items() if float(v) > now and k != run_id}
        others = bool(leases)
        leases[run_id] = int(now + LEASE_SECONDS)
        state = await container_repo.get_browser_state(owner)
        saved = _saved_key(owner, state)
        base = row.get("base_key")
        same = loaded == owner and row.get("profile_id") == profile_id
        sync, changes, fence, replaced = None, {"leases": leases}, False, None
        if same and not base and float(row.get("deferred_until") or 0) > now:
            pass  # Deferred: the last load failed moments ago; browse without a sync
        elif not same or not base:
            base_out = _new_key(owner, "macbase")
            if saved:
                steps = [
                    {
                        "action": "load",
                        "stateUrl": await _url(saved, "get_object"),
                        "baseOutUrl": await _url(base_out, "put_object"),
                    }
                ]
            else:
                steps = [{"action": "base", "stateUrl": await _url(base_out, "put_object")}]
            pending = {"steps": [steps[0]["action"]], "expected": saved, "base_out": base_out, "saved_out": None}
            sync, fence, replaced = {"steps": steps}, True, base
            changes = {
                "loaded_owner": owner,
                "profile_id": profile_id,
                "base_key": None,
                "synced_key": None,
                "deferred_until": None,
                "loading_owner": owner,
                "loading_expires": int(now + CLAIM_SECONDS),
                "pending": pending,
                "leases": leases if same else {run_id: leases[run_id]},
            }
        elif not others and (not renewing or row.get("synced_key") != saved):
            # A run's first acquire on the profile this workspace holds saves before it loads,
            # even when the workspace save has not moved: the person may have signed in in the
            # "Lightbulb agent" window between runs, and that must reach the workspace now.
            saved_out, scratch, base_out = (
                _new_key(owner, "mac"),
                _new_key(owner, "macbase"),
                _new_key(owner, "macbase"),
            )
            steps = [
                await _save_step(owner, row, saved, saved_out, scratch),
                {
                    "action": "load",
                    "stateUrl": await _url(saved_out, "get_object"),
                    "baseOutUrl": await _url(base_out, "put_object"),
                },
            ]
            pending = {
                "steps": ["save", "load"],
                "expected": saved,
                "base_out": base_out,
                "saved_out": saved_out,
                "scratch": scratch,
            }
            sync, fence = {"steps": steps}, True
            changes |= {"loading_owner": owner, "loading_expires": int(now + CLAIM_SECONDS), "pending": pending}
        if row.get("wipe_profile"):
            changes["wipe_profile"] = None  # the Mac reported a new profile id: the wipe happened
        try:
            load_id = await _write(native, row, changes, fence=fence)
        except LoadSuperseded:
            continue
        if replaced:
            await _delete(replaced)
        if sync is not None:
            _meter(row["pending"]["steps"], "claimed")
            sync = {"syncId": load_id, **sync}
        return {"sync": sync}
    return STARTING  # three claims lost in a row: another sync is taking the row


async def _ensure(target: dict, seconds: float) -> dict | None:
    """The Mac's `ensure` (launch the agent Chrome), waited for at most `seconds`; None while it
    still runs. It keeps running past the request, shared by every acquire of that Mac meanwhile,
    so a poll after a cold launch finds Chrome warm."""
    key = (target["deviceId"], target["generation"])
    task = _ensures.get(key)
    if task is None:
        task = asyncio.create_task(_invoke(target, CDP_COMMAND, {"action": "ensure"}, ENSURE_TIMEOUT_S))
        _ensures[key] = task

        def done(t: asyncio.Task) -> None:
            if _ensures.get(key) is t:
                del _ensures[key]
            if not t.cancelled():
                t.exception()  # retrieved: a launch nobody waited for never logs as unhandled

        task.add_done_callback(done)
    try:
        return await asyncio.wait_for(asyncio.shield(task), max(0.0, seconds))
    except TimeoutError:
        return None


async def _stale_wipe(target: dict) -> None:
    result = await _invoke(target, WIPE_COMMAND, {}, WIPE_TIMEOUT_S)
    if not result.get("ok"):
        logger.warning("mac browser stale-profile wipe failed owner=%s", target["ownerId"])


PAIR_NAME_SUFFIX = " | Lightbulb Mac"  # desktop_pair appends it to the pair route's base


def clean_name(value: object, limit: int = 32) -> str | None:
    """A person-supplied name, safe to show anywhere: no control chars or markup, bounded."""
    if not isinstance(value, str):
        return None
    value = "".join(c for c in value if c.isprintable() and c not in "<>|`").strip()[:limit].strip()
    return value or None


_first_names: dict[str, str] = {}  # ponytail: per member, never evicted; a Clerk rename shows after a restart


async def _first_name(member: str) -> str | None:
    if member not in _first_names:
        try:
            from core.services.clerk_admin import get_user

            name = clean_name(((await asyncio.wait_for(get_user(member), 3)) or {}).get("first_name"))
        except Exception:  # noqa: BLE001 -- no name; the next acquire asks again
            return None
        if name is None:
            return None
        _first_names[member] = name
    return _first_names[member]


async def _device_name(owner: str, native: str) -> str | None:
    """ "<First name>'s Mac": the pair route records the member's first name. A legacy pairing
    (its base is the user id, and nothing forces a re-pair) asks Clerk; None when it has none,
    and clients say "a Mac"."""
    row = await container_repo.get_by_owner_id(owner) or {}
    entry = (row.get("paired_devices") or {}).get(native) or {}
    name = entry.get("displayName")
    base = clean_name(name.removesuffix(PAIR_NAME_SUFFIX) if isinstance(name, str) else None)
    if base is None or base.startswith("user_"):
        member = entry.get("userId")
        base = await _first_name(member) if isinstance(member, str) and member else None
    return f"{base}'s Mac" if base else None


async def acquire(
    auth: dict, device_id: str | None, *, forwarded: bool = False, budget_s: float = ACQUIRE_BUDGET_S
) -> dict:
    """Answers within `budget_s` (the caller's remaining time): STARTING when a cold launch or a
    slow check spent it, before any claim is written."""
    deadline = _clock() + min(max(budget_s, 0.0), ACQUIRE_BUDGET_S)
    owner = auth["ownerId"]
    # Disabled first: it is the one answer every other check below would mask.
    if not desktop_execution.submissions_enabled(owner):
        raise _err("desktop_disabled", 503)
    native, generation = await _pick(auth, device_id)
    target = await _target(auth, native, generation)
    if target["taskAddress"] != task_address.own_address():
        if forwarded:
            raise _err("desktop_forward_loop")
        left = deadline - _clock()
        if left < DECIDE_RESERVE_S:
            return STARTING
        return await _forward_mac(
            target["taskAddress"],
            {"action": "mac_acquire", "auth": auth, "request": {"deviceId": native, "budgetS": round(left - 1, 3)}},
            timeout=left,
        )
    result = await _ensure(target, deadline - DECIDE_RESERVE_S - _clock())
    if result is None or _clock() > deadline - DECIDE_RESERVE_S:
        return STARTING  # Chrome is still launching (or it took the budget): the next poll is warm
    profile_id = (result.get("payload") or {}).get("profileId") if result.get("ok") else None
    if not isinstance(profile_id, str) or not _ID.fullmatch(profile_id):
        code = (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None
        raise _err(
            {
                "BROWSER_OFF": "browser_off",
                "CHROME_MISSING": "chrome_missing",
                "CAPACITY": "mac_browser_busy",
                "NodeCapacity": "mac_browser_busy",
            }.get(code, "chrome_start_failed")
        )
    decided = await _decide(owner, auth["runId"], native, profile_id, target)
    if decided == STARTING:
        return STARTING
    return {
        "deviceId": native,
        "generation": generation,
        "cdpUrl": mac_cdp.door_url(target, mac_cdp.mint(target=target, profile_id=profile_id, auth=auth)),
        "deviceName": await _device_name(owner, native),
        "humanInputAt": 0,
        # The profile this lease is for: a shared session's channel opens on it (session_browser).
        "profileId": profile_id,
        "sync": decided["sync"],
    }


# ---- release, synced ------------------------------------------------------------------------


async def _native_for(owner: str, device_id: str) -> str | None:
    return _native_of(await container_repo.get_by_owner_id(owner, consistent=True) or {}, device_id)


async def release(auth: dict, device_id: str, generation: str, *, forwarded: bool = False) -> dict:
    """Drop the run's lease; the last lease out saves the Mac back (spec §6.2). DynamoDB + S3
    only, so it never forwards (`forwarded` exists for the internal action's symmetry)."""
    owner, run_id = auth["ownerId"], auth["runId"]
    native = await _native_for(owner, device_id)
    if native is None:
        return {"sync": None}
    for _ in range(3):
        now = _clock()
        row = await _row(native)
        leases = dict(row.get("leases") or {})
        if leases.pop(run_id, None) is None:
            return {"sync": None}  # this run never leased it (or already released)
        changes, sync = {"leases": leases}, None
        others = any(float(v) > now for v in leases.values())
        if (
            not others
            and row.get("base_key")
            and row.get("loaded_owner") == owner
            and not _claim_live(row, now)
            and await _still_paired(owner, native)
        ):
            saved = _saved_key(owner, await container_repo.get_browser_state(owner))
            saved_out, base_out = _new_key(owner, "mac"), _new_key(owner, "macbase")
            sync = {"steps": [await _save_step(owner, row, saved, saved_out, base_out)]}
            changes |= {
                "loading_owner": owner,
                "loading_expires": int(now + CLAIM_SECONDS),
                "pending": {"steps": ["save"], "expected": saved, "base_out": base_out, "saved_out": saved_out},
            }
        try:
            load_id = await _write(native, row, changes, fence=sync is not None)
        except LoadSuperseded:
            continue
        if sync is None:
            return {"sync": None}
        _meter(["save"], "claimed")
        return {"sync": {"syncId": load_id, **sync}} | await _fresh_door(auth, native, row["profile_id"])
    return {"sync": None}


async def _fresh_door(auth: dict, native: str, profile_id: str) -> dict:
    """A new ticket for the release's save: the run's own ticket is single-use and may have
    expired, so a pipe that dropped would redial into a 4401. {} when the Mac is not reachable
    now (the save then runs on the pipe the run still has, or fails and is reported)."""
    try:
        ready = await _ready(auth["ownerId"], native)
        if ready is None:
            return {}
        target = await _target(auth, native, ready["generation"])
    except DesktopError:
        return {}
    ticket = mac_cdp.mint(target=target, profile_id=profile_id, auth=auth)
    return {"cdpUrl": mac_cdp.door_url(target, ticket), "generation": target["generation"]}


async def _publish_save(owner: str, expected: str | None, new_key: str) -> str:
    """CAS the workspace save from `expected` to `new_key`: "ok", "lost" (the cloud saved
    meanwhile) or "busy" (a cloud start or save holds the pointer)."""
    for attempt in range(3):
        old = await container_repo.get_browser_state(owner)
        if old and old.get("mode") == "transition":
            if attempt < 2:
                await asyncio.sleep(BUSY_RETRY_SECONDS)
            continue
        if (old or {}).get("saved_key") != expected or not await asyncio.to_thread(_exists, new_key):
            return "lost"  # the cloud saved meanwhile, or an unpair's fence removed the upload
        if expected:
            # The cloud keeps each extension's own storage beside the save; carry it over.
            # Only a confirmed-missing source may be skipped: dropping it deletes it for good.
            try:
                await asyncio.to_thread(_copy, browser_control._ext_key(expected), browser_control._ext_key(new_key))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
                    raise
        new = (
            {**old, "saved_key": new_key}
            if old
            else {
                "generation": secrets.token_hex(32),
                "mode": "idle",
                "saved_key": new_key,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        try:
            await browser_control._publish(owner, old, new)
        except browser_control.BrowserError:
            return "lost"
        if expected:
            await browser_control._delete_snapshot(expected)
        return "ok"
    return "busy"


def _exists(key: str) -> bool:
    import boto3

    from core.config import settings

    try:
        boto3.client("s3", region_name=settings.AWS_REGION).head_object(Bucket=settings.OWNER_STATE_BUCKET, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _copy(source: str, key: str) -> None:
    import boto3

    from core.config import settings

    boto3.client("s3", region_name=settings.AWS_REGION).copy_object(
        Bucket=settings.OWNER_STATE_BUCKET, Key=key, CopySource={"Bucket": settings.OWNER_STATE_BUCKET, "Key": source}
    )


async def synced(auth: dict, device_id: str, sync_id: str, ok: bool) -> dict:
    """The bridge ran a claimed sync. Only the keys recorded in the claim are ever used."""
    owner = auth["ownerId"]
    native = await _native_for(owner, device_id)
    row = await _row(native) if native else {}
    now = _clock()
    pending = row.get("pending") or {}
    kinds = list(pending.get("steps") or [])
    if row.get("loading_owner") != owner or row.get("load_id") != sync_id or not _claim_live(row, now) or not kinds:
        _meter(kinds or ["load"], "superseded")
        return {"ok": False, "superseded": True}
    scratch = [pending.get("scratch")]
    changes = dict(_CLEARED)
    if not ok:
        if kinds[0] != "save":  # a failed load/base: browse unsynced, retry after the defer
            changes |= {"base_key": None, "deferred_until": int(now + DEFER_SECONDS)}
        else:
            await _room_note(auth, sync_id)
        result, drop = "error", [pending.get("base_out"), pending.get("saved_out"), *scratch]
    else:
        result = await _publish_save(owner, pending.get("expected"), pending["saved_out"]) if "save" in kinds else "ok"
        if result == "ok":
            changes |= {
                "base_key": pending["base_out"],
                "synced_key": pending["saved_out"] if "save" in kinds else pending.get("expected"),
                "deferred_until": None,
            }
            drop = [*scratch, row.get("base_key")]
        else:
            # Lost or busy: the workspace never took the Mac's changes, so the old base (and
            # synced_key) stay and the next acquire is Stale again.
            drop = [pending.get("base_out"), pending.get("saved_out"), *scratch]
    try:
        await _write(native, row, changes)
    except LoadSuperseded:  # fenced meanwhile (unpair): a published save stays published
        await _delete(*(drop if result != "ok" else [pending.get("base_out"), *scratch]))
        _meter(kinds, "superseded")
        return {"ok": False, "superseded": True}
    await _delete(*drop)
    _meter(kinds, {"lost": "superseded"}.get(result, result))
    return {"ok": True}


async def _room_note(auth: dict, sync_id: str) -> None:
    """A failed sign-in save: one muted system line in the run's room. Never raises."""
    if auth["family"] != "room":
        return
    try:
        from core.constants import SYSTEM_ACTOR_ID
        from core.repositories import messaging_repo

        owner, room = auth["ownerId"], auth["roomId"]
        await messaging_repo.create_message(
            room_id=room,
            owner_id=owner,
            idemp_pk=messaging_repo.owner_pk(owner),
            idemp_sk=messaging_repo.agent_idemp_sk(f"mac-save-failed:{sync_id}"),
            req_hash=messaging_repo.request_hash(room, SAVE_FAILED_NOTE, None),
            author={"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"},
            text=SAVE_FAILED_NOTE,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
            silent=True,
        )
    except Exception as exc:  # noqa: BLE001 -- advisory
        logger.warning("mac browser save-failed note not posted owner=%s: %s", auth["ownerId"], type(exc).__name__)


# ---- holder check, revocation ---------------------------------------------------------------


async def check_holder(owner: str, device_id: str, profile_id: str) -> None:
    """The door's re-check: this Mac's profile still holds this workspace's sign-ins."""
    row = await _row(device_id)
    if row.get("loaded_owner") != owner or row.get("profile_id") != profile_id or row.get("wipe_profile") == profile_id:
        raise _err("browser_profile_held_by_another_workspace", 403)


async def wipe(owner: str, native: str, seconds: float, *, forwarded: bool = False) -> bool:
    """Dispatch `browser.wipe.v1` to the still-paired Mac, forwarding to its task. Best effort;
    it runs whatever the Browser grant says (the Mac wipes on Browser off too)."""
    from core.services.connection_service import ConnectionService, DesktopTargetError

    ready = await _ready(owner, native)
    if ready is None:
        return False
    try:
        target = await ConnectionService()._resolve_desktop_target(
            owner, ready["userId"], native, ready["generation"], WIPE_COMMAND, allow_revoking=True, allow_ungranted=True
        )
    except DesktopTargetError:
        return False
    if target["taskAddress"] != task_address.own_address():
        if forwarded:
            return False
        await _forward_mac(target["taskAddress"], {"ownerId": owner, "deviceId": native}, WIPE_PATH)
        return True
    return bool((await _invoke(target, WIPE_COMMAND, {}, seconds)).get("ok"))


async def clear_device(owner: str, device_id: str) -> bool:
    """Unpair / member removal (spec §6.3): fence the row, drop its keys, wipe the Mac. Never
    raises; False when nothing could be done. `device_id` is either device of the pair."""
    result = "error"
    try:
        native = await _native_for(owner, device_id)
        if native is None:
            result = "skipped"
            return False
        row = await _row(native)
        holder = await _row_holder(row, native, _clock())
        if holder not in (None, owner):
            result = "skipped"
            return True  # another workspace holds it: its row and sign-ins stay as they are
        changes, keys = _fence_changes(row)
        try:
            await _write(native, row, changes, fence=True)
        except LoadSuperseded:
            result = "skipped"
            return True  # another claim landed meanwhile: leave it to that owner
        await _delete(*keys)
        wiped = await wipe(owner, native, CLEAR_SECONDS - 3)
        result = "ok" if wiped else "error"
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("mac browser clear failed owner=%s: %s", owner, type(exc).__name__)
        return False
    finally:
        with contextlib.suppress(Exception):
            put_metric("mac_browser.clear", dimensions={"result": result})


async def clear_member(owner: str, member_id: str) -> None:
    """A removed member's paired Macs are fenced and wiped. Never raises."""
    try:
        row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("mac browser member clear failed owner=%s: %s", owner, type(exc).__name__)
        return
    for device_id, entry in (row.get("paired_devices") or {}).items():
        if isinstance(entry, dict) and entry.get("userId") == member_id and entry.get("clientId") == NATIVE_CLIENT:
            try:
                await asyncio.wait_for(clear_device(owner, device_id), CLEAR_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mac browser member clear failed owner=%s: %s", owner, type(exc).__name__)


CLEAR_MEMBER_SECONDS = 60
_clears: set[asyncio.Task] = set()


def revoke_member_soon(owner: str, member_id: str, still_current=None) -> None:
    """A removed member's Macs, in the background (the webhook never waits on a Mac): mark every
    device they paired to this workspace REVOKING (refused from then on, and a stale owner for
    the Mac profile), try to clear the workspace's sign-ins from them under one deadline, then
    unpair and disconnect them -- whether or not an offline Mac could be reached.

    Every device write is conditional on the entry still being this member's (and, for the
    removal, still REVOKING), so a re-add and re-pair meanwhile survives. ``still_current``
    (async, True while this delete is still the member's lifecycle epoch) is checked before
    touching anything: a superseded delete does nothing. Once it has marked devices it finishes
    them, and a re-pair meanwhile is spared by the conditional removal."""

    async def current() -> bool:
        if still_current is None:
            return True
        try:
            if await still_current():
                return True
            logger.info("mac revocation superseded owner=%s", owner)
        except Exception as exc:  # noqa: BLE001 -- unknown: stop; a redelivery retries nothing, REVOKING holds
            logger.warning("mac revocation lifecycle unreadable owner=%s: %s", owner, type(exc).__name__)
        return False

    async def run() -> None:
        from core.services.connection_service import ConnectionService

        if not await current():
            return
        try:
            row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
            devices = [
                device_id
                for device_id, entry in (row.get("paired_devices") or {}).items()
                if isinstance(entry, dict)
                and entry.get("userId") == member_id
                and await container_repo.mark_paired_device_revoking(owner, device_id, user_id=member_id)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("mac revocation could not list devices owner=%s: %s", owner, type(exc).__name__)
            return
        try:
            await asyncio.wait_for(clear_member(owner, member_id), CLEAR_MEMBER_SECONDS)  # never raises
        except Exception as exc:  # noqa: BLE001
            logger.warning("mac browser sign-in clear gave up owner=%s: %s", owner, type(exc).__name__)
        finally:
            for device_id in devices:
                try:
                    if await container_repo.remove_revoked_paired_device(owner, device_id, member_id):
                        # Disconnect it too, as DELETE /desktop/devices does.
                        await ConnectionService().delete_relay_rows_for_device(owner, device_id)
                except Exception as exc:  # noqa: BLE001 -- REVOKING already refuses it
                    logger.warning("mac pairing not removed owner=%s: %s", owner, type(exc).__name__)

    task = asyncio.create_task(run())
    _clears.add(task)
    task.add_done_callback(_clears.discard)
