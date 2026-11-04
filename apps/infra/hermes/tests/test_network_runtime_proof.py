"""bridge.network: the runtime fetches, writes and renews its workspace proof."""
from __future__ import annotations

import io
import json
import logging
import stat
import urllib.error

import pytest

import bridge
from bridge import network
from test_network import net  # noqa: F401 -- the fake sidecar fixture

PROOF = "eyJhbGciOiJFZERTQSJ9.SECRETPAYLOAD.SIGSECRET"
TOKEN = "hook-token-xyz"


@pytest.fixture
def posts(monkeypatch):
    calls: list = []
    state = {"exp": 2_000_000_000, "fail": False}

    def urlopen(req, timeout=None):
        calls.append(req)
        if state["fail"]:
            raise urllib.error.URLError("unreachable")
        return io.BytesIO(json.dumps({"proof": PROOF, "exp": state["exp"]}).encode())

    monkeypatch.setattr(network.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bridge, "API", "http://backend")
    monkeypatch.setattr(bridge, "HOOK_TOKEN", TOKEN)
    monkeypatch.setattr(network, "_proof_exp", 0.0)
    return calls, state


def test_start_writes_login_json_before_the_engine_starts(net, posts, monkeypatch):  # noqa: F811
    seen = {}
    real = network.subprocess.Popen

    def popen(*a, **kw):
        seen["login"] = (network.NET_HOME / "login.json").exists()
        return real(*a, **kw)

    monkeypatch.setattr(network.subprocess, "Popen", popen)
    assert network.ensure_started(background=False)
    path = network.NET_HOME / "login.json"
    assert seen["login"] is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"proof": PROOF, "route_url": None, "name": "workspace"}
    req = posts[0][0]
    assert req.full_url == "http://backend/api/v1/internal/network/org_test/runtime-proof"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"


def test_start_proceeds_when_the_backend_is_unreachable(net, posts):  # noqa: F811
    posts[1]["fail"] = True
    assert network.ensure_started(background=False)
    assert not (network.NET_HOME / "login.json").exists()
    assert net.spawns() == 1


def test_renews_when_less_than_15_days_remain(net, posts):  # noqa: F811
    calls, state = posts
    assert network.ensure_started(background=False)
    assert len(calls) == 1 and net.calls("login.changed") == []
    assert network.renew_proof_if_due(now=state["exp"] - 20 * 86_400) is False
    assert len(calls) == 1
    assert network.renew_proof_if_due(now=state["exp"] - 10 * 86_400) is True
    assert len(calls) == 2
    assert net.calls("login.changed")


def test_the_proof_is_never_logged(net, posts, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    assert network.ensure_started(background=False)
    posts[1]["fail"] = True
    assert network.refresh_runtime_proof() is False
    text = caplog.text
    for secret in (PROOF, "SECRETPAYLOAD", "SIGSECRET", TOKEN):
        assert secret not in text


def _http_error(code):
    return urllib.error.HTTPError("http://backend", code, "x", {}, io.BytesIO(b"{}"))


@pytest.mark.parametrize("code", [404, 409])
def test_final_refusal_stops_asking_and_removes_login(net, posts, monkeypatch, code):  # noqa: F811
    calls, _ = posts
    assert network.ensure_started(background=False)
    login = network.NET_HOME / "login.json"
    assert login.exists()

    def refuse(req, timeout=None):
        calls.append(req)
        raise _http_error(code)

    monkeypatch.setattr(network.urllib.request, "urlopen", refuse)
    before = len(net.calls("login.changed"))
    assert network.renew_proof_if_due(now=2_000_000_000) is False
    assert not login.exists()
    assert len(net.calls("login.changed")) == before + 1
    n = len(calls)
    assert network.renew_proof_if_due(now=2_000_000_000) is False
    assert len(calls) == n


def test_a_500_still_retries(net, posts, monkeypatch):  # noqa: F811
    calls, _ = posts
    assert network.ensure_started(background=False)

    def boom(req, timeout=None):
        calls.append(req)
        raise _http_error(500)

    monkeypatch.setattr(network.urllib.request, "urlopen", boom)
    n = len(calls)
    assert network.renew_proof_if_due(now=2_000_000_000) is False
    assert network.renew_proof_if_due(now=2_000_000_000) is False
    assert len(calls) == n + 2
    assert (network.NET_HOME / "login.json").exists()
