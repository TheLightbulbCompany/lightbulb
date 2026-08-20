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

    async def send(self, data):
        self.sent.append(json.loads(data))
        method = self.sent[-1].get("method")
        if method == "Page.screencastFrameAck":
            self.ack_seen.set()
        elif method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
            self.input_seen.set()

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
