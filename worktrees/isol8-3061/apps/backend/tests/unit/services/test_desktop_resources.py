import base64
import hashlib
import io
import tarfile

import pytest

from core.services import desktop_execution as execution, node_broker
from tests.unit.services.test_desktop_execution import AUTH, REQUEST, Harness


def catalog(*tools):
    return {
        "type": "req",
        "id": "catalog",
        "method": "node.pluginTools.update",
        "params": {
            "tools": [
                {
                    "pluginId": "node-mcp",
                    "name": "echo",
                    "description": "Untrusted description",
                    "parameters": {"type": "object"},
                    "command": "mcp.tools.call.v1",
                    "mcp": {"server": server, "tool": tool},
                }
                for server, tool in tools
            ]
        },
    }


def test_native_policy_command_requires_files_grant():
    from core.services.desktop_pair import DesktopPermissionGrant

    assert "file.access.policy.v1" in DesktopPermissionGrant(files=True).mac_commands()
    assert "file.access.policy.v1" not in DesktopPermissionGrant(shell=True).mac_commands()


def test_catalog_is_scoped_bounded_and_invalidated():
    session = node_broker.NodeSession("mac", {"mcp.tools.call.v1"})
    session.generation = "generation"
    assert session.resolve(catalog(("one", "echo"), ("two", "echo")))["ok"]
    listing = session.mcp_catalog()
    assert [(t["server"], t["tool"]) for t in listing["tools"]] == [("one", "echo"), ("two", "echo")]
    version = listing["catalogVersion"]
    session.resolve(catalog(("two", "echo")))
    assert session.mcp_catalog()["catalogVersion"] != version
    with pytest.raises(node_broker.CommandNotAllowed):
        session.mcp_call("one", "echo", version)
    bad = catalog(("two", "echo"))
    bad["params"]["tools"][0]["parameters"] = {"text": "x" * 70000}
    assert not session.resolve(bad)["ok"]
    assert session.mcp_catalog()["tools"] == []
    session.resolve(catalog(("two", "echo")))
    session.fail_pending(node_broker.NodeDetached())
    assert session.mcp_catalog()["tools"] == []


async def test_mcp_dispatch_uses_current_catalog_and_receipt_identity(monkeypatch):
    from routers.gateway_relay import node_sessions

    h = Harness()
    session = node_broker.NodeSession("device_1", {"mcp.tools.call.v1"})
    session.generation = "gen_1"
    session.resolve(catalog(("one", "echo")))
    monkeypatch.setitem(node_sessions, ("user_1", "device_1"), session)
    h.target["commands"] = ["mcp.tools.call.v1"]
    version = session.mcp_catalog()["catalogVersion"]
    request = REQUEST | {
        "command": "mcp_call",
        "params": {"server": "one", "tool": "echo", "arguments": {"value": 3}, "catalogVersion": version},
    }
    token = h.token()
    receipt = await h.store.submit(AUTH, token, request)
    await h.finish()
    assert h.effects[0][0]["command"] == "mcp.tools.call.v1"
    assert h.effects[0][0]["params"] == {"server": "one", "tool": "echo", "arguments": {"value": 3}}
    await h.store.submit(AUTH, token, request)
    assert len(h.effects) == 1
    session.resolve(catalog())
    receipt = await h.store.submit(AUTH, token, request | {"requestId": "00000000-0000-4000-8000-000000000001"})
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "rejected"
    assert len(h.effects) == 1


