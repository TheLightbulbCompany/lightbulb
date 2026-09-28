import asyncio
import time
import json
import logging

import pytest
from botocore.exceptions import ClientError

from core.services import browser_control, desktop_browser_state as sync
from core.services import desktop_execution as execution
from core.services.desktop_browser import STATE_ACTIONS, proxy
from core.services.node_broker import NodeSession
from tests.unit.services.test_desktop_browser import TAB, BrowserHarness, browser
from tests.unit.services.test_desktop_execution import AUTH

SECRET = "SECRET-COOKIE-VALUE-4f2a"
CAP = 1_900_000_000
TOKEN = "SECRET-STORAGE-VALUE-9c1d"
OWNER = AUTH["ownerId"]


def cookie(domain, value, name="sid"):
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


class Mac(BrowserHarness):
    """The Mac's openclaw-host cookie/storage routes over browser.proxy, in memory."""

    def __init__(self):
        super().__init__()
        self.target = self.target | {"deviceId": "device_1"}
        self.store.browser_state = sync
        self.jar = [cookie(".mac-only.test", "stale")]
        self.storage = {}
        self.next_tab = 0
        self.malformed = False
        self.hang_set = False
        self.redirects = {}
        self.fail_domain = None

    async def dispatch(self, target, request, invoke_id):
        p = request["params"]
        path, body, query = p["path"], p.get("body") or {}, p.get("query") or {}
        route = {"status": "resolved", "profile": "lightbulb", "driver": self.driver}
        state_paths = (
            "/cookies",
            "/cookies/clear",
            "/cookies/set-many",
            "/storage/local",
            "/storage/local/clear",
            "/storage/local/set",
        )
        if path == "/tabs/open" and "url" in body:
            self.next_tab += 1
            tab = f"{self.next_tab:032X}"
            self.targets.append(
                {"targetId": tab, "url": self.redirects.get(body["url"], body["url"] + "/"), "type": "page"}
            )
            self.storage.setdefault(body["url"], {})
            self.calls.append(p)
            return {"ok": True, "payload": {"route": route, "result": {"ok": True, "targetId": tab}}}
        if p["method"] == "DELETE" and path.startswith("/tabs/"):
            self.targets = [t for t in self.targets if t["targetId"] != path.removeprefix("/tabs/")]
            self.calls.append(p)
            return {"ok": True, "payload": {"route": route, "result": {"ok": True}}}
        if path not in state_paths:
            return await super().dispatch(target, request, invoke_id)
        self.calls.append(p)
        assert p["profile"] == "lightbulb"
        tab = (query or body).get("targetId")
        origin = next((sync._origin(t["url"]) for t in self.targets if t["targetId"] == tab), None)
        store = self.storage.setdefault(origin, {})
        result = {"ok": True}
        if path == "/cookies":
            result["cookies"] = None if self.malformed else [dict(c) for c in self.jar]
        elif path == "/cookies/clear":
            self.jar = []
        elif path == "/cookies/set-many":
            if any(c["domain"] == self.fail_domain for c in body["cookies"]):
                raise TimeoutError("batch lost")
            if self.hang_set:
                await asyncio.sleep(5)
            # Chrome caps expiry at 400 days on the way in.
            self.jar += [
                c | {"expires": min(c.get("expires", -1), CAP)} if c.get("expires", -1) > CAP else c
                for c in body["cookies"]
            ]
            result["added"] = len(body["cookies"])
        elif path == "/storage/local":
            result["values"] = dict(store)
        elif path == "/storage/local/clear":
            store.clear()
        else:
            store[body["key"]] = body["value"]
        return {"ok": True, "payload": {"route": route, "result": result}}


@pytest.fixture
def workspace(monkeypatch):
    """The fenced pointer row and the backend-only S3 prefix, in memory."""
    ws = {"row": None, "objects": {}, "reads": 0, "active": True, "mac": {}, "capable": True, "loads": []}

    async def get_run(owner, run_id):
        return {"status": "started" if ws["active"] else "succeeded"}

    async def get_state(owner):
        return ws["row"]

    async def s3_get(key):
        ws["reads"] += 1
        return ws["objects"][key]

    async def s3_put(key, body):
        assert key.startswith(f"browser-state/{OWNER}/")
        ws["objects"][key] = body

    async def s3_copy(source, key):
        if ws.get("copy_error"):
            raise ClientError({"Error": {"Code": ws["copy_error"]}}, "CopyObject")
        if source not in ws["objects"]:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "CopyObject")
        ws["objects"][key] = ws["objects"][source]

    async def publish(owner, old, new):
        if ws["row"] != old:
            raise browser_control.BrowserError("changed")
        ws["row"] = new
        return new

    async def delete(key):
        ws["objects"].pop(key, None)
        ws["objects"].pop(browser_control._ext_key(key), None)

    async def authorize_run(owner, run, agent, family, *, active):
        return AUTH

    monkeypatch.setattr(sync.container_repo, "get_browser_state", get_state)
    monkeypatch.setattr(sync, "_s3_get", s3_get)
    monkeypatch.setattr(sync, "_s3_put", s3_put)
    monkeypatch.setattr(sync, "_s3_copy", s3_copy)
    monkeypatch.setattr(browser_control, "_publish", publish)
    monkeypatch.setattr(browser_control, "_delete_snapshot", delete)
    monkeypatch.setattr(execution, "authorize_run", authorize_run)

    async def mac_row(device):
        return dict(ws["mac"].get(device, {}))

    async def put_mac_row(device, owner, origins, base=None, *, expect=None):
        current = ws["mac"].get(device, {})
        if owner is not None and current.get("load_id") != expect:
            raise sync.LoadSuperseded()
        load_id = f"load-{len(ws['loads'])}" if owner is None else expect
        if owner is None:
            ws["loads"].append(load_id)
        row = {"loaded_owner": owner, "origins": sorted(origins), "load_id": load_id}
        if base is not None and owner is not None:
            row |= {"cookies": dict(base["cookies"]), "storage": dict(base["origins"])}
        ws["mac"][device] = row
        return load_id

    async def capable(owner, timeout_s=0):
        return ws["capable"]

    monkeypatch.setattr(sync, "_merge_capable", capable)

    monkeypatch.setattr(sync, "_mac_row", mac_row)
    monkeypatch.setattr(sync, "_put_mac_row", put_mac_row)
    monkeypatch.setattr(sync.messaging_repo, "get_run", get_run)
    monkeypatch.setattr(sync, "POLL_SECONDS", 0.01)
    sync._records.clear()
    sync._deferred.clear()
    sync._pending_rows.clear()

    def save(state, key="browser-state/user_1/g1.json"):
        ws["objects"][key] = json.dumps(state).encode()
        ws["row"] = {"generation": "g", "mode": "idle", "saved_key": key}

    ws["save"] = save
    ws["saved"] = lambda: json.loads(ws["objects"][ws["row"]["saved_key"]])
    yield ws
    sync._records.clear()
    sync._deferred.clear()
    sync._pending_rows.clear()
    sync._watchers.clear()  # a finished test's watcher belongs to its own, closed loop


