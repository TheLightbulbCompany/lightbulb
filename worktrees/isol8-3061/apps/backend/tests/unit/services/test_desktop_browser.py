import asyncio
import base64
import hashlib
import uuid

import pytest

from core.services import desktop_execution as execution
from core.services.node_broker import NodeSession, NodeResultTooLarge
from tests.unit.services.test_desktop_execution import AUTH, REQUEST, TARGET


def browser(action, **params):
    return REQUEST | {
        "command": "browser.proxy",
        "params": {
            "action": action,
            "profile": "lightbulb",
            "args": {},
            **params,
        },
    }


def test_browser_public_request_is_typed_and_never_a_proxy_tunnel():
    assert execution.validate_request(browser("tabs"))["params"]["action"] == "tabs"
    for params in (
        {"method": "DELETE", "path": "/profiles/user"},
        {"action": "navigate", "profile": "lightbulb", "args": {"url": "file:///etc/passwd"}},
        {"action": "status", "profile": "../user", "args": {}},
        {"action": "click", "profile": "lightbulb", "target": "tab", "args": {"ref": "e1"}},
        {"action": "profiles", "profile": "lightbulb", "args": {"cdpUrl": "http://localhost"}},
    ):
        with pytest.raises(execution.DesktopError):
            execution.validate_request(REQUEST | {"command": "browser.proxy", "params": params})


TAB = "A" * 32


class BrowserHarness:
    """Pinned proxy envelopes; only the actual Mac transport is replaced."""

    def __init__(self, driver="openclaw"):
        self.driver = driver
        self.calls = []
        self.targets = [{"targetId": TAB, "url": "https://example.com/", "title": "Example", "type": "page"}]
        self.lost_click = False
        self.media = None
        self.bad_preflight = False
        self.cancel_before_effect = False
        self.target = TARGET | {"commands": ["browser.proxy", "browser.proxy.upload.v1"]}
        self.store = execution.DesktopOperations(
            secret="s",
            address=TARGET["taskAddress"],
            resolve=self.resolve,
            dispatch=self.dispatch,
            cancel=self.cancel,
            authorize=self.authorize,
        )

    async def resolve(self, auth, request):
        assert request["command"] in self.target["commands"]
        if self.cancel_before_effect and any(c["path"] == "/tabs" for c in self.calls):
            for row in self.store.records.values():
                if row["state"] == "queued":
                    row["state"] = "cancel_requested"
        return self.target

    async def authorize(self, auth):
        assert auth == AUTH

    async def cancel(self, *_):
        pass

    async def dispatch(self, target, request, invoke_id):
        assert target["deviceId"] == self.target["deviceId"]
        params = request["params"]
        self.calls.append(params)
        assert params["path"] != "/profiles", "all-profile enumeration starts unselected Chrome MCP"
        assert params["errorEnvelope"] == "browser-v1"
        assert params["profile"] == "lightbulb"
        path = params["path"]
        if path == "/tabs/focus" and params["body"] == {}:
            return {
                "ok": True,
                "payload": {
                    "error": {
                        "status": 400,
                        "body": {"error": "changed" if self.bad_preflight else "targetId is required"},
                    },
                    "route": {"status": "resolved", "profile": "lightbulb", "driver": self.driver},
                },
            }
        result = {"ok": True, "targetId": TAB}
        if path == "/":
            result = {"profile": "lightbulb", "driver": self.driver, "running": True}
        elif path == "/tabs":
            result = {"running": True, "tabs": self.targets}
        elif path == "/snapshot":
            result |= {"snapshot": '- button "Submit" [ref=e1]', "refs": {"e1": {"role": "button", "name": "Submit"}}}
        elif path == "/act" and self.lost_click:
            raise TimeoutError("response lost after click")
        payload = {"result": result, "route": {"status": "resolved", "profile": "lightbulb", "driver": self.driver}}
        if path == "/screenshot" and self.media is not None:
            payload["result"]["path"] = "/private/tmp/openclaw/browser/screen.jpg"
            payload["files"] = [
                {
                    "path": payload["result"]["path"],
                    "base64": base64.b64encode(self.media).decode(),
                    "mimeType": "image/jpeg",
                }
            ]
        return {"ok": True, "payload": payload}

    async def run(self, action, **params):
        receipt = await self.store.submit(
            AUTH,
            self.store.admit(AUTH, self.target),
            browser(action, **params)
            | {
                "requestId": str(uuid.uuid4()),
                "deviceId": self.target["deviceId"],
                "expectedConnectionGeneration": self.target["generation"],
            },
        )
        await asyncio.gather(*self.store.tasks.values())
        return await self.store.get(AUTH, receipt["id"])


