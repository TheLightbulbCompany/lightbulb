"""Live frames of the Mac browser tab an agent is driving, for the room reading its run.

When a room run acts on a tab in a person's paired Mac browser (``browser.proxy``), this task
(the one holding that Mac's node socket) screenshots THAT tab about once a second -- never the
screen, never any other tab -- while the run is still active and someone in the workspace is
watching frames (``human_frames_at``), and publishes each one to the run's room as
``room.browser.frame``: the same event the cloud browser's live frames use. View-only: nothing
comes back from viewers. It stops IDLE_STOP_S after the run's last browser action, when the run
ends, or when the Mac, its pairing or its Browser consent goes (``store.authorize`` and the
Mac's own permission check run on every frame, exactly as for the agent's own action).

ponytail: polled viewport screenshots (the node host's /screenshot has no size option and no
screencast), downscaled here; a screencast in the node host if ~1 fps is not enough.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
import uuid

from core.repositories import container_repo

logger = logging.getLogger(__name__)

INTERVAL_S = 1.0
IDLE_STOP_S = 60.0
#: A frame watcher's heartbeat (~20 s) counts this long.
WATCH_S = 45.0
MAX_SIDE = 480
QUALITY = 50
#: AppSync silently drops an event over ~240 KB.
MAX_FRAME_B64 = 150_000
SHOT_TIMEOUT_MS = 5000

_casts: dict[str, dict] = {}  # run id -> {"row", "target", "tab", "profile", "at", "seq"}
_tasks: set[asyncio.Task] = set()


def track(store, row, target, tab, profile, driver) -> None:
    """A room run just acted on ``tab``: cast that tab (only it) for the run's room."""
    auth = row["auth"]
    if auth.get("family") != "room" or not auth.get("roomId") or not isinstance(tab, str) or not tab:
        return
    if driver == "existing-session":  # its tab identity cannot be verified
        return
    run_id = auth["runId"]
    cast = _casts.get(run_id)
    fresh = {"row": row, "target": target, "tab": tab, "profile": profile, "at": time.monotonic()}
    if cast is not None:
        cast.update(fresh)
        return
    _casts[run_id] = {**fresh, "seq": 0}
    task = asyncio.create_task(_loop(store, run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _watched(owner: str) -> bool:
    at = await container_repo.get_browser_input_at(owner, "human_frames_at")
    return bool(at) and time.time() - float(at) < WATCH_S


def _shrink(jpeg: bytes) -> tuple[str, int, int] | None:
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=QUALITY)
        data = base64.b64encode(out.getvalue()).decode()
        return (data, image.width, image.height) if len(data) <= MAX_FRAME_B64 else None


async def _grab(store, cast) -> tuple[str, int, int] | None:
    from core.services.desktop_browser import proxy, unpack

    request = {
        "command": "browser.proxy",
        "params": proxy("screenshot", cast["profile"], cast["tab"], {}, SHOT_TIMEOUT_MS),
        "executionDeadlineMs": SHOT_TIMEOUT_MS,
    }
    response = await store.dispatch(cast["target"], request, f"{cast['row']['invokeId']}-frame-{uuid.uuid4().hex[:8]}")
    payload = unpack(response, cast["profile"])
    if payload["result"].get("targetId", cast["tab"]) != cast["tab"]:
        return None  # never a frame of a tab the run did not drive
    files = payload.get("files") or []
    if not files or not isinstance(files[0], dict) or not isinstance(files[0].get("base64"), str):
        return None
    return await asyncio.to_thread(_shrink, base64.b64decode(files[0]["base64"]))


async def _publish(cast, frame: tuple[str, int, int]) -> None:
    from core.services.live_events import get_live_event_publisher
    from core.services.messaging import make_live_event

    auth = cast["row"]["auth"]
    cast["seq"] += 1
    data, w, h = frame
    payload = {
        "roomId": auth["roomId"],
        "runId": auth["runId"],
        "agentId": auth["agentId"],
        "seq": cast["seq"],
        "jpegBase64": data,
        "w": w,
        "h": h,
    }
    event = make_live_event("room.browser.frame", payload, room_id=auth["roomId"])
    await get_live_event_publisher().publish_room(auth["roomId"], event)


async def _loop(store, run_id: str) -> None:
    try:
        while (cast := _casts.get(run_id)) and time.monotonic() - cast["at"] < IDLE_STOP_S:
            started = time.monotonic()
            auth = cast["row"]["auth"]
            try:
                if await _watched(auth["ownerId"]):
                    await store.authorize(auth)  # raises once the run ends or loses its Mac
                    frame = await _grab(store, cast)
                    if frame:
                        await _publish(cast, frame)
            except Exception as exc:
                from core.services.desktop_execution import DesktopError

                if isinstance(exc, DesktopError) and exc.code in {"run_not_active", "run_forbidden"}:
                    return
                logger.info("desktop frame skipped: %s", type(exc).__name__)  # class only
            await asyncio.sleep(max(0.0, INTERVAL_S - (time.monotonic() - started)))
    finally:
        _casts.pop(run_id, None)
