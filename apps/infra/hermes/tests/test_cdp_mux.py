"""The loopback CDP multiplexer against a fake Chrome: one upstream, per-run tabs, private routing."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from bridge import cdp_mux
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.sync.server import serve


class FakeChrome:
    """Just enough of AWS's automation stream: targets, flat attach, session events."""

    def __init__(self):
        self.connects, self.targets, self.closed, self.seen = 0, ["person"], [], []
        self.casting, self.inputs, self.burst = [], [], 3
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
            elif method == "Page.startScreencast" and sid:
                self.casting.append((sid, p))
                after = [{"method": "Page.screencastFrame", "sessionId": sid, "params": {  # a burst faster than
                    "data": f"frame{n}", "sessionId": n,                                  # the publish interval
                    "metadata": {"deviceWidth": 1280, "deviceHeight": 800}}} for n in range(self.burst)]
            elif method == "Target.detachFromTarget":
                ws.send(json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": p["sessionId"]}}))
            elif method.startswith("Input.") or method in ("Page.screencastFrameAck", "Runtime.evaluate"):
                self.inputs.append((sid, method, p))
            elif method == "Page.enable" and sid:
                ws.send(json.dumps({"method": "Page.javascriptDialogOpening", "sessionId": sid,
                                    "params": {"type": "confirm", "message": "sure?"}}))
            if sid:
                reply["sessionId"] = sid
            ws.send(json.dumps(reply))
            if method == "Page.startScreencast":
                time.sleep(0.05)  # the mux's frame loop starts once the reply lands
                for event in after:
                    ws.send(json.dumps(event))
                    time.sleep(0.02)


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
    cdp_mux.S.want.clear()
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


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_cast_starts_on_the_runs_tab_throttles_to_the_latest_and_stays_private(chrome):
    frames = []
    a, b = Client("run-a"), Client("run-b")
    tab = a.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    cdp_mux.cast("run-a", frames.append, ttl=5)
    assert _wait(lambda: frames)
    assert chrome.casting == [("S-" + tab, cdp_mux.CAST_PARAMS)]
    assert "Emulation.setFocusEmulationEnabled" in chrome.seen
    # Three frames in one burst: the first goes, the rest collapse into the latest one.
    assert _wait(lambda: len(frames) == 2)
    assert [f["jpegBase64"] for f in frames] == ["frame0", "frame2"]
    assert frames[0] == {"seq": 1, "jpegBase64": "frame0", "w": 1280, "h": 800}
    assert len([m for _, m, _ in chrome.inputs if m == "Page.screencastFrameAck"]) == 3
    a.quiet()
    b.quiet()  # neither the mux's attach nor its frames reached a run's client


def test_one_cast_per_run_and_a_resend_republishes_the_still_page(chrome):
    frames = []
    a = Client("run-a")
    a.call("Target.getTargets")
    cdp_mux.cast("run-a", frames.append, ttl=5)
    assert _wait(lambda: len(frames) == 2)
    cdp_mux.cast("run-a", frames.append, ttl=5)  # a repeat ask is not a second cast
    cdp_mux.cast("run-a", frames.append, ttl=5, resend=True)
    assert _wait(lambda: len(frames) == 3)
    assert frames[-1]["jpegBase64"] == "frame2" and frames[-1]["seq"] == 3
    assert len(chrome.casting) == 1


def test_cast_waits_for_the_runs_first_client(chrome):
    frames = []
    cdp_mux.url_for("run-a")
    cdp_mux.cast("run-a", frames.append, ttl=5)  # watched before agent-browser connected
    Client("run-a").call("Target.getTargets")
    assert _wait(lambda: frames)


def test_cast_stops_when_nobody_asks_again_and_when_the_run_ends(chrome):
    frames = []
    a, b = Client("run-a"), Client("run-b")
    a.call("Target.getTargets")
    b.call("Target.getTargets")
    cdp_mux.cast("run-a", frames.append, ttl=0.5)
    cdp_mux.cast("run-b", frames.append, ttl=5)
    assert _wait(lambda: not cdp_mux._run(_casting("run-a")) and cdp_mux._run(_casting("run-b")))
    cdp_mux.close_run("run-b")
    assert _wait(lambda: not cdp_mux._run(_casting("run-b")))
    assert chrome.seen.count("Target.detachFromTarget") == 2


async def _casting(run_id):
    return cdp_mux.key_for(run_id) in cdp_mux.S.casts


def test_at_most_max_casts(chrome):
    clients = [Client(f"run-{n}") for n in range(3)]
    for n, c in enumerate(clients):
        c.call("Target.getTargets")
        cdp_mux.cast(f"run-{n}", lambda f: None, ttl=5)
    assert _wait(lambda: len(chrome.casting) == cdp_mux.MAX_CASTS)
    assert len(cdp_mux._run(_all())) == cdp_mux.MAX_CASTS


async def _all():
    return list(cdp_mux.S.casts)


def test_oversize_frames_are_never_published(chrome, monkeypatch):
    monkeypatch.setattr(cdp_mux, "MAX_FRAME_B64", 3)
    frames = []
    Client("run-a").call("Target.getTargets")
    cdp_mux.cast("run-a", frames.append, ttl=1)
    assert _wait(lambda: chrome.casting)
    assert not _wait(lambda: frames, timeout=0.5)


def test_input_maps_to_cdp_on_the_cast_tab_clamped_to_the_viewport(chrome):
    a = Client("run-a")
    tab = a.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    with pytest.raises(LookupError):
        cdp_mux.dispatch("run-a", {"type": "click", "x": 1, "y": 1})
    frames = []
    cdp_mux.cast("run-a", frames.append, ttl=5)
    assert _wait(lambda: frames)
    chrome.inputs.clear()
    cdp_mux.dispatch("run-a", {"type": "click", "x": 5000, "y": 10, "button": "left"})
    cdp_mux.dispatch(None, {"type": "wheel", "x": 1, "y": 1, "deltaY": 240})
    cdp_mux.dispatch("run-a", {"type": "text", "text": "hello"})
    cdp_mux.dispatch("run-a", {"type": "key", "key": "Enter"})
    got = [(m, p.get("type"), p.get("x"), p.get("text")) for s, m, p in chrome.inputs if m != "Page.screencastFrameAck"]
    scroll = next(p["expression"] for _, m, p in chrome.inputs if m == "Runtime.evaluate")
    assert "elementFromPoint(1.000000, 1.000000)" in scroll and "const dy = 240.000000" in scroll
    assert all(s == "S-" + tab for s, m, _ in chrome.inputs)
    assert got == [
        ("Input.dispatchMouseEvent", "mouseMoved", 1280.0, None),
        ("Input.dispatchMouseEvent", "mousePressed", 1280.0, None),
        ("Input.dispatchMouseEvent", "mouseReleased", 1280.0, None),
        ("Runtime.evaluate", None, None, None),
        ("Input.insertText", None, None, "hello"),
        ("Input.dispatchKeyEvent", "keyDown", None, "\r"),
        ("Input.dispatchKeyEvent", "keyUp", None, None),
    ]
    with pytest.raises(ValueError):
        cdp_mux.dispatch("run-a", {"type": "key", "key": "F13"})
