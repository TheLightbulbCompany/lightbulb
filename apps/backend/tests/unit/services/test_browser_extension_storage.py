"""Each installed extension's own chrome.storage survives a browser restart: read over the
automation stream at every save, written back before the tabs at the next start."""

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import browser_control as control

A, B, GONE = "a" * 32, "b" * 32, "c" * 32
OWNER = "org_x"
KEY = f"browser-state/{OWNER}/g2.json"


class FakeBrowser:
    """A fake CDP socket: each extension page answers with that extension's storage."""

    def __init__(self, storage):
        self.storage = storage
        self.targets = {}
        self.written = {}
        self.closed = []

    async def call(self, method, params=None, session=None):
        params = params or {}
        if method == "Target.createTarget":
            ext = params["url"].split("/")[2]
            self.targets[f"t-{ext}"] = ext
            return {"targetId": f"t-{ext}"}
        if method == "Target.attachToTarget":
            return {"sessionId": params["targetId"]}
        if method == "Target.closeTarget":
            self.closed.append(params["targetId"])
            return {}
        if method == "Runtime.evaluate":
            ext = self.targets[session]
            expression = params["expression"]
            if "chrome.storage.local.set" in expression:
                self.written[ext] = json.loads(expression[expression.rindex("})(") + 3 : -1])
                return {"result": {"value": "ok"}}
            if ext not in self.storage:
                return {"exceptionDetails": {"text": "no chrome.storage"}}
            return {"result": {"value": json.dumps(self.storage[ext])}}
        raise AssertionError(method)


@pytest.fixture
def world(monkeypatch):
    s3 = MagicMock()
    snapshots = {}

    async def read(key):
        return snapshots.get(key, {})

    browser = FakeBrowser({A: {"local": {"theme": "dark"}, "sync": {"x": 1}}})

    @contextlib.asynccontextmanager
    async def devtools(session_id, failure):
        yield browser.call

    monkeypatch.setattr(control, "_read_snapshot", read)
    monkeypatch.setattr(control, "_devtools", devtools)
    monkeypatch.setattr(control.boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(control.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        control.container_repo,
        "get_browser_extensions",
        AsyncMock(return_value=[{"slug": A}, {"slug": B}, {"slug": "my-upload"}]),
    )
    return browser, s3, snapshots


@pytest.mark.asyncio
async def test_save_reads_each_store_extension_and_keeps_the_last_copy_of_an_unreadable_one(world):
    browser, s3, snapshots = world
    snapshots[f"browser-state/{OWNER}/g1.extensions.json"] = {
        A: {"local": {"theme": "old"}},
        B: {"local": {"signedIn": True}},  # unreadable this time: kept
        GONE: {"local": {"y": 1}},  # removed since: dropped
    }
    state = {"session_id": "s", "saved_key": f"browser-state/{OWNER}/g1.json"}
    await control._save_extension_storage(OWNER, state, KEY)
    put = s3.put_object.call_args.kwargs
    assert put["Key"] == f"browser-state/{OWNER}/g2.extensions.json"
    assert json.loads(put["Body"]) == {
        A: {"local": {"theme": "dark"}, "sync": {"x": 1}},
        B: {"local": {"signedIn": True}},
    }
    assert sorted(browser.closed) == [f"t-{A}", f"t-{B}"]  # every page it opened is closed


@pytest.mark.asyncio
async def test_save_never_fails_when_the_stream_is_held(world, monkeypatch):
    _, s3, snapshots = world
    snapshots[f"browser-state/{OWNER}/g1.extensions.json"] = {A: {"local": {"theme": "old"}}}

    @contextlib.asynccontextmanager
    async def refused(session_id, failure):
        raise RuntimeError("429: an agent run holds the automation stream")
        yield  # pragma: no cover

    monkeypatch.setattr(control, "_devtools", refused)
    await control._save_extension_storage(
        OWNER, {"session_id": "s", "saved_key": f"browser-state/{OWNER}/g1.json"}, KEY
    )
    assert json.loads(s3.put_object.call_args.kwargs["Body"]) == {A: {"local": {"theme": "old"}}}


@pytest.mark.asyncio
async def test_restore_writes_back_only_installed_extensions(world):
    browser, _, snapshots = world
    snapshots[f"browser-state/{OWNER}/g2.extensions.json"] = {
        A: {"local": {"theme": "dark"}, "sync": {}},
        GONE: {"local": {"y": 1}},
    }
    await control._restore_extension_storage(OWNER, {"session_id": "s"}, KEY)
    assert browser.written == {A: {"local": {"theme": "dark"}, "sync": {}}}


@pytest.mark.asyncio
async def test_restore_puts_extension_data_back_before_the_tabs(monkeypatch):
    order = []
    monkeypatch.setattr(control, "_restore_extension_storage", AsyncMock(side_effect=lambda *a: order.append("ext")))
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://x"))
    monkeypatch.setattr(control, "_state_url", lambda key, action: "https://s3/x")

    async def load(*args, **kwargs):
        order.append("tabs")
        return {"ok": True}

    monkeypatch.setattr(control.container_rpc, "call", load)
    assert await control._restore(OWNER, {"session_id": "s", "saved_key": KEY, "generation": "g"}) is None  # old image
    assert order == ["ext", "tabs"]


async def test_a_start_that_fails_after_restore_deletes_its_base(monkeypatch):
    """A real _restore writes this generation's base; a later failure must not orphan it."""
    deleted = []
    workspace = {"row": {"generation": "old", "mode": "idle", "saved_key": KEY, "session_id": None}}

    async def get_row(owner, consistent=False):
        return {"owner_id": owner, "browser_profile_id": "p"}

    async def get_state(owner):
        return workspace["row"]

    async def compare(owner, old, new):
        if workspace["row"] != old:
            return False
        workspace["row"] = new
        return True

    async def rpc(owner, method, params, timeout_s=0):
        return {"ok": True, "bytes": 0, "base": True}

    async def owned(owner, row, state):
        return {"status": "READY"} if state.get("session_id") else None

    class Client:
        def start_browser_session(self, **kwargs):
            return {"sessionId": "fresh"}

    async def delete(key):
        deleted.append(key)

    async def nothing(*_a, **_k):
        return None

    async def stream_fails(*_a, **_k):
        raise RuntimeError("stream update refused")

    monkeypatch.setattr(control.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr(control.container_repo, "get_browser_state", get_state)
    monkeypatch.setattr(control.container_repo, "compare_browser_state", compare)
    monkeypatch.setattr(control.container_repo, "get_browser_viewport", nothing)
    monkeypatch.setattr(control.browser_extensions, "session_config", AsyncMock(return_value={}))
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: Client())
    monkeypatch.setattr(control, "_owned", owned)
    monkeypatch.setattr(control, "_restore_extension_storage", nothing)
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://x"))
    monkeypatch.setattr(control, "_state_url", lambda key, action: "https://s3/x")
    monkeypatch.setattr(control.container_rpc, "call", rpc)
    monkeypatch.setattr(control, "_stream", stream_fails)
    monkeypatch.setattr(control, "_delete_snapshot", delete)
    with pytest.raises(control.BrowserError):
        await control._claim(OWNER, None, start_only=True)
    generation = workspace["row"]["generation"]
    assert f"browser-state/{OWNER}/{generation}.base.json" in deleted
    assert workspace["row"].get("base_key") is None and workspace["row"]["mode"] == "error"
