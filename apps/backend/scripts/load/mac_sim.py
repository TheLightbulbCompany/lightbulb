"""Synthetic Macs for load scenario 3 (see scripts/load/README.md).

Each fixture user (the first ``--macs`` in fixtures.json) plays one paired Mac: it pairs two fresh
Ed25519 keys (``POST /desktop/pair``), enrolls, opens the local-session relay socket with a
self-minted EdDSA device token, registers ``--sessions`` held sessions, then heartbeats every 10 s,
sends ``local.session.events`` at ``--events-per-s`` and ``output`` screen frames at ``--fps`` x
``--frame-bytes`` (each frame's text carries ``LT=<Date.now()>;`` for the viewers to time). A
``local.session.watch`` nudge gets a snapshot, as a real Mac answers a new viewer.

Run it under ``fixtures.py run`` (it reads FIXTURES), with the viewers as its trailing command:

    uv run python -m scripts.load.fixtures run --orgs 1 --users-per-org 26 -- \\
      uv run python -m scripts.load.mac_sim --macs 1 --fps 4 --frame-bytes 8192 -- \\
      k6 run -e SEND_S=180 -e VIEWERS=25 scripts/load/k6/viewers.js

It writes ``mac_sessions.json`` (0600) for the viewers, runs the command once every session is
registered, and on exit (command done, ``--duration`` over, or SIGINT) ends the sessions, closes the
sockets, unpairs the devices and deletes the file. Proves the backend half only: no real Claude/Codex,
holder PTY, journal or Tauri transport. Never run it without the owner's go for that run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import signal
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import jwt
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.load.fixtures import remove, write_private
from scripts.multiparty_e2e.runner import API, Api, Session, dev_only

HERE = Path(__file__).parent
SESSIONS_OUT = HERE / "mac_sessions.json"
WS_DEV_HOST = "ws-dev.thelightbulb.company"
CLIENT_ID = "lightbulb-local-sessions"
HEARTBEAT_S = 10
# The desktop's own numbers (device_token.rs): 90 s lifetime, iat backdated 60 s; cap is 300 s.
TOKEN_LIFETIME_S, IAT_BACKDATE_S = 90, 60
MAX_RAW = 11 * 1024  # session_screen.MAX_RAW_BYTES
AWAITED = {
    "local.session.register": "local.session.registered",
    "local.device.heartbeat": "local.device.heartbeat.ack",
    "local.session.events": "local.session.events.ack",
    "local.session.ended": "local.session.ended.ack",
}


# ------------------------------------------------------------------ pure builders (unit-tested)
def keypair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, pem.decode()


def device_token(key: Ed25519PrivateKey, device_id: str, owner: str, now: int) -> str:
    return jwt.encode(
        {
            "kind": "device",
            "deviceId": device_id,
            "owner": owner,
            "iat": now - IAT_BACKDATE_S,
            "exp": now + TOKEN_LIFETIME_S,
        },
        key,
        algorithm="EdDSA",
    )


def relay_url(gateway_url: str, token: str) -> str:
    parts = urlsplit(gateway_url)
    if parts.scheme != "wss" or parts.hostname != WS_DEV_HOST:
        raise SystemExit(f"refusing relay {gateway_url}: dev only")
    return f"{gateway_url.split('?')[0]}?{urlencode({'clientId': CLIENT_ID, 'token': token})}"


def session_identity(device_id: str, index: int) -> dict:
    """Stable per device + slot, so a reconnect re-registers the same session."""
    seed = f"{device_id}:{index}"
    return {
        "profileFingerprint": hashlib.sha256(f"profile:{device_id}".encode()).hexdigest(),
        "nativeConversationId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"load-sim:{seed}")),
        "generation": "g" + hashlib.sha256(seed.encode()).hexdigest()[:16],
        "holderId": hashlib.sha256(f"holder:{seed}".encode()).hexdigest()[:8],
    }


def register_frame(ident: dict, index: int) -> dict:
    return {
        "type": "local.session.register",
        "session": {
            "provider": "claude",
            "profileFingerprint": ident["profileFingerprint"],
            "nativeConversationId": ident["nativeConversationId"],
            "generation": ident["generation"],
            "providerVersion": "load-sim",
            "status": "idle",
            "title": f"load sim {index}",
            "holderId": ident["holderId"],
        },
    }


def heartbeat_frame(sessions: list[dict]) -> dict:
    return {
        "type": "local.device.heartbeat",
        "sessions": [
            {"sessionId": s["sessionId"], "generation": s["generation"], "status": "idle", "terminal": "held"}
            for s in sessions
        ],
        "liveTerminal": True,
    }


def events_frame(session: dict, after_seq: int, n: int) -> dict:
    return {
        "type": "local.session.events",
        "sessionId": session["sessionId"],
        "generation": session["generation"],
        "batchId": uuid.uuid4().hex,
        "afterSeq": after_seq,
        "events": [{"kind": "text", "role": "assistant", "text": f"load event {n}", "truncated": False}],
    }


def screen_frame(session: dict, epoch: int, seq: int, kind: str, size: int, now_ms: int) -> dict:
    """``size`` raw bytes of terminal text; the first line carries the send time for the viewer."""
    head = f"\r\nLT={now_ms};".encode()
    raw = (head + b"x" * max(0, size - len(head)))[: max(len(head), min(size, MAX_RAW))]
    return {
        "type": "local.session.screen",
        "sessionId": session["sessionId"],
        "generation": session["generation"],
        "epoch": epoch,
        "seq": seq,
        "cols": 120,
        "rows": 40,
        "kind": kind,
        "data": base64.b64encode(raw).decode(),
    }


def ended_frame(session: dict, now: int) -> dict:
    return {
        "type": "local.session.ended",
        "sessionId": session["sessionId"],
        "generation": session["generation"],
        "reason": "exited",
        "endedAt": now,
    }


def pct(values: list[float], q: int) -> float | None:
    if len(values) < 2:
        return values[0] if values else None
    return round(statistics.quantiles(values, n=100)[q - 1], 1)


# ------------------------------------------------------------------ one synthetic Mac
class Mac:
    def __init__(self, user: dict, args, stats: Counter, rtts: list[float]):
        self.user, self.args, self.stats, self.rtts = user, args, stats, rtts
        self.api = Api(Session.resume(user["sid"], user["clientJwt"]))
        self.key, mac_pem = keypair()
        _, cli_pem = keypair()
        self.pems = (mac_pem, cli_pem)
        self.sessions: list[dict] = []
        self.ws = None
        self.slot = asyncio.Lock()
        self.pending: tuple[str, asyncio.Future] | None = None
        self.epoch, self.seq = int(time.time()), 0
        self.quiet_until = 0.0  # no offline check right after a (re)connect

    async def pair(self) -> None:
        resp = await asyncio.to_thread(
            self.api,
            "POST",
            "/desktop/pair",
            json={
                "tauriMacPubKey": self.pems[0],
                "cliPubKey": self.pems[1],
                "permissions": {"screen": False, "shell": False, "browser": False, "files": False},
            },
        )
        self.gateway, self.owner = resp["gatewayUrl"], resp["ownerId"]
        self.devices = (resp["deviceIdMac"], resp["deviceIdCli"])
        relay_url(self.gateway, "probe")  # dev guard before anything connects
        await asyncio.to_thread(
            self.api, "PUT", "/desktop/local-sessions/enrollment", json={"deviceId": self.devices[0], "enabled": True}
        )

    async def connect(self) -> None:
        url = relay_url(self.gateway, device_token(self.key, self.devices[0], self.owner, int(time.time())))
        self.ws = await websockets.connect(url, max_size=64 * 1024)
        self.reader = asyncio.create_task(self.read(self.ws))
        self.quiet_until = time.monotonic() + 15
        self.stats["connects"] += 1
        for i in range(self.args.sessions):
            ident = session_identity(self.devices[0], i)
            ack = await self.request(register_frame(ident, i))
            if ack.get("type") != "local.session.registered":
                raise RuntimeError(f"register refused: {ack}")
            entry = {"sessionId": ack["sessionId"], "generation": ack["generation"], "afterSeq": ack["afterSeq"]}
            if i < len(self.sessions):
                self.sessions[i] = entry
            else:
                self.sessions.append(entry)

    async def read(self, ws) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if self.pending and kind in (self.pending[0], "local.session.error"):
                    if not self.pending[1].done():
                        self.pending[1].set_result(msg)
                elif kind == "local.session.watch":
                    self.stats["watch_nudges"] += 1
                    s = next((s for s in self.sessions if s["sessionId"] == msg.get("sessionId")), None)
                    if s:
                        await self.screen(s, "snapshot")
        except Exception as exc:  # noqa: BLE001 -- a dropped socket is a measurement, not a crash
            self.stats[f"socket_closed:{type(exc).__name__}"] += 1

    async def request(self, frame: dict) -> dict:
        """One awaited frame at a time, as the desktop holds one request slot."""
        async with self.slot:
            fut = asyncio.get_running_loop().create_future()
            self.pending = (AWAITED[frame["type"]], fut)
            started = time.monotonic()
            try:
                await self.ws.send(json.dumps(frame, separators=(",", ":")))
                msg = await asyncio.wait_for(fut, 10)
            except asyncio.TimeoutError:
                self.stats[f"timeout:{frame['type']}"] += 1
                return {}
            except websockets.ConnectionClosed:
                self.stats[f"closed:{frame['type']}"] += 1  # a storm reconnect in progress
                return {}
            finally:
                self.pending = None
            if msg.get("type") == "local.session.error":
                self.stats[f"error:{frame['type']}:{msg.get('code')}"] += 1
            elif frame["type"] == "local.device.heartbeat":
                self.rtts.append((time.monotonic() - started) * 1000)
            return msg

    async def screen(self, session: dict, kind: str) -> None:
        self.seq += 1
        frame = screen_frame(session, self.epoch, self.seq, kind, self.args.frame_bytes, int(time.time() * 1000))
        try:
            await self.ws.send(json.dumps(frame, separators=(",", ":")))
            self.stats["frames_sent"] += 1
        except Exception:  # noqa: BLE001
            self.stats["frames_failed"] += 1

    async def heartbeats(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.request(heartbeat_frame(self.sessions))
            await _sleep(stop, HEARTBEAT_S)

    async def events(self, stop: asyncio.Event) -> None:
        n = 0
        while self.args.events_per_s and not stop.is_set():
            for s in list(self.sessions):
                n += 1
                ack = await self.request(events_frame(s, s["afterSeq"], n))
                if ack.get("type") == "local.session.events.ack":
                    s["afterSeq"] = ack["afterSeq"]
            await _sleep(stop, 1 / self.args.events_per_s)

    async def frames(self, stop: asyncio.Event) -> None:
        # On a fixed schedule, so the viewers' "fps x seconds watched" is what was really sent.
        start, n = time.monotonic(), 0
        while self.args.fps and not stop.is_set():
            for s in list(self.sessions):
                await self.screen(s, "output")
            n += 1
            await _sleep(stop, max(0.0, start + n / self.args.fps - time.monotonic()))

    async def offline_check(self, stop: asyncio.Event) -> None:
        """Spec §4: sessions listed offline while their Mac is connected must stay 0."""
        while not stop.is_set():
            await _sleep(stop, 30)
            if stop.is_set() or time.monotonic() < self.quiet_until:
                continue
            try:
                listed = (await asyncio.to_thread(self.api, "GET", "/messaging/sessions"))["sessions"]
            except RuntimeError:
                self.stats["sessions_read_failed"] += 1
                continue
            mine = {s["sessionId"] for s in self.sessions}
            self.stats["offline_while_connected"] += sum(
                s["sessionId"] in mine and s["status"] == "offline" for s in listed
            )

    async def storm(self, stop: asyncio.Event) -> None:
        """3c: close the socket and reopen it within 1 s."""
        await _sleep(stop, self.args.storm_at)
        if stop.is_set():
            return
        await self.ws.close()
        self.reader.cancel()
        await asyncio.sleep(random.random())
        await self.connect()
        self.stats["storm_reconnects"] += 1

    async def finish(self) -> None:
        try:
            if self.ws is not None:
                for s in self.sessions:
                    await self.request(ended_frame(s, int(time.time())))
                await self.ws.close()
        except Exception as exc:  # noqa: BLE001 -- still unpair
            self.stats[f"finish_failed:{type(exc).__name__}"] += 1
        for device in getattr(self, "devices", ()):
            try:
                await asyncio.to_thread(self.api, "DELETE", f"/desktop/devices/{device}")
            except RuntimeError:
                self.stats["unpair_failed"] += 1


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


CHILD_GRACE_S = 30


async def run_child(command: list[str], stop: asyncio.Event, env: dict, grace: float = CHILD_GRACE_S) -> int:
    """Run the viewers' command until it exits or ``stop`` fires. A stop that came first launches
    nothing; a stop while it runs sends SIGTERM (k6 stops gracefully and prints its summary), then
    kills it after ``grace`` seconds."""
    if stop.is_set():
        return 130
    proc = await asyncio.create_subprocess_exec(*command, env=env)
    done = asyncio.create_task(proc.wait())
    stopped = asyncio.create_task(stop.wait())
    await asyncio.wait([done, stopped], return_when=asyncio.FIRST_COMPLETED)
    stopped.cancel()
    if done.done():
        return proc.returncode
    try:
        proc.terminate()
        await asyncio.wait_for(asyncio.shield(done), grace)
    except ProcessLookupError:
        pass
    except asyncio.TimeoutError:
        proc.kill()
        await done
    return 130


async def simulate(args, users: list[dict]) -> int:
    stats: Counter = Counter()
    rtts: list[float] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    macs = [Mac(u, args, stats, rtts) for u in users]
    code = 0
    try:
        await asyncio.gather(*(m.pair() for m in macs))
        await asyncio.gather(*(m.connect() for m in macs))
        write_private(
            SESSIONS_OUT,
            {
                "fps": args.fps,
                "sessions": [
                    {"sessionId": s["sessionId"], "orgId": m.user["orgId"], "ownerUserId": m.user["userId"]}
                    for m in macs
                    for s in m.sessions
                ],
            },
        )
        print(f"{len(macs)} Macs, {sum(len(m.sessions) for m in macs)} sessions registered", file=sys.stderr)
        work = [
            asyncio.create_task(job(stop))
            for m in macs
            for job in (m.heartbeats, m.events, m.frames, m.offline_check) + ((m.storm,) if args.storm_at else ())
        ]
        if args.command:
            code = await run_child(args.command, stop, {**os.environ, "MAC_SESSIONS": str(SESSIONS_OUT.resolve())})
        else:
            await _sleep(stop, args.duration)
        stop.set()
        await asyncio.gather(*work, return_exceptions=True)
    finally:
        stop.set()
        remove(SESSIONS_OUT)
        await asyncio.gather(*(m.finish() for m in macs), return_exceptions=True)
        summary = {
            "heartbeat_rtt_ms": {"p50": pct(rtts, 50), "p95": pct(rtts, 95), "n": len(rtts)},
            **dict(sorted(stats.items())),
        }
        print(json.dumps(summary, indent=2))
    return code


def at_least(low: float, kind=float):
    """argparse type: a finite number >= ``low`` (0 means "off" where the help says so)."""

    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
        if value != value or value in (float("inf"), float("-inf")) or value < low:
            raise argparse.ArgumentTypeError(f"{text!r} must be >= {low}")
        return value

    return parse


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--macs", type=at_least(1, int), default=1, help="one per fixture user, from the first")
    ap.add_argument("--sessions", type=at_least(1, int), default=1, help="held sessions per Mac (<= 24 per heartbeat)")
    ap.add_argument("--fps", type=at_least(0, float), default=4.0, help="output frames/s per session (0 = none)")
    ap.add_argument("--frame-bytes", type=at_least(1, int), default=8192, help=f"raw bytes per frame (max {MAX_RAW})")
    ap.add_argument("--events-per-s", type=at_least(0, float), default=0.0, help="events batches per second")
    ap.add_argument(
        "--storm-at", type=at_least(0, float), default=0.0, help="3c: reconnect every socket at this second"
    )
    ap.add_argument("--duration", type=at_least(1, float), default=900.0, help="seconds, when no command is given")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="run after registration, e.g. -- k6 run viewers.js")
    args = ap.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if args.sessions > 24:
        raise SystemExit("--sessions over 24: one heartbeat frame lists at most 24")
    if args.frame_bytes > MAX_RAW:
        raise SystemExit(f"--frame-bytes over {MAX_RAW}: the relay drops it")
    per_min = args.fps * args.frame_bytes * 60 / 2**20
    if args.dry_run:
        print(f"[dry-run] {args.macs} Macs x {args.sessions} sessions via {API}: pair, enroll, connect {WS_DEV_HOST}")
        print(
            f"  heartbeat every {HEARTBEAT_S}s, events {args.events_per_s}/s, frames {args.fps}/s x {args.frame_bytes} B"
        )
        print(f"  = {per_min:.2f} MiB/min per session (relay budget 2 MiB/min per backend task)")
        if args.storm_at:
            print(f"  reconnect storm at {args.storm_at}s")
        print(f"  then {'run ' + ' '.join(args.command) if args.command else f'hold {args.duration}s'}; end, unpair")
        return 0
    dev_only(API)
    fixtures = json.loads(Path(os.environ["FIXTURES"]).read_text())
    if fixtures.get("api") != API:
        raise SystemExit("refusing: fixtures are not for the dev API")
    users = fixtures["users"][: args.macs]
    if len(users) < args.macs:
        raise SystemExit(f"{args.macs} Macs need {args.macs} fixture users; have {len(users)}")
    return asyncio.run(simulate(args, users))


if __name__ == "__main__":
    sys.exit(main())
