"""The Mac browser lease (spec §6): device pick, claim/fence, sync steps, revocation, messages."""

import asyncio
from unittest.mock import AsyncMock
import copy
import inspect
import logging

import httpx
import pytest
from botocore.exceptions import ClientError

from core.config import settings
from core.repositories import container_repo
from core.services import browser_control, desktop_execution, mac_browser, mac_cdp
from core.services.desktop_execution import DesktopError

OWNER, OTHER, MEMBER = "org_1", "org_2", "user_1"
NATIVE, CLI = "mac1", "cli1"
ADDRESS, PEER = "http://10.0.3.7:8000", "http://10.0.3.8:8000"
NOW = 1_000_000.0
ROOM = {"ownerId": OWNER, "memberId": MEMBER, "runId": "r1", "agentId": "a1", "family": "room", "roomId": "rm1"}
SAVED = f"browser-state/{OWNER}/s1.json"
ALL = {"browser.cdp.v1", "browser.cast.v1", "browser.wipe.v1", "desktop.operation.approve.v1", "system.notify"}


def _pair(native, cli, member, group):
    return {
        native: {
            "userId": member,
            "clientId": "openclaw-macos",
            "pairingGroupId": group,
            "displayName": f"{member}{mac_browser.PAIR_NAME_SUFFIX}",
        },
        cli: {"userId": member, "clientId": "node-host", "pairingGroupId": group},
    }


class Table:
    def __init__(self, db, writes):
        self.db, self.writes = db, writes

    def put_item(self, Item, ConditionExpression, ExpressionAttributeValues=None):
        current = self.db.get(Item["owner_id"]) or {}
        if ConditionExpression == "attribute_not_exists(load_id)":
            ok = "load_id" not in current
        else:
            assert ConditionExpression == "load_id = :seen"
            ok = current.get("load_id") == ExpressionAttributeValues[":seen"]
        if not ok:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.writes.append((ConditionExpression, (ExpressionAttributeValues or {}).get(":seen")))
        self.db[Item["owner_id"]] = copy.deepcopy(Item)


class H:
    def __init__(self):
        self.db = {OWNER: {"paired_devices": _pair(NATIVE, CLI, MEMBER, "pg1")}}
        self.writes, self.deleted, self.dispatched, self.copies, self.touched, self.metrics = [], [], [], [], [], []
        self.ready = {NATIVE: {"generation": "g1", "commands": set(ALL), "userId": MEMBER}}
        self.address = {NATIVE: ADDRESS}
        self.granted = set(ALL)
        self.state = {"generation": "x", "mode": "idle", "saved_key": SAVED}
        self.ensure = {"ok": True, "payload": {"profileId": "prof1"}}
        self.enabled = True
        self.resolve_error = None
        self.copy_error = None
        self.now = NOW

    @property
    def row(self):
        return self.db.get(mac_browser.ROW_PREFIX + NATIVE) or {}

    def set_row(self, **fields):
        self.db[mac_browser.ROW_PREFIX + NATIVE] = {
            "owner_id": mac_browser.ROW_PREFIX + NATIVE,
            "load_id": "L0",
        } | fields


