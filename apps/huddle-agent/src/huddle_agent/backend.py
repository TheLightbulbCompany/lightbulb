"""The worker's client for the backend's /internal/huddles routes (apps/backend/routers/huddles.py)."""

from __future__ import annotations

import asyncio
import os

import httpx


class BackendError(Exception):
    def __init__(self, status: int, code: str, reason: str | None):
        super().__init__(f"{status} {code} {reason or ''}".strip())
        self.status, self.code, self.reason = status, code, reason


class Backend:
    """Every call carries the room's ownerId and roomToken (the dispatch binds the worker to its room)."""

    def __init__(self, room_id: str, owner_id: str, room_token: str, client: httpx.AsyncClient | None = None):
        self._room_id, self._owner_id, self._room_token = room_id, owner_id, room_token
        # Accept LIGHTBULB_API_URL with or without the /api/v1 suffix.
        base = os.environ["LIGHTBULB_API_URL"].rstrip("/").removesuffix("/api/v1") + "/api/v1"
        self._http = client or httpx.AsyncClient(
            base_url=base,
            headers={"X-Lightbulb-Huddle-Secret": os.environ["LIGHTBULB_HUDDLE_SECRET"]},
            timeout=100,  # a turn POST waits on a cold workspace's invoke (backend budget: 90 s)
        )

    async def _post(self, path: str, body: dict) -> dict:
        try:
            r = await self._http.post(path, json={**body, "ownerId": self._owner_id, "roomToken": self._room_token})
        except httpx.HTTPError as e:
            raise BackendError(0, "huddles_upstream", type(e).__name__) from e
        if r.is_success:
            return r.json()
        try:
            body = r.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        detail = body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):  # HTTPException {code, reason}
            raise BackendError(r.status_code, detail["code"], detail.get("reason"))
        if isinstance(body.get("code"), str):  # ApiProblem {code: "INVALID_REQUEST", message}
            raise BackendError(r.status_code, body["code"].lower(), None)
        fallback = {400: "invalid_request", 401: "unauthorized"}.get(r.status_code, f"http_{r.status_code}")
        raise BackendError(r.status_code, fallback, None)

    async def summon(self, speaker: str) -> dict:
        return await self._post(f"/internal/huddles/{self._room_id}/summon", {"speakerUserId": speaker})

    async def turn(self, call_id: str, speaker: str, seq: int, utterance: str, transcript: str) -> dict:
        body = {"speakerUserId": speaker, "seq": seq, "utterance": utterance, "transcript": transcript}
        return await self._post(f"/internal/huddles/calls/{call_id}/turns", body)

    async def end(self, call_id: str, usage_seconds: int) -> None:
        # An unsent /end leaves the call's hold or free claim open: retry transport failures (status 0) and
        # a gateway 502/503/504 during a backend deploy. The close is idempotent.
        for attempt in range(4):
            try:
                await self._post(f"/internal/huddles/calls/{call_id}/end", {"usageSeconds": usage_seconds})
                return
            except BackendError as e:
                if e.status == 404:  # already closed
                    return
                if e.status not in (0, 502, 503, 504) or attempt == 3:
                    raise
            await asyncio.sleep(2**attempt)

    async def presence(self, user_ids: list[str], version: int = 0) -> None:
        body = {"userIds": user_ids, "version": version}
        await self._post(f"/internal/huddles/{self._room_id}/presence", body)

    async def aclose(self) -> None:
        await self._http.aclose()
