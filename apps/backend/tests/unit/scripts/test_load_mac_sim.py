"""The synthetic Mac's wire frames and device token (scripts/load/mac_sim.py), checked with the
backend's own verifiers: machine_auth for the token, the relay's frame models for every frame."""

import asyncio
import base64
import time
from unittest.mock import AsyncMock

import pytest

from core.services import machine_auth, session_screen
from core.services.desktop_pair import build_paired_entry
from core.services.local_session_relay import _WIRE, check_envelope
from scripts.load import mac_sim as sim

SESSION = {"sessionId": "6f1c2b0e-7d6a-4c1e-9a55-1f2e3d4c5b6a", "generation": "g0123456789abcdef"}


async def test_device_token_passes_the_backends_own_verifier(monkeypatch):
    key, pem = sim.keypair()
    entry = build_paired_entry(pubkey_pem=pem, client_id="node-host", display_name="x", commands=[], user_id="user_1")
    row = {"paired_devices": {entry["deviceId"]: entry}}
    monkeypatch.setattr(machine_auth.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    now = int(time.time())
    ident = await machine_auth._device(sim.device_token(key, entry["deviceId"], "org_1", now), "node-host")
    assert ident is not None and ident.user_id == "user_1" and ident.org_id == "org_1"
    other, _ = sim.keypair()  # a key that was never paired cannot sign for this device
    assert await machine_auth._device(sim.device_token(other, entry["deviceId"], "org_1", now), "node-host") is None


def test_relay_url_is_dev_only():
    url = sim.relay_url("wss://ws-dev.thelightbulb.company", "t.o.k")
    assert url == "wss://ws-dev.thelightbulb.company?clientId=lightbulb-local-sessions&token=t.o.k"
    for bad in (
        "wss://ws.thelightbulb.company",
        "ws://ws-dev.thelightbulb.company",
        "wss://ws-dev.thelightbulb.company.evil.io",
    ):
        with pytest.raises(SystemExit):
            sim.relay_url(bad, "t")


def test_session_identity_is_stable_and_registers_a_held_session():
    a = sim.session_identity("d" * 64, 0)
    assert a == sim.session_identity("d" * 64, 0) and a != sim.session_identity("d" * 64, 1)
    frame = sim.register_frame(a, 0)
    check_envelope(frame)
    parsed = _WIRE.validate_python(frame)
    assert parsed.session.holder_id == a["holderId"]  # a holder => terminal "held": viewable


@pytest.mark.parametrize(
    "frame",
    [
        sim.heartbeat_frame([SESSION, {**SESSION, "sessionId": "0b8e2b9c-1d2e-4f3a-8b4c-5d6e7f8a9b0c"}]),
        sim.events_frame(SESSION, 4, 1),
        sim.ended_frame(SESSION, 1_700_000_000),
    ],
)
def test_awaited_frames_validate_against_the_relay(frame):
    check_envelope(frame)
    assert _WIRE.validate_python(frame).type == frame["type"]
    assert frame["type"] in sim.AWAITED


def test_screen_frames_fit_the_relay_and_carry_their_send_time():
    for size in (0, 100, 8192, sim.MAX_RAW, sim.MAX_RAW + 500):
        frame = sim.screen_frame(SESSION, 1, 2, "output", size, 1_700_000_000_123)
        check_envelope(frame)  # under 16 KiB, as the router checks
        raw = base64.b64decode(session_screen.Screen.model_validate(frame).data)
        assert len(raw) <= sim.MAX_RAW
        assert raw.startswith(b"\r\nLT=1700000000123;")
    assert len(base64.b64decode(sim.screen_frame(SESSION, 1, 2, "output", 8192, 1)["data"])) == 8192


def test_pct():
    assert sim.pct([], 95) is None and sim.pct([5.0], 95) == 5.0
    assert sim.pct([float(i) for i in range(1, 101)], 50) == pytest.approx(50.5)


def test_dry_run_calls_nothing(monkeypatch, capsys):
    def boom(url):
        raise AssertionError("network")

    monkeypatch.setattr(sim, "dev_only", boom)
    assert sim.main(["--dry-run", "--macs", "2", "--fps", "20", "--", "k6", "run", "v.js"]) == 0
    out = capsys.readouterr().out
    assert "9.38 MiB/min" in out and "k6 run v.js" in out
    with pytest.raises(SystemExit):
        sim.main(["--dry-run", "--frame-bytes", str(sim.MAX_RAW + 1)])


class FakeProc:
    """A child that exits on its own (``exits_alone``), on SIGTERM (``obeys``), or only on kill."""

    def __init__(self, obeys: bool, exits_alone: bool = False):
        self.obeys, self.calls, self.returncode = obeys, [], None
        self._exit = asyncio.Event()
        if exits_alone:
            self.returncode = 0
            self._exit.set()

    async def wait(self):
        await self._exit.wait()
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if self.obeys:
            self.returncode = -15
            self._exit.set()

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9
        self._exit.set()


async def _child(monkeypatch, proc, stop_now=False, stop_later=False):
    spawned = []

    async def spawn(*cmd, env):
        spawned.append(cmd)
        return proc

    monkeypatch.setattr(sim.asyncio, "create_subprocess_exec", spawn)
    stop = asyncio.Event()
    if stop_now:
        stop.set()
    if stop_later:
        asyncio.get_running_loop().call_later(0.01, stop.set)
    return await sim.run_child(["k6", "run"], stop, {}, grace=0.05), spawned


async def test_a_stop_before_the_command_launches_nothing(monkeypatch):
    assert await _child(monkeypatch, FakeProc(obeys=True), stop_now=True) == (130, [])


async def test_the_command_finishing_first_returns_its_code(monkeypatch):
    assert await _child(monkeypatch, FakeProc(obeys=True, exits_alone=True)) == (0, [("k6", "run")])


async def test_a_stop_while_running_terminates_the_child(monkeypatch):
    proc = FakeProc(obeys=True)
    assert (await _child(monkeypatch, proc, stop_later=True))[0] == 130
    assert proc.calls == ["terminate"]


async def test_a_child_that_ignores_sigterm_is_killed_after_the_grace(monkeypatch):
    proc = FakeProc(obeys=False)
    assert (await _child(monkeypatch, proc, stop_later=True))[0] == 130
    assert proc.calls == ["terminate", "kill"]


@pytest.mark.parametrize(
    "bad",
    [
        ["--fps", "-1"],
        ["--fps", "nan"],
        ["--events-per-s", "-0.5"],
        ["--frame-bytes", "0"],
        ["--sessions", "0"],
        ["--sessions", "25"],
        ["--macs", "two"],
        ["--duration", "0"],
        ["--storm-at", "-3"],
    ],
)
def test_bad_numbers_are_refused_before_anything_runs(bad):
    with pytest.raises(SystemExit):
        sim.main(["--dry-run", *bad])