@pytest.fixture
def h(monkeypatch):
    h = H()
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(mac_browser, "_clock", lambda: h.now)
    monkeypatch.setattr(mac_cdp, "_clock", lambda: int(h.now))
    monkeypatch.setattr(mac_cdp.task_address, "own_address", lambda: ADDRESS)
    mac_cdp._redeemed.clear()

    async def get_row(key, consistent=False):
        return copy.deepcopy(h.db.get(key))

    monkeypatch.setattr(container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr(container_repo, "_get_table", lambda: Table(h.db, h.writes))

    async def get_state(owner):
        return copy.deepcopy(h.state)

    monkeypatch.setattr(container_repo, "get_browser_state", get_state)

    async def touch(owner, at, field="human_input_at", *, create=False):
        h.touched.append((owner, at, field, create))

    monkeypatch.setattr(container_repo, "touch_browser_input", touch)

    async def ready(owner, device):
        return copy.deepcopy(h.ready.get(device))

    monkeypatch.setattr(mac_browser, "_ready", ready)
    monkeypatch.setattr(desktop_execution, "submissions_enabled", lambda owner: h.enabled)

    async def resolve(auth, request):
        device = request["deviceId"]
        if h.resolve_error:
            raise DesktopError(h.resolve_error, 409)
        ready = h.ready.get(device)
        if ready is None:
            raise DesktopError("desktop_not_ready", 409)
        if ready["generation"] != request["expectedConnectionGeneration"]:
            raise DesktopError("desktop_generation_stale", 409)
        effective = ready["commands"] & h.granted
        if request["command"] not in effective:
            raise DesktopError("desktop_forbidden", 409)
        entry = h.db[OWNER]["paired_devices"][device]
        return {
            "taskAddress": h.address.get(device, ADDRESS),
            "ownerId": auth["ownerId"],
            "memberId": entry["userId"],
            "deviceId": device,
            "clientId": "openclaw-macos",
            "pairingGroupId": entry["pairingGroupId"],
            "generation": ready["generation"],
            "commands": sorted(effective),
        }

    monkeypatch.setattr(desktop_execution, "_resolve", resolve)

    async def dispatch(target, request, invoke_id):
        h.dispatched.append((target["deviceId"], request["command"], request["params"]))
        if request["command"] == "browser.cdp.v1":
            return h.ensure
        return {"ok": True, "payload": {}}

    monkeypatch.setattr(desktop_execution, "_dispatch", dispatch)

    async def companion(auth, target, command=None):
        return target | {"approvalAuth": auth, "cliDeviceId": target["deviceId"], "cliGeneration": target["generation"]}

    monkeypatch.setattr(desktop_execution, "_resolve_companion", companion)

    async def reauthorize(auth):
        return None

    monkeypatch.setattr(desktop_execution, "_reauthorize", reauthorize)

    monkeypatch.setattr(browser_control, "_state_url", lambda key, action: f"{action}:{key}")

    async def delete_snapshot(key):
        h.deleted.append(key)

    monkeypatch.setattr(browser_control, "_delete_snapshot", delete_snapshot)

    async def publish(owner, old, new):
        if old != h.state:
            raise browser_control.BrowserError("changed")
        h.state = copy.deepcopy(new)
        return new

    monkeypatch.setattr(browser_control, "_publish", publish)

    def copy_object(source, key):
        if h.copy_error:
            raise ClientError({"Error": {"Code": h.copy_error}}, "CopyObject")
        h.copies.append((source, key))

    monkeypatch.setattr(mac_browser, "_copy", copy_object)
    monkeypatch.setattr(mac_browser, "_exists", lambda key: key not in h.deleted)
    monkeypatch.setattr(mac_browser, "put_metric", lambda name, dimensions: h.metrics.append((name, dimensions)))
    monkeypatch.setattr(mac_browser, "BUSY_RETRY_SECONDS", 0)
    from core.services import clerk_admin

    async def no_user(member):
        return None

    monkeypatch.setattr(clerk_admin, "get_user", no_user)
    mac_browser._first_names.clear()
    return h


async def _raises(code, coro, status=None):
    with pytest.raises(DesktopError) as err:
        await coro
    assert err.value.code == code
    assert err.value.message  # every error the agent sees carries its message
    if status is not None:
        assert err.value.status == status
    return err.value


async def _drain():
    await asyncio.gather(*list(mac_browser._tasks))


# ---- pick -----------------------------------------------------------------------------------


async def test_zero_arg_picks_the_requesters_ready_mac(h):
    h.db[OWNER]["paired_devices"] |= _pair("mac2", "cli2", "user_2", "pg2")  # another member's Mac
    h.ready["mac2"] = {"generation": "g9", "commands": set(ALL), "userId": "user_2"}
    out = await mac_browser.acquire(ROOM, None)
    assert out["deviceId"] == NATIVE and out["generation"] == "g1"
    assert out["cdpUrl"].startswith("ws://10.0.3.7:8000/api/v1/internal/mac-cdp/")
    assert out["deviceName"] is None  # a legacy pairing, and Clerk has no name for the member


async def test_legacy_pairing_names_the_mac_from_clerk(h, monkeypatch):
    from core.services import clerk_admin

    calls = []

    async def get_user(member):
        calls.append(member)
        return {"first_name": "Prasiddha"}

    monkeypatch.setattr(clerk_admin, "get_user", get_user)
    mac_browser._first_names.clear()
    assert await mac_browser._device_name(OWNER, NATIVE) == "Prasiddha's Mac"
    assert await mac_browser._device_name(OWNER, NATIVE) == "Prasiddha's Mac"
    assert calls == [MEMBER]  # cached per member
    mac_browser._first_names.clear()


async def test_device_name_is_the_members_first_name(h):
    h.db[OWNER]["paired_devices"][NATIVE]["displayName"] = "Prasiddha | Lightbulb Mac"
    assert (await mac_browser.acquire(ROOM, None))["deviceName"] == "Prasiddha's Mac"
    assert mac_browser.clean_name("<b>Eve</b>\x07 " + "y" * 50) == "bEve/b " + "y" * 25
    assert mac_browser.clean_name("  ") is None and mac_browser.clean_name(None) is None


async def test_zero_arg_without_a_ready_mac_is_no_mac(h):
    h.ready.clear()
    await _raises("no_mac", mac_browser.acquire(ROOM, None), 409)


async def test_zero_arg_with_two_ready_macs_is_choose_mac(h):
    h.db[OWNER]["paired_devices"] |= _pair("mac2", "cli2", MEMBER, "pg2")
    h.ready["mac2"] = {"generation": "g2", "commands": set(ALL), "userId": MEMBER}
    await _raises("choose_mac", mac_browser.acquire(ROOM, None), 409)


async def test_device_id_of_either_pair_member_maps_to_native(h):
    assert (await mac_browser.acquire(ROOM, CLI))["deviceId"] == NATIVE
    h.db.pop(mac_browser.ROW_PREFIX + NATIVE)  # its sync claim would answer "starting"
    assert (await mac_browser.acquire(ROOM, NATIVE))["deviceId"] == NATIVE


async def test_unpaired_device_is_access_ended(h):
    await _raises("browser_access_ended", mac_browser.acquire(ROOM, "ghost"), 403)
    h.db[OWNER]["paired_devices"][NATIVE]["revoking"] = True
    err = await _raises("browser_access_ended", mac_browser.acquire(ROOM, CLI), 403)
    assert err.message == "Browser access on that Mac was turned off."


async def test_generation_comes_from_ready_row_after_reconnect(h):
    assert (await mac_browser.acquire(ROOM, None))["generation"] == "g1"
    h.db.pop(mac_browser.ROW_PREFIX + NATIVE)
    h.ready[NATIVE]["generation"] = "g2"  # the Mac slept and reconnected
    out = await mac_browser.acquire(ROOM, None)
    assert out["generation"] == "g2"
    assert mac_cdp.redeem(out["cdpUrl"].rsplit("/", 1)[1])["generation"] == "g2"


# ---- resolve and ensure mapping ---------------------------------------------------------------


async def test_acquire_update_required_when_ready_row_lacks_wipe_marker(h):
    h.ready[NATIVE]["commands"] = {"desktop.operation.approve.v1", "system.notify"}
    await _raises("desktop_update_required", mac_browser.acquire(ROOM, None), 409)


async def test_acquire_update_required_when_old_mac_lacks_live_cast(h):
    h.ready[NATIVE]["commands"].remove("browser.cast.v1")
    await _raises("desktop_update_required", mac_browser.acquire(ROOM, None), 409)
    assert h.dispatched == []  # refuse before launching Chrome or claiming a browser lease


async def test_acquire_browser_off_when_new_app_lacks_cdp(h):
    h.ready[NATIVE]["commands"] = {"browser.wipe.v1", "system.notify"}
    await _raises("browser_off", mac_browser.acquire(ROOM, None), 409)


async def test_acquire_maps_not_ready_and_stale_codes_to_generation_lost(h):
    for code in ("desktop_not_ready", "desktop_generation_stale"):
        h.resolve_error = code
        err = await _raises("desktop_generation_lost", mac_browser.acquire(ROOM, None), 409)
        assert "Try again" in err.message


async def test_acquire_maps_mac_browser_off(h):
    h.ensure = {"ok": False, "error": {"code": "BROWSER_OFF"}}
    await _raises("browser_off", mac_browser.acquire(ROOM, None))


async def test_acquire_maps_chrome_missing(h):
    h.ensure = {"ok": False, "error": {"code": "CHROME_MISSING"}}
    await _raises("chrome_missing", mac_browser.acquire(ROOM, None))
    h.ensure = {"ok": False, "error": {"code": "WHATEVER"}}
    await _raises("chrome_start_failed", mac_browser.acquire(ROOM, None))
    h.ensure = {"ok": False, "error": {"code": "CAPACITY"}}
    err = await _raises("mac_browser_busy", mac_browser.acquire(ROOM, None))
    assert err.message == "This Mac's browser is busy; try again shortly."


async def test_acquire_disabled_owner(h):
    h.enabled = False
    await _raises("desktop_disabled", mac_browser.acquire(ROOM, None), 503)
    assert h.dispatched == []


# ---- the row: holder, claim, sync -------------------------------------------------------------


async def test_acquire_held_by_other_workspace(h, monkeypatch):
    h.db[OTHER] = {"paired_devices": _pair(NATIVE, CLI, "user_9", "pgx")}  # still paired there
    h.set_row(loaded_owner=OTHER, profile_id="prof1", base_key=f"browser-state/{OTHER}/b.json")

    async def get_organization(org_id):
        return {"name": "Acme"}

    monkeypatch.setattr("core.services.clerk_admin.get_organization", get_organization)
    err = await _raises("browser_profile_held_by_another_workspace", mac_browser.acquire(ROOM, None), 409)
    assert "Acme" in err.message
    assert h.row["loaded_owner"] == OTHER  # untouched


async def test_acquire_own_claim_answers_starting(h):
    h.set_row(loaded_owner=OWNER, profile_id="prof1", loading_owner=OWNER, loading_expires=NOW + 60)
    assert await mac_browser.acquire(ROOM, None) == mac_browser.STARTING
    h.set_row(loaded_owner=OWNER, profile_id="prof1", loading_owner=OTHER, loading_expires=NOW + 60)
    await _raises("browser_profile_held_by_another_workspace", mac_browser.acquire(ROOM, None))


async def test_stale_holder_same_profile_fences_and_wipes_in_background(h):
    old_base = f"browser-state/{OTHER}/macbase-b.json"
    h.set_row(loaded_owner=OTHER, profile_id="prof1", base_key=old_base)  # OTHER no longer pairs it
    assert await mac_browser.acquire(ROOM, None) == mac_browser.STARTING
    assert h.row["loaded_owner"] is None and h.row["load_id"] != "L0" and h.row["base_key"] is None
    assert h.row["wipe_profile"] == "prof1"  # dirty until the Mac reports a new profile id
    assert old_base in h.deleted
    await _drain()
    assert (NATIVE, "browser.wipe.v1", {}) in h.dispatched


async def test_fresh_claim_relabels_row_and_returns_load_step(h):
    out = await mac_browser.acquire(ROOM, None)
    row = h.row
    base_out = row["pending"]["base_out"]
    assert base_out.startswith(f"browser-state/{OWNER}/macbase-")
    assert out["sync"] == {
        "syncId": row["load_id"],
        "steps": [{"action": "load", "stateUrl": f"get_object:{SAVED}", "baseOutUrl": f"put_object:{base_out}"}],
    }
    assert row["loaded_owner"] == OWNER and row["profile_id"] == "prof1" and row["base_key"] is None
    assert row["loading_owner"] == OWNER and row["loading_expires"] == NOW + mac_browser.CLAIM_SECONDS
    assert row["pending"] == {"steps": ["load"], "expected": SAVED, "base_out": base_out, "saved_out": None}
    await mac_browser.check_holder(OWNER, NATIVE, "prof1")  # the run's own ticket passes at once


async def test_fresh_without_workspace_save_returns_base_step_into_state_url(h):
    h.state = None
    out = await mac_browser.acquire(ROOM, None)
    base_out = h.row["pending"]["base_out"]
    assert out["sync"]["steps"] == [{"action": "base", "stateUrl": f"put_object:{base_out}"}]


def _loaded(h, **extra):
    h.set_row(
        **{
            "loaded_owner": OWNER,
            "profile_id": "prof1",
            "base_key": f"browser-state/{OWNER}/macbase-old.json",
            "synced_key": f"browser-state/{OWNER}/s0.json",
            "leases": {},
        }
        | extra
    )


async def test_stale_returns_save_then_load_steps(h):
    _loaded(h)
    out = await mac_browser.acquire(ROOM, None)
    pending = h.row["pending"]
    assert pending["steps"] == ["save", "load"] and pending["expected"] == SAVED
    save, load = out["sync"]["steps"]
    assert save == {
        "action": "save",
        "previousStateUrl": f"get_object:{SAVED}",
        "baseStateUrl": f"get_object:browser-state/{OWNER}/macbase-old.json",
        "stateUrl": f"put_object:{pending['saved_out']}",
        "baseOutUrl": f"put_object:{pending['scratch']}",
    }
    assert load == {
        "action": "load",
        "stateUrl": f"get_object:{pending['saved_out']}",
        "baseOutUrl": f"put_object:{pending['base_out']}",
    }
    assert h.row["base_key"] == f"browser-state/{OWNER}/macbase-old.json"  # a claim, not a relabel


async def test_stale_skipped_while_other_run_leases_device(h):
    _loaded(h)
    h.db[mac_browser.ROW_PREFIX + NATIVE]["leases"] = {"r2": NOW + 100}
    out = await mac_browser.acquire(ROOM, None)
    assert out["sync"] is None and not h.row.get("loading_owner")
    assert set(h.row["leases"]) == {"r1", "r2"}


async def test_held_live_profile_acquire_saves_then_loads(h):
    """In sync with the workspace, a run's first acquire still saves first: the person may have
    signed in in the agent window between runs."""
    _loaded(h)
    h.db[mac_browser.ROW_PREFIX + NATIVE]["synced_key"] = SAVED
    out = await mac_browser.acquire(ROOM, None)
    assert [step["action"] for step in out["sync"]["steps"]] == ["save", "load"]
    assert h.row["pending"]["steps"] == ["save", "load"]


async def test_in_sync_renewal_needs_no_sync(h):
    _loaded(h)
    row = h.db[mac_browser.ROW_PREFIX + NATIVE]
    row["synced_key"] = SAVED
    row["leases"] = {ROOM["runId"]: NOW + 100}  # this run is already browsing: every call acquires
    assert (await mac_browser.acquire(ROOM, None))["sync"] is None


async def test_deferred_is_checked_before_fresh(h):
    h.set_row(loaded_owner=OWNER, profile_id="prof1", base_key=None, deferred_until=NOW + 30)
    out = await mac_browser.acquire(ROOM, None)
    assert out["sync"] is None and not h.row.get("loading_owner")
    h.now = NOW + 31  # the defer ran out: Fresh again
    assert (await mac_browser.acquire(ROOM, None))["sync"]["steps"][0]["action"] == "load"


async def test_acquire_returns_pipe_lease(h):
    out = await mac_browser.acquire(ROOM, None)
    assert out["humanInputAt"] == 0 and "framesAt" not in out  # the Mac casts itself (mac_live)
    assert h.row["leases"] == {"r1": int(NOW + mac_browser.LEASE_SECONDS)}
    assert h.touched == []  # no mac_browsing_at stamp any more
    claims = mac_cdp.redeem(out["cdpUrl"].rsplit("/", 1)[1])
    assert claims["deviceId"] == NATIVE and claims["profileId"] == "prof1" and claims["address"] == ADDRESS
    assert mac_cdp.auth_from_claims(claims) == ROOM
    # every row write was conditional on the load_id read
    assert all(cond in ("attribute_not_exists(load_id)", "load_id = :seen") for cond, _ in h.writes)


async def test_acquire_forwards_to_holder_task(h, monkeypatch):
    h.address[NATIVE] = PEER
    calls = []

    async def forward(address, body, path=mac_browser.INTERNAL_PATH, timeout=None):
        calls.append((address, body, path, timeout))
        return {"forwarded": True}

    monkeypatch.setattr(mac_browser, "_forward_mac", forward)
    assert await mac_browser.acquire(ROOM, None, budget_s=20) == {"forwarded": True}
    # The holder answers inside what is left of the caller's budget, less the hop back.
    assert calls == [
        (
            PEER,
            {"action": "mac_acquire", "auth": ROOM, "request": {"deviceId": NATIVE, "budgetS": 19}},
            mac_browser.INTERNAL_PATH,
            20,
        )
    ]
    assert h.dispatched == []  # nothing ran here
    await _raises("desktop_forward_loop", mac_browser.acquire(ROOM, NATIVE, forwarded=True))


def test_timeouts_fit_under_api_gateway_and_the_macs_launch():
    # The bridge reaches acquire through API Gateway (30 s): nothing on the path may outlive it.
    assert mac_browser.ACQUIRE_BUDGET_S < mac_browser.FORWARD_TIMEOUT_S < 30
    # The Mac's own worst launch is LAUNCH_TIMEOUT 10 + START_TIMEOUT 15; ensure outlasts it
    # (in the background: acquire answers STARTING instead of blocking on it).
    assert mac_browser.ENSURE_TIMEOUT_S >= 27


async def test_cold_launch_answers_starting_without_a_claim_then_the_poll_is_warm(h, monkeypatch):
    """A cold Chrome launch outlives the caller's budget: acquire answers STARTING before it
    claims anything, the launch keeps running, and the next poll finds it done."""
    launched = asyncio.Event()
    real = desktop_execution._dispatch
    ensures = []

    async def slow(target, request, invoke_id):
        if request["command"] == "browser.cdp.v1":
            ensures.append(1)
            await launched.wait()
        return await real(target, request, invoke_id)

    monkeypatch.setattr(desktop_execution, "_dispatch", slow)
    assert await mac_browser.acquire(ROOM, None, budget_s=mac_browser.DECIDE_RESERVE_S + 0.05) == mac_browser.STARTING
    assert h.row == {} and h.writes == []  # no claim that nobody would receive
    assert await mac_browser.acquire(ROOM, None, budget_s=mac_browser.DECIDE_RESERVE_S + 0.05) == mac_browser.STARTING
    assert len(ensures) == 1  # the poll joins the launch in flight
    launched.set()
    out = await mac_browser.acquire(ROOM, None)  # the launch's own answer serves this poll
    assert out["sync"]["steps"][0]["action"] == "load" and len(ensures) == 1
    assert mac_browser._ensures == {}


async def test_budget_spent_by_the_launch_answers_starting_before_the_claim(h, monkeypatch):
    real = mac_browser._invoke

    async def invoke(target, command, params, seconds):
        h.now += 22  # ensure answered, but only after 22 of the 25 s
        return await real(target, command, params, seconds)

    monkeypatch.setattr(mac_browser, "_invoke", invoke)
    assert await mac_browser.acquire(ROOM, None) == mac_browser.STARTING
    assert h.row == {}


async def test_forward_with_no_budget_left_answers_starting(h, monkeypatch):
    h.address[NATIVE] = PEER
    monkeypatch.setattr(mac_browser, "_forward_mac", pytest.fail)
    assert await mac_browser.acquire(ROOM, None, budget_s=mac_browser.DECIDE_RESERVE_S - 1) == mac_browser.STARTING


async def test_forward_keeps_error_message(monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    def handler(request):
        seen["secret"] = request.headers.get("x-lightbulb-ws-internal")
        return httpx.Response(409, json={"detail": {"code": "chrome_missing", "message": "From the holder."}})

    def client(**kwargs):
        seen["timeout"] = kwargs["timeout"]
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(httpx, "AsyncClient", client)
    err = await _raises("chrome_missing", mac_browser._forward_mac(PEER, {"action": "mac_acquire"}), 409)
    assert err.message == "From the holder." and seen == {"secret": "s3cret", "timeout": mac_browser.FORWARD_TIMEOUT_S}

    def bare(request):
        return httpx.Response(409, json={"detail": {"code": "browser_off"}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(bare), **kw))
    err = await _raises("browser_off", mac_browser._forward_mac(PEER, {}))
    assert err.message == mac_browser.MESSAGES["browser_off"]


# ---- release, synced ------------------------------------------------------------------------


async def test_release_returns_save_step_for_last_lease(h):
    _loaded(h, leases={"r1": NOW + 100})
    out = await mac_browser.release(ROOM, NATIVE, "g1")
    pending = h.row["pending"]
    assert pending["steps"] == ["save"] and pending["expected"] == SAVED
    assert {"sync": out["sync"]} == {
        "sync": {
            "syncId": h.row["load_id"],
            "steps": [
                {
                    "action": "save",
                    "previousStateUrl": f"get_object:{SAVED}",
                    "baseStateUrl": f"get_object:browser-state/{OWNER}/macbase-old.json",
                    "stateUrl": f"put_object:{pending['saved_out']}",
                    "baseOutUrl": f"put_object:{pending['base_out']}",
                }
            ],
        }
    }
    assert h.row["leases"] == {} and h.row["loading_owner"] == OWNER


async def test_release_with_a_save_carries_a_fresh_ticket(h):
    """The run's own ticket is single-use and may have expired: a dropped pipe redials this one."""
    _loaded(h, leases={"r1": NOW + 100})
    out = await mac_browser.release(ROOM, NATIVE, "g1")
    assert out["sync"]["steps"][0]["action"] == "save" and out["generation"] == "g1"
    claims = mac_cdp.redeem(out["cdpUrl"].rsplit("/", 1)[1])
    assert claims["profileId"] == "prof1" and mac_cdp.auth_from_claims(claims) == ROOM
    h.ready.clear()  # the Mac is not reachable now: the save still goes, on the run's own pipe
    _loaded(h, leases={"r1": NOW + 100})
    out = await mac_browser.release(ROOM, NATIVE, "g1")
    assert out["sync"] and "cdpUrl" not in out


async def test_release_waits_for_last_lease(h):
    _loaded(h, leases={"r1": NOW + 100, "r2": NOW + 100})
    assert await mac_browser.release(ROOM, NATIVE, "g1") == {"sync": None}
    assert h.row["leases"] == {"r2": NOW + 100} and not h.row.get("loading_owner")


async def test_release_skips_save_without_base(h):
    _loaded(h, leases={"r1": NOW + 100})
    h.db[mac_browser.ROW_PREFIX + NATIVE]["base_key"] = None
    assert await mac_browser.release(ROOM, NATIVE, "g1") == {"sync": None}
    assert not h.row.get("loading_owner")


async def test_synced_load_writes_base_conditionally_on_load_id(h):
    sync_id = (await mac_browser.acquire(ROOM, None))["sync"]["syncId"]
    base_out = h.row["pending"]["base_out"]
    h.writes.clear()
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, True) == {"ok": True}
    assert h.writes == [("load_id = :seen", sync_id)]
    row = h.row
    assert row["base_key"] == base_out and row["synced_key"] == SAVED
    assert row["loading_owner"] is None and row["pending"] is None and row["load_id"] == sync_id


async def test_synced_superseded_writes_nothing(h):
    sync_id = (await mac_browser.acquire(ROOM, None))["sync"]["syncId"]
    h.db[mac_browser.ROW_PREFIX + NATIVE]["load_id"] = "someone-else"
    h.writes.clear()
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, True) == {"ok": False, "superseded": True}
    assert h.writes == [] and h.deleted == []