class Files(Harness):
    def __init__(self):
        super().__init__()
        self.policy = {"readRoots": ["/approved"], "writeRoots": ["/approved"], "denyRoots": ["/approved/private"]}
        self.policy |= {"nativeDeviceId": "native_1", "nativeGeneration": "generation_1"}
        self.target["commands"] = ["file.fetch", "file.write", "dir.list", "dir.fetch"]
        self.store.file_policy = self.get_policy
        self.data = b"\x00binary\xff"
        self.binding = {"kind": "existing", "device": "1", "inode": "2"}
        self.preflight = {"ok": True, "path": "/approved/file.bin", "binding": self.binding}
        self.reply = {
            "ok": True,
            "path": "/approved/file.bin",
            "base64": base64.b64encode(self.data).decode(),
            "size": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }

    async def get_policy(self, auth, target, command):
        return self.policy.copy()

    async def dispatch(self, target, request, invoke_id):
        self.effects.append((request, invoke_id))
        if request["params"].get("preflightOnly"):
            return {"ok": True, "payload": self.preflight}
        return {"ok": True, "payload": self.reply}


async def test_file_binary_binding_and_receipt_artifacts():
    h = Files()
    receipt = await h.store.submit(
        AUTH, h.token(), REQUEST | {"command": "file.fetch", "params": {"path": "/approved/file.bin"}}
    )
    await h.finish()
    result = await h.store.get(AUTH, receipt["id"])
    assert result["state"] == "succeeded", result
    assert result["result"]["artifacts"][0]["sha256"] == hashlib.sha256(h.data).hexdigest()
    assert base64.b64decode((await h.store.artifact(AUTH, receipt["id"], 0, 0))["base64"]) == h.data
    assert h.effects[1][0]["params"]["expectedBinding"] == h.binding
    assert h.effects[1][0]["params"]["followSymlinks"] is False
    with pytest.raises(execution.DesktopError, match="desktop_forbidden"):
        await h.store.artifact(AUTH | {"ownerId": "user_2"}, receipt["id"], 0, 0)


