"""The loopback CDP multiplexer against a fake Chrome: one upstream, per-run tabs, private routing."""
from __future__ import annotations

import base64
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
        self.casting, self.inputs, self.burst, self.shown = [], [], 3, []
        self.stages, self.uploads = [], []  # stages None: a desktop that predates staging
        self.urls = {}  # targetId -> url, for targets created with one
        self.server = serve(self.handle, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}/automation"

    def handle(self, ws):
        self.connects += 1
        for raw in ws:
            m = json.loads(raw)
            method, p, sid = m["method"], m.get("params") or {}, m.get("sessionId")
            self.seen.append(method)
            if method == "Lightbulb.show":
                self.shown.append(p.get("targetId"))
            reply = {"id": m["id"], "result": {}}
            if method == "Target.createTarget":
                target = f"T{len(self.seen)}"  # unique even after a tab is removed
                self.targets.append(target)
                self.urls[target] = p.get("url") or "about:blank"
                reply["result"] = {"targetId": target}
            elif method == "Target.getTargets":
                reply["result"] = {"targetInfos": [{"targetId": t, "type": "page", "url": self.urls.get(t, "about:blank")} for t in self.targets]}
            elif method == "Target.attachToTarget":
                session = "S-" + p["targetId"]
                ws.send(json.dumps({"method": "Target.attachedToTarget",
                                    "params": {"sessionId": session, "targetInfo": {"targetId": p["targetId"]}}}))
                reply["result"] = {"sessionId": session}
            elif method == "Target.closeTarget":
                self.closed.append(p["targetId"])
            elif method == "Test.emit":  # the door's own events (Lightbulb.humanInput)
                ws.send(json.dumps(p))
            elif method == "Lightbulb.stageFile":  # the desktop's; an old one leaves it to Chrome
                if self.stages is None:
                    reply = {"id": m["id"], "error": {"code": -32601, "message": "'Lightbulb.stageFile' wasn't found"}}
                else:
                    path = p.get("path") or f"/Mac/uploads/{len(self.stages)}/{p['name']}"
                    self.stages.append((path, p["data"]))
                    reply["result"] = {"path": path}
            elif method == "DOM.setFileInputFiles":
                self.uploads.append(p["files"])
            elif method == "Test.close":  # the door closing the pipe with a code
                ws.close(p["code"], p["reason"])
                return
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
    cdp_mux.S.visited.clear()
    cdp_mux.S.quiet.clear()
    cdp_mux.S.runs.clear()
    cdp_mux.S.want.clear()
    cdp_mux.S.generation = None


class Client:
    def __init__(self, run_id, upstream=cdp_mux.CLOUD):
        self.ws = connect(cdp_mux.url_for(run_id, upstream), open_timeout=5)
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


def test_close_run_leaves_a_tab_waiting_on_a_sign_in(chrome):
    """Google's "Verify it's you" waits on the person's phone after the turn ends (prod 2026-10-26)."""
    a = Client("run-a")
    a.call("Target.createTarget", {"url": "https://accounts.google.com/v3/signin/challenge/dp?TL=x"})
    a.call("Target.createTarget", {"url": "https://search.google.com/search-console/about"})
    challenge, console = (t for t, u in chrome.urls.items() if u.startswith("https://"))
    cdp_mux.close_run("run-a")
    assert _wait(lambda: console in chrome.closed)
    assert challenge not in chrome.closed


def test_signing_in_url():
    assert cdp_mux.signing_in_url("https://accounts.google.com/v3/signin/accountchooser?continue=x")
    assert cdp_mux.signing_in_url("https://login.microsoftonline.com/common/oauth2/v2.0/authorize")
    assert cdp_mux.signing_in_url("https://github.com/sessions/two-factor/app")
    assert not cdp_mux.signing_in_url("https://example.com/authors?next=/login")  # never the query
    assert not cdp_mux.signing_in_url("about:blank")


def test_a_new_generation_on_a_new_session_drops_every_client(chrome):
    a = Client("run-a")
    a.call("Target.getTargets")
    cdp_mux.set_upstream(chrome.url.replace("/automation", "/s2/automation"), "g2")  # the browser restarted
    with pytest.raises(ConnectionClosed):
        a.call("Target.getTargets")
    assert Client("run-a").call("Target.getTargets")[0]["result"]  # reconnects on the next client
    assert chrome.connects == 2