async def test_native_target_snapshot_scope_and_no_replay_after_lost_click():
    h = BrowserHarness()
    tabs = await h.run("tabs")
    assert tabs["state"] == "succeeded", tabs
    tab = tabs["result"]["tabs"][0]
    snap = await h.run("snapshot", target=TAB, browserGeneration=tab["browserGeneration"])
    assert snap["state"] == "succeeded", snap
    h.lost_click = True
    click = await h.run("click", target=TAB, browserGeneration=snap["result"]["browserGeneration"], args={"ref": "e1"})
    assert click["state"] == "unknown"
    assert len([c for c in h.calls if c["path"] == "/act"]) == 1
    assert (await h.store.get(AUTH, click["id"]))["state"] == "unknown"


@pytest.mark.parametrize("driver", ["openclaw", "extension"])
async def test_stale_native_target_cannot_retarget_by_url(driver):
    h = BrowserHarness(driver)
    tab = (await h.run("tabs"))["result"]["tabs"][0]
    h.targets = [{**h.targets[0], "targetId": "B" * 32}]
    failed = await h.run("snapshot", target=TAB, browserGeneration=tab["browserGeneration"])
    assert failed["state"] == "rejected"
    assert failed["error"]["code"] == "browser_target_stale"
    assert not any(c["path"] == "/snapshot" for c in h.calls)


async def test_ambiguous_personal_target_and_unattached_extension_are_unavailable():
    for driver, target in (("existing-session", "mcp:nonce:1"), ("extension", "tab-12")):
        h = BrowserHarness(driver)
        h.targets[0]["targetId"] = target
        tabs = await h.run("tabs")
        if driver == "existing-session":
            assert tabs["error"]["code"] == "browser_ownership_unverifiable"
            assert len(h.calls) == 1 and h.calls[0]["path"] == "/tabs/focus"
            continue
        assert tabs["state"] == "succeeded", tabs
        tab = tabs["result"]["tabs"][0]
        assert tab["unavailableReason"] == "browser_ownership_unverifiable"
        assert "browserGeneration" not in tab


async def test_passive_profiles_never_starts_unselected_browser():
    h = BrowserHarness()
    profiles = await h.run("profiles")
    assert profiles["state"] == "succeeded"
    assert not h.calls
    assert all(p["readiness"] == "unknown" for p in profiles["result"]["profiles"])


async def test_media_is_receipt_bound_chunked_hashed_and_expires_without_replay():
    h = BrowserHarness()
    h.media = bytes(range(256)) * 1024
    tab = (await h.run("tabs"))["result"]["tabs"][0]
    receipt = await h.run("screenshot", target=TAB, browserGeneration=tab["browserGeneration"])
    assert receipt["state"] == "succeeded", receipt
    artifact = receipt["result"]["artifacts"][0]
    assert artifact == {
        "id": 0,
        "name": "screen.jpg",
        "mimeType": "image/jpeg",
        "bytes": len(h.media),
        "sha256": hashlib.sha256(h.media).hexdigest(),
    }
    assert "/private/" not in str(receipt) and "base64" not in str(receipt)
    chunk = await h.store.artifact(AUTH, receipt["id"], 0, 0)
    assert base64.b64decode(chunk["base64"]) == h.media
    with pytest.raises(execution.DesktopError):
        await h.store.artifact(AUTH | {"runId": "foreign"}, receipt["id"], 0, 0)
    with pytest.raises(execution.DesktopError):
        await h.store.artifact(AUTH, receipt["id"], 0, -1)
    row = h.store.records[receipt["id"]]
    row["completed"] -= 301
    with pytest.raises(execution.DesktopError, match="artifact_expired"):
        await h.store.artifact(AUTH, receipt["id"], 0, 0)
    assert len([c for c in h.calls if c["path"] == "/screenshot"]) == 1


