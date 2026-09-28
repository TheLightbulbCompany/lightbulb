"""Workspace sign-ins on a paired Mac's agent browser profile.

The agent browser is ONE profile per workspace, whichever browser runs it. The cloud
browser's saved state (cookies + localStorage, Playwright storageState JSON under the
backend-only ``browser-state/<owner>/`` prefix, pointed at by the fenced
``__browser__#<owner>`` row) is loaded onto the Mac's ``lightbulb`` profile before a run's
first page action there, and each such run's changes are merged back when it ends.

Everything rides the existing ``browser.proxy`` node command (the Mac's openclaw-host
cookie/storage routes), dispatched straight from this task: no operation row, receipt,
frame, tool result or log line ever carries a cookie or storage value. Only the agent
profile is ever touched (``desktop_browser.proxy`` refuses any other). A Mac whose load did
not happen never writes back, and the merge is three-way per origin (cookie domain or
localStorage origin): the Mac writes only what it changed since its load, so a sign-in the
cloud browser saved meanwhile survives.

The per-Mac record lives in the task holding that Mac's socket. Losing it (a deploy, a
reconnect to another task) skips the save -- the safe direction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import browser_control
from core.services.desktop_browser import STATE_PROFILE, proxy, unpack
from core.services.desktop_execution import DesktopError

logger = logging.getLogger(__name__)

PROFILE = STATE_PROFILE
MAC_KEY_PREFIX = "__macbrowser__#"
POLL_SECONDS = 10
PAPERCLIP_POLL_SECONDS = 60
#: A run that never reports an end (a lost ledger row) stops being watched after this.
MAX_WATCH_SECONDS = 9 * 3600
LOAD_SECONDS = 25
#: localStorage needs a page on each origin; past this, the rest of the origins stay unloaded.
STORAGE_SECONDS = 15
CALL_MS = 10_000
MAX_STATE_BYTES = 50 * 1024 * 1024
#: node_broker caps browser.proxy paramsJSON at 16 KiB; leave room for the envelope.
MAX_PARAMS_BYTES = 15_000
MAX_TABS = 32
MAX_OBSERVED = 500
#: A save that would sign the workspace out of more loaded domains than this (or half of
#: them, whichever is more) at once is refused.
MASS_SIGNOUT_MIN = 5
CLEAR_SECONDS = 15
COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
MAC_READ_FAILED = (
    "Sign-ins that existed only in this Mac's browser could not be read before loading the "
    "workspace's, so they may be gone from it."
)
LOAD_FAILED = (
    "Workspace sign-ins could not be loaded into this Mac's browser, so this run's sign-ins "
    "there will not be saved back to the workspace. Browsing continues without them."
)

#: (owner, CLI device id) -> {"loaded", "runs": {runId: auth}, "target", load digests}.
_records: dict[tuple[str, str], dict] = {}
_watchers: set[asyncio.Task] = set()


def _digest(pairs) -> str:
    return hashlib.sha256(json.dumps(sorted(pairs), separators=(",", ":")).encode()).hexdigest()


def _cookie_digest(cookies: list[dict]) -> str:
    # Every persisted field: an expiry renewal or a flag change is a change the Mac made.
    return _digest([json.dumps([c.get(k) for k in COOKIE_FIELDS]) for c in cookies])


def _storage_digest(items: list[dict]) -> str:
    return _digest([(i["name"], i["value"]) for i in items])


EMPTY = _digest([])


def _origin(url: object) -> str | None:
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            return None
        parts.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _cookies(value) -> list[dict]:
    items = value if isinstance(value, list) else []
    return [
        {k: c[k] for k in COOKIE_FIELDS if k in c}
        for c in items
        if isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ("name", "value", "domain", "path"))
    ]


def _storage(value) -> list[dict]:
    items = value if isinstance(value, list) else []
    return [
        {"name": i["name"], "value": i["value"]}
        for i in items
        if isinstance(i, dict) and isinstance(i.get("name"), str) and isinstance(i.get("value"), str)
    ]


def _state(value) -> dict:
    """The storageState shape, reduced to fields we understand; invalid origins are dropped."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("cookies"), list)
        or not isinstance(value.get("origins"), list)
    ):
        raise ValueError("browser state is invalid")
    origins = {}
    for o in value["origins"]:
        if isinstance(o, dict) and _origin(o.get("origin")) == o.get("origin"):
            origins[o["origin"]] = {"origin": o["origin"], "localStorage": _storage(o.get("localStorage"))}
    return {**value, "cookies": _cookies(value["cookies"]), "origins": list(origins.values())}


