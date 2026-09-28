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
import contextlib
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
from core.services.desktop_browser import STATE_PROFILE, profile_lock, proxy, unpack
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
CAPABLE_TTL_S = 600
INCAPABLE_TTL_S = 60
HELD_BACK = (
    "Many sign-ins vanished from this Mac's browser at once, so they were kept in the workspace "
    "rather than signed out; everything else synced."
)
CLEAR_SECONDS = 15
COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
MAC_READ_FAILED = (
    "This Mac's browser kept its own sign-ins for now: its recent changes could not be merged "
    "into the workspace, so the workspace's were not loaded over them. It will try again."
)
MERGE_GATED = (
    "The workspace's cloud browser cannot merge sign-ins yet, so sign-ins made in this Mac's "
    "browser stay on this Mac for now and nothing is written back from it."
)
CAPABILITY_TIMEOUT_S = 20
LOAD_FAILED = (
    "Workspace sign-ins could not be loaded into this Mac's browser, so this run's sign-ins "
    "there will not be saved back to the workspace. Browsing continues without them."
)

#: CLI device id -> {"owner", "loaded", "runs": {runId: auth}, "target", load digests}. One per
#: Mac, not per workspace: the agent profile is one physical Chrome profile.
_records: dict[str, dict] = {}
_watchers: set[asyncio.Task] = set()
#: owner -> (runtime merges three-way on its cloud saves, until when that answer holds).
_capable: dict[str, tuple[bool, float]] = {}
#: (owner, device) -> no load attempt before this monotonic time (a deferred load's backoff).
_deferred: dict[tuple[str, str], float] = {}
DEFER_SECONDS = 60
MAX_DEFERRED = 1000
SWITCH_SAVE_SECONDS = 15
#: How long a committed write-back's Mac-row base is retried before the row is untrusted.
PENDING_BASE_SECONDS = 600
#: device -> a committed pre-merge's (owner, observed, base, load_id) whose row write failed.
_pending_rows: dict[str, tuple] = {}


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
    # Whole cookies, unknown fields (partitionKey, ...) included: the merge copies them untouched.
    return [
        dict(c)
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
        # A base cut short at MAX_OBSERVED says nothing about what it dropped: skip those.
        unknown = record.get("truncated") and domain not in record["cookies"]
        changed = (
            not unknown
            and domain not in record["skipped_domains"]
            and _cookie_digest(ours) != record["cookies"].get(domain, EMPTY)
        )
        cookies += ours if changed else t_c.get(domain, [])
    origins = {o["origin"]: o for o in theirs["origins"]}
    for o in mine["origins"]:
        name = o["origin"]
        if (
            not (record.get("truncated") and name not in record["origins"])
            and name not in record["skipped_origins"]
            and _storage_digest(o["localStorage"]) != record["origins"].get(name, EMPTY)
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
    for cookie in ({k: c[k] for k in COOKIE_FIELDS if k in c} for c in cookies):
        if len(json.dumps([cookie]).encode()) > limit:
            too_large.append(cookie)
            continue
        if batch and len(json.dumps(batch + [cookie]).encode()) > limit:
            yield batch
            batch = []
        batch.append(cookie)
    if batch:
        yield batch


async def _open_at(store, target: dict, origin: str) -> str:
    """Open a tab on ``origin`` and prove it is still there: a redirect elsewhere means touching
    that page's storage would read or change a different site's."""
    tab = (await _call(store, target, "open", args={"url": origin})).get("targetId")
    if not isinstance(tab, str) or not tab:
        raise DesktopError("invalid_browser_result", 409)
    tabs = (await _call(store, target, "tabs")).get("tabs")
    url = (
        next((t.get("url") for t in tabs if isinstance(t, dict) and t.get("targetId") == tab), None)
        if isinstance(tabs, list)
        else None
    )
    if _origin(url) != origin:
        try:
            await _call(store, target, "close", tab)
        except Exception:  # noqa: BLE001
            pass
        raise DesktopError("browser_origin_mismatch", 409)
    return tab


async def _replace_storage(store, target: dict, origin: str, items: list[dict]) -> list[dict] | None:
    """Replace one origin's localStorage; return what the page holds afterwards."""
    tab = await _open_at(store, target, origin)
    try:
        await _call(store, target, "_storage_clear", tab)
        for item in items:
            await _call(store, target, "_storage_set", tab, {"key": item["name"], "value": item["value"]})
        return await _read_storage(store, target, tab)
    finally:
        try:
            await _call(store, target, "close", tab)
        except Exception:  # noqa: BLE001 -- a stray tab is harmless
            pass


def _suspect_lost(mine: dict, base: dict) -> set[str]:
    """Loaded domains the Mac lost all at once: far likelier a broken profile (a reset Chrome,
    a wiped user-data dir) than a person signing out everywhere. Empty when it is a few."""
    kept = {c["domain"] for c in mine["cookies"]}
    lost = {
        d
        for d, digest in base["cookies"].items()
        if digest != EMPTY and d not in kept and d not in base["skipped_domains"]
    }
    return lost if len(lost) > max(MASS_SIGNOUT_MIN, len(base["cookies"]) // 2) else set()


def _partitioned(cookies: list[dict]) -> set[str]:
    """Domains holding a partitioned (CHIPS) cookie: they load unpartitioned on the Mac and
    collapse, so the Mac neither loads nor writes them."""
    return {c["domain"] for c in cookies if c.get("partitionKey")}


def _advance(base: dict, mine: dict, held: set[str]) -> None:
    """After a commit, the Mac's committed state is the base its next changes are measured
    against. ``held`` (domains that were not written) keep their old base."""
    for domain, items in _by_domain(mine["cookies"]).items():
        if domain not in base["skipped_domains"] and domain not in held:
            base["cookies"][domain] = _cookie_digest(items)
    for domain in [d for d in base["cookies"] if d not in held and d not in _by_domain(mine["cookies"])]:
        base["cookies"][domain] = EMPTY
    for o in mine["origins"]:
        if o["origin"] not in base["skipped_origins"]:
            base["origins"][o["origin"]] = _storage_digest(o["localStorage"])


async def _write_back(owner: str, mine: dict, base: dict) -> int:
    """Three-way into the workspace save; advance ``base``. Returns how many loaded domains
    were held back as a likely broken profile."""
    held = _suspect_lost(mine, base)
    skip = held | _partitioned(mine["cookies"])
    view = {**base, "skipped_domains": base["skipped_domains"] | skip}
    await _commit(owner, lambda theirs: merge(theirs, mine, view))
    _advance(base, mine, skip)
    return len(held)


async def can_write_back(owner: str, timeout_s: float = CAPABILITY_TIMEOUT_S) -> bool | None:
    """A Mac may write into the workspace save only when every cloud save that can follow
    merges three-way: the runtime says so, and a live cloud session has its own base (one
    started before its image could record it would save its cookies wholesale). None: the
    runtime could not be asked (a timeout or transport failure) -- try again later."""
    state = await container_repo.get_browser_state(owner) or {}
    if state.get("session_id") and not state.get("base_key"):
        return False
    return await _merge_capable(owner, timeout_s)


async def _merge_capable(owner: str, timeout_s: float = CAPABILITY_TIMEOUT_S) -> bool | None:
    """Whether this workspace's runtime merges three-way on its cloud browser saves. An older
    image copies its session's cookies wholesale, so anything the Mac wrote back would be
    overwritten by the next cloud save: until the roll, the Mac only loads."""
    from core.services import container_rpc

    hit = _capable.get(owner)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    try:
        async with asyncio.timeout(timeout_s):
            payload = await container_rpc.call(owner, "health", {})
    except Exception:  # noqa: BLE001 -- unknown, not no: never write on it, never cache it
        return None
    ok = isinstance(payload, dict) and payload.get("browserStateMerge", 0) >= 1
    _capable[owner] = (ok, time.monotonic() + (CAPABLE_TTL_S if ok else INCAPABLE_TTL_S))
    return ok


async def _load(store, owner: str, target: dict) -> dict:
    """Make the Mac profile the workspace's saved state; return the merge base (digests of what
    the profile holds right after the load, so Chrome's own normalisation is baked in) plus
    ``notes`` for the agent and ``observed``, the origins this profile is known to hold."""
    notes: list[str] = []
    device = target["deviceId"]
    mac = await _mac_row(device)
    observed = set(mac.get("origins") or ())
    # What this Mac changed since its last sync, three-way against the base stored with it --
    # only when this workspace was the last one fully loaded onto it: whatever another
    # workspace, a partial load, or the person left in the profile never enters this save.
    # If that merge cannot run, the load does not either: clearing now would wipe changes that
    # exist only here. The Mac keeps what it has and a later action tries again.
    if device in _pending_rows:
        # A pre-merge committed earlier but its base never reached the row: settle that first.
        await _settle_base(device, *_pending_rows.pop(device))
        mac = await _mac_row(device)
    if mac.get("loaded_owner") == owner and isinstance(mac.get("cookies"), dict):
        if not await can_write_back(owner, timeout_s=5):
            raise LoadDeferred(MERGE_GATED)
        try:
            mine, _unread = await _read_mac(store, target, observed, require_running=False)
            # Read under no cross-task lock: prove no other load fenced the profile meanwhile, so
            # what was read is this workspace's.
            await _still_loaded(device, mac.get("load_id"))
            last = {
                "cookies": dict(mac["cookies"]),
                "origins": dict(mac.get("storage") or {}),
                "skipped_domains": set(),
                "skipped_origins": set(),
                "truncated": bool(mac.get("truncated")),
            }
            if await _write_back(owner, mine, last):
                notes.append(HELD_BACK)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mac browser state pre-load merge failed owner=%s: %s", owner, type(exc).__name__)
            raise LoadDeferred(MAC_READ_FAILED) from None
        # Committed: record the new base at once, so nothing that fails from here on can leave
        # the old digests to revert a cloud change later -- or, failing that, untrust the row.
        await _settle_base(device, owner, observed, last, mac.get("load_id"))
    saved = await _workspace(owner, await container_repo.get_browser_state(owner))
    # The fence: from here until the load completes, the profile is nobody's to merge.
    fence = await _put_mac_row(device, None, observed)
    loaded = {"cookies": {}, "skipped_domains": set(), "origins": {}, "skipped_origins": set()}
    # An empty workspace save still replaces, never inherits, the Mac profile's cookies.
    await _call(store, target, "_cookies_clear")
    chips = _partitioned(saved["cookies"])
    loaded["skipped_domains"] |= chips
    if chips:
        notes.append(f"{len(chips)} site(s) with partitioned cookies were not loaded into this Mac's browser.")
    too_large: list[dict] = []
    for batch in _batches([c for c in saved["cookies"] if c["domain"] not in chips], too_large):
        try:
            added = (await _call(store, target, "_cookies_set", args={"cookies": batch})).get("added")
        except Exception:  # noqa: BLE001
            added = None
        if added != len(batch):  # which one Playwright refused is unknown: skip the batch's domains
            loaded["skipped_domains"] |= {c["domain"] for c in batch}
    loaded["skipped_domains"] |= {c["domain"] for c in too_large}
    if too_large:
        notes.append(f"{len(too_large)} saved cookie(s) were too large to load into this Mac's browser.")
    held = (await _call(store, target, "_cookies")).get("cookies")
    if not isinstance(held, list):
        raise DesktopError("invalid_browser_result", 409)
    held_by_domain = _by_domain(_cookies(held))
    loaded["cookies"] = {
        domain: _cookie_digest(held_by_domain.get(domain, []))
        for domain in set(_by_domain(saved["cookies"])) | set(held_by_domain)
        if domain not in loaded["skipped_domains"]
    }
    # localStorage: every origin the profile is known to hold is cleared, then the saved ones set.
    items_by_origin = {o: [] for o in observed} | {o["origin"]: o["localStorage"] for o in saved["origins"]}
    deadline = time.monotonic() + STORAGE_SECONDS
    for origin, items in items_by_origin.items():
        try:
            if time.monotonic() > deadline:
                raise TimeoutError()
            after = await _replace_storage(store, target, origin, items)
            if after is None:
                raise ValueError()
            loaded["origins"][origin] = _storage_digest(after)
        except Exception:  # noqa: BLE001 -- this origin keeps the workspace's copy at save
            loaded["skipped_origins"].add(origin)
    loaded["observed"] = observed | set(items_by_origin)
    loaded["load_id"] = await _put_mac_row(device, owner, loaded["observed"], loaded, expect=fence)
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
            tab = await _open_at(store, target, origin)
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


async def _settle_base(device: str, owner: str, observed, base: dict, load_id: str | None) -> None:
    """After a pre-merge committed: write its base to the Mac row (conditional on ``load_id``);
    if that fails, clear the row's digests (same condition) so no pre-merge -- on any backend
    task -- reads the stale ones. If even that fails, keep it pending and defer the load."""
    for untrusted in (False, True):
        try:
            await _put_mac_row(device, owner, observed, None if untrusted else base, expect=load_id)
            return
        except LoadSuperseded:
            return  # someone else fenced the profile: their row, not ours
        except Exception as exc:  # noqa: BLE001
            logger.warning("mac browser state pre-load base not recorded owner=%s: %s", owner, type(exc).__name__)
    _pending_rows[device] = (owner, observed, base, load_id)
    raise LoadDeferred(MAC_READ_FAILED)


class LoadSuperseded(Exception):
    """Another load fenced this Mac's profile since this one read it: its base is stale."""


async def _put_mac_row(
    device_id: str, owner: str | None, origins, base: dict | None = None, *, expect: str | None = None
) -> str:
    """Which workspace's sign-ins this Mac's agent profile holds (None after a revocation) and
    the origins it is known to have stored data for -- origins only, never values. Keyed by
    the device id (sha256 of the device's own key), so it follows the Mac across workspaces
    and backend restarts. A reserved key: every row scan skips it."""
    # ponytail: origins are capped, not aged; a profile that touched more sites than this
    # forgets the rest (the complete fix is the Mac-side profile wipe).
    kept = sorted(origins)[:MAX_OBSERVED]
    # A fence (owner None) starts a new load generation. Every owner or base write must still
    # match the generation it read, or another load -- in this task or another -- fenced the
    # profile since, and writing would relabel someone else's sign-ins.
    load_id = secrets.token_hex(16) if owner is None else (expect or "")
    item = {"owner_id": f"{MAC_KEY_PREFIX}{device_id}", "loaded_owner": owner, "origins": kept, "load_id": load_id}
    if base is not None and owner is not None:
        # The last-synced digests the next load's pre-merge is measured against.
        item["cookies"] = dict(sorted(base["cookies"].items())[:MAX_OBSERVED])
        item["storage"] = dict(sorted(base["origins"].items())[:MAX_OBSERVED])
        # Once cut, always cut: re-writing a 500-entry base must not forget what it dropped.
        item["truncated"] = (
            bool(base.get("truncated")) or len(base["cookies"]) > MAX_OBSERVED or len(base["origins"]) > MAX_OBSERVED
        )
    condition = {}
    if owner is not None:
        condition = (
            {"ConditionExpression": "load_id = :expect", "ExpressionAttributeValues": {":expect": expect}}
            if expect
            else {"ConditionExpression": "attribute_not_exists(load_id)"}
        )
    try:
        await run_in_thread(container_repo._get_table().put_item, Item=item, **condition)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise LoadSuperseded() from None
        raise
    return load_id


async def _still_loaded(device_id: str, load_id: str | None) -> None:
    """Raise unless no other load has fenced this profile since ``load_id`` was read."""
    if (await _mac_row(device_id)).get("load_id") != load_id:
        raise LoadSuperseded()


def observe(row: dict, target: dict, urls) -> None:
    """After a `lightbulb` page action: remember the origins its tabs reached (redirects and
    clicks included), so the save reads them and a load or revocation clears them."""
    record = _records.get(target["deviceId"])
    if record is not None and record["owner"] == row["auth"]["ownerId"]:
        record["observed"] |= {o for u in urls if (o := _origin(u))}


class LoadDeferred(Exception):
    """The load did not start: the Mac's own changes could not be merged first."""


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
    """Called under the Mac's profile lock before a `lightbulb` page action. Loads once per Mac
    while this workspace's runs use it, and again whenever another workspace drives it;
    returns a note for the agent when something did not sync."""
    auth = row["auth"]
    owner, device = auth["ownerId"], target["deviceId"]
    record = _records.get(device)
    if record is not None and record["owner"] != owner:
        # The same profile, another workspace: save what it did (bounded), then forget it, so
        # nothing it holds is ever merged into this workspace or saved back later.
        await _switch_away(store, device, record)
        record = None
    if record is not None:
        joined = auth["runId"] not in record["runs"]
        record["runs"][auth["runId"]] = auth
        record["target"] = target
        if joined and not record["loaded"]:
            return LOAD_FAILED
        return MERGE_GATED if joined and not record.get("capable", True) else None
    now = time.monotonic()
    for stale in [k for k, until in _deferred.items() if until <= now]:
        del _deferred[stale]
    if _deferred.get((owner, device), 0) > now:
        return None  # a deferred load retries after DEFER_SECONDS; the Mac is untouched meanwhile
    record = {
        "owner": owner,
        "loaded": False,
        "runs": {auth["runId"]: auth},
        "target": target,
        "started": now,
        "observed": set(),
        "next_check": {},
    }
    _records[device] = record
    task = asyncio.create_task(_watch(store, device, record))
    _watchers.add(task)
    task.add_done_callback(_watchers.discard)
    try:
        # Inside the agent's own action deadline, leaving it room to run.
        async with asyncio.timeout(max(0.1, min(LOAD_SECONDS, row["deadline"] - store.clock() - 10))):
            loaded = await _load(store, owner, target)
        notes = loaded.pop("notes")
        record["observed"] |= loaded.pop("observed")
        record.update(loaded)
        record["loaded"] = True
        record["capable"] = MERGE_GATED not in notes
        if MERGE_GATED not in notes and not await can_write_back(owner, timeout_s=5):
            notes.append(MERGE_GATED)
            record["capable"] = False
        put_metric("desktop.browser_state.load", dimensions={"result": "ok"})
        return " ".join(notes) or None
    except LoadDeferred as deferred:
        # Nothing was touched: forget this attempt so a later action loads (and syncs) instead.
        if _records.get(device) is record:
            _records.pop(device)
        if len(_deferred) < MAX_DEFERRED:
            _deferred[(owner, device)] = time.monotonic() + DEFER_SECONDS
        put_metric("desktop.browser_state.load", dimensions={"result": "deferred"})
        return str(deferred)
    except Exception as exc:  # noqa: BLE001 -- never fail the agent's action
        # Class name only: a failure here must not echo a cookie or a signed URL.
        logger.warning("mac browser state load failed owner=%s: %s", owner, type(exc).__name__)
        put_metric("desktop.browser_state.load", dimensions={"result": "error"})
        return LOAD_FAILED


async def _switch_away(store, device: str, record: dict) -> None:
    """Another workspace is about to drive this Mac's profile (under its lock already)."""
    if _records.get(device) is record:
        _records.pop(device)
    auth = next(iter(record["runs"].values()), None)
    if not record["loaded"] or auth is None:
        return
    try:
        await asyncio.wait_for(_save(store, device, record, auth, locked=True), SWITCH_SAVE_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- its unsynced Mac changes are lost, never mixed
        logger.warning("mac browser state switch save failed owner=%s: %s", record["owner"], type(exc).__name__)
    if record.get("pending_base"):  # no later tick will retry it: never leave a stale base
        await _untrust(device, record, record["owner"])


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


async def _save(store, device: str, record: dict, auth: dict, *, locked: bool = False) -> bool:
    """One ended run's write-back. True: done (saved, or it never will be: authority, pairing
    or the gate is gone). False: a transient failure (a busy workspace save, a probe or
    transport error); try again next tick. ``locked``: the caller holds the profile lock."""
    from core.services.desktop_execution import authorize_run

    owner = auth["ownerId"]
    if record.get("pending_base"):
        # The workspace save already committed; only its base failed to reach the Mac row.
        # Retry that one conditional write, never the commit.
        return await _persist_base(device, record, owner)
    try:
        # The run's authority must still hold (a removed member's Mac never writes back),
        # and so must the Mac's pairing, Browser grant and connection generation.
        if await authorize_run(owner, auth["runId"], auth["agentId"], auth["family"], active=False) != auth:
            raise DesktopError("run_forbidden", 403)
        if not store.enabled(owner):
            raise DesktopError("desktop_disabled", 503)
        gate = await can_write_back(owner)
        if gate is None:
            raise TimeoutError("capability probe")
        if not gate:
            raise DesktopError("cloud_merge_unavailable", 409)  # the Mac keeps its sign-ins
        target = record["target"]
        target = await store.resolve(
            auth,
            {
                "deviceId": target["deviceId"],
                "expectedConnectionGeneration": target["generation"],
                "command": "browser.proxy",
            },
        )
        lock = contextlib.nullcontext() if locked else store.browser.locked(profile_lock(device))
        async with lock:
            if not locked and _records.get(device) is not record:  # cleared, or another workspace
                return True
            mine, unread = await _read_mac(store, target, record["observed"])
            record["observed"] |= {o["origin"] for o in mine["origins"]}
        # Another load (here or on another backend task) may have fenced the profile since this
        # workspace loaded it: then what was read is not this workspace's to write back.
        try:
            await _still_loaded(target["deviceId"], record.get("load_id"))
        except LoadSuperseded:
            raise DesktopError("mac_profile_superseded", 409) from None
    except DesktopError as exc:  # authority, pairing, the gate, or the Mac's browser is gone
        logger.warning("mac browser state save dropped owner=%s: %s", owner, exc.code)
        put_metric("desktop.browser_state.save", dimensions={"result": "dropped"})
        return True
    except Exception as exc:  # noqa: BLE001 -- a probe, a read or the transport: retry
        logger.warning("mac browser state save retrying owner=%s: %s", owner, type(exc).__name__)
        put_metric("desktop.browser_state.save", dimensions={"result": "retry"})
        return False
    try:
        held = await _write_back(owner, mine, record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mac browser state save retrying owner=%s: %s", owner, type(exc).__name__)
        put_metric("desktop.browser_state.save", dimensions={"result": "retry"})
        return False
    if held:  # the run is over: nobody to tell but the logs
        logger.warning("mac browser state save held back %d vanished domain(s) owner=%s", held, owner)
    if unread:
        logger.info("mac browser state save left %d origin(s) unread owner=%s", unread, owner)
    record["pending_base"] = time.monotonic()
    return await _persist_base(device, record, owner)


async def _persist_base(device: str, record: dict, owner: str) -> bool:
    """Write the committed base to the Mac row, conditional on the load generation. Until it
    lands the run stays pending; past PENDING_BASE_SECONDS the row is marked untrusted (its
    digests cleared, still conditionally) so no later pre-merge reads a stale base."""
    try:
        await _put_mac_row(device, owner, record["observed"], record, expect=record.get("load_id"))
    except LoadSuperseded:
        # Another workspace loaded this profile after the commit: never relabel it; this base is
        # stale, and nothing more from this record is written back.
        logger.warning("mac browser state base dropped owner=%s: the profile was loaded by another", owner)
        put_metric("desktop.browser_state.save", dimensions={"result": "superseded"})
        if _records.get(device) is record:
            _records.pop(device)
        return True
    except Exception as exc:  # noqa: BLE001
        if time.monotonic() - record["pending_base"] < PENDING_BASE_SECONDS:
            logger.info("mac browser state base pending owner=%s: %s", owner, type(exc).__name__)
            return False
        await _untrust(device, record, owner)
        return True
    record.pop("pending_base", None)
    put_metric("desktop.browser_state.save", dimensions={"result": "ok"})
    return True


async def _untrust(device: str, record: dict, owner: str) -> None:
    """The Mac row's digests are stale and could not be replaced: clear them, so the next load
    finds no trusted base and skips the pre-merge (its changes were already committed)."""
    record.pop("pending_base", None)
    try:
        await _put_mac_row(device, owner, record["observed"], expect=record.get("load_id"))
    except Exception as exc:  # noqa: BLE001 -- superseded (someone else fenced) is fine too
        logger.warning("mac browser state base could not be untrusted owner=%s: %s", owner, type(exc).__name__)
    put_metric("desktop.browser_state.save", dimensions={"result": "untrusted"})


async def _watch(store, key: str, record: dict) -> None:
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
    if record.get("pending_base"):
        await _untrust(key, record, record["owner"])
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
    # The internal clear resolves the still-paired Mac even when Browser was switched off
    # (the grant no longer lists browser.proxy): only its own cookie/storage calls on the
    # agent profile follow. The Mac may still refuse, since desktop_permissions.rs enforces
    # Browser consent locally; the complete fix is the desktop-side wipe on Browser off.
    target = await connections._resolve_desktop_target(
        owner, cli["userId"], cli_id, generation, "browser.proxy", allow_revoking=True, allow_ungranted=True
    )
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
    record = _records.pop(cli_id, None) or {}  # nothing from this Mac is saved back any more
    async with store.browser.locked(profile_lock(cli_id)):
        try:
            mac = await _mac_row(cli_id)
            # The fence first: whatever the profile holds from here on is nobody's to merge.
            await _put_mac_row(cli_id, None, ())
        except Exception:  # noqa: BLE001 -- clear the cookies regardless
            mac = {}
        await _call(store, target, "_cookies_clear")
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
        missed = 0
        for origin in sorted(origins):
            try:
                await _replace_storage(store, target, origin, [])
            except Exception:  # noqa: BLE001 -- e.g. it redirected elsewhere: nothing touched
                missed += 1
        if missed:
            logger.warning("mac browser sign-in clear left %d origin(s) uncleared owner=%s", missed, owner)
    return True


CLEAR_MEMBER_SECONDS = 60
_clears: set[asyncio.Task] = set()


def clear_member_soon(owner: str, member_id: str) -> None:
    """``clear_member`` in the background, all of that member's Macs under one deadline."""

    async def run() -> None:
        try:
            await asyncio.wait_for(clear_member(owner, member_id), CLEAR_MEMBER_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mac browser sign-in clear gave up owner=%s: %s", owner, type(exc).__name__)

    task = asyncio.create_task(run())
    _clears.add(task)
    task.add_done_callback(_clears.discard)


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
