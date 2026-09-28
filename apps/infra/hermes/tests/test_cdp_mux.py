"""The loopback CDP multiplexer against a fake Chrome: one upstream, per-run tabs, private routing."""
from __future__ import annotations

import json
import socket
import threading

import pytest
from bridge import cdp_mux
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.sync.server import serve


class FakeChrome:
    """Just enough of AWS's automation stream: targets, flat attach, session events."""

    def __init__(self):
        self.connects, self.targets, self.closed, self.seen = 0, ["person"], [], []
        self.server = serve(self.handle, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}/automation"

    def handle(self, ws):
        self.connects += 1
        for raw in ws:
            m = json.loads(raw)
            method, p, sid = m["method"], m.get("params") or {}, m.get("sessionId")
            self.seen.append(method)
            reply = {"id": m["id"], "result": {}}
            if method == "Target.createTarget":
                target = f"T{len(self.targets)}"
                self.targets.append(target)
                reply["result"] = {"targetId": target}
            elif method == "Target.getTargets":
                reply["result"] = {"targetInfos": [{"targetId": t, "type": "page", "url": "about:blank"} for t in self.targets]}
            elif method == "Target.attachToTarget":
                session = "S-" + p["targetId"]
                ws.send(json.dumps({"method": "Target.attachedToTarget",
                                    "params": {"sessionId": session, "targetInfo": {"targetId": p["targetId"]}}}))
                reply["result"] = {"sessionId": session}
            elif method == "Target.closeTarget":
                self.closed.append(p["targetId"])
            elif method == "Page.enable" and sid:
                ws.send(json.dumps({"method": "Page.javascriptDialogOpening", "sessionId": sid,
                                    "params": {"type": "confirm", "message": "sure?"}}))
            if sid:
                reply["sessionId"] = sid
            ws.send(json.dumps(reply))


@pytest.fixture(scope="module", autouse=True)
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        cdp_mux.PORT = s.getsockname()[1]


@pytest.fixture
def chrome():
    fake = FakeChrome()
    cdp_mux.set_upstream(fake.url, "g1")
    yield fake
    cdp_mux.disconnect()
    cdp_mux._run(_clear())
    fake.server.shutdown()


async def _clear():
    cdp_mux.S.runs.clear()
    cdp_mux.S.generation = None


class Client:
    def __init__(self, run_id):
        self.ws = connect(cdp_mux.url_for(run_id), open_timeout=5)
        self.n = 0

    def call(self, method, params=None, session=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {},
                                 **({"sessionId": session} if session else {})}))
        events = []
        while True:
            m = json.loads(self.ws.recv(timeout=5))
            if m.get("id") == self.n:
                return m, events
            events.append(m)

    def quiet(self):
        with pytest.raises(TimeoutError):
            self.ws.recv(timeout=0.3)


def test_two_runs_share_one_upstream_and_each_sees_its_own_tab_first(chrome):
    a, b = Client("run-a"), Client("run-b")
    first_a = a.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    first_b = b.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    assert chrome.connects == 1
    assert first_a != first_b and "person" not in (first_a, first_b)
    assert {first_a, first_b} == set(chrome.targets) - {"person"}


def test_attach_and_session_events_reach_only_their_client(chrome):
    a, b = Client("run-a"), Client("run-b")
    tab = a.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    reply, events = a.call("Target.attachToTarget", {"targetId": tab, "flatten": True})
    session = reply["result"]["sessionId"]
    assert [e["method"] for e in events] == ["Target.attachedToTarget"]
    _, events = a.call("Page.enable", session=session)
    assert [e["method"] for e in events] == ["Page.javascriptDialogOpening"]
    b.quiet()  # neither the attach nor the dialog leaked to the other run


def test_hermes_dialog_bridge_is_answered_here_not_installed(chrome):
    a = Client("run-a")
    reply, _ = a.call("Fetch.enable", {"patterns": [{"urlPattern": "http://hermes-dialog-bridge.invalid/*"}]}, session="S-x")
    assert reply["result"] == {"identifier": "0"} and reply["sessionId"] == "S-x"
    assert "Fetch.enable" not in chrome.seen


def test_close_run_closes_its_tabs_and_clients_only(chrome):
    a, b = Client("run-a"), Client("run-b")
    a.call("Target.createTarget", {"url": "https://example.com"})
    mine = [t for t in chrome.targets if t != "person"]
    b_tab = b.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    cdp_mux.close_run("run-a")
    assert b.call("Target.getTargets")[0]["result"]  # run-b still works
    assert sorted(chrome.closed) == sorted(t for t in mine if t != b_tab)
    with pytest.raises(ConnectionClosed):
        a.call("Target.getTargets")


def test_a_new_generation_drops_every_client(chrome):
    a = Client("run-a")
    a.call("Target.getTargets")
    cdp_mux.set_upstream(chrome.url, "g2")
    with pytest.raises(ConnectionClosed):
        a.call("Target.getTargets")
    assert Client("run-a").call("Target.getTargets")[0]["result"]  # reconnects on the next client
    assert chrome.connects == 2


def test_an_unknown_run_is_refused(chrome):
    ws = connect(f"ws://127.0.0.1:{cdp_mux.PORT}{cdp_mux.PREFIX}nobody", open_timeout=5)
    with pytest.raises(ConnectionClosed):
        ws.recv(timeout=2)
    assert chrome.connects == 0
