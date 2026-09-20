"""Explicit opt-in real pinned CLI, backend coordinator and temporary stdio MCP.

OPENCLAW_PACKAGE_ROOT=... OPENCLAW_NODE_BIN=... pytest <this file> --no-cov
No existing device, browser, credentials, or user's MCP server is touched.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import uuid

import pytest
from websockets.asyncio.server import serve

from core.services import desktop_execution as execution, node_broker
from routers.gateway_relay import node_sessions
from tests.unit.services.test_desktop_execution import AUTH, TARGET, REQUEST


@pytest.mark.skipif(not os.getenv("OPENCLAW_PACKAGE_ROOT"), reason="explicit isolated CLI opt-in required")
async def test_pinned_files_and_configured_stdio_mcp(tmp_path, monkeypatch):
    package = Path(os.environ["OPENCLAW_PACKAGE_ROOT"])
    node = os.environ["OPENCLAW_NODE_BIN"]
    assert json.loads((package / "package.json").read_text())["version"] == "2026.8.1"
    root = tmp_path.resolve()
    approved = root / "approved"
    approved.mkdir()
    config = {
        "tools": {"exec": {"security": "deny", "ask": "always"}},
        "plugins": {
            "entries": {
                "anthropic": {"config": {"sessionCatalog": {"enabled": False}}},
                "ollama": {"config": {"nodeInference": {"enabled": False}}},
            }
        },
        "nodeHost": {
            "mcp": {
                "servers": {
                    "fixture": {"command": node, "args": [str(Path(__file__).with_name("desktop_mcp_fixture.mjs"))]}
                }
            }
        },
    }
    config_path = root / "openclaw.json"
    config_path.write_text(json.dumps(config))
    env = {
        "HOME": str(root),
        "TMPDIR": str(root),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "OPENCLAW_HOME": str(root),
        "OPENCLAW_STATE_DIR": str(root / "state"),
        "OPENCLAW_CONFIG_PATH": str(config_path),
        "OPENCLAW_OAUTH_DIR": str(root / "credentials"),
        "OPENCLAW_GATEWAY_TOKEN": "isolated-fixture",
    }
    ready, changed = asyncio.Event(), asyncio.Event()
    sessions = []

    async def connection(ws):
        if sessions:
            await ws.close(1008, "no reconnect/replay in fixture")
            return
        challenge = node_broker.challenge_frame()
        await ws.send(json.dumps(challenge))
        connect = json.loads(await ws.recv())
        print("CLI connected", flush=True)
        assert node_broker.signature_matches(connect["params"], challenge["payload"]["nonce"])
        session = node_broker.NodeSession(connect["params"]["device"]["id"], set(connect["params"]["commands"]))
        session.generation = "gen_1"
        sessions.append(session)
        monkeypatch.setitem(node_sessions, (AUTH["ownerId"], "device_1"), session)
        await ws.send(json.dumps(node_broker.hello_ok(connect["id"], "gen_1")))

        async def writer():
            while True:
                await ws.send(json.dumps(await session.send.get()))

        sender = asyncio.create_task(writer())
        try:
            async for raw in ws:
                frame = json.loads(raw)
                answer = session.resolve(frame)
                if frame.get("method") == "node.pluginTools.update":
                    print("CLI catalog", len(frame.get("params", {}).get("tools", [])), answer, flush=True)
                if answer:
                    await ws.send(json.dumps(answer))
                if frame.get("method") == "node.pluginTools.update":
                    if session.mcp_catalog()["tools"]:
                        ready.set()
                    elif ready.is_set():
                        changed.set()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            session.fail_pending(node_broker.NodeDetached())

    async with serve(connection, "127.0.0.1", 0, max_size=node_broker.MAX_PAYLOAD) as server:
        with (root / "cli.log").open("w+") as log:
            proc = await asyncio.create_subprocess_exec(
                node,
                str(package / "openclaw.mjs"),
                "node",
                "run",
                "--host",
                "127.0.0.1",
                "--port",
                str(server.sockets[0].getsockname()[1]),
                "--no-share-installed-apps",
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(ready.wait(), 120)
                session = sessions[0]
                target = TARGET | {"commands": sorted(session.commands)}
                policy = {"readRoots": [str(approved)], "writeRoots": [str(approved)], "denyRoots": []}

                async def resolve(auth, request):
                    assert execution._ID.fullmatch(request["deviceId"])
                    return target

                async def authorize(auth):
                    assert auth == AUTH

                async def file_policy(*args):
                    return policy.copy()

                store = execution.DesktopOperations(
                    secret="isolated",
                    address=TARGET["taskAddress"],
                    resolve=resolve,
                    authorize=authorize,
                    dispatch=execution._dispatch,
                    cancel=execution._cancel,
                    file_policy=file_policy,
                )
                token = store.admit(AUTH, target)

                async def call(command, params):
                    receipt = await store.submit(
                        AUTH,
                        token,
                        REQUEST
                        | {
                            "requestId": str(uuid.uuid4()),
                            "command": command,
                            "params": params,
                            "executionDeadlineMs": 15000,
                        },
                    )
                    await asyncio.gather(*store.tasks.values())
                    return await store.get(AUTH, receipt["id"])

                binary = bytes(range(256)) * 512
                sha = hashlib.sha256(binary).hexdigest()
                path = approved / "binary.bin"
                receipt = await call(
                    "file.write", {"path": str(path), "contentBase64": base64.b64encode(binary).decode(), "sha256": sha}
                )
                assert receipt["state"] == "succeeded", receipt
                assert path.read_bytes() == binary
                # Revoke the standing root after a real pinned preflight, before write.
                original_dispatch = store.dispatch

                async def revoke_after_preflight(target, request, invoke_id):
                    result = await original_dispatch(target, request, invoke_id)
                    if request["params"].get("preflightOnly"):
                        policy["writeRoots"] = []
                    return result

                store.dispatch = revoke_after_preflight
                revoked_path = approved / "revoked.bin"
                revoked = await call(
                    "file.write",
                    {"path": str(revoked_path), "contentBase64": "Yg==", "sha256": hashlib.sha256(b"b").hexdigest()},
                )
                assert revoked["state"] == "rejected", revoked
                assert not revoked_path.exists()
                store.dispatch = original_dispatch
                policy["writeRoots"] = [str(approved)]
                receipt = await call(
                    "file.write",
                    {"path": str(path), "contentBase64": "Yg==", "sha256": hashlib.sha256(b"b").hexdigest()},
                )
                assert receipt["state"] == "rejected", receipt
                assert path.read_bytes() == binary
                receipt = await call("file.fetch", {"path": str(path)})
                assert receipt["state"] == "succeeded", receipt
                chunk = await store.artifact(AUTH, receipt["id"], 0, 0)
                assert hashlib.sha256(base64.b64decode(chunk["base64"])).hexdigest() == sha
                listing = await call("dir.list", {"path": str(approved)})
                assert listing["state"] == "succeeded", listing
                assert listing["result"]["entries"][0]["name"] == "binary.bin"
                archive = await call("dir.fetch", {"path": str(approved)})
                assert archive["state"] == "succeeded", archive
                outside = root / "secret"
                outside.write_text("private")
                (approved / "link").symlink_to(outside)
                assert (await call("file.fetch", {"path": str(approved / "link")}))["state"] == "rejected"
                # Actual pinned inode binding rejects replacement after preflight.
                pre = await session.invoke(
                    "file.fetch",
                    {"path": str(path), "preflightOnly": True, "followSymlinks": False},
                    5000,
                    file_authorized=True,
                )
                path.rename(approved / "old.bin")
                path.write_text("replacement")
                swapped = await session.invoke(
                    "file.fetch",
                    {
                        "path": str(path),
                        "expectedCanonicalPath": str(path),
                        "expectedBinding": pre["payload"]["binding"],
                        "followSymlinks": False,
                    },
                    5000,
                    file_authorized=True,
                )
                assert swapped["payload"]["code"] == "CANONICAL_PATH_CHANGED", swapped
                # No absolute/traversal schema can turn the fixture's outside path into authority.
                with pytest.raises(execution.DesktopError):
                    await call("file.fetch", {"path": str(approved) + "/../secret"})
                listed = await call("mcp_list", {})
                assert listed["state"] == "succeeded", listed
                version = listed["result"]["catalogVersion"]
                mcp = {"server": "fixture", "tool": "echo", "arguments": {"value": 42}, "catalogVersion": version}
                result = await call("mcp_call", mcp)
                assert result["state"] == "succeeded", result
                assert "42" in json.dumps(result["result"])
                await call("mcp_call", mcp | {"arguments": {"remove": True}})
                await asyncio.wait_for(changed.wait(), 10)
                assert (await call("mcp_call", mcp))["state"] == "rejected"
                session.commands.discard("mcp.tools.call.v1")
                target["commands"] = sorted(session.commands)
                assert session.mcp_catalog()["tools"] == []
                assert (await call("mcp_call", mcp))["state"] == "rejected"
                print(
                    "Pinned CLI: binary 128KiB round-trip, no overwrite, listing/archive, symlink/inode denial, stdio MCP list/call/change/revoke passed"
                )
            except BaseException:
                log.seek(0)
                print(log.read()[-12000:])
                raise
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