async def end_run(workspace):
    workspace["active"] = False
    loop = asyncio.get_running_loop()
    await asyncio.gather(*[t for t in sync._watchers if t.get_loop() is loop])


async def test_workspace_state_loads_before_the_first_page_action(workspace, monkeypatch):
    workspace["save"](
        {
            "cookies": [cookie(".example.com", SECRET)],
            "origins": [{"origin": "https://example.com", "localStorage": [{"name": "token", "value": TOKEN}]}],
        }
    )
    h = Mac()
    tabs = await h.run("tabs")
    assert tabs["state"] == "succeeded", tabs
    paths = [c["path"] for c in h.calls]
    assert paths.index("/cookies/clear") < paths.index("/cookies/set-many") < paths.index("/tabs")
    agent_tabs = max(i for i, path in enumerate(paths) if path == "/tabs")  # the action's own read
    assert paths.index("/storage/local/set") < agent_tabs
    assert h.jar == [cookie(".example.com", SECRET)]  # the Mac-only cookie did not survive the load
    assert h.storage["https://example.com"] == {"token": TOKEN}
    assert "workspaceSignIns" not in tabs["result"]
    # Loaded once per Mac: the next action does not clear the profile again.
    await h.run("snapshot", target=TAB, browserGeneration=tabs["result"]["tabs"][0]["browserGeneration"])
    assert [c["path"] for c in h.calls].count("/cookies/clear") == 1
    await end_run(workspace)


async def test_run_end_merges_per_origin_under_the_fence(workspace, monkeypatch):
    workspace["save"](
        {
            "cookies": [cookie(".a.test", "a0"), cookie(".b.test", "b0"), cookie(".gone.test", "g0")],
            "origins": [{"origin": "https://a.test", "localStorage": [{"name": "k", "value": "a0"}]}],
        }
    )
    workspace["objects"]["browser-state/user_1/g1.extensions.json"] = b'{"ext": {}}'
    h = Mac()
    assert (await h.run("tabs"))["state"] == "succeeded"
    # On the Mac: a.test signs in again, gone.test signs out, new.test signs in.
    h.jar = [cookie(".a.test", "a1"), cookie(".b.test", "b0"), cookie(".new.test", "n1")]
    h.targets = [{"targetId": TAB, "url": "https://new.test/page", "type": "page"}]
    h.storage["https://new.test"] = {"k": "n1"}
    # Meanwhile the cloud browser saved a new b.test sign-in.
    workspace["save"](
        {
            "cookies": [cookie(".a.test", "a0"), cookie(".b.test", "b-cloud"), cookie(".gone.test", "g0")],
            "origins": [{"origin": "https://a.test", "localStorage": [{"name": "k", "value": "a0"}]}],
        },
        key="browser-state/user_1/g2.json",
    )
    workspace["objects"]["browser-state/user_1/g2.extensions.json"] = b'{"ext": {"x": 1}}'
    await end_run(workspace)
    saved = workspace["saved"]()
    assert {c["domain"]: c["value"] for c in saved["cookies"]} == {
        ".a.test": "a1",
        ".b.test": "b-cloud",
        ".new.test": "n1",
    }
    assert {o["origin"]: o["localStorage"] for o in saved["origins"]} == {
        "https://a.test": [{"name": "k", "value": "a0"}],
        "https://new.test": [{"name": "k", "value": "n1"}],
    }
    key = workspace["row"]["saved_key"]
    assert key.startswith("browser-state/user_1/mac-") and workspace["row"]["generation"] == "g"
    assert workspace["objects"][browser_control._ext_key(key)] == b'{"ext": {"x": 1}}'
    assert "browser-state/user_1/g2.json" not in workspace["objects"]  # replaced like a cloud save
    assert not sync._records


async def test_a_failed_load_is_a_note_and_never_saves_back(workspace, monkeypatch, caplog):
    workspace["row"] = {"generation": "g", "mode": "idle", "saved_key": "browser-state/user_1/missing.json"}
    h = Mac()
    caplog.set_level(logging.INFO)
    tabs = await h.run("tabs")
    assert tabs["state"] == "succeeded"
    assert tabs["result"]["workspaceSignIns"] == sync.LOAD_FAILED
    assert "/cookies/clear" not in [c["path"] for c in h.calls]  # the unloaded profile is untouched
    h.jar = [cookie(".mac.test", SECRET)]
    await end_run(workspace)
    assert workspace["row"]["saved_key"] == "browser-state/user_1/missing.json"
    assert "/cookies" not in [c["path"] for c in h.calls]
    assert SECRET not in caplog.text


async def test_the_everyday_chrome_profile_is_never_loaded_or_saved(workspace):
    workspace["save"]({"cookies": [cookie(".example.com", SECRET)], "origins": []})
    for action in STATE_ACTIONS:
        with pytest.raises(execution.DesktopError, match="browser_state_profile_refused"):
            proxy(action, "chrome", None, {}, 1000)
    h = Mac()
    seen = []

    async def dispatch(target, request, invoke_id):
        p = request["params"]
        seen.append(p)
        route = {"status": "resolved", "profile": "chrome", "driver": "extension"}
        if p["path"] == "/tabs/focus":
            return {
                "ok": True,
                "payload": {"route": route, "error": {"status": 400, "body": {"error": "targetId is required"}}},
            }
        return {"ok": True, "payload": {"route": route, "result": {"tabs": []}}}

    h.store.dispatch = dispatch
    receipt = await h.store.submit(
        AUTH, h.store.admit(AUTH, h.target), browser("tabs", profile="chrome") | {"deviceId": "device_1"}
    )
    await asyncio.gather(*h.store.tasks.values())
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "succeeded"
    assert [p["path"] for p in seen] == ["/tabs/focus", "/tabs"]
    assert {p["profile"] for p in seen} == {"chrome"}
    assert workspace["reads"] == 0 and not sync._records


async def test_no_browser_consent_means_no_load(workspace):
    workspace["save"]({"cookies": [cookie(".example.com", SECRET)], "origins": []})
    h = Mac()

    async def resolve(auth, request):
        raise execution.DesktopError("desktop_forbidden", 409)  # browser.proxy not granted at pairing

    h.store.resolve = resolve
    with pytest.raises(execution.DesktopError, match="desktop_forbidden"):
        await h.run("tabs")
    assert not h.calls and workspace["reads"] == 0 and not sync._records