async def test_synced_failed_load_defers(h):
    sync_id = (await mac_browser.acquire(ROOM, None))["sync"]["syncId"]
    base_out = h.row["pending"]["base_out"]
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, False) == {"ok": True}
    row = h.row
    assert row["base_key"] is None and row["deferred_until"] == NOW + mac_browser.DEFER_SECONDS
    assert row["loading_owner"] is None and base_out in h.deleted


async def _release_claim(h):
    _loaded(h, leases={"r1": NOW + 100})
    return (await mac_browser.release(ROOM, NATIVE, "g1"))["sync"]["syncId"]


async def test_synced_save_publishes_cas_and_carries_extension_storage(h):
    sync_id = await _release_claim(h)
    pending = h.row["pending"]
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, True) == {"ok": True}
    assert h.state["saved_key"] == pending["saved_out"] and h.state["generation"] == "x"
    assert h.copies == [(browser_control._ext_key(SAVED), browser_control._ext_key(pending["saved_out"]))]
    assert SAVED in h.deleted and f"browser-state/{OWNER}/macbase-old.json" in h.deleted
    assert h.row["base_key"] == pending["base_out"] and h.row["synced_key"] == pending["saved_out"]
    assert ("mac_browser.sync", {"kind": "save", "result": "ok"}) in h.metrics