def _by_domain(cookies: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in cookies:
        out.setdefault(c["domain"], []).append(c)
    return out


def merge(theirs: dict, mine: dict, record: dict) -> dict:
    """Three-way, per origin: the Mac's version of a cookie domain or localStorage origin wins
    only where it changed since its own load (deletion included); everything else keeps the
    workspace's current save. Domains/origins the load skipped are never written."""
    t_c, m_c = _by_domain(theirs["cookies"]), _by_domain(mine["cookies"])
    cookies: list[dict] = []
    for domain in sorted(set(t_c) | set(m_c) | set(record["cookies"])):
        ours = m_c.get(domain, [])
        changed = domain not in record["skipped_domains"] and _cookie_digest(ours) != record["cookies"].get(
            domain, EMPTY
        )
        cookies += ours if changed else t_c.get(domain, [])
    origins = {o["origin"]: o for o in theirs["origins"]}
    for o in mine["origins"]:
        name = o["origin"]
        if name not in record["skipped_origins"] and _storage_digest(o["localStorage"]) != record["origins"].get(
            name, EMPTY
        ):
            origins[name] = o
    return {**theirs, "cookies": cookies, "origins": list(origins.values())}


# ---- S3 (backend-only prefix) ---------------------------------------------------------------


def _s3():
    return boto3.client("s3", region_name=settings.AWS_REGION)


async def _s3_get(key: str) -> bytes:
    got = await run_in_thread(_s3().get_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key)
    body = await run_in_thread(got["Body"].read, MAX_STATE_BYTES + 1)
    if len(body) > MAX_STATE_BYTES:
        raise ValueError("browser state is too large")
    return body


async def _s3_put(key: str, body: bytes) -> None:
    await run_in_thread(
        _s3().put_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key, Body=body, ContentType="application/json"
    )


async def _s3_copy(source: str, key: str) -> None:
    await run_in_thread(
        _s3().copy_object,
        Bucket=settings.OWNER_STATE_BUCKET,
        Key=key,
        CopySource={"Bucket": settings.OWNER_STATE_BUCKET, "Key": source},
    )


async def _workspace(owner: str, state: dict | None) -> dict:
    key = (state or {}).get("saved_key")
    if not key:
        return {"cookies": [], "origins": []}
    if not key.startswith(f"browser-state/{owner}/"):
        raise ValueError("saved browser state is outside this workspace")
    return _state(json.loads(await _s3_get(key)))


# ---- the Mac --------------------------------------------------------------------------------


async def _call(store, target: dict, action: str, tab: str | None = None, args: dict | None = None) -> dict:
    params = proxy(action, PROFILE, tab, args or {}, CALL_MS)
    if len(json.dumps(params).encode()) > MAX_PARAMS_BYTES:
        raise DesktopError("browser_state_item_too_large", 413)
    response = await store.dispatch(
        target, {"command": "browser.proxy", "params": params, "executionDeadlineMs": CALL_MS}, str(uuid.uuid4())
    )
    payload = unpack(response, PROFILE)
    if payload["route"]["driver"] != "openclaw":
        raise DesktopError("browser_driver_changed", 409)
    return payload["result"]


def _batches(cookies: list[dict], too_large: list[dict]):
    """Batches under the node's param cap. A cookie too large to send even alone goes to
    ``too_large`` instead of failing the load."""
    limit = MAX_PARAMS_BYTES - 1000
    batch: list[dict] = []
    for cookie in cookies:
        if len(json.dumps([cookie]).encode()) > limit:
            too_large.append(cookie)
            continue
        if batch and len(json.dumps(batch + [cookie]).encode()) > limit:
            yield batch
            batch = []
        batch.append(cookie)
    if batch:
        yield batch


def merge_new(theirs: dict, mine: dict) -> dict:
    """Add-only, for a Mac that was not loaded: no three-way base exists for what it holds, so
    it only adds cookies (by name + domain + path) and localStorage keys the workspace save
    lacks, and never overwrites or deletes a workspace entry."""
    have = {(c["name"], c["domain"], c["path"]) for c in theirs["cookies"]}
    cookies = list(theirs["cookies"])
    for c in mine["cookies"]:
        if (c["name"], c["domain"], c["path"]) not in have:
            have.add((c["name"], c["domain"], c["path"]))
            cookies.append(c)
    origins = {o["origin"]: {**o, "localStorage": list(o["localStorage"])} for o in theirs["origins"]}
    for o in mine["origins"]:
        current = origins.get(o["origin"], {"origin": o["origin"], "localStorage": []})
        names = {i["name"] for i in current["localStorage"]}
        extra = [i for i in o["localStorage"] if i["name"] not in names]
        if extra:
            origins[o["origin"]] = {**current, "localStorage": current["localStorage"] + extra}
    return {**theirs, "cookies": cookies, "origins": list(origins.values())}


async def _replace_storage(store, target: dict, origin: str, items: list[dict]) -> None:
    tab = (await _call(store, target, "open", args={"url": origin})).get("targetId")
    if not isinstance(tab, str) or not tab:
        raise DesktopError("invalid_browser_result", 409)
    try:
        await _call(store, target, "_storage_clear", tab)
        for item in items:
            await _call(store, target, "_storage_set", tab, {"key": item["name"], "value": item["value"]})
    finally:
        try:
            await _call(store, target, "close", tab)
        except Exception:  # noqa: BLE001 -- a stray tab is harmless
            pass


async def _load(store, owner: str, target: dict) -> dict:
    """Make the Mac profile the workspace's saved state; return what was loaded, as digests,
    plus ``notes`` for the agent and ``observed``, the origins this profile is known to hold."""
    notes: list[str] = []
    saved = await _workspace(owner, await container_repo.get_browser_state(owner))
    mac = await _mac_row(target["deviceId"])
    observed = set(mac.get("origins") or ())
    # Before replacing anything, keep what only this Mac holds (a run whose save was skipped) --
    # but only when this workspace was the last one loaded onto it: whatever another workspace,
    # or the person, left in the profile must never enter this workspace's save.
    mine = None
    if mac.get("loaded_owner") == owner:
        try:
            mine, _unread = await _read_mac(store, target, observed, require_running=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mac browser state pre-load read failed owner=%s: %s", owner, type(exc).__name__)
            notes.append(MAC_READ_FAILED)
    if mine is not None:
        merged = merge_new(saved, mine)
        if merged != saved:
            try:
                merged = await _commit(owner, lambda theirs: merge_new(theirs, mine))
            except Exception as exc:  # noqa: BLE001 -- they stay on this Mac; the next load retries
                logger.warning("mac browser state pre-load merge failed owner=%s: %s", owner, type(exc).__name__)
        saved = merged
    loaded = {"cookies": {}, "skipped_domains": set(), "origins": {}, "skipped_origins": set()}
    # An empty workspace save still replaces, never inherits, the Mac profile's cookies.
    await _call(store, target, "_cookies_clear")
    too_large: list[dict] = []
    for batch in _batches(saved["cookies"], too_large):
        try:
            added = (await _call(store, target, "_cookies_set", args={"cookies": batch})).get("added")
        except Exception:  # noqa: BLE001
            added = None
        if added != len(batch):  # which one Playwright refused is unknown: skip the batch's domains
            loaded["skipped_domains"] |= {c["domain"] for c in batch}
    loaded["skipped_domains"] |= {c["domain"] for c in too_large}
    if too_large:
        notes.append(f"{len(too_large)} saved cookie(s) were too large to load into this Mac's browser.")
    loaded["cookies"] = {
        domain: _cookie_digest(items)
        for domain, items in _by_domain(saved["cookies"]).items()
        if domain not in loaded["skipped_domains"]
    }
    # localStorage: every origin the profile is known to hold is cleared, then the saved ones set.
    items_by_origin = {o: [] for o in observed} | {o["origin"]: o["localStorage"] for o in saved["origins"]}
    deadline = time.monotonic() + STORAGE_SECONDS
    for origin, items in items_by_origin.items():
        try:
            if time.monotonic() > deadline:
                raise TimeoutError()
            await _replace_storage(store, target, origin, items)
            loaded["origins"][origin] = _storage_digest(items)
        except Exception:  # noqa: BLE001 -- this origin keeps the workspace's copy at save
            loaded["skipped_origins"].add(origin)
    loaded["observed"] = observed | set(items_by_origin)
    await _put_mac_row(target["deviceId"], owner, loaded["observed"])
    loaded["notes"] = notes
    return loaded


async def _read_storage(store, target: dict, tab: str) -> list[dict] | None:
    values = (await _call(store, target, "_storage", tab)).get("values")
    return _storage([{"name": k, "value": v} for k, v in values.items()]) if isinstance(values, dict) else None


async def _read_mac(store, target: dict, observed=(), *, require_running: bool = True) -> tuple[dict, int]:
    """The profile's cookies plus localStorage for every open tab's origin and every other
    ``observed`` origin (reopened briefly, within STORAGE_SECONDS). Returns the state and how
    many observed origins could not be read."""
    if require_running and not (await _call(store, target, "status")).get("running"):
        # At run end, reading cookies would relaunch a Chrome window the person closed.
        raise DesktopError("browser_not_running", 409)
    tabs = (await _call(store, target, "tabs")).get("tabs")
    cookies = (await _call(store, target, "_cookies")).get("cookies")
    if not isinstance(cookies, list):
        # Never read as "signed out of everything": that would be merged into the shared save.
        raise DesktopError("invalid_browser_result", 409)
    origins: dict[str, dict] = {}
    for tab in (tabs if isinstance(tabs, list) else [])[:MAX_TABS]:
        origin = _origin(tab.get("url")) if isinstance(tab, dict) else None
        if not origin or origin in origins or tab.get("type", "page") != "page":
            continue
        try:
            items = await _read_storage(store, target, tab.get("targetId"))
        except Exception:  # noqa: BLE001 -- an unreadable origin keeps the workspace's copy
            continue
        if items is not None:
            origins[origin] = {"origin": origin, "localStorage": items}
    unread = 0
    deadline = time.monotonic() + STORAGE_SECONDS
    for origin in sorted(set(observed) - set(origins)):
        tab = None
        try:
            if time.monotonic() > deadline:
                raise TimeoutError()
            tab = (await _call(store, target, "open", args={"url": origin})).get("targetId")
            items = await _read_storage(store, target, tab)
            if items is None:
                raise ValueError()
            origins[origin] = {"origin": origin, "localStorage": items}
        except Exception:  # noqa: BLE001 -- this origin keeps the workspace's copy
            unread += 1
        finally:
            if tab:
                try:
                    await _call(store, target, "close", tab)
                except Exception:  # noqa: BLE001
                    pass
    return {"cookies": _cookies(cookies), "origins": list(origins.values())}, unread


async def _mac_row(device_id: str) -> dict:
    return await container_repo.get_by_owner_id(f"{MAC_KEY_PREFIX}{device_id}", consistent=True) or {}


async def _put_mac_row(device_id: str, owner: str | None, origins) -> None:
    """Which workspace's sign-ins this Mac's agent profile holds (None after a revocation) and
    the origins it is known to have stored data for -- origins only, never values. Keyed by
    the device id (sha256 of the device's own key), so it follows the Mac across workspaces
    and backend restarts. A reserved key: every row scan skips it."""
    # ponytail: origins are capped, not aged; a profile that touched more sites than this
    # forgets the rest (the complete fix is the Mac-side profile wipe).
    kept = sorted(origins)[:MAX_OBSERVED]
    await run_in_thread(
        container_repo._get_table().put_item,
        Item={"owner_id": f"{MAC_KEY_PREFIX}{device_id}", "loaded_owner": owner, "origins": kept},
    )


def observe(row: dict, target: dict, urls) -> None:
    """After a `lightbulb` page action: remember the origins its tabs reached (redirects and
    clicks included), so the save reads them and a load or revocation clears them."""
    record = _records.get((row["auth"]["ownerId"], target["deviceId"]))
    if record is not None:
        record["observed"] |= {o for u in urls if (o := _origin(u))}


class Busy(Exception):
    """A cloud start or save holds the pointer (for up to TRANSITION_SECONDS): retry later."""


async def _commit(owner: str, combine) -> dict:
    """Write ``combine(workspace save)`` under the same compare-and-set the cloud save uses;
    return what the workspace save now is."""
    for _ in range(3):
        old = await container_repo.get_browser_state(owner)
        if old and old.get("mode") == "transition":
            raise Busy()
        previous = (old or {}).get("saved_key")
        theirs = await _workspace(owner, old)
        merged = combine(theirs)
        if merged == theirs:
            return theirs
        body = json.dumps(merged, separators=(",", ":")).encode()
        if len(body) > MAX_STATE_BYTES:
            raise ValueError("browser state is too large")
        key = f"browser-state/{owner}/mac-{secrets.token_hex(16)}.json"
        await _s3_put(key, body)
        if previous:
            # The cloud keeps each extension's own storage beside the save; carry it over.
            # Only a confirmed-missing source (none saved yet) may be skipped: publishing without
            # it would delete the previous snapshot's extension storage for good.
            try:
                await _s3_copy(browser_control._ext_key(previous), browser_control._ext_key(key))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
                    await browser_control._delete_snapshot(key)
                    raise
        # An existing row keeps its saved_at (the cloud autosave compares a person's last input
        # against it) and its base_key (the cloud session's own merge base).
        new = (
            {**old, "saved_key": key}
            if old
            else {
                "generation": secrets.token_hex(32),
                "mode": "idle",
                "saved_key": key,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        try:
            await browser_control._publish(owner, old, new)
        except browser_control.BrowserError:
            await browser_control._delete_snapshot(key)
            continue
        if previous:
            await browser_control._delete_snapshot(previous)
        return merged
    raise Busy()


# ---- run lifecycle --------------------------------------------------------------------------


async def before_action(store, row: dict, target: dict) -> str | None:
    """Called under the profile lock before a `lightbulb` page action. Loads once per Mac
    while any run uses it; returns a note for the agent when something did not sync."""
    auth = row["auth"]
    key = (auth["ownerId"], target["deviceId"])
    record = _records.get(key)
    if record is not None:
        joined = auth["runId"] not in record["runs"]
        record["runs"][auth["runId"]] = auth
        record["target"] = target
        return LOAD_FAILED if joined and not record["loaded"] else None
    record = {
        "loaded": False,
        "runs": {auth["runId"]: auth},
        "target": target,
        "started": time.monotonic(),
        "observed": set(),
        "next_check": {},
    }
    _records[key] = record
    task = asyncio.create_task(_watch(store, key, record))
    _watchers.add(task)
    task.add_done_callback(_watchers.discard)
    try:
        # Inside the agent's own action deadline, leaving it room to run.
        async with asyncio.timeout(max(0.1, min(LOAD_SECONDS, row["deadline"] - store.clock() - 10))):
            loaded = await _load(store, auth["ownerId"], target)
        notes = loaded.pop("notes")
        record["observed"] |= loaded.pop("observed")
        record.update(loaded)
        record["loaded"] = True
        put_metric("desktop.browser_state.load", dimensions={"result": "ok"})
        return " ".join(notes) or None
    except Exception as exc:  # noqa: BLE001 -- never fail the agent's action
        # Class name only: a failure here must not echo a cookie or a signed URL.
        logger.warning("mac browser state load failed owner=%s: %s", auth["ownerId"], type(exc).__name__)
        put_metric("desktop.browser_state.load", dimensions={"result": "error"})
        return LOAD_FAILED


async def _run_active(auth: dict) -> bool:
    if auth["family"] == "paperclip":
        from core.services.desktop_execution import authorize_run

        try:
            await authorize_run(auth["ownerId"], auth["runId"], auth["agentId"], auth["family"], active=True)
        except DesktopError as exc:
            if exc.code == "run_not_active":
                return False
            raise
        return True
    run = await messaging_repo.get_run(auth["ownerId"], auth["runId"])
    return bool(run) and run.get("status") in {"dispatched", "started"}


async def _save(store, key: tuple[str, str], record: dict, auth: dict) -> bool:
    """One ended run's write-back. True: done (saved, or its authority is gone, so it never
    will be). False: the workspace save is busy or unreachable; try again next tick."""
    from core.services.desktop_execution import authorize_run

    owner = auth["ownerId"]
    try:
        # The run's authority must still hold (a removed member's Mac never writes back),
        # and so must the Mac's pairing, Browser grant and connection generation.
        if await authorize_run(owner, auth["runId"], auth["agentId"], auth["family"], active=False) != auth:
            raise DesktopError("run_forbidden", 403)
        if not store.enabled(owner):
            raise DesktopError("desktop_disabled", 503)
        target = record["target"]
        target = await store.resolve(
            auth,
            {
                "deviceId": target["deviceId"],
                "expectedConnectionGeneration": target["generation"],
                "command": "browser.proxy",
            },
        )
        async with store.browser.locked((owner, target["deviceId"], target["generation"], PROFILE)):
            if _records.get(key) is not record:  # cleared by a revocation meanwhile
                return True
            mine, unread = await _read_mac(store, target, record["observed"])
            record["observed"] |= {o["origin"] for o in mine["origins"]}
    except Exception as exc:  # noqa: BLE001 -- authority, pairing or the Mac itself is gone
        logger.warning("mac browser state save dropped owner=%s: %s", owner, type(exc).__name__)
        put_metric("desktop.browser_state.save", dimensions={"result": "dropped"})
        return True
    # A read that lost most loaded sign-ins at once is far likelier a broken profile (a reset
    # Chrome, a wiped user-data dir) than a person signing out everywhere: keep the workspace's.
    kept = {c["domain"] for c in mine["cookies"]}
    lost = [d for d, digest in record["cookies"].items() if digest != EMPTY and d not in kept]
    if len(lost) > max(MASS_SIGNOUT_MIN, len(record["cookies"]) // 2):
        logger.warning("mac browser state save refused owner=%s: %d loaded domains gone", owner, len(lost))
        put_metric("desktop.browser_state.save", dimensions={"result": "refused"})
        return True
    try:
        await _commit(owner, lambda theirs: merge(theirs, mine, record))
    except Exception as exc:  # noqa: BLE001
        logger.warning("mac browser state save retrying owner=%s: %s", owner, type(exc).__name__)
        put_metric("desktop.browser_state.save", dimensions={"result": "retry"})
        return False
    # The Mac now holds `mine`: that is the base the next run's changes are measured against.
    record["cookies"] = {
        domain: _cookie_digest(items)
        for domain, items in _by_domain(mine["cookies"]).items()
        if domain not in record["skipped_domains"]
    }
    for o in mine["origins"]:
        if o["origin"] not in record["skipped_origins"]:
            record["origins"][o["origin"]] = _storage_digest(o["localStorage"])
    if unread:  # the run is over: nobody to tell but the logs
        logger.info("mac browser state save left %d origin(s) unread owner=%s", unread, owner)
    try:
        await _put_mac_row(target["deviceId"], owner, record["observed"])
    except Exception as exc:  # noqa: BLE001 -- the next save writes it again
        logger.info("mac browser origins not recorded owner=%s: %s", owner, type(exc).__name__)
    put_metric("desktop.browser_state.save", dimensions={"result": "ok"})
    return True


async def _watch(store, key: tuple[str, str], record: dict) -> None:
    """Save back as each run that used this Mac profile ends; forget the Mac after the last.
    A save that hits a busy workspace save stays pending and retries every tick."""
    while _records.get(key) is record:
        await asyncio.sleep(POLL_SECONDS)
        if time.monotonic() - record["started"] > MAX_WATCH_SECONDS:
            break
        for run_id, auth in list(record["runs"].items()):
            # authorize_run(active=True) is Paperclip HTTP: check a live Paperclip run less often.
            if time.monotonic() < record["next_check"].get(run_id, 0):
                continue
            try:
                active = await _run_active(auth)
            except DesktopError:
                record["runs"].pop(run_id, None)  # authority gone: never saved
                continue
            except Exception:  # noqa: BLE001 -- a transient read; look again next tick
                continue
            if active:
                if auth["family"] == "paperclip":
                    record["next_check"][run_id] = time.monotonic() + PAPERCLIP_POLL_SECONDS
                continue
            if record["loaded"] and _records.get(key) is record and not await _save(store, key, record, auth):
                continue
            record["runs"].pop(run_id, None)
        if not record["runs"]:
            break
    if _records.get(key) is record:
        _records.pop(key)


# ---- revocation -----------------------------------------------------------------------------


async def clear_device(owner: str, device_id: str, *, forwarded: bool = False) -> bool:
    """Remove workspace sign-ins from a paired Mac's agent profile (unpair / member removal).

    Best effort and only while that Mac is connected: an offline Mac keeps them until a
    desktop release clears on unpair locally. ``device_id`` is either device of the pair.
    """
    import httpx

    from core.services.connection_service import ConnectionService
    from core.services.desktop_execution import operations
    from core.services.desktop_pair import paired_physical_mac

    row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
    devices = row.get("paired_devices") or {}
    entry = devices.get(device_id)
    pair = paired_physical_mac(row, entry.get("userId"), device_id) if isinstance(entry, dict) else None
    if pair is None:
        return False
    cli = next(p for p in pair if p.get("clientId") == "node-host")
    cli_id = next(k for k, v in devices.items() if v == cli)
    connections = ConnectionService()
    ready = await asyncio.to_thread(
        connections._client.get_item,
        TableName=connections.table_name,
        Key={"connectionId": {"S": connections.desktop_ready_id(owner, cli_id)}},
        ConsistentRead=True,
    )
    generation = ready.get("Item", {}).get("generation", {}).get("S", "")
    # The Browser grant is required: without it nothing was ever loaded there.
    target = await connections._resolve_desktop_target(owner, cli["userId"], cli_id, generation, "browser.proxy")
    store = operations()
    if target["taskAddress"] != store.address:
        if forwarded:
            return False
        async with httpx.AsyncClient(timeout=CLEAR_SECONDS - 1, follow_redirects=False) as client:
            response = await client.post(
                target["taskAddress"] + "/api/v1/desktop-execution/internal/browser-clear",
                headers={"x-lightbulb-ws-internal": settings.WS_INTERNAL_SECRET},
                json={"ownerId": owner, "deviceId": cli_id},
            )
        return response.status_code == 200
    record = _records.pop((owner, cli_id), None) or {}  # nothing from this Mac is saved back any more
    async with store.browser.locked((owner, cli_id, target["generation"], PROFILE)):
        await _call(store, target, "_cookies_clear")
        try:
            mac = await _mac_row(cli_id)
            await _put_mac_row(cli_id, None, ())  # whatever the profile holds now is nobody's to merge
        except Exception:  # noqa: BLE001 -- the cookies are already gone
            mac = {}
        # localStorage clears per origin, so gather every origin we can enumerate: the workspace
        # save's, the ones this Mac loaded or navigated to, and the ones open in its tabs now.
        origins = (
            set(mac.get("origins") or ())
            | set(record.get("observed", ()))
            | set(record.get("origins", {}))
            | set(record.get("skipped_origins", ()))
        )
        try:
            origins |= {
                o["origin"] for o in (await _workspace(owner, await container_repo.get_browser_state(owner)))["origins"]
            }
        except Exception:  # noqa: BLE001 -- cookies (the sign-ins) are already gone
            pass
        try:
            tabs = (await _call(store, target, "tabs")).get("tabs")
            origins |= {
                o
                for t in (tabs if isinstance(tabs, list) else [])
                if isinstance(t, dict) and (o := _origin(t.get("url")))
            }
        except Exception:  # noqa: BLE001
            pass
        for origin in sorted(origins):
            try:
                await _replace_storage(store, target, origin, [])
            except Exception:  # noqa: BLE001
                pass
    return True


async def clear_member(owner: str, member_id: str) -> None:
    """A removed member's paired Macs lose the workspace sign-ins. Never raises."""
    try:
        row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("mac browser sign-in clear failed owner=%s: %s", owner, type(exc).__name__)
        return
    for device_id, entry in (row.get("paired_devices") or {}).items():
        if isinstance(entry, dict) and entry.get("userId") == member_id and entry.get("clientId") == "node-host":
            try:
                await asyncio.wait_for(clear_device(owner, device_id), CLEAR_SECONDS)
            except Exception as exc:  # noqa: BLE001 -- e.g. that Mac is offline
                logger.warning("mac browser sign-in clear failed owner=%s: %s", owner, type(exc).__name__)