def test_a_new_generation_on_the_same_cloud_session_keeps_its_clients(chrome):
    a = Client("run-a")
    a.call("Target.getTargets")
    cdp_mux.set_upstream(chrome.url + "?X-Amz-Signature=new", "g2")  # re-signed, re-claimed lease
    assert a.call("Target.getTargets")[0]["result"]
    assert chrome.connects == 1 and cdp_mux.S.generation == "g2"


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
    # The ack is fire-and-forget on the loop (asyncio.ensure_future), so it lands after the frame does.
    assert _wait(lambda: len([m for _, m, _ in chrome.inputs if m == "Page.screencastFrameAck"]) == 3)
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


def test_a_ttl_0_stops_a_live_cast_without_one_more_frame_and_starts_none(chrome):
    frames = []
    a = Client("run-a")
    a.call("Target.getTargets")
    cdp_mux.cast("run-a", frames.append, ttl=5)
    assert _wait(lambda: len(frames) == 2)
    cdp_mux.cast("run-a", frames.append, ttl=0, resend=True)
    assert _wait(lambda: not cdp_mux._run(_casting("run-a")))
    assert len(frames) == 2, "the stop's resend publishes nothing"
    cdp_mux.cast("run-a", frames.append, ttl=0)
    assert not _wait(lambda: cdp_mux._run(_casting("run-a")), timeout=0.3)
    assert len(chrome.casting) == 1, "a stop never starts a cast"


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


# ---- named (Mac) upstreams: a second _State alongside CLOUD, on the same loopback server --------

NAMED = "mac:device-1:run-1"


def _clear_named():
    for name in [k for k in cdp_mux._UPS if k != cdp_mux.CLOUD]:
        cdp_mux._UPS.pop(name, None)
    for key in [k for k, v in cdp_mux._KEYS.items() if v != cdp_mux.CLOUD]:
        cdp_mux._KEYS.pop(key, None)


@pytest.fixture
def named_chrome():
    _clear_named()
    fake = FakeChrome()
    cdp_mux.set_upstream(fake.url, "m1", upstream=NAMED)
    yield fake
    cdp_mux.disconnect(NAMED)
    _clear_named()
    fake.server.shutdown()


def test_two_upstreams_route_independently(chrome, named_chrome):
    cloud, mac = Client("run-a"), Client("run-a", NAMED)  # same run id, different upstream
    cloud.call("Target.createTarget", {"url": "https://cloud.example"})
    mac.call("Target.createTarget", {"url": "https://mac.example"})
    assert chrome.connects == 1 and named_chrome.connects == 1  # one automation connection each
    # each chrome only ever heard its own client's calls: the run's own-tab create plus this one
    assert chrome.seen.count("Target.createTarget") == 2
    assert named_chrome.seen.count("Target.createTarget") == 2


def test_named_upstream_client_gets_its_own_tab(named_chrome):
    a, b = Client("run-a", NAMED), Client("run-b", NAMED)
    first_a = a.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    first_b = b.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    assert named_chrome.connects == 1  # one automation connection shared by both runs
    assert first_a != first_b and "person" not in (first_a, first_b)
    assert {first_a, first_b} == set(named_chrome.targets) - {"person"}


def test_close_run_on_one_upstream_leaves_the_other(chrome, named_chrome):
    cloud, mac = Client("run-a"), Client("run-a", NAMED)
    cloud.call("Target.getTargets")   # force both handshakes to finish registering before closing
    mac.call("Target.getTargets")
    cdp_mux.close_run("run-a", NAMED)
    assert cloud.call("Target.getTargets")[0]["result"]  # the cloud run is untouched
    with pytest.raises(ConnectionClosed):
        mac.call("Target.getTargets")