async def test_synced_save_lost_cas_deletes_new_key(h):
    sync_id = await _release_claim(h)
    pending = h.row["pending"]
    h.state["saved_key"] = f"browser-state/{OWNER}/cloud.json"  # the cloud saved meanwhile
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, True) == {"ok": True}
    assert h.state["saved_key"] == f"browser-state/{OWNER}/cloud.json"
    assert pending["saved_out"] in h.deleted and pending["base_out"] in h.deleted
    assert h.row["base_key"] == f"browser-state/{OWNER}/macbase-old.json"  # the next acquire is Stale again
    assert h.row["synced_key"] == f"browser-state/{OWNER}/s0.json"


async def test_synced_save_copy_failure_is_not_skipped(h):
    sync_id = await _release_claim(h)
    h.copy_error = "AccessDenied"
    with pytest.raises(ClientError):
        await mac_browser.synced(ROOM, NATIVE, sync_id, True)
    assert h.state["saved_key"] == SAVED  # never published without the extension storage


async def test_synced_refuses_cloud_transition(h):
    sync_id = await _release_claim(h)
    h.state["mode"] = "transition"
    before = copy.deepcopy(h.state)
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, True) == {"ok": True}
    assert h.state == before
    assert ("mac_browser.sync", {"kind": "save", "result": "busy"}) in h.metrics


