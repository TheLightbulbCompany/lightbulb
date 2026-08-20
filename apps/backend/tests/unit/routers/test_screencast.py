"""Auth + reachability fence and the frame/ack/input relay on the CDP screencast.

Sibling of test_browser_view.py. The fence tests (bad/missing credential,
unreachable container) mirror the VNC relay exactly — the screencast rides the
same bv door and the same owner-scoping. The relay-loop tests pin what is NEW
and easy to get wrong: a screencastFrame must be forwarded to the client AND
acked back to CDP (no ack = Chrome never sends frame 2), and a client input
message must be routed to the right CDP Input.* method.
"""

import json
import threading
import time

import pytest


def _build_app():
    from fastapi import FastAPI

    from routers.screencast import router

    app = FastAPI()
    app.include_router(router)
    return app


def test_ws_rejects_missing_credential():
    """No subprotocol credential and no __session cookie → 4401 before accept()."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(_build_app())
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover
    assert exc_info.value.code == 4401


def test_ws_unreachable_container_closes_4503(monkeypatch):
    """Valid owner, container asleep/absent → 4503, the panel's 'asleep' code."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.screencast as mod

    async def _fake_decode(token: str):
        return {"sub": "user_test1", "email": "t@example.com"}

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            assert owner_id == "user_test1"
            return {"status": "running"}, None

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())

    client = TestClient(_build_app())
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", subprotocols=["isol8-screencast", "ok.jwt"]):
            pass  # pragma: no cover
    assert exc_info.value.code == 4503


