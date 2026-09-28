import asyncio
import json
import logging

import pytest
from botocore.exceptions import ClientError

from core.services import browser_control, desktop_browser_state as sync
from core.services import desktop_execution as execution
from core.services.desktop_browser import STATE_ACTIONS, proxy
from tests.unit.services.test_desktop_browser import TAB, BrowserHarness, browser
from tests.unit.services.test_desktop_execution import AUTH

SECRET = "SECRET-COOKIE-VALUE-4f2a"
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
            self.targets.append({"targetId": tab, "url": body["url"] + "/", "type": "page"})
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
            result["cookies"] = [dict(c, partitionKey=None) for c in self.jar]
        elif path == "/cookies/clear":
            self.jar = []
        elif path == "/cookies/set-many":
            self.jar += body["cookies"]
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
    ws = {"row": None, "objects": {}, "reads": 0, "active": True}

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
    monkeypatch.setattr(sync.messaging_repo, "get_run", get_run)
    monkeypatch.setattr(sync, "POLL_SECONDS", 0.01)
    sync._records.clear()

    def save(state, key="browser-state/user_1/g1.json"):
        ws["objects"][key] = json.dumps(state).encode()
        ws["row"] = {"generation": "g", "mode": "idle", "saved_key": key}

    ws["save"] = save
    ws["saved"] = lambda: json.loads(ws["objects"][ws["row"]["saved_key"]])
    yield ws
    sync._records.clear()
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
    assert paths.index("/storage/local/set") < paths.index("/tabs")
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

    async def conflict(*_):
        raise browser_control.BrowserError("changed")

    monkeypatch.setattr(browser_control, "_publish", conflict)  # the save fails: its log line too
    await end_run(workspace)
    assert "mac browser state save failed" in caplog.text
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

        async def _resolve_desktop_target(self, owner, member, device, generation, command):
            assert (device, command) == ("device_1", "browser.proxy")
            return h.target

    monkeypatch.setattr(sync.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr("core.services.connection_service.ConnectionService", Connections)
    monkeypatch.setattr(execution, "operations", lambda: h.store)
    assert await sync.clear_device(OWNER, "mac_1")
    assert h.jar == [] and h.storage["https://example.com"] == {}
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
    await end_run(workspace)
    assert workspace["row"]["saved_key"] == "browser-state/user_1/g1.json"
    assert set(workspace["objects"]) == {"browser-state/user_1/g1.json", "browser-state/user_1/g1.extensions.json"}


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

        async def _resolve_desktop_target(self, owner, member, device, generation, command):
            return h.target

    monkeypatch.setattr(sync.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr("core.services.connection_service.ConnectionService", Connections)
    monkeypatch.setattr(execution, "operations", lambda: h.store)
    assert await sync.clear_device(OWNER, "device_1")
    assert h.storage["https://visited.test"] == {} and h.storage["https://open.test"] == {}
