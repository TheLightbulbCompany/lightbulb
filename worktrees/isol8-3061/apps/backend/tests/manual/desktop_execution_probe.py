"""Probe the actual vendored node on loopback, with disposable state and marker files.

This emulates a trusted gateway, NOT a human approval UI or production enrollment.
No approval setters are called. Exit 2 means the shell release gate is still blocked.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

# Load only the pure frame layer: core.services.__init__ imports live backend settings.
spec = importlib.util.spec_from_file_location(
    "probe_node_broker", Path(__file__).resolve().parents[2] / "core/services/node_broker.py"
)
node_broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_broker)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def host_digest(root: Path) -> str:
    """Bind all host files, including sibling dependencies and native modules."""
    result = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result.update(f"{path.relative_to(root)}\0{digest(path)}\n".encode())
    return result.hexdigest()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def until(predicate, seconds: float = 8) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


class Loopback:
    def __init__(self, evidence: dict):
        self.evidence = evidence
        self.ready = asyncio.Event()
        self.results = {}
        self.ws = None
        self.node_id = None

    async def send(self, frame: dict):
        self.evidence["frames"].append({"direction": "gateway->node", "frame": frame})
        await self.ws.send(json.dumps(frame))

    async def connection(self, ws):
        if self.ws is not None:
            await ws.close(1008, "probe does not replay on reconnect")
            return
        self.ws = ws
        challenge = node_broker.challenge_frame()
        await self.send(challenge)
        connect = json.loads(await asyncio.wait_for(ws.recv(), 15))
        self.evidence["connect"] = connect
        assert connect["method"] == "connect"
        assert node_broker.signature_matches(connect["params"], challenge["payload"]["nonce"])
        self.node_id = connect["params"]["device"]["id"]
        await self.send(node_broker.hello_ok(connect["id"], "isolated-probe"))
        self.ready.set()

        async def ticks():
            while True:
                await asyncio.sleep(5)
                await self.send(node_broker.tick_frame())

        ticker = asyncio.create_task(ticks())
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.evidence["frames"].append({"direction": "node->gateway", "frame": frame})
                if frame.get("type") != "req":
                    continue
                if frame.get("method") == "node.invoke.result":
                    params = frame["params"]
                    result = dict(params)
                    if isinstance(result.get("payloadJSON"), str):
                        result["payload"] = json.loads(result["payloadJSON"])
                    self.results[params["id"]] = result
                    await self.send(node_broker.ack(frame["id"]))
                elif frame.get("method") in node_broker.NODE_METHODS:
                    await self.send(node_broker.ack(frame["id"]))
                else:
                    # Do not accidentally emulate an approval authority or backend APIs.
                    await self.send(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": False,
                            "error": {"code": "UNAVAILABLE", "message": "isolated probe"},
                        }
                    )
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    async def dispatch(self, command, params, invoke_id=None):
        invoke_id = invoke_id or str(uuid.uuid4())
        await self.send(node_broker.invoke_request(invoke_id, self.node_id, command, params, 15_000))
        return invoke_id

    async def result(self, invoke_id, seconds=8):
        if await until(lambda: invoke_id in self.results, seconds):
            return self.results[invoke_id]
        return {"outcome": "no-result", "invokeId": invoke_id}

    async def invoke(self, command, params):
        return await self.result(await self.dispatch(command, params))

    async def cancel(self, invoke_id):
        await self.send(
            {"type": "event", "event": "node.invoke.cancel", "payload": {"invokeId": invoke_id, "nodeId": self.node_id}}
        )


async def probe(args, evidence, root):
    from websockets.asyncio.server import serve

    env = {
        "HOME": str(root),
        "TMPDIR": str(root),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "OPENCLAW_HOME": str(root / "home"),
        "OPENCLAW_STATE_DIR": str(root / "state"),
        "OPENCLAW_CONFIG_PATH": str(root / "config.json"),
        "XDG_CONFIG_HOME": str(root / "xdg"),
        "OPENCLAW_GATEWAY_TOKEN": "isolated-probe-no-external-authority",
        "NO_COLOR": "1",
    }
    config = {} if args.policy == "default" else {"tools": {"exec": {"security": "allowlist", "ask": "always"}}}
    (root / "config.json").write_text(json.dumps(config))
    evidence["syntheticConfig"] = config
    openclaw_dir = args.host_dir / "node_modules/openclaw"
    base = [str(args.node_bin), str(openclaw_dir / "openclaw.mjs")]
    evidence["artifacts"] = {
        "node": str(args.node_bin),
        "nodeSha256": digest(args.node_bin),
        "host": str(args.host_dir),
        "hostSha256": host_digest(args.host_dir),
        "packageVersion": json.loads((openclaw_dir / "package.json").read_text())["version"],
    }
    evidence["inspection"] = []
    for argv in ([str(args.node_bin), "--version"], [*base, "--version"], [*base, "node", "run", "--help"]):
        result = subprocess.run(argv, env=env, cwd=root, capture_output=True, text=True, timeout=30)
        evidence["inspection"].append(
            {"argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        )
        assert result.returncode == 0, result.stderr
    help_text = evidence["inspection"][-1]["stdout"]
    assert "--host" in help_text and "--port" in help_text
    gateway = Loopback(evidence)
    process = None
    tracked_pids = set()
    async with serve(gateway.connection, "127.0.0.1", 0, max_size=26_214_400) as server:
        argv = [
            *base,
            "node",
            "run",
            "--host",
            "127.0.0.1",
            "--port",
            str(server.sockets[0].getsockname()[1]),
            "--display-name",
            "isolated-probe",
            "--no-share-installed-apps",
        ]
        evidence["launch"] = {"argv": argv, "env": env, "cwd": str(root)}
        with (root / "host.log").open("w+") as log:
            try:
                process = subprocess.Popen(argv, env=env, cwd=root, stdout=log, stderr=log, start_new_session=True)
                await asyncio.wait_for(gateway.ready.wait(), 30)
                marker_program = root / "marker.py"
                marker_program.write_text("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')\n")
                for name in (
                    "unapproved",
                    "deny",
                    "allow-once",
                    "changed-argv",
                    "changed-cwd",
                    "expired",
                    "changed-run",
                ):
                    marker = root / f"{name}.marker"
                    params = {
                        "command": [
                            sys.executable,
                            str(marker_program),
                            str(marker),
                        ],
                        "cwd": str(root),
                        "agentId": "probe-agent",
                        "sessionKey": "probe-session",
                        "runId": str(uuid.uuid4()),
                        "timeoutMs": 2000,
                    }
                    prepared = await gateway.invoke("system.run.prepare", params)
                    assert prepared.get("ok"), prepared
                    plan = prepared["payload"]["plan"]
                    params.update(command=plan["argv"], cwd=plan["cwd"], systemRunPlan=plan)
                    if name != "unapproved":
                        params["approvalDecision"] = "deny" if name == "deny" else "allow-once"
                    if name == "changed-argv":
                        params["command"] = [*params["command"], "changed"]
                    if name == "changed-cwd":
                        other = root / "other"
                        other.mkdir()
                        params["cwd"] = str(other)
                    if name == "expired":
                        params["expiresAtMs"] = 1  # Diagnostic only: NOT a documented node field.
                    if name == "changed-run":
                        params["runId"] = "different-run"
                    result = await gateway.invoke("system.run", params)
                    evidence["cases"][name] = {"prepared": prepared, "result": result, "markerExists": marker.exists()}
                    if name in ("changed-argv", "changed-cwd"):
                        assert result.get("ok") is False and result.get("error") == {
                            "code": "INVALID_REQUEST",
                            "message": "explicit approval requires matching systemRunPlan",
                        }, result
                        assert not marker.exists(), evidence["cases"][name]
                    elif name in ("unapproved", "deny") and args.policy == "restrictive":
                        assert result.get("ok") is False and result.get("error") == {
                            "code": "SYSTEM_RUN_DENIED",
                            "message": "SYSTEM_RUN_DENIED: approval required",
                        }, result
                        assert not marker.exists(), evidence["cases"][name]
                    else:
                        payload = result.get("payload", {})
                        assert result.get("ok") is True and payload.get("exitCode") == 0, result
                        assert payload.get("success") is True and payload.get("timedOut") is False, result
                        assert marker.exists() and marker.read_text() == "ran", evidence["cases"][name]

                # Child + grandchild write PID files before waiting and a late marker after 3s.
                # Observe beyond that deadline before cleanup; harness cleanup cannot fake a pass.
                for name in (
                    "cancel-child",
                    "cancel-grandchild",
                    "timeout-grandchild",
                    "stop-before-spawn",
                    "disconnect-grandchild",
                ):
                    case_dir = root / name
                    case_dir.mkdir()
                    leaf = (
                        "import os,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                        "(p/'leaf.pid').write_text(str(os.getpid())); time.sleep(3); (p/'late.marker').write_text('late'); time.sleep(7)"
                    )
                    child = (
                        "import os,pathlib,subprocess,sys,time; p=pathlib.Path(sys.argv[1]); "
                        "(p/'child.pid').write_text(str(os.getpid())); "
                        f"subprocess.Popen([sys.executable,'-c',{leaf!r},str(p)]); time.sleep(10)"
                    )
                    program = leaf if name == "cancel-child" else child
                    parent = (
                        "import os,pathlib,subprocess,sys,time; p=pathlib.Path(sys.argv[1]); "
                        "(p/'parent.pid').write_text(str(os.getpid())); "
                        f"subprocess.Popen([sys.executable,'-c',{program!r},str(p)]); time.sleep(10)"
                    )
                    parent_program = case_dir / "tree.py"
                    parent_program.write_text(parent + "\n")
                    params = {
                        "command": [sys.executable, str(parent_program), str(case_dir)],
                        "cwd": str(root),
                        "agentId": "probe-agent",
                        "sessionKey": "probe-session",
                        "runId": str(uuid.uuid4()),
                        "timeoutMs": 1000 if name.startswith("timeout") else 12_000,
                    }
                    prepared = await gateway.invoke("system.run.prepare", params)
                    assert prepared.get("ok"), prepared
                    plan = prepared["payload"]["plan"]
                    params.update(
                        command=plan["argv"], cwd=plan["cwd"], systemRunPlan=plan, approvalDecision="allow-once"
                    )
                    invoke_id = str(uuid.uuid4())
                    if name == "stop-before-spawn":
                        await gateway.cancel(invoke_id)
                    await gateway.dispatch("system.run", params, invoke_id)
                    started = await until(lambda: (case_dir / "leaf.pid").exists(), 3)
                    pids = [int(path.read_text()) for path in case_dir.glob("*.pid")]
                    tracked_pids.update(pids)
                    process_tree = (
                        subprocess.run(
                            ["/bin/ps", "-o", "pid,ppid,pgid,stat,command", "-p", ",".join(map(str, pids))],
                            capture_output=True,
                            text=True,
                        ).stdout
                        if pids
                        else ""
                    )
                    case = {"invokeId": invoke_id, "started": started, "pids": pids, "processTree": process_tree}
                    evidence["cases"][name] = case
                    if name.startswith("cancel") or name == "stop-before-spawn":
                        await gateway.cancel(invoke_id)
                    elif name.startswith("disconnect"):
                        await gateway.ws.close(1000, "probe disconnect")
                    await asyncio.sleep(3.5)
                    case.update(
                        aliveAfterDeadline=[pid for pid in pids if alive(pid)],
                        lateMarkerExists=(case_dir / "late.marker").exists(),
                        result=gateway.results.get(invoke_id, {"outcome": "no-result"}),
                    )
                    case["terminationProven"] = (
                        started and not case["aliveAfterDeadline"] and not case["lateMarkerExists"]
                    )
                    if name == "stop-before-spawn":
                        case["spawnPreventionProven"] = not started and not pids
                    print(
                        f"{name}: started={started}, alive={case['aliveAfterDeadline']}, "
                        f"lateMarker={case['lateMarkerExists']}, result={case['result'].get('outcome', 'received')}",
                        flush=True,
                    )
                    # Clean up only observed, test-owned PIDs after recording failure evidence.
                    for pid in pids:
                        if alive(pid):
                            with contextlib.suppress(ProcessLookupError):
                                os.kill(pid, signal.SIGKILL)
                    assert case["terminationProven"], case
                    if name == "timeout-grandchild":
                        result = case["result"]
                        payload = result.get("payload", {})
                        assert result.get("ok") is True and payload.get("exitCode") == 124, result
                        assert payload.get("timedOut") is True and payload.get("success") is False, result
            finally:
                for pid in tracked_pids:
                    if alive(pid):
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)
                if process is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.to_thread(process.wait, timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        await asyncio.to_thread(process.wait)
                log.seek(0)
                evidence["hostLog"] = log.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loopback",
        action="store_true",
        required=True,
        help="Explicitly opt in to a disposable local node; no real device is selected or paired",
    )
    parser.add_argument("--node-bin", type=Path, required=True, help="Existing vendored Node binary (read only)")
    parser.add_argument(
        "--host-dir", type=Path, required=True, help="Complete vendored openclaw-host-<triple> directory (read only)"
    )
    parser.add_argument("--output", type=Path, required=True, help="New JSON evidence file; refuses overwrite")
    parser.add_argument(
        "--policy",
        choices=("default", "restrictive"),
        default="restrictive",
        help="Disposable config only: restrictive uses allowlist/always; default records upstream defaults",
    )
    args = parser.parse_args()
    args.node_bin = args.node_bin.resolve(strict=True)
    args.host_dir = args.host_dir.resolve(strict=True)
    evidence = {
        "schemaVersion": 1,
        "scope": "isolated-loopback-simulated-gateway-approval",
        "frames": [],
        "cases": {},
        "shellReleaseGate": "blocked-native-human-approval-unproven",
        "unproven": ["native approve-once/deny/close/expiry UI", "stop during native approval"],
    }
    with args.output.open("x") as output:
        try:
            with tempfile.TemporaryDirectory(prefix="lightbulb-desktop-probe-") as temporary:
                asyncio.run(probe(args, evidence, Path(temporary)))
        except Exception as error:
            evidence["error"] = repr(error)
            raise
        finally:
            json.dump(evidence, output, indent=2)
            output.write("\n")
    print(f"Evidence: {args.output}. Shell release gate remains blocked; this is not human approval proof.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