async def test_synced_ignores_bridge_named_keys(h):
    assert list(inspect.signature(mac_browser.synced).parameters) == ["auth", "device_id", "sync_id", "ok"]
    sync_id = await _release_claim(h)
    pending = h.row["pending"]
    await mac_browser.synced(ROOM, NATIVE, sync_id, True)
    assert h.state["saved_key"] == pending["saved_out"]  # only the claim's own key is published


async def test_failed_save_posts_a_muted_room_note(h, monkeypatch):
    from core.repositories import messaging_repo

    posted = []

    async def create_message(**kwargs):
        posted.append(kwargs)
        return {}, True

    monkeypatch.setattr(messaging_repo, "create_message", create_message)
    sync_id = await _release_claim(h)
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, False) == {"ok": True}
    [note] = posted
    assert note["room_id"] == "rm1" and note["silent"] is True and note["author"]["kind"] == "system"
    assert note["text"] == "Couldn't save the sign-ins from the Mac browser this time."
    assert h.row["base_key"] == f"browser-state/{OWNER}/macbase-old.json"  # the Mac keeps its changes


# ---- holder check, revocation ---------------------------------------------------------------


class _Connections:
    calls: list = []

    async def _resolve_desktop_target(self, owner, member, device, generation, command, **kwargs):
        _Connections.calls.append((device, command, kwargs))
        return {"taskAddress": ADDRESS, "ownerId": owner, "deviceId": device, "generation": generation}