async def test_a_removed_member_mac_never_writes_back(workspace, monkeypatch):
    workspace["save"]({"cookies": [cookie(".example.com", "v0")], "origins": []})
    h = Mac()
    await h.run("tabs")
    h.jar = []

    async def forbidden(*_args, **_kwargs):
        raise execution.DesktopError("run_forbidden", 403)

    monkeypatch.setattr(execution, "authorize_run", forbidden)
    await end_run(workspace)
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"


async def test_cookie_and_storage_values_never_reach_receipts_rows_or_logs(workspace, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    workspace["save"](
        {
            "cookies": [cookie(".example.com", SECRET)],
            "origins": [{"origin": "https://example.com", "localStorage": [{"name": "t", "value": TOKEN}]}],
        }
    )
    h = Mac()
    tabs = await h.run("tabs")
    snap = await h.run("snapshot", target=TAB, browserGeneration=tabs["result"]["tabs"][0]["browserGeneration"])

    h.jar = h.jar + [cookie(".new.test", "n")]  # something to save

    async def conflict(*_):
        raise browser_control.BrowserError("changed")

    monkeypatch.setattr(browser_control, "_publish", conflict)  # the save fails: its log line too
    workspace["active"] = False
    for _ in range(30):
        await asyncio.sleep(0.01)
    assert "mac browser state save retrying" in caplog.text
    sync._records.clear()
    await end_run(workspace)
    for data in (tabs, snap, h.store.records, caplog.text):
        assert SECRET not in str(data) and TOKEN not in str(data)


def test_merge_writes_only_what_the_mac_changed():
    base = {
        "cookies": {".a": sync._cookie_digest([cookie(".a", "1")])},
        "skipped_domains": {".skip"},
        "origins": {},
        "skipped_origins": {"https://s.test"},
    }
    theirs = {
        "cookies": [cookie(".a", "1"), cookie(".skip", "cloud")],
        "origins": [{"origin": "https://s.test", "localStorage": [{"name": "k", "value": "cloud"}]}],
    }
    mine = {
        "cookies": [cookie(".skip", "mac")],
        "origins": [{"origin": "https://s.test", "localStorage": [{"name": "k", "value": "mac"}]}],
    }
    merged = sync.merge(theirs, mine, base)
    # .a was signed out on the Mac: the deletion propagates. Skipped ones keep the cloud's.
    assert merged["cookies"] == [cookie(".skip", "cloud")]
    assert merged["origins"] == theirs["origins"]


async def test_unpair_clears_the_mac_agent_profile(workspace, monkeypatch):
    workspace["save"](
        {"cookies": [], "origins": [{"origin": "https://example.com", "localStorage": [{"name": "t", "value": TOKEN}]}]}
    )
    h = Mac()
    await h.run("tabs")
    h.jar = [cookie(".example.com", SECRET)]
    row = {
        "paired_devices": {
            "mac_1": {"userId": "user_1", "clientId": "openclaw-macos", "pairingGroupId": "pair_1"},
            "device_1": {"userId": "user_1", "clientId": "node-host", "pairingGroupId": "pair_1"},
        }
    }

    async def get_row(owner, consistent=False):
        return row

    class Connections:
        table_name = "t"

        class _client:
            @staticmethod
            def get_item(**_):
                return {"Item": {"generation": {"S": "gen_1"}}}

        desktop_ready_id = staticmethod(lambda owner, device: device)

        async def _resolve_desktop_target(self, owner, member, device, generation, command, **_):
            assert (device, command) == ("device_1", "browser.proxy")
            assert _.get("allow_ungranted") is True  # Browser switched off still gets the clear
            return h.target

    monkeypatch.setattr(sync.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr("core.services.connection_service.ConnectionService", Connections)
    monkeypatch.setattr(execution, "operations", lambda: h.store)
    fenced_before_clear = []
    real_put = sync._put_mac_row

    async def put(device, owner, origins, base=None):
        if owner is None:
            fenced_before_clear.append("/cookies/clear" not in [c["path"] for c in h.calls[-3:]])
        await real_put(device, owner, origins, base)

    monkeypatch.setattr(sync, "_put_mac_row", put)
    assert await sync.clear_device(OWNER, "mac_1")
    assert h.jar == [] and h.storage["https://example.com"] == {}
    assert fenced_before_clear == [True]  # the fence is written before the profile changes
    assert not sync._records  # nothing from this Mac is saved back any more


async def test_member_removal_clears_only_that_members_macs_and_never_raises(monkeypatch):
    row = {
        "paired_devices": {
            "cli_a": {"userId": "user_a", "clientId": "node-host"},
            "mac_a": {"userId": "user_a", "clientId": "openclaw-macos"},
            "cli_a2": {"userId": "user_a", "clientId": "node-host"},
            "cli_b": {"userId": "user_b", "clientId": "node-host"},
        }
    }
    cleared = []

    async def get_row(owner, consistent=False):
        return row

    async def clear(owner, device_id):
        cleared.append(device_id)
        raise execution.DesktopError("desktop_target_not_ready", 409)  # an offline Mac

    monkeypatch.setattr(sync.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr(sync, "clear_device", clear)
    await sync.clear_member(OWNER, "user_a")
    assert cleared == ["cli_a", "cli_a2"]


async def test_an_extension_snapshot_copy_failure_aborts_the_save(workspace):
    workspace["save"]({"cookies": [cookie(".a.test", "a0")], "origins": []})
    workspace["objects"]["browser-state/user_1/g1.extensions.json"] = b'{"ext": {"x": 1}}'
    h = Mac()
    await h.run("tabs")
    h.jar = [cookie(".a.test", "a1")]
    workspace["copy_error"] = "AccessDenied"
    workspace["active"] = False
    for _ in range(30):
        await asyncio.sleep(0.01)
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"
    assert set(workspace["objects"]) == {"browser-state/user_1/g1.json", "browser-state/user_1/g1.extensions.json"}
    workspace["copy_error"] = None  # the save stayed pending and lands once the copy works
    await end_run(workspace)
    assert workspace["objects"][browser_control._ext_key(workspace["row"]["saved_key"])] == b'{"ext": {"x": 1}}'


def test_an_expiry_renewal_is_a_change_the_mac_made():
    old, renewed = cookie(".a", "1"), cookie(".a", "1") | {"expires": 2_000_000_000}
    base = {
        "cookies": {".a": sync._cookie_digest([old])},
        "skipped_domains": set(),
        "origins": {},
        "skipped_origins": set(),
    }
    merged = sync.merge({"cookies": [old], "origins": []}, {"cookies": [renewed], "origins": []}, base)
    assert merged["cookies"] == [renewed]


async def test_unpair_clears_origins_the_mac_reached_during_an_unsaved_run(workspace, monkeypatch):
    workspace["save"]({"cookies": [], "origins": []})
    h = Mac()
    await h.run("open", args={"url": "https://visited.test/login"})
    h.storage["https://visited.test"] = {"session": TOKEN}  # written during the run, never saved
    h.targets = [t for t in h.targets if "visited" not in t["url"]]  # its tab is closed again
    h.targets.append({"targetId": "C" * 32, "url": "https://open.test/x", "type": "page"})
    h.storage["https://open.test"] = {"session": TOKEN}
    row = {
        "paired_devices": {
            "mac_1": {"userId": "user_1", "clientId": "openclaw-macos", "pairingGroupId": "pair_1"},
            "device_1": {"userId": "user_1", "clientId": "node-host", "pairingGroupId": "pair_1"},
        }
    }

    async def get_row(owner, consistent=False):
        return row

    class Connections:
        table_name = "t"

        class _client:
            @staticmethod
            def get_item(**_):
                return {"Item": {"generation": {"S": "gen_1"}}}

        desktop_ready_id = staticmethod(lambda owner, device: device)

        async def _resolve_desktop_target(self, owner, member, device, generation, command, **_):
            return h.target

    monkeypatch.setattr(sync.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr("core.services.connection_service.ConnectionService", Connections)
    monkeypatch.setattr(execution, "operations", lambda: h.store)
    workspace["mac"]["device_1"] = {"loaded_owner": OWNER, "origins": ["https://earlier-run.test"]}
    h.storage["https://earlier-run.test"] = {"session": TOKEN}  # from a run this record never saw
    assert await sync.clear_device(OWNER, "device_1")
    assert h.storage["https://visited.test"] == {} and h.storage["https://open.test"] == {}
    assert h.storage["https://earlier-run.test"] == {}  # (d) the persisted observed set
    assert {k: v for k, v in workspace["mac"]["device_1"].items() if k != "load_id"} == {
        "loaded_owner": None,
        "origins": [],
    }


def bundle(cookies, origins=()):
    return {"cookies": cookies, "origins": [{"origin": o, "localStorage": items} for o, items in origins]}


def synced(cookies, storage=()):
    """A Mac row as a previous full load/save of this workspace left it."""
    return {
        "loaded_owner": OWNER,
        "load_id": "load-prior",
        "origins": [o for o, _ in storage],
        "cookies": {d: sync._cookie_digest(items) for d, items in sync._by_domain(cookies).items()},
        "storage": {o: sync._storage_digest(items) for o, items in storage},
    }


async def test_pre_load_merge_is_three_way_and_never_resurrects_a_cloud_sign_out(workspace):
    last = [cookie(".changed.test", "c0"), cookie(".same.test", "s0"), cookie(".signedout.test", "x0")]
    workspace["mac"]["device_1"] = synced(last, [("https://mac.test", [{"name": "k", "value": "v0"}])])
    # Since then the cloud renewed same.test and signed out of signedout.test.
    workspace["save"](
        bundle(
            [cookie(".changed.test", "c0"), cookie(".same.test", "s-cloud")],
            [("https://mac.test", [{"name": "k", "value": "v0"}])],
        )
    )
    h = Mac()
    # ...and this Mac (its save skipped) changed changed.test, added new.test, and kept the rest.
    h.jar = [
        cookie(".changed.test", "c-mac"),
        cookie(".same.test", "s0"),
        cookie(".signedout.test", "x0"),
        cookie(".new.test", "n"),
    ]
    h.storage["https://mac.test"] = {"k": "v-mac"}
    assert (await h.run("tabs"))["state"] == "succeeded"
    saved = workspace["saved"]()
    assert {c["domain"]: c["value"] for c in saved["cookies"]} == {
        ".changed.test": "c-mac",
        ".same.test": "s-cloud",
        ".new.test": "n",
    }  # no .signedout.test
    assert {o["origin"]: o["localStorage"] for o in saved["origins"]} == {
        "https://mac.test": [{"name": "k", "value": "v-mac"}]
    }
    assert {c["domain"]: c["value"] for c in h.jar} == {
        ".changed.test": "c-mac",
        ".same.test": "s-cloud",
        ".new.test": "n",
    }
    await end_run(workspace)


async def test_another_workspaces_leftovers_never_enter_this_workspace(workspace):
    workspace["save"](bundle([cookie(".ws.test", "w")]))
    other = synced([cookie(".other.test", "theirs")], [("https://other.test", [])])
    workspace["mac"]["device_1"] = other | {"loaded_owner": "org_other"}
    h = Mac()
    h.jar = [cookie(".other.test", "theirs2")]
    h.storage["https://other.test"] = {"t": "theirs"}
    await h.run("tabs")
    assert [c["domain"] for c in workspace["saved"]()["cookies"]] == [".ws.test"]
    assert h.jar == [cookie(".ws.test", "w")] and h.storage["https://other.test"] == {}  # cleared
    assert workspace["mac"]["device_1"]["loaded_owner"] == OWNER
    await end_run(workspace)


async def test_a_load_that_dies_after_the_fence_leaves_the_profile_nobodys(workspace, monkeypatch):
    workspace["mac"]["device_1"] = synced([cookie(".w1.test", "w1")])
    workspace["save"](bundle([cookie(".w1.test", "w1")]))
    monkeypatch.setattr(sync, "LOAD_SECONDS", 0.2)
    h = Mac()
    h.jar = [cookie(".w1.test", "w1")]
    h.hang_set = True  # another workspace's load, cut off between its cookie phase and the row write
    tabs = await h.run("tabs")
    assert tabs["result"]["workspaceSignIns"] == sync.LOAD_FAILED
    assert workspace["mac"]["device_1"]["loaded_owner"] is None
    # The profile now holds someone else's cookies; this workspace's next load must not merge them.
    sync._records.clear()
    h.hang_set = False
    h.jar = [cookie(".w2.test", "not-ours")]
    await h.run("tabs")
    assert [c["domain"] for c in workspace["saved"]()["cookies"]] == [".w1.test"]
    await end_run(workspace)


async def test_until_writes_can_merge_a_synced_mac_keeps_its_own_changes(workspace):
    workspace["capable"] = False
    workspace["mac"]["device_1"] = synced([cookie(".a.test", "a0")])
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    h.jar = [cookie(".a.test", "mac-changed")]
    tabs = await h.run("tabs")
    assert tabs["state"] == "succeeded" and tabs["result"]["workspaceSignIns"] == sync.MERGE_GATED
    assert h.jar == [cookie(".a.test", "mac-changed")]  # not wiped by a load
    assert "/cookies/clear" not in [c["path"] for c in h.calls]
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"
    assert not sync._records  # a later action tries again


async def test_a_fresh_mac_still_loads_while_writes_are_gated_but_never_writes_back(workspace):
    workspace["capable"] = False
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    tabs = await h.run("tabs")
    assert tabs["result"]["workspaceSignIns"] == sync.MERGE_GATED
    assert h.jar == [cookie(".a.test", "a0")]
    h.jar = [cookie(".a.test", "a1")]
    await end_run(workspace)
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"


async def test_a_live_cloud_session_without_a_base_closes_the_gate(workspace):
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    workspace["row"] |= {"session_id": "live-old-image-session"}  # no base_key
    assert not await sync.can_write_back(OWNER)
    workspace["row"] |= {"base_key": "browser-state/user_1/g.base.json"}
    assert await sync.can_write_back(OWNER)


async def test_a_pre_merge_that_hits_a_busy_cloud_save_defers_the_load(workspace):
    workspace["mac"]["device_1"] = synced([cookie(".a.test", "a0")])
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    workspace["row"]["mode"] = "transition"
    h = Mac()
    h.jar = [cookie(".a.test", "mac-changed")]
    tabs = await h.run("tabs")
    assert tabs["result"]["workspaceSignIns"] == sync.MAC_READ_FAILED
    assert h.jar == [cookie(".a.test", "mac-changed")]


def test_a_truncated_base_skips_what_it_dropped():
    base = {
        "cookies": {".known.test": sync._cookie_digest([cookie(".known.test", "k0")])},
        "skipped_domains": set(),
        "origins": {},
        "skipped_origins": set(),
        "truncated": True,
    }
    theirs = bundle(
        [cookie(".known.test", "k0"), cookie(".dropped.test", "cloud")],
        [("https://o.test", [{"name": "k", "value": "c"}])],
    )
    mine = bundle(
        [cookie(".known.test", "k1"), cookie(".dropped.test", "mac")],
        [("https://o.test", [{"name": "k", "value": "m"}])],
    )
    merged = sync.merge(theirs, mine, base)
    assert {c["domain"]: c["value"] for c in merged["cookies"]} == {".known.test": "k1", ".dropped.test": "cloud"}
    assert merged["origins"] == theirs["origins"]


def test_unknown_cookie_fields_pass_through_untouched():
    part = cookie(".p.test", "v") | {"partitionKey": "https://top.test", "priority": "High"}
    state = sync._state({"cookies": [part], "origins": []})
    assert state["cookies"] == [part]
    assert list(sync._batches([part], [])) == [[cookie(".p.test", "v")]]  # the node only gets what it takes


async def test_the_real_gate_cache_and_mac_row_work_together(monkeypatch):
    """No fakes for the gate, its cache or the Mac row: only the runtime and the table."""
    import core.services.container_rpc as container_rpc

    calls = []

    async def health(owner, method, params, timeout_s=30.0):
        calls.append(method)
        return {"ready": True, "browserStateMerge": 1}

    table = {}

    class Table:
        def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
            current = table.get(Item["owner_id"], {})
            ok = (
                ConditionExpression is None
                or ("attribute_not_exists" in ConditionExpression and "load_id" not in current)
                or current.get("load_id") == (ExpressionAttributeValues or {}).get(":expect")
            )
            if not ok:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
            table[Item["owner_id"]] = dict(Item)

    async def get(owner_id, consistent=False):
        return table.get(owner_id)

    async def state(owner):
        return {"session_id": "s", "base_key": "browser-state/o/g.base.json"}

    monkeypatch.setattr(container_rpc, "call", health)
    monkeypatch.setattr(sync.container_repo, "_get_table", lambda: Table())
    monkeypatch.setattr(sync.container_repo, "get_by_owner_id", get)
    monkeypatch.setattr(sync.container_repo, "get_browser_state", state)
    sync._capable.clear()
    assert await sync.can_write_back("org_real") and await sync.can_write_back("org_real")
    assert calls == ["health"]  # cached
    base = {"cookies": {f".d{i}.test": "x" for i in range(sync.MAX_OBSERVED + 1)}, "origins": {}}
    fence = await sync._put_mac_row("dev", None, ())
    await sync._put_mac_row("dev", "org_real", {"https://a.test"}, base, expect=fence)
    row = await sync._mac_row("dev")
    assert row["loaded_owner"] == "org_real" and row["truncated"] is True
    assert len(row["cookies"]) == sync.MAX_OBSERVED and row["origins"] == ["https://a.test"]
    cut = {"cookies": dict(row["cookies"]), "origins": {}, "truncated": row["truncated"]}
    await sync._put_mac_row("dev", "org_real", (), cut, expect=fence)  # re-writing an already-cut base
    assert (await sync._mac_row("dev"))["truncated"] is True
    await sync._put_mac_row("dev", None, ())
    assert (await sync._mac_row("dev")).get("cookies") is None  # the fence keeps no base
    with pytest.raises(sync.LoadSuperseded):  # a late write from before that fence
        await sync._put_mac_row("dev", "org_real", (), cut, expect=fence)
    sync._capable.clear()


async def test_a_load_round_trip_normalisation_is_not_a_change_but_a_renewal_is(workspace):
    far = cookie(".a.test", "a0") | {"expires": 2_500_000_000}  # beyond Chrome's 400-day cap
    workspace["save"](bundle([far, cookie(".b.test", "b0")]))
    h = Mac()
    await h.run("tabs")
    assert h.jar[0]["expires"] == CAP  # Chrome capped it on the way in
    # The cloud renews b.test meanwhile; the Mac renews a.test's expiry for real later on.
    saved = workspace["saved"]()
    saved["cookies"] = [cookie(".b.test", "b-cloud") if c["domain"] == ".b.test" else c for c in saved["cookies"]]
    workspace["save"](saved, key="browser-state/user_1/cloud.json")
    await end_run(workspace)
    after = {c["domain"]: c for c in workspace["saved"]()["cookies"]}
    assert after[".a.test"]["expires"] == 2_500_000_000  # a capped copy is not a change
    assert after[".b.test"]["value"] == "b-cloud"
    workspace["active"] = True
    h2 = Mac()
    h2.jar = list(h.jar)
    sync._records.clear()
    workspace["mac"]["device_1"]["loaded_owner"] = OWNER
    await h2.run("tabs")
    h2.jar = [c | {"expires": CAP - 5} if c["domain"] == ".a.test" else c for c in h2.jar]  # a real renewal
    await end_run(workspace)
    assert {c["domain"]: c for c in workspace["saved"]()["cookies"]}[".a.test"]["expires"] == CAP - 5


async def test_a_cookie_too_large_to_send_is_skipped_and_counted(workspace):
    workspace["save"](bundle([cookie(".big.test", "x" * 20_000), cookie(".ok.test", SECRET)]))
    h = Mac()
    tabs = await h.run("tabs")
    assert tabs["state"] == "succeeded"
    assert "1 saved cookie(s) were too large" in tabs["result"]["workspaceSignIns"]
    assert SECRET not in tabs["result"]["workspaceSignIns"]
    assert h.jar == [cookie(".ok.test", SECRET)]
    await end_run(workspace)


async def test_a_second_runs_save_does_not_revert_the_first(workspace):
    workspace["save"](bundle([cookie(".a.test", "a0"), cookie(".b.test", "b0")]))
    h = Mac()
    await h.run("tabs")
    record = next(iter(sync._records.values()))
    # Run 1 changes a.test and ends; its save commits.
    h.jar = [cookie(".a.test", "a1"), cookie(".b.test", "b0")]
    auth1 = record["runs"]["run_1"]
    assert await sync._save(h.store, "device_1", record, auth1)
    # The cloud then updates a.test; run 2 changes only b.test.
    saved = workspace["saved"]()
    saved["cookies"] = [cookie(".a.test", "a-cloud") if c["domain"] == ".a.test" else c for c in saved["cookies"]]
    workspace["save"](saved, key="browser-state/user_1/cloud.json")
    h.jar = [cookie(".a.test", "a1"), cookie(".b.test", "b1")]
    assert await sync._save(h.store, "device_1", record, auth1)
    assert {c["domain"]: c["value"] for c in workspace["saved"]()["cookies"]} == {".a.test": "a-cloud", ".b.test": "b1"}
    await end_run(workspace)


async def test_a_save_blocked_by_a_cloud_transition_stays_pending_until_it_lands(workspace, monkeypatch):
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    await h.run("tabs")
    h.jar = [cookie(".a.test", "a1")]
    workspace["row"]["mode"] = "transition"
    workspace["active"] = False
    for _ in range(50):
        await asyncio.sleep(0.01)
    assert sync._records  # still pending, not dropped
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"
    workspace["row"] = {**workspace["row"], "mode": "idle"}
    await end_run(workspace)
    assert workspace["saved"]()["cookies"] == [cookie(".a.test", "a1")]


async def test_observed_origins_are_saved_even_after_their_tab_closed(workspace):
    workspace["save"](bundle([]))
    h = Mac()
    tabs = await h.run("tabs")
    h.targets.append({"targetId": "D" * 32, "url": "https://redirected.test/after-login", "type": "page"})
    await h.run("snapshot", target=TAB, browserGeneration=tabs["result"]["tabs"][0]["browserGeneration"])
    record = next(iter(sync._records.values()))
    assert "https://redirected.test" in record["observed"]  # (a) from the tabs call, not an arg
    h.storage["https://redirected.test"] = {"session": "s1"}
    h.targets = [t for t in h.targets if "redirected" not in t["url"]]  # the tab closed
    await end_run(workspace)
    by_origin = {o["origin"]: o["localStorage"] for o in workspace["saved"]()["origins"]}
    assert by_origin["https://redirected.test"] == [{"name": "session", "value": "s1"}]  # (b)
    assert "https://redirected.test" in workspace["mac"]["device_1"]["origins"]  # persisted


def test_a_cloud_save_records_its_base_only_when_the_image_wrote_one(monkeypatch):
    calls = []

    async def rpc(owner, method, params, timeout_s):
        calls.append(params)
        return {"ok": True, "bytes": 1, **({"base": True} if len(calls) == 1 else {})}

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(browser_control.container_rpc, "call", rpc)
    monkeypatch.setattr(browser_control, "_cdp", lambda sid: asyncio.sleep(0, "wss://cdp"))
    monkeypatch.setattr(browser_control, "_state_url", lambda key, action: f"https://s3/{key}?{action}")
    monkeypatch.setattr(browser_control, "_save_extension_storage", nothing)
    state = {
        "generation": "g2",
        "session_id": "s",
        "saved_key": "browser-state/o/g1.json",
        "base_key": "browser-state/o/g1.base.json",
    }
    new = asyncio.run(browser_control._checkpoint("o", state))
    assert calls[0]["baseOutUrl"] == "https://s3/browser-state/o/g2.base.json?put_object"
    assert calls[0]["baseStateUrl"] == "https://s3/browser-state/o/g1.base.json?get_object"
    assert new["base_key"] == "browser-state/o/g2.base.json"
    assert asyncio.run(browser_control._checkpoint("o", state))["base_key"] is None  # an older image


async def test_a_malformed_cookie_read_never_signs_the_workspace_out(workspace):
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    await h.run("tabs")
    h.malformed = True
    await end_run(workspace)
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"


async def test_a_failing_cookie_batch_skips_its_domains_and_keeps_the_rest(workspace, monkeypatch):
    monkeypatch.setattr(sync, "MAX_PARAMS_BYTES", 1200)  # one cookie per batch
    workspace["save"](bundle([cookie(".bad.test", "b"), cookie(".good.test", "g")]))
    h = Mac()
    h.fail_domain = ".bad.test"
    assert (await h.run("tabs"))["state"] == "succeeded"
    assert h.jar == [cookie(".good.test", "g")]  # not left signed out of everything
    record = next(iter(sync._records.values()))
    assert record["loaded"] and ".bad.test" in record["skipped_domains"]
    await end_run(workspace)
    assert {c["domain"] for c in workspace["saved"]()["cookies"]} == {".bad.test", ".good.test"}


async def test_a_mass_vanish_holds_those_domains_and_commits_the_rest(workspace):
    workspace["save"](bundle([cookie(f".s{i}.test", "v") for i in range(8)]))
    h = Mac()
    await h.run("tabs")
    # Seven of eight vanish (a reset profile, not a person) while a new sign-in lands.
    h.jar = [cookie(".s0.test", "v"), cookie(".new.test", "n")]
    await end_run(workspace)
    domains = {c["domain"] for c in workspace["saved"]()["cookies"]}
    assert domains == {f".s{i}.test" for i in range(8)} | {".new.test"}
    # The base advanced for what was committed, so the next save is not refused wholesale.
    base = workspace["mac"]["device_1"]["cookies"]
    assert ".new.test" in base and base[".s1.test"] != sync.EMPTY


async def test_sign_in_sync_reads_get_the_larger_node_result_budget(monkeypatch):
    seen = {}

    class Session:
        async def invoke(self, command, params, timeout_ms, **kwargs):
            seen[params["path"]] = kwargs.get("browser_media", False)
            return {"ok": True}

    monkeypatch.setattr(execution, "_session", lambda target: Session())
    for path in ("/cookies", "/storage/local", "/snapshot"):
        await execution._dispatch(
            {}, {"command": "browser.proxy", "params": {"path": path}, "executionDeadlineMs": 1}, "i"
        )
    assert seen == {"/cookies": True, "/storage/local": True, "/snapshot": False}
    session = NodeSession("n", {"browser.proxy"})
    task = asyncio.create_task(session.invoke("browser.proxy", {"path": "/cookies"}, 50, browser_media=True))
    await asyncio.sleep(0)
    assert max(session.result_limits.values()) == 6 * 1024 * 1024  # not node_broker's default 1 MiB
    task.cancel()


async def test_another_workspace_on_the_same_mac_forces_a_load_and_never_mixes(workspace, monkeypatch):
    workspace["save"](bundle([cookie(".w1.test", "w1")]))
    h = Mac()
    await h.run("tabs")
    w1 = sync._records["device_1"]
    saved_with = []

    async def save(store, device, record, auth, *, locked=False):
        saved_with.append((record["owner"], locked))
        return True

    monkeypatch.setattr(sync, "_save", save)
    w1_state = sync.container_repo.get_browser_state

    async def per_owner(owner):
        return await w1_state(owner) if owner == OWNER else None  # W2 has nothing saved yet

    monkeypatch.setattr(sync.container_repo, "get_browser_state", per_owner)
    w2_auth = AUTH | {"ownerId": "org_w2", "runId": "run_w2"}
    row = {"auth": w2_auth, "request": {"params": {"args": {}}}, "deadline": h.store.clock() + 40}
    await sync.before_action(h.store, row, h.target)
    assert saved_with == [(OWNER, True)]  # W1 saved first, under the lock the action already holds
    now = sync._records["device_1"]
    assert now is not w1 and now["owner"] == "org_w2" and now["loaded"]
    assert workspace["mac"]["device_1"]["loaded_owner"] == "org_w2"
    sync.observe(row | {"auth": AUTH}, h.target, ["https://w1-only.test/"])  # a late W1 frame
    assert "https://w1-only.test" not in now["observed"]
    await end_run(workspace)


async def test_every_workspace_takes_one_lock_per_mac_profile(workspace):
    h = Mac()
    keys = []
    real = h.store.browser.locked

    def spy(key):
        keys.append(key)
        return real(key)

    h.store.browser.locked = spy
    await h.run("tabs")
    assert sync.profile_lock("device_1") in keys  # not keyed by workspace
    await end_run(workspace)


async def test_a_redirected_origin_is_never_touched_on_load_save_or_clear(workspace, monkeypatch):
    workspace["save"](bundle([], [("https://login.test", [{"name": "k", "value": "ws"}])]))
    h = Mac()
    h.redirects["https://login.test"] = "https://sso.elsewhere.test/login"
    h.storage["https://sso.elsewhere.test"] = {"theirs": "keep"}
    await h.run("tabs")
    record = sync._records["device_1"]
    assert "https://login.test" in record["skipped_origins"]
    assert h.storage["https://sso.elsewhere.test"] == {"theirs": "keep"}
    unread = (await sync._read_mac(h.store, h.target, {"https://login.test"}, require_running=False))[1]
    assert unread == 1 and h.storage["https://sso.elsewhere.test"] == {"theirs": "keep"}
    await end_run(workspace)


async def test_a_probe_timeout_at_run_end_retries_but_a_closed_gate_drops(workspace, monkeypatch):
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    await h.run("tabs")
    record = sync._records["device_1"]
    h.jar = [cookie(".a.test", "a1")]
    auth = record["runs"]["run_1"]

    async def unknown(owner, timeout_s=0):
        return None

    monkeypatch.setattr(sync, "_merge_capable", unknown)
    assert await sync._save(h.store, "device_1", record, auth) is False  # transient: keep it pending
    workspace["capable"] = False

    async def closed(owner, timeout_s=0):
        return False

    monkeypatch.setattr(sync, "_merge_capable", closed)
    assert await sync._save(h.store, "device_1", record, auth) is True  # gone: drop it
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"
    sync._records.clear()


async def test_partitioned_cookie_domains_are_skipped_on_the_mac(workspace):
    chip = cookie(".chips.test", "c") | {"partitionKey": "https://top.test"}
    workspace["save"](bundle([chip, cookie(".plain.test", "p")]))
    h = Mac()
    tabs = await h.run("tabs")
    assert "1 site(s) with partitioned cookies" in tabs["result"]["workspaceSignIns"]
    assert [c["domain"] for c in h.jar] == [".plain.test"]
    h.jar = [cookie(".plain.test", "p"), cookie(".chips.test", "mac-collapsed")]
    await end_run(workspace)
    saved = {c["domain"]: c for c in workspace["saved"]()["cookies"]}
    assert saved[".chips.test"] == chip  # never overwritten by the collapsed Mac copy


async def test_pre_merge_digests_are_recorded_before_anything_else_can_fail(workspace, monkeypatch):
    workspace["mac"]["device_1"] = synced([cookie(".a.test", "a0")])
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    h.jar = [cookie(".a.test", "a-mac")]

    async def boom(*_a, **_k):
        raise RuntimeError("S3 read failed after the commit")

    real_workspace = sync._workspace
    calls = {"n": 0}

    async def flaky(owner, state):
        calls["n"] += 1
        if calls["n"] >= 3:  # the load's own read of the save, after the pre-merge committed
            raise RuntimeError("S3 read failed")
        return await real_workspace(owner, state)

    monkeypatch.setattr(sync, "_workspace", flaky)
    await h.run("tabs")
    base = workspace["mac"]["device_1"]["cookies"]
    assert base[".a.test"] == sync._cookie_digest([cookie(".a.test", "a-mac")])  # advanced, not stale
    sync._records.clear()


async def test_deferred_loads_are_pruned(workspace):
    for i in range(5):
        sync._deferred[(f"org_{i}", "d")] = time.monotonic() - 1  # expired backoffs of other Macs
    h = Mac()
    await h.run("tabs")
    assert not any(owner.startswith("org_") for owner, _ in sync._deferred)
    await end_run(workspace)


async def test_a_late_run_end_save_never_relabels_a_profile_another_workspace_loaded(workspace, monkeypatch):
    workspace["save"](bundle([cookie(".w1.test", "w1")]))
    h = Mac()
    await h.run("tabs")
    record = sync._records["device_1"]
    auth = record["runs"]["run_1"]
    h.jar = [cookie(".w1.test", "w1-changed")]
    real_write_back = sync._write_back

    async def interleaved(owner, mine, base):
        # Between W1's read and its row write, W2 (maybe on another backend task) loads.
        await sync._put_mac_row("device_1", None, ())
        workspace["mac"]["device_1"] = workspace["mac"]["device_1"] | {"loaded_owner": "org_w2"}
        return await real_write_back(owner, mine, base)

    monkeypatch.setattr(sync, "_write_back", interleaved)
    assert await sync._save(h.store, "device_1", record, auth) is True
    assert workspace["mac"]["device_1"]["loaded_owner"] == "org_w2"  # not relabelled W1
    assert "cookies" not in workspace["mac"]["device_1"]  # W1's base dropped
    assert "device_1" not in sync._records


async def test_a_save_after_another_load_fenced_the_profile_writes_nothing(workspace):
    workspace["save"](bundle([cookie(".w1.test", "w1")]))
    h = Mac()
    await h.run("tabs")
    record = sync._records["device_1"]
    await sync._put_mac_row("device_1", None, ())  # W2 fenced before W1's run ended
    h.jar = [cookie(".w2.test", "not-w1s")]
    assert await sync._save(h.store, "device_1", record, record["runs"]["run_1"]) is True
    assert [c["domain"] for c in workspace["saved"]()["cookies"]] == [".w1.test"]
    sync._records.clear()


async def test_a_row_write_that_fails_after_the_commit_is_retried_without_recommitting(workspace, monkeypatch):
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    await h.run("tabs")
    record = sync._records["device_1"]
    auth = record["runs"]["run_1"]
    h.jar = [cookie(".a.test", "a1")]
    commits = []
    real_commit, real_put = sync._commit, sync._put_mac_row

    async def counting_commit(owner, combine):
        commits.append(owner)
        return await real_commit(owner, combine)

    fail = {"left": 1}

    async def flaky_put(*args, **kwargs):
        if fail["left"]:
            fail["left"] -= 1
            raise RuntimeError("DynamoDB throttled")
        return await real_put(*args, **kwargs)

    monkeypatch.setattr(sync, "_commit", counting_commit)
    monkeypatch.setattr(sync, "_put_mac_row", flaky_put)
    assert await sync._save(h.store, "device_1", record, auth) is False  # committed, base pending
    assert record.get("pending_base")
    assert await sync._save(h.store, "device_1", record, auth) is True  # only the row write retried
    assert len(commits) == 1 and not record.get("pending_base")
    assert workspace["mac"]["device_1"]["cookies"][".a.test"] == sync._cookie_digest([cookie(".a.test", "a1")])
    sync._records.clear()


async def test_a_base_that_never_lands_leaves_the_row_untrusted(workspace, monkeypatch):
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    await h.run("tabs")
    record = sync._records["device_1"]
    auth = record["runs"]["run_1"]
    h.jar = [cookie(".a.test", "a1")]
    real_put = sync._put_mac_row

    async def failing_base(device, owner, origins, base=None, **kwargs):
        if base is not None:
            raise RuntimeError("DynamoDB down")
        return await real_put(device, owner, origins, base, **kwargs)

    monkeypatch.setattr(sync, "_put_mac_row", failing_base)
    monkeypatch.setattr(sync, "PENDING_BASE_SECONDS", 0)
    assert await sync._save(h.store, "device_1", record, auth) is True
    row = workspace["mac"]["device_1"]
    assert row["loaded_owner"] == OWNER and "cookies" not in row  # untrusted, not stale
    # The cloud then renews a.test; the next load must not treat the Mac's copy as a change.
    saved = workspace["saved"]()
    saved["cookies"] = [cookie(".a.test", "a-cloud")]
    workspace["save"](saved, key="browser-state/user_1/cloud.json")
    monkeypatch.setattr(sync, "_put_mac_row", real_put)
    sync._records.clear()
    await Mac().run("tabs")
    assert workspace["saved"]()["cookies"] == [cookie(".a.test", "a-cloud")]
    await end_run(workspace)


async def test_a_pre_merge_whose_base_write_fails_untrusts_the_row(workspace, monkeypatch):
    workspace["mac"]["device_1"] = synced([cookie(".a.test", "a0")])
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    h.jar = [cookie(".a.test", "a-mac")]
    real_put = sync._put_mac_row
    failures = {"base": 1}

    async def put(device, owner, origins, base=None, **kwargs):
        if base is not None and owner is not None and failures["base"]:
            failures["base"] -= 1
            raise RuntimeError("DynamoDB throttled")
        return await real_put(device, owner, origins, base, **kwargs)

    monkeypatch.setattr(sync, "_put_mac_row", put)
    await h.run("tabs")  # the pre-merge commits a-mac, its base write fails, the row is untrusted
    assert [c["value"] for c in workspace["saved"]()["cookies"]] == ["a-mac"]
    # The stale a0 digest is gone: the load that followed recorded what the Mac now holds.
    assert workspace["mac"]["device_1"]["cookies"][".a.test"] != sync._cookie_digest([cookie(".a.test", "a0")])
    await end_run(workspace)
    # The cloud renews a.test; the Mac still holds what was committed. No stale base may treat
    # it as a change and overwrite the newer cloud value.
    saved = workspace["saved"]()
    saved["cookies"] = [cookie(".a.test", "a-cloud")]
    workspace["save"](saved, key="browser-state/user_1/cloud.json")
    sync._records.clear()
    workspace["active"] = True
    h2 = Mac()
    h2.jar = [cookie(".a.test", "a-mac")]
    await h2.run("tabs")
    assert workspace["saved"]()["cookies"] == [cookie(".a.test", "a-cloud")]
    await end_run(workspace)


async def test_a_pre_merge_base_that_cannot_be_written_or_untrusted_stays_pending(workspace, monkeypatch):
    workspace["mac"]["device_1"] = synced([cookie(".a.test", "a0")])
    workspace["save"](bundle([cookie(".a.test", "a0")]))
    h = Mac()
    h.jar = [cookie(".a.test", "a-mac")]
    real_put = sync._put_mac_row
    down = {"on": True}

    async def put(device, owner, origins, base=None, **kwargs):
        if owner is not None and down["on"]:
            raise RuntimeError("DynamoDB down")
        return await real_put(device, owner, origins, base, **kwargs)

    monkeypatch.setattr(sync, "_put_mac_row", put)
    tabs = await h.run("tabs")
    assert tabs["result"]["workspaceSignIns"] == sync.MAC_READ_FAILED
    assert "device_1" in sync._pending_rows
    stale = workspace["mac"]["device_1"]["cookies"]
    down["on"] = False
    sync._deferred.clear()
    await h.run("tabs")  # the next attempt settles the pending base before anything else
    assert "device_1" not in sync._pending_rows
    assert workspace["mac"]["device_1"]["cookies"] != stale
    await end_run(workspace)


async def test_a_mac_origin_cleared_to_empty_is_reported_empty_and_deletes(workspace):
    """The Mac side reads storage per origin (open or reopened), so a fully cleared origin comes
    back as an empty list, never omitted: the three-way merge sees it as a deletion."""
    workspace["save"](bundle([], [("https://app.test", [{"name": "token", "value": "t"}])]))
    h = Mac()
    await h.run("tabs")
    h.storage["https://app.test"] = {}  # the site signed out on the Mac: its last key is gone
    await end_run(workspace)
    assert {o["origin"]: o["localStorage"] for o in workspace["saved"]()["origins"]} == {"https://app.test": []}