def test_ws_no_cdp_target_closes_4503(monkeypatch):
    """Container reachable but CDP has no page target / no proxy (old image) → 4503.

    This is the image-pin gate in code: on a container without the CDP proxy,
    discovery returns None and the panel shows disconnected rather than hanging.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.screencast as mod

    async def _fake_decode(token: str):
        return {"sub": "user_test1"}

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            return {"status": "running"}, "10.0.0.42"

    async def _no_target(ip):
        return None

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod, "_discover_page_target", _no_target)

    client = TestClient(_build_app())
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", subprotocols=["isol8-screencast", "ok.jwt"]):
            pass  # pragma: no cover
    assert exc_info.value.code == 4503


class _FakeCdp:
    """A fake CDP WS connection: records sent commands, yields queued events."""

    subprotocol = None

    def __init__(self, frames):
        self._frames = list(frames)  # raw JSON strings to yield, then block
        self.sent = []
        self.ack_seen = threading.Event()
        self.input_seen = threading.Event()
        self.resize_seen = threading.Event()

    async def send(self, data):
        self.sent.append(json.loads(data))
        method = self.sent[-1].get("method")
        if method == "Page.screencastFrameAck":
            self.ack_seen.set()
        elif method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
            self.input_seen.set()
        elif method == "Emulation.setDeviceMetricsOverride":
            self.resize_seen.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        if self._frames:
            return self._frames.pop(0)
        # No more scripted events — block like a quiet CDP socket so the leg
        # stays alive until the relay tears it down.
        await asyncio.Event().wait()


def _wire_connected(monkeypatch, mod, fake_cdp):
    async def _fake_decode(token: str):
        if token == "the.jwt":
            return {"sub": "user_test1"}
        raise ValueError("not a credential")

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            return {"status": "running"}, "10.0.0.42"

    async def _target(ip):
        return f"ws://{ip}:18801/devtools/page/ABC"

    class _FakeConnect:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return fake_cdp

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod, "_discover_page_target", _target)
    monkeypatch.setattr(mod, "ws_connect", lambda *a, **k: _FakeConnect())


def test_screencast_frame_forwarded_and_acked(monkeypatch):
    """A CDP screencastFrame → {type:frame} to the client AND an ack to CDP."""
    from fastapi.testclient import TestClient

    import routers.screencast as mod

    frame = json.dumps(
        {
            "method": "Page.screencastFrame",
            "params": {
                "data": "BASE64JPEG",
                "sessionId": 7,
                "metadata": {"deviceWidth": 1280, "deviceHeight": 800},
            },
        }
    )
    fake_cdp = _FakeCdp(frames=[frame])
    _wire_connected(monkeypatch, mod, fake_cdp)

    client = TestClient(_build_app())
    with client.websocket_connect("/ws", subprotocols=["isol8-screencast", "the.jwt"]) as ws:
        assert ws.accepted_subprotocol == "isol8-screencast"
        msg = ws.receive_json()
        assert msg["type"] == "frame"
        assert msg["data"] == "BASE64JPEG"
        assert msg["metadata"]["deviceWidth"] == 1280
        # The ack must go back to CDP, echoing the frame's sessionId.
        assert fake_cdp.ack_seen.wait(timeout=5), "no screencastFrameAck sent to CDP"

    ack = next(c for c in fake_cdp.sent if c.get("method") == "Page.screencastFrameAck")
    assert ack["params"]["sessionId"] == 7
    # Screencast was started on connect.
    assert any(c.get("method") == "Page.startScreencast" for c in fake_cdp.sent)


def test_client_mouse_input_routed_to_cdp(monkeypatch):
    """A client {type:mouse,event} → Input.dispatchMouseEvent with those params."""
    from fastapi.testclient import TestClient

    import routers.screencast as mod

    fake_cdp = _FakeCdp(frames=[])
    _wire_connected(monkeypatch, mod, fake_cdp)

    client = TestClient(_build_app())
    with client.websocket_connect("/ws", subprotocols=["isol8-screencast", "the.jwt"]) as ws:
        ws.send_text(json.dumps({"type": "mouse", "event": {"type": "mousePressed", "x": 10, "y": 20}}))
        assert fake_cdp.input_seen.wait(timeout=5), "no Input.* dispatched to CDP"

    dispatched = next(c for c in fake_cdp.sent if c.get("method") == "Input.dispatchMouseEvent")
    assert dispatched["params"] == {"type": "mousePressed", "x": 10, "y": 20}


def test_client_unknown_input_type_dropped(monkeypatch):
    """An unknown message type is never blindly forwarded to CDP as a method."""
    from fastapi.testclient import TestClient

    import routers.screencast as mod

    fake_cdp = _FakeCdp(frames=[])
    _wire_connected(monkeypatch, mod, fake_cdp)

    client = TestClient(_build_app())
    with client.websocket_connect("/ws", subprotocols=["isol8-screencast", "the.jwt"]) as ws:
        ws.send_text(json.dumps({"type": "evil", "event": {"method": "Runtime.evaluate"}}))
        # Give the relay a beat, then a known input to prove ordering: only the
        # known one reaches CDP.
        ws.send_text(json.dumps({"type": "key", "event": {"type": "keyDown", "key": "a"}}))
        assert fake_cdp.input_seen.wait(timeout=5)

    methods = [c.get("method") for c in fake_cdp.sent]
    assert "Input.dispatchKeyEvent" in methods
    assert "Runtime.evaluate" not in methods


def test_client_resize_routed_to_device_metrics_override(monkeypatch):
    """A client {type:resize} → Emulation.setDeviceMetricsOverride with the
    panel dims, mobile:false, and deviceScaleFactor FORCED to 1 (frame cost is
    per-pixel on software GL — a client-supplied dsf must not quadruple it)."""
    from fastapi.testclient import TestClient

    import routers.screencast as mod

    fake_cdp = _FakeCdp(frames=[])
    _wire_connected(monkeypatch, mod, fake_cdp)

    client = TestClient(_build_app())
    with client.websocket_connect("/ws", subprotocols=["isol8-screencast", "the.jwt"]) as ws:
        ws.send_text(json.dumps({"type": "resize", "width": 1024, "height": 640, "deviceScaleFactor": 2}))
        assert fake_cdp.resize_seen.wait(timeout=5), "no setDeviceMetricsOverride sent to CDP"

    override = next(c for c in fake_cdp.sent if c.get("method") == "Emulation.setDeviceMetricsOverride")
    assert override["params"] == {"width": 1024, "height": 640, "deviceScaleFactor": 1, "mobile": False}


def test_resize_params_validated_and_capped():
    """Trust-boundary shaping: degenerate sizes drop; oversized panels scale
    down ASPECT-PRESERVING to the viewport ceiling (pixel count is the fps
    lever under SwiftShader — but the shape must survive, it's the whole fix)."""
    from routers.screencast import _MAX_VIEWPORT_HEIGHT, _MAX_VIEWPORT_WIDTH, _resize_override_params

    # In-range passes through unchanged.
    assert _resize_override_params({"width": 1280, "height": 800}) == {
        "width": 1280,
        "height": 800,
        "deviceScaleFactor": 1,
        "mobile": False,
    }

    # Oversized: scaled down keeping the aspect ratio (2000x1200 → 1600x960,
    # not a shape-changing clamp to 1600x1000).
    capped = _resize_override_params({"width": 2000, "height": 1200})
    assert capped["width"] == _MAX_VIEWPORT_WIDTH == 1600
    assert capped["height"] == 960

    # Height-bound panel caps on height.
    tall = _resize_override_params({"width": 800, "height": 2000})
    assert tall["height"] == _MAX_VIEWPORT_HEIGHT == 1000
    assert tall["width"] == 400

    # Degenerate / garbage → dropped, never forwarded to CDP.
    assert _resize_override_params({"width": 0, "height": 600}) is None
    assert _resize_override_params({"width": 800, "height": -1}) is None
    assert _resize_override_params({"width": "wide", "height": 600}) is None
    assert _resize_override_params({}) is None


def test_resize_burst_coalesces_to_latest():
    """Rapid resizes apply only the LATEST: a synchronous burst yields exactly
    one setDeviceMetricsOverride, carrying the final size (no CDP thrash)."""
    import asyncio

    from routers.screencast import _ResizeCoalescer

    applied = []

    async def apply(params):
        applied.append(params)

    async def run():
        coalescer = _ResizeCoalescer(apply)
        for width in (100, 200, 300, 400, 500):
            coalescer.submit({"width": width})
        # Let the drain task run its first (and only) iteration.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert applied == [{"width": 500}]


def test_resize_apply_failure_is_swallowed():
    """A failing CDP send must not raise out of the coalescer (never crash the
    session), and the drain task ends instead of retrying a dead socket."""
    import asyncio

    from routers.screencast import _ResizeCoalescer

    async def apply(params):
        raise ConnectionError("cdp gone")

    async def run():
        coalescer = _ResizeCoalescer(apply)
        coalescer.submit({"width": 100})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert coalescer._task.done()

    asyncio.run(run())


def test_ack_delay_paces_a_burst_but_not_a_slow_stream():
    """The ack pacer holds forwarded frames at ≤ SCREENCAST_MAX_FPS.

    CDP emits the next frame only after our ack, so spacing acks by one
    frame-interval caps delivery. This drives the pacing math with a fake clock
    (monotonic seconds passed in): a burst arriving faster than 30fps waits; a
    stream already slower than 30fps passes through with zero added latency.
    """
    from routers.screencast import SCREENCAST_MAX_FPS, _MIN_FRAME_INTERVAL, _ack_delay

    assert SCREENCAST_MAX_FPS == 30

    # First frame of the stream: nothing to pace against, no wait.
    assert _ack_delay(None, 100.0) == 0.0

    # Burst: frames arriving faster than the interval must wait out the balance.
    assert _ack_delay(100.0, 100.0) == pytest.approx(_MIN_FRAME_INTERVAL)  # 0s gap
    assert _ack_delay(100.0, 100.0 + _MIN_FRAME_INTERVAL / 2) == pytest.approx(_MIN_FRAME_INTERVAL / 2)

    # Slow stream: last ack already ≥ one interval old → acked at once, no delay.
    # (At exactly one interval, float subtraction leaves fs-scale noise — approx.)
    assert _ack_delay(100.0, 100.0 + _MIN_FRAME_INTERVAL) == pytest.approx(0.0, abs=1e-9)
    assert _ack_delay(100.0, 100.0 + 1.0) == 0.0


def test_frame_burst_is_paced_through_the_relay(monkeypatch):
    """A burst of frames offered all at once is throttled to ≤30fps by the relay.

    Proves the pacer is actually wired into the cdp→client loop (not a dead
    helper): the fake CDP hands over every frame immediately, yet the acks for
    the burst span ≥ (n-1) frame-intervals of wall time.
    """
    from fastapi.testclient import TestClient

    import routers.screencast as mod
    from routers.screencast import _MIN_FRAME_INTERVAL

    n = 4
    frames = [
        json.dumps(
            {
                "method": "Page.screencastFrame",
                "params": {"data": f"F{i}", "sessionId": i, "metadata": {}},
            }
        )
        for i in range(n)
    ]

    class _TimedCdp(_FakeCdp):
        def __init__(self, frames):
            super().__init__(frames)
            self.ack_times: list[float] = []
            self.all_acked = threading.Event()

        async def send(self, data):
            await super().send(data)
            if json.loads(data).get("method") == "Page.screencastFrameAck":
                self.ack_times.append(time.monotonic())
                if len(self.ack_times) >= n:
                    self.all_acked.set()

    fake_cdp = _TimedCdp(frames)
    _wire_connected(monkeypatch, mod, fake_cdp)

    client = TestClient(_build_app())
    with client.websocket_connect("/ws", subprotocols=["isol8-screencast", "the.jwt"]) as ws:
        for _ in range(n):
            ws.receive_json()  # drain forwarded frames so send_text never blocks
        assert fake_cdp.all_acked.wait(timeout=5), "not all frames were acked"

    span = fake_cdp.ack_times[-1] - fake_cdp.ack_times[0]
    # n acks paced at 30fps take ≥ (n-1) intervals; allow 10% scheduling slack.
    assert span >= (n - 1) * _MIN_FRAME_INTERVAL * 0.9