@pytest.fixture
def connections(monkeypatch):
    _Connections.calls = []
    monkeypatch.setattr("core.services.connection_service.ConnectionService", _Connections)
    return _Connections.calls


async def test_clear_device_fences_and_wipes(h, connections):
    _loaded(h)
    h.db[OWNER]["paired_devices"][NATIVE]["revoking"] = True  # unpair marks REVOKING first
    assert await mac_browser.clear_device(OWNER, CLI) is True
    assert h.row["loaded_owner"] is None and h.row["load_id"] != "L0" and h.row["base_key"] is None
    assert f"browser-state/{OWNER}/macbase-old.json" in h.deleted
    assert (NATIVE, "browser.wipe.v1", {}) in h.dispatched
    assert ("mac_browser.clear", {"result": "ok"}) in h.metrics


async def test_clear_device_skips_other_workspace(h, connections):
    h.db[OTHER] = {"paired_devices": _pair(NATIVE, CLI, "user_9", "pgx")}
    h.set_row(loaded_owner=OTHER, profile_id="prof1", base_key=f"browser-state/{OTHER}/b.json")
    before = copy.deepcopy(h.row)
    assert await mac_browser.clear_device(OWNER, NATIVE) is True
    assert h.row == before and h.dispatched == [] and h.deleted == []
    assert ("mac_browser.clear", {"result": "skipped"}) in h.metrics