def test_cast_never_reaches_a_named_upstream(chrome, named_chrome):
    """cast() always targets CLOUD now: the desktop app casts a Mac's own live view itself."""
    cloud_frames = []
    Client("run-a").call("Target.getTargets")
    Client("run-a", NAMED).call("Target.getTargets")
    cdp_mux.cast("run-a", cloud_frames.append, ttl=5)
    assert _wait(lambda: len(cloud_frames) == 2)
    assert not named_chrome.casting  # the named (Mac) upstream is never screencast by the mux
    chrome.inputs.clear()
    named_chrome.inputs.clear()
    cdp_mux.dispatch("run-a", {"type": "click", "x": 5, "y": 5})
    assert _wait(lambda: any(m.startswith("Input.") for _, m, _ in chrome.inputs))
    assert not named_chrome.inputs  # a named upstream never sees viewer input


def test_drop_closes_named_clients_and_forgets_upstream(named_chrome):
    a = Client("run-a", NAMED)
    a.call("Target.getTargets")
    cdp_mux.drop(NAMED)
    with pytest.raises(ConnectionClosed):
        a.call("Target.getTargets")
    assert NAMED not in cdp_mux._UPS


def test_drop_refuses_cloud():
    with pytest.raises(ValueError):
        cdp_mux.drop(cdp_mux.CLOUD)


def test_mux_swallows_and_records_lightbulb_human_input(named_chrome):
    mac = Client("run-a", NAMED)
    before = time.time()
    _reply, events = mac.call("Test.emit", {"method": "Lightbulb.humanInput", "params": {"at": 1234.5}})
    assert events == []  # never forwarded to a client
    assert before <= cdp_mux.human_input_at(NAMED) <= time.time()  # our receive time, not the Mac's clock
    assert cdp_mux.human_input_at("mac:nobody:run") == 0.0


def test_named_upstream_records_the_doors_close_once(named_chrome):
    mac = Client("run-a", NAMED)
    mac.call("Target.getTargets")
    mac.ws.send(json.dumps({"id": 99, "method": "Test.close", "params": {"code": 4503, "reason": "CAPACITY"}}))
    assert _wait(lambda: cdp_mux._UPS[NAMED].closed is not None)
    assert cdp_mux.last_close(NAMED) == (4503, "CAPACITY")
    assert cdp_mux.last_close(NAMED) is None  # read once


def test_named_upstream_reconnect_reuses_the_runs_existing_tab(named_chrome):
    tab = Client("run-a", NAMED).call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    cdp_mux.set_upstream(named_chrome.url, "m2", upstream=NAMED)  # the Mac slept and reconnected
    again = Client("run-a", NAMED).call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    assert again == tab and named_chrome.connects == 2
    assert named_chrome.seen.count("Target.createTarget") == 1


def test_named_upstream_reconnect_creates_tab_when_gone(named_chrome):
    tab = Client("run-a", NAMED).call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    cdp_mux.set_upstream(named_chrome.url, "m2", upstream=NAMED)
    named_chrome.targets.remove(tab)  # Chrome closed it while the pipe was down
    again = Client("run-a", NAMED).call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    assert again != tab and named_chrome.seen.count("Target.createTarget") == 2


def test_show_sends_lightbulb_show_on_the_runs_first_tab(named_chrome):
    tab = Client("run-a", NAMED).call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    assert cdp_mux.show("run-a", NAMED) is True
    assert _wait(lambda: "Lightbulb.show" in named_chrome.seen)
    assert named_chrome.shown == [tab]
    assert cdp_mux.show("run-z", NAMED) is False  # no tab yet: nothing to raise


