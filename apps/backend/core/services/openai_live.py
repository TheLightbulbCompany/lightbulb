"""GPT-Live session broker. The backend creates every session (the client sends its SDP offer
here; the key never leaves the backend) and ends every session through the sideband, whose
``session.closed`` carries the authoritative billed seconds."""

from __future__ import annotations

import asyncio
import json
import math
import uuid

import httpx
from websockets.asyncio.client import connect as _ws_connect

from core.config import settings

_BASE = "https://api.openai.com/v1/live/sessions"
_SIDEBAND = "wss://api.openai.com/v1/live/sessions/{session_id}/attach"
_CLOSE_WAIT_S = 15.0
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a MockTransport


class LiveUnavailable(Exception):
    """No platform key configured."""


class LiveUpstreamError(Exception):
    """OpenAI refused or failed the session create."""


def _key() -> str:
    if not settings.OPENAI_API_KEY:
        raise LiveUnavailable()
    return settings.OPENAI_API_KEY


async def create_session(*, sdp: str, instructions: str) -> tuple[str, str]:
    body = {
        "session": {
            "model": "gpt-live-1",
            "instructions": instructions,
            "audio": {"output": {"voice": "marin"}},
            "delegation": {"type": "client"},
        },
        "transport": {"type": "webrtc", "sdp": sdp},  # byte-exact: a stripped CRLF 400s
    }
    async with httpx.AsyncClient(timeout=20.0, transport=_transport) as client:
        resp = await client.post(_BASE, json=body, headers={"Authorization": f"Bearer {_key()}"})
    if resp.status_code != 201:
        raise LiveUpstreamError(f"live session create returned {resp.status_code}")
    data = resp.json()
    return data["session"]["id"], data["transport"]["sdp"]


def _connect(session_id: str):
    return _ws_connect(
        _SIDEBAND.format(session_id=session_id),
        additional_headers={"Authorization": f"Bearer {_key()}"},
        open_timeout=10,
    )


async def close_session(session_id: str) -> int | None:
    try:
        async with _connect(session_id) as ws:
            await ws.send(json.dumps({"type": "session.close", "event_id": f"evt_{uuid.uuid4().hex}"}))
            async with asyncio.timeout(_CLOSE_WAIT_S):
                while True:
                    event = json.loads(await ws.recv())
                    if event.get("type") == "session.closed":
                        return math.ceil(float((event.get("usage") or {}).get("seconds") or 0))
    except Exception:  # noqa: BLE001 -- attach refused, socket died or timed out: caller bills by fallback
        return None
