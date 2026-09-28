"""Live frames of the Mac tab a room run drives: only that tab, only while watched and active."""

import asyncio
import base64
import io
import time

import pytest
from PIL import Image

from core.services import desktop_frames as frames
from core.services.desktop_execution import DesktopError

TAB = "a" * 32


def _jpeg(w=2000, h=1250) -> str:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode()


class Store:
    def __init__(self, tab=TAB, active=2):
        self.requests, self.tab, self.active = [], tab, active

    async def authorize(self, auth):
        self.active -= 1
        if self.active < 0:
            raise DesktopError("run_not_active", 403)

    async def dispatch(self, target, request, invoke_id):
        self.requests.append((target, request, invoke_id))
        return {
            "ok": True,
            "payload": {
                "route": {"status": "resolved", "profile": "chrome", "driver": "extension"},
                "result": {"ok": True, "targetId": self.tab, "path": "/tmp/x.jpg"},
                "files": [{"path": "/tmp/x.jpg", "base64": _jpeg(), "mimeType": "image/jpeg"}],
            },
        }


def _row(family="room"):
    return {
        "invokeId": "inv",
        "auth": {"family": family, "roomId": "r-1", "runId": "run-1", "agentId": "ceo", "ownerId": "org_1"},
    }


@pytest.fixture
def world(monkeypatch):
    published, watched = [], {"at": time.time()}
    monkeypatch.setattr(frames, "INTERVAL_S", 0.01)
    monkeypatch.setattr(frames, "_casts", {})
    monkeypatch.setattr(frames, "_tasks", set())

    async def input_at(owner, field):
        assert field == "human_frames_at"
        return watched["at"]

    async def publish(cast, frame):
        published.append((cast["row"]["auth"]["roomId"], cast["tab"], frame))

    monkeypatch.setattr(frames.container_repo, "get_browser_input_at", input_at)
    monkeypatch.setattr(frames, "_publish", publish)
    return published, watched


async def _settle():
    await asyncio.gather(*frames._tasks)


@pytest.mark.asyncio
async def test_only_the_driven_tab_is_shot_small_until_the_run_ends(world):
    published, _ = world
    store = Store(active=2)
    frames.track(store, _row(), {"deviceId": "mac"}, TAB, "chrome", "extension")
    await _settle()
    assert len(published) == 2 and all(room == "r-1" and tab == TAB for room, tab, _ in published)
    data, w, h = published[0][2]
    assert max(w, h) == frames.MAX_SIDE and len(data) < frames.MAX_FRAME_B64
    _, request, invoke = store.requests[0]
    assert request["command"] == "browser.proxy"
    assert request["params"]["path"] == "/screenshot" and request["params"]["body"]["targetId"] == TAB
    assert request["params"]["body"]["fullPage"] is False  # the tab's viewport, never the screen
    assert invoke.startswith("inv-frame-")
    assert frames._casts == {}


@pytest.mark.asyncio
async def test_nothing_is_shot_while_nobody_watches(world, monkeypatch):
    published, watched = world
    watched["at"] = time.time() - frames.WATCH_S - 1
    monkeypatch.setattr(frames, "IDLE_STOP_S", 0.05)
    store = Store()
    frames.track(store, _row(), {}, TAB, "chrome", "extension")
    await _settle()
    assert published == [] and store.requests == []


@pytest.mark.asyncio
async def test_a_frame_of_another_tab_is_never_published(world):
    published, _ = world
    frames.track(Store(tab="b" * 32, active=1), _row(), {}, TAB, "chrome", "extension")
    await _settle()
    assert published == []


@pytest.mark.asyncio
async def test_paperclip_runs_and_unverifiable_tabs_are_not_cast(world):
    frames.track(Store(), _row("paperclip"), {}, TAB, "chrome", "extension")
    frames.track(Store(), _row(), {}, TAB, "chrome", "existing-session")
    assert frames._casts == {}


@pytest.mark.asyncio
async def test_one_loop_per_run_following_its_latest_tab(world):
    published, _ = world
    store = Store(active=1)
    frames.track(store, _row(), {}, "c" * 32, "chrome", "extension")
    frames.track(store, _row(), {}, TAB, "chrome", "extension")
    assert len(frames._tasks) == 1
    await _settle()
    assert [tab for _, tab, _ in published] == [TAB]