async def test_clear_device_wipes_when_browser_switched_off(h, connections):
    _loaded(h)
    h.ready[NATIVE]["commands"] = {"browser.wipe.v1"}  # Browser off: no browser.cdp.v1
    assert await mac_browser.clear_device(OWNER, NATIVE) is True
    [(device, command, kwargs)] = connections
    assert command == "browser.wipe.v1" and kwargs == {"allow_revoking": True, "allow_ungranted": True}
    assert (NATIVE, "browser.wipe.v1", {}) in h.dispatched


async def test_clear_member_clears_each_paired_mac(h, monkeypatch):
    h.db[OWNER]["paired_devices"] |= _pair("mac2", "cli2", MEMBER, "pg2") | _pair("mac3", "cli3", "user_2", "pg3")
    cleared = []

    async def clear(owner, device):
        cleared.append(device)
        if device == NATIVE:
            raise RuntimeError("offline")  # one failing Mac never stops the rest
        return True

    monkeypatch.setattr(mac_browser, "clear_device", clear)
    await mac_browser.clear_member(OWNER, MEMBER)
    assert sorted(cleared) == [NATIVE, "mac2"]


async def test_check_holder_refuses_profile_change(h):
    h.set_row(loaded_owner=OWNER, profile_id="prof1")
    await mac_browser.check_holder(OWNER, NATIVE, "prof1")
    await _raises("browser_profile_held_by_another_workspace", mac_browser.check_holder(OWNER, NATIVE, "prof2"), 403)
    await _raises("browser_profile_held_by_another_workspace", mac_browser.check_holder(OTHER, NATIVE, "prof1"), 403)


async def test_no_values_in_logs(h, caplog, monkeypatch):
    secret = "https://bucket.s3.amazonaws.com/browser-state/org_1/s1.json?X-Amz-Signature=abc&cookie=SID"

    async def boom(device):
        raise RuntimeError(secret)

    monkeypatch.setattr(mac_browser, "_row", boom)
    with caplog.at_level(logging.DEBUG, logger="core.services.mac_browser"):
        assert await mac_browser.clear_device(OWNER, NATIVE) is False
        await mac_browser.clear_member(OWNER, MEMBER)
    assert caplog.records
    for record in caplog.records:
        text = record.getMessage()
        assert "http" not in text and "browser-state/" not in text and "SID" not in text


async def test_every_raised_error_has_a_message(h):
    # Codes raised from the other modules (no message of their own) are given one here.
    assert mac_browser._with_message(DesktopError("desktop_peer_unavailable", 503)).message
    for code in (
        "desktop_disabled",
        "desktop_forbidden",
        "desktop_generation_lost",
        "desktop_update_required",
        "browser_off",
        "browser_access_ended",
        "chrome_missing",
        "chrome_start_failed",
        "browser_profile_held_by_another_workspace",
        "no_mac",
        "choose_mac",
        "mac_browser_busy",
    ):
        assert mac_browser.MESSAGES[code]
    h.resolve_error = "desktop_task_moved"  # an unmapped code from _resolve keeps its code, gains a message
    await _raises("desktop_task_moved", mac_browser.acquire(ROOM, None))


async def test_fenced_profile_stays_held_until_the_mac_reports_a_new_id(h, connections):
    _loaded(h)
    h.db[OWNER]["paired_devices"][NATIVE]["revoking"] = True
    ready = h.ready.pop(NATIVE)  # the Mac is offline: the unpair's wipe cannot reach it
    assert await mac_browser.clear_device(OWNER, NATIVE) is True
    assert h.row["loaded_owner"] is None and h.row["wipe_profile"] == "prof1"
    assert ("mac_browser.clear", {"result": "error"}) in h.metrics
    # Re-paired (here, or by another workspace): the old sign-ins are still in that profile.
    h.db[OWNER]["paired_devices"][NATIVE].pop("revoking")
    h.ready[NATIVE] = ready
    assert await mac_browser.acquire(ROOM, None) == mac_browser.STARTING  # not Fresh
    assert ("mac_browser.dirty_hold", {}) in h.metrics
    assert not h.row.get("loading_owner")
    await _drain()
    assert (NATIVE, "browser.wipe.v1", {}) in h.dispatched  # the wipe is retried
    h.ensure = {"ok": True, "payload": {"profileId": "prof2"}}  # wiped: a new profile
    out = await mac_browser.acquire(ROOM, None)
    assert out["sync"]["steps"][0]["action"] == "load" and h.row["wipe_profile"] is None


async def test_check_holder_refuses_a_dirty_profile(h):
    h.set_row(loaded_owner=OWNER, profile_id="prof1", wipe_profile="prof1")
    await _raises("browser_profile_held_by_another_workspace", mac_browser.check_holder(OWNER, NATIVE, "prof1"))