def test_origins_a_run_navigated_away_from_or_closed_stay_visited_until_a_save_forgets_them(chrome):
    a = Client("run-a")
    tab = a.call("Target.getTargets")[0]["result"]["targetInfos"][0]["targetId"]
    for url in ("https://first.test/login", "https://second.test/app", "chrome://newtab/"):
        a.call("Test.emit", {"method": "Page.frameNavigated", "sessionId": "S-" + tab,
                             "params": {"frame": {"id": tab, "url": url}}})  # navigate-away
    a.call("Test.emit", {"method": "Page.frameNavigated", "sessionId": "S-" + tab,
                         "params": {"frame": {"id": "child", "parentId": tab, "url": "https://ad.test/x"}}})
    a.call("Test.emit", {"method": "Target.targetInfoChanged",
                         "params": {"targetInfo": {"targetId": "T9", "type": "page", "url": "http://localhost:3000/"}}})
    cdp_mux.close_run("run-a")  # tab close
    assert _wait(lambda: list(cdp_mux.visited_origins()) == ["https://first.test", "https://second.test", "http://localhost:3000"])
    seen = cdp_mux.visited_origins()  # what a save reads
    a2 = Client("run-b")
    a2.call("Test.emit", {"method": "Target.targetInfoChanged",  # a page lands on second.test again mid-save
                          "params": {"targetInfo": {"targetId": "T8", "type": "page", "url": "https://second.test/x"}}})
    a2.call("Target.getTargets")
    cdp_mux.forget_origins(None, {o: seen[o] for o in ("https://first.test", "https://second.test")})
    assert list(cdp_mux.visited_origins()) == ["http://localhost:3000", "https://second.test"]  # the new landing stays


def test_a_saves_own_background_reads_are_never_visits(chrome):
    a = Client("run-a")
    bg = a.call("Target.createTarget", {"url": "about:blank", "background": True})[0]["result"]["targetId"]
    a.call("Test.emit", {"method": "Page.frameNavigated", "sessionId": "S-" + bg,
                         "params": {"frame": {"id": bg, "url": "https://read.test/"}}})
    a.call("Test.emit", {"method": "Target.targetInfoChanged",
                         "params": {"targetInfo": {"targetId": bg, "type": "page", "url": "https://read.test/"}}})
    a.call("Target.getTargets")
    assert cdp_mux.visited_origins() == {}


def test_visited_origins_are_capped_oldest_first(chrome, monkeypatch):
    monkeypatch.setattr(cdp_mux, "MAX_VISITED", 2)
    a = Client("run-a")
    for n in range(3):
        a.call("Test.emit", {"method": "Target.targetInfoChanged",
                             "params": {"targetInfo": {"targetId": "T", "type": "page", "url": f"https://s{n}.test/"}}})
    a.call("Target.getTargets")
    assert list(cdp_mux.visited_origins()) == ["https://s1.test", "https://s2.test"]

# ---- uploads: a runtime file is staged on the Mac before DOM.setFileInputFiles ---------------


def test_mac_upload_of_a_runtime_file_is_staged_in_chunks_and_rewritten(named_chrome, tmp_path, monkeypatch):
    monkeypatch.setattr(cdp_mux, "STAGE_CHUNK", 4)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    mac = Client("run-a", NAMED)
    reply, _ = mac.call("DOM.setFileInputFiles", {"files": [str(pdf), "/Users/me/on-the-mac.txt"], "nodeId": 5}, "S1")
    assert "error" not in reply and reply["sessionId"] == "S1"
    staged = named_chrome.stages[0][0]
    assert [(p, base64.b64decode(d)) for p, d in named_chrome.stages] == [(staged, b"%PDF"), (staged, b"-1.7")]
    assert staged.endswith("/report.pdf")
    assert named_chrome.uploads == [[staged, "/Users/me/on-the-mac.txt"]]  # a Mac path passes as is


def test_cloud_upload_is_never_staged(chrome, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF")
    Client("run-a").call("DOM.setFileInputFiles", {"files": [str(pdf)]})
    assert chrome.uploads == [[str(pdf)]] and chrome.stages == []


def test_a_desktop_without_staging_gets_the_command_unchanged(named_chrome, tmp_path):
    named_chrome.stages = None
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF")
    reply, _ = Client("run-a", NAMED).call("DOM.setFileInputFiles", {"files": [str(pdf)]})
    assert "error" not in reply and named_chrome.uploads == [[str(pdf)]]


def test_an_oversize_upload_is_refused_with_a_reason(named_chrome, tmp_path, monkeypatch):
    monkeypatch.setattr(cdp_mux, "MAX_STAGE_BYTES", 3)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF")
    reply, _ = Client("run-a", NAMED).call("DOM.setFileInputFiles", {"files": [str(pdf)]})
    assert "upload to the Mac failed" in reply["error"]["message"]
    assert named_chrome.stages == [] and named_chrome.uploads == []