async def test_directory_archive_uses_pinned_tar_fields():
    h = Files()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        entry = tarfile.TarInfo("./file.bin")
        entry.size = len(h.data)
        archive.addfile(entry, io.BytesIO(h.data))
    data = buf.getvalue()
    # macOS bsdtar pads its gzip stream to a block boundary with zero bytes.
    data += b"\0" * 8907
    h.preflight |= {"path": "/approved", "entries": ["file.bin"]}
    h.reply = {
        "ok": True,
        "path": "/approved",
        "tarBase64": base64.b64encode(data).decode(),
        "tarBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    receipt = await h.store.submit(AUTH, h.token(), REQUEST | {"command": "dir.fetch", "params": {"path": "/approved"}})
    await h.finish()
    status = await h.store.get(AUTH, receipt["id"])
    assert status["state"] == "succeeded", status
    assert status["result"]["artifacts"][0]["name"] == "directory.tar.gz"


@pytest.mark.parametrize("failure", ["deny", "escape", "revoke", "expiry", "binding", "native_generation"])
async def test_file_authority_denies_before_transfer(failure):
    h = Files()
    if failure == "native_generation":
        h.policy |= {"nativeDeviceId": "native_1", "nativeGeneration": "generation_1"}
    path = "/approved/private/key" if failure == "deny" else "/approved/file.bin"
    if failure == "escape":
        h.preflight["path"] = "/outside/file.bin"
    if failure == "binding":
        h.preflight.pop("binding")
    calls = 0
    original = h.get_policy

    async def policy(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure == "revoke":
                h.policy["readRoots"] = []
            if failure == "expiry":
                h.now += 31
            if failure == "native_generation":
                h.policy["nativeGeneration"] = "generation_2"
        return await original(*args)

    h.store.file_policy = policy
    receipt = await h.store.submit(AUTH, h.token(), REQUEST | {"command": "file.fetch", "params": {"path": path}})
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "rejected"
    assert not any(not r["params"].get("preflightOnly") for r, _ in h.effects)


async def test_uncertain_write_is_not_replayed():
    h = Files()
    h.preflight["binding"] = {"kind": "write", "anchorPath": "/approved", "anchorDevice": "1", "anchorInode": "2"}
    original = h.dispatch

    async def dispatch(*args):
        result = await original(*args)
        if not args[1]["params"].get("preflightOnly"):
            raise node_broker.NodeDetached()
        return result

    h.store.dispatch = dispatch
    request = REQUEST | {
        "command": "file.write",
        "params": {
            "path": "/approved/file.bin",
            "contentBase64": base64.b64encode(h.data).decode(),
            "sha256": hashlib.sha256(h.data).hexdigest(),
        },
    }
    token = h.token()
    receipt = await h.store.submit(AUTH, token, request)
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "unknown"
    await h.store.submit(AUTH, token, request)
    assert len(h.effects) == 2
    assert "contentBase64" not in h.store.records[receipt["id"]]["request"]["params"]


async def test_cancel_during_final_policy_check_never_writes():
    import asyncio

    h = Files()
    h.preflight["binding"] = {"kind": "write", "anchorPath": "/approved", "anchorDevice": "1", "anchorInode": "2"}
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def policy(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return h.policy.copy()

    h.store.file_policy = policy
    request = REQUEST | {
        "command": "file.write",
        "params": {
            "path": "/approved/file.bin",
            "contentBase64": base64.b64encode(h.data).decode(),
            "sha256": hashlib.sha256(h.data).hexdigest(),
        },
    }
    receipt = await h.store.submit(AUTH, h.token(), request)
    await entered.wait()
    await h.store.cancel(AUTH, receipt["id"])
    release.set()
    await h.finish()
    assert len(h.effects) == 1  # preflight only
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "rejected"


@pytest.mark.parametrize(
    "name,kind", [("../secret", tarfile.REGTYPE), ("safe/link", tarfile.SYMTYPE), ("safe/big", tarfile.REGTYPE)]
)
def test_archive_traversal_links_and_expansion_are_rejected(name, kind):
    from core.services.desktop_resources import validate_archive

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.linkname = "/outside"
        entry.size = 5 * 1024 * 1024 if name.endswith("big") else 0
        archive.addfile(entry, io.BytesIO(b"x" * entry.size))
    with pytest.raises(execution.DesktopError):
        validate_archive(buf.getvalue(), "/approved", {"readRoots": ["/approved"], "writeRoots": [], "denyRoots": []})


def test_archive_header_decompression_is_bounded():
    import gzip
    from core.services.desktop_resources import validate_archive

    # Empty tar followed by excessive compressed padding must not bypass byte limits.
    data = gzip.compress(b"\0" * (16 * 1024 * 1024))
    with pytest.raises(execution.DesktopError):
        validate_archive(data, "/approved", {"readRoots": ["/approved"], "writeRoots": [], "denyRoots": []})


@pytest.mark.parametrize(
    "name,kind,count",
    [
        ("../outside", tarfile.REGTYPE, 1),
        ("link", tarfile.SYMTYPE, 1),
        ("private/secret", tarfile.REGTYPE, 1),
        ("extra", tarfile.REGTYPE, 1001),
    ],
)
def test_archive_rejects_nonzero_headers_after_first_tar_eof(name, kind, count):
    import gzip
    from core.services.desktop_resources import validate_archive

    def tar_bytes(prefix, entry_kind, entries):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for index in range(entries):
                entry = tarfile.TarInfo(f"{prefix}-{index}")
                entry.type = entry_kind
                if entry_kind == tarfile.SYMTYPE:
                    entry.linkname = "/outside"
                archive.addfile(entry)
        return buffer.getvalue()

    first = tar_bytes("safe", tarfile.REGTYPE, 1)
    second = tar_bytes(name, kind, count)
    policy = {"readRoots": ["/approved"], "writeRoots": [], "denyRoots": ["/approved/private"]}
    # Ordinary zero padding remains legal, including BSD tar's padded gzip.
    validate_archive(gzip.compress(first + b"\0" * 1024) + b"\0" * 1024, "/approved", policy)
    with tarfile.open(fileobj=io.BytesIO(first + second), mode="r:", ignore_zeros=True) as reader:
        assert len(reader.getmembers()) == count + 1
    with pytest.raises(execution.DesktopError, match="file_archive_invalid"):
        validate_archive(gzip.compress(first + second), "/approved", policy)


@pytest.mark.parametrize(
    "command,params",
    [
        ("file.fetch", {"path": "/approved/../secret"}),
        ("file.fetch", {"path": "/approved/file", "followSymlinks": True}),
        ("file.write", {"path": "/approved/file", "overwrite": True}),
        ("mcp_call", {"server": "s", "tool": "t", "arguments": {"large": "x" * 17000}, "catalogVersion": "v"}),
    ],
)
def test_no_public_path_policy_or_oversized_argument_escape(command, params):
    with pytest.raises(execution.DesktopError):
        execution.validate_request(REQUEST | {"command": command, "params": params})


async def test_raw_file_broker_requires_trusted_policy():
    session = node_broker.NodeSession("node", {"file.fetch"})
    with pytest.raises(node_broker.CommandNotAllowed, match="file_policy_required"):
        await session.invoke("file.fetch", {"path": "/etc/passwd"}, 100)
    assert session.send.empty()


async def test_mcp_result_budget_is_enforced_before_nested_json_decode():
    import asyncio

    session = node_broker.NodeSession("node", {"mcp.tools.call.v1"})
    task = asyncio.create_task(session.invoke("mcp.tools.call.v1", {"server": "s", "tool": "t", "arguments": {}}, 1000))
    frame = await session.send.get()
    session.resolve(
        {
            "type": "req",
            "id": "reply",
            "method": "node.invoke.result",
            "params": {"id": frame["payload"]["id"], "ok": True, "payloadJSON": "x" * (1024 * 1024)},
        }
    )
    with pytest.raises(node_broker.NodeResultTooLarge):
        await task
    assert not session.pending


async def test_native_policy_peer_binds_both_devices_and_generations(monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from tests.unit.services.test_desktop_execution import TARGET

    native = TARGET | {"deviceId": "native_1", "generation": "native_gen", "commands": ["file.access.policy.v1"]}
    policy = {"readRoots": ["/approved"], "writeRoots": ["/approved"], "denyRoots": []}
    calls = []

    async def companion(auth, target, command):
        assert auth == AUTH and target == TARGET
        calls.append(command)
        return native

    monkeypatch.setattr(execution, "_reauthorize", AsyncMock())
    monkeypatch.setattr(execution, "_resolve", AsyncMock(return_value=TARGET))
    monkeypatch.setattr(execution, "_resolve_companion", companion)
    monkeypatch.setattr(execution, "_dispatch", AsyncMock(return_value={"ok": True, "payload": policy}))
    monkeypatch.setattr(execution, "operations", lambda: SimpleNamespace(address=TARGET["taskAddress"]))
    monkeypatch.setattr(execution, "submissions_enabled", lambda owner: True)
    body = {
        "auth": AUTH,
        "deviceId": "device_1",
        "generation": "gen_1",
        "nativeId": "native_1",
        "nativeGeneration": "native_gen",
        "command": "file.fetch",
    }
    result = await execution.handle_file_policy_peer(body)
    assert result == policy | {"nativeDeviceId": "native_1", "nativeGeneration": "native_gen"}
    assert calls == ["file.access.policy.v1"] * 3
    for field in ("nativeId", "nativeGeneration"):
        with pytest.raises(execution.DesktopError, match="desktop_generation_lost"):
            await execution.handle_file_policy_peer(body | {field: "another"})
    with pytest.raises(execution.DesktopError, match="invalid_request"):
        await execution.handle_file_policy_peer(body | {"command": "system.run"})
    native = native | {"taskAddress": "http://another-worker"}
    with pytest.raises(execution.DesktopError, match="desktop_forward_loop"):
        await execution._file_policy(AUTH, TARGET, "file.fetch", forwarded=True)