async def test_upload_nontrivial_bytes_use_pinned_envelope_and_scrub_retained_input():
    h = BrowserHarness()
    tab = (await h.run("tabs"))["result"]["tabs"][0]
    snap = (await h.run("snapshot", target=TAB, browserGeneration=tab["browserGeneration"]))["result"]
    data = b"safe" * 32768
    file = {
        "name": "test.txt",
        "contentBase64": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    receipt = await h.run(
        "upload", target=TAB, browserGeneration=snap["browserGeneration"], args={"ref": "e1", "files": [file]}
    )
    assert receipt["state"] == "succeeded", receipt
    upload = [c for c in h.calls if c["path"] == "/hooks/file-chooser"][0]
    assert upload["upload"] == {
        "envelope": "browser-upload-v1",
        "files": [{"name": "test.txt", "contentBase64": file["contentBase64"]}],
    }
    assert upload["body"] == {"targetId": TAB, "ref": "e1"}
    assert "contentBase64" not in str(h.store.records[receipt["id"]]["request"])
    for change in ({"name": "../secret"}, {"sha256": "0" * 64}, {"contentBase64": "%%%"}):
        with pytest.raises(execution.DesktopError):
            execution.validate_request(
                browser("upload", target=TAB, browserGeneration="token", args={"ref": "e1", "files": [file | change]})
            )


async def test_only_trusted_browser_phase_may_use_bounded_larger_frames():
    session = NodeSession("mac", {"browser.proxy", "browser.proxy.upload.v1", "device.info"})
    invoke = asyncio.create_task(session.invoke("browser.proxy", {}, 1000, browser_media=True))
    frame = await session.send.get()
    payload = {"data": "x" * (2 * 1024 * 1024)}
    session.resolve(
        {
            "type": "req",
            "id": "response",
            "method": "node.invoke.result",
            "params": {"id": frame["payload"]["id"], "ok": True, "payload": payload},
        }
    )
    assert (await invoke)["payload"] == payload
    with pytest.raises(ValueError):
        await session.invoke("device.info", {}, 1000, browser_media=True)
    upload = asyncio.create_task(session.invoke("browser.proxy.upload.v1", {"upload": "x" * 180000}, 1000))
    frame = await session.send.get()
    session.resolve(
        {
            "type": "req",
            "id": "response",
            "method": "node.invoke.result",
            "params": {"id": frame["payload"]["id"], "ok": True, "payload": {}},
        }
    )
    assert (await upload)["ok"]
    oversized = asyncio.create_task(session.invoke("browser.proxy", {}, 1000, browser_media=True))
    frame = await session.send.get()
    session.resolve(
        {
            "type": "req",
            "id": "response",
            "method": "node.invoke.result",
            "params": {"id": frame["payload"]["id"], "ok": True, "payload": "x" * (6 * 1024 * 1024)},
        }
    )
    with pytest.raises(NodeResultTooLarge):
        await oversized


async def test_cancel_during_browser_preflight_never_becomes_a_click():
    h = BrowserHarness()
    tab = (await h.run("tabs"))["result"]["tabs"][0]
    snap = (await h.run("snapshot", target=TAB, browserGeneration=tab["browserGeneration"]))["result"]
    h.calls.clear()
    h.cancel_before_effect = True
    receipt = await h.run("click", target=TAB, browserGeneration=snap["browserGeneration"], args={"ref": "e1"})
    assert receipt["state"] == "rejected"
    assert not any(call["path"] == "/act" for call in h.calls)


async def test_changed_preflight_envelope_cannot_start_selected_browser():
    h = BrowserHarness()
    h.bad_preflight = True
    receipt = await h.run("status")
    assert receipt["state"] == "rejected"
    assert receipt["error"]["code"] == "browser_protocol_mismatch"
    assert len(h.calls) == 1


@pytest.mark.parametrize("changed", [{"deviceId": "device_2"}, {"generation": "gen_2"}])
async def test_browser_lease_cannot_move_to_another_device_or_connection(changed):
    h = BrowserHarness()
    tab = (await h.run("tabs"))["result"]["tabs"][0]
    h.target |= changed
    h.calls.clear()
    rejected = await h.run("snapshot", target=TAB, browserGeneration=tab["browserGeneration"])
    assert rejected["state"] == "rejected"
    assert rejected["error"]["code"] == "browser_target_stale"
    assert not any(call["path"] == "/snapshot" for call in h.calls)


async def test_new_snapshot_invalidates_old_refs_and_artifact_budgets_evict_bytes():
    h = BrowserHarness()
    tab = (await h.run("tabs"))["result"]["tabs"][0]
    first = (await h.run("snapshot", target=TAB, browserGeneration=tab["browserGeneration"]))["result"]
    second = (await h.run("snapshot", target=TAB, browserGeneration=first["browserGeneration"]))["result"]
    rejected = await h.run("click", target=TAB, browserGeneration=first["browserGeneration"], args={"ref": "e1"})
    assert rejected["error"]["code"] == "browser_target_stale"
    h.media = b"x" * (4 * 1024 * 1024)
    receipt = await h.run("screenshot", target=TAB, browserGeneration=second["browserGeneration"])
    await h.run("screenshot", target=TAB, browserGeneration=second["browserGeneration"])
    assert sum(h.store._retained_bytes(r) for r in h.store.records.values()) <= execution.MAX_BODY_BYTES
    assert (await h.store.get(AUTH, receipt["id"]))["error"]["code"] == "result_expired"
    assert "_artifacts" not in h.store.records[receipt["id"]]


def test_media_rejects_unreferenced_paths_and_limits_decoding():
    from core.services.desktop_browser import media_result, decode_file

    with pytest.raises(execution.DesktopError, match="invalid_browser_file"):
        media_result(
            {
                "result": {"path": "/tmp/browser/output.pdf"},
                "files": [{"path": "/Users/person/secret.txt", "base64": "eA==", "mimeType": "text/plain"}],
            },
            {},
        )
    with pytest.raises(execution.DesktopError, match="too_large"):
        decode_file("A" * 4096, 10)