async def test_unpair_racing_a_pending_save_never_publishes_a_deleted_key(h, connections, monkeypatch):
    sync_id = await _release_claim(h)
    pending = h.row["pending"]
    publish = mac_browser._publish_save

    async def racing(owner, expected, new_key):
        await mac_browser.clear_device(OWNER, NATIVE)  # the unpair lands mid-synced
        return await publish(owner, expected, new_key)

    monkeypatch.setattr(mac_browser, "_publish_save", racing)
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, True) == {"ok": False, "superseded": True}
    assert pending["saved_out"] not in h.deleted  # the fence left the upload in place
    assert h.state["saved_key"] == pending["saved_out"]  # so what was published exists


async def test_publish_refuses_a_missing_upload(h):
    sync_id = await _release_claim(h)
    pending = h.row["pending"]
    h.deleted.append(pending["saved_out"])  # gone before the CAS
    assert await mac_browser.synced(ROOM, NATIVE, sync_id, True) == {"ok": True}
    assert h.state["saved_key"] == SAVED and SAVED not in h.deleted  # the previous save survives


def _revocation_repo(monkeypatch, rows):
    """Conditional pairing writes, as container_repo does them (moved from the old sign-in sync)."""

    async def get_row(owner, consistent=False):
        return rows.get(owner)

    async def mark(owner, device, *, user_id=None):
        entry = rows[owner]["paired_devices"].get(device)
        if entry is None or (user_id is not None and entry.get("userId") != user_id):
            return False
        entry["revoking"] = True
        return True

    async def remove(owner, device, user_id):
        entry = rows[owner]["paired_devices"].get(device) or {}
        if entry.get("revoking") is not True or entry.get("userId") != user_id:
            return False
        del rows[owner]["paired_devices"][device]
        return True

    monkeypatch.setattr(mac_browser.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr(mac_browser.container_repo, "mark_paired_device_revoking", mark)
    monkeypatch.setattr(mac_browser.container_repo, "remove_revoked_paired_device", remove)
    closed = []

    class Connections:
        async def delete_relay_rows_for_device(self, owner, device):
            closed.append((owner, device))

    monkeypatch.setattr("core.services.connection_service.ConnectionService", Connections)
    return closed


async def _drain_revocations():
    loop = asyncio.get_running_loop()
    await asyncio.gather(*[t for t in mac_browser._clears if t.get_loop() is loop])


async def test_revoke_member_soon_marks_clears_and_unpairs(monkeypatch):
    rows = {
        "org_w1": {
            "paired_devices": {
                "mac_1": {"userId": "member_1", "clientId": "openclaw-macos", "pairingGroupId": "p1"},
                "cli_1": {"userId": "member_1", "clientId": "node-host", "pairingGroupId": "p1"},
                "other": {"userId": "member_2", "clientId": "node-host", "pairingGroupId": "p2"},
            }
        }
    }
    closed = _revocation_repo(monkeypatch, rows)
    cleared = []

    async def clear(owner, member):
        # Marked REVOKING before the clear runs, so nothing else can use them meanwhile.
        assert rows[owner]["paired_devices"]["mac_1"]["revoking"] is True
        cleared.append((owner, member))

    monkeypatch.setattr(mac_browser, "clear_member", clear)
    mac_browser.revoke_member_soon("org_w1", "member_1")  # the webhook never waits
    await _drain_revocations()
    assert cleared == [("org_w1", "member_1")]
    assert set(rows["org_w1"]["paired_devices"]) == {"other"}  # only the removed member's go
    assert sorted(closed) == [("org_w1", "cli_1"), ("org_w1", "mac_1")]


async def test_revoke_member_soon_spares_a_re_pair_and_a_superseded_delete(monkeypatch):
    rows = {"org_w1": {"paired_devices": {"cli_1": {"userId": "member_1", "clientId": "node-host"}}}}
    closed = _revocation_repo(monkeypatch, rows)

    async def re_paired_while_clearing(owner, member):
        rows["org_w1"]["paired_devices"]["cli_1"] = {"userId": "member_1", "clientId": "node-host"}

    monkeypatch.setattr(mac_browser, "clear_member", re_paired_while_clearing)
    mac_browser.revoke_member_soon("org_w1", "member_1")
    await _drain_revocations()
    assert rows["org_w1"]["paired_devices"]["cli_1"] == {"userId": "member_1", "clientId": "node-host"}
    assert closed == []

    cleared = AsyncMock()
    monkeypatch.setattr(mac_browser, "clear_member", cleared)

    async def superseded():
        return False

    mac_browser.revoke_member_soon("org_w1", "member_1", superseded)
    await _drain_revocations()
    cleared.assert_not_awaited()
    assert "revoking" not in rows["org_w1"]["paired_devices"]["cli_1"]


SESSION = {
    "ownerId": OWNER,
    "memberId": MEMBER,
    "runId": "session-11111111-1111-4111-8111-111111111111",
    "agentId": "session",
    "family": "session",
    "roomId": "",
}


async def test_a_session_acquires_its_requesters_mac_and_learns_the_profile_id(h):
    out = await mac_browser.acquire(SESSION, None)
    assert out["deviceId"] == NATIVE and out["profileId"] == "prof1"
    assert h.row["leases"] == {SESSION["runId"]: int(NOW + mac_browser.LEASE_SECONDS)}
