"""The worker's client for the backend's /internal/huddles routes (apps/backend/routers/huddles.py)."""

from __future__ import annotations

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
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            raise BackendError(r.status_code, detail["code"], detail.get("reason"))
        raise BackendError(r.status_code, f"http_{r.status_code}", None)

    async def summon(self, speaker: str) -> dict:
        return await self._post(f"/internal/huddles/{self._room_id}/summon", {"speakerUserId": speaker})

    async def turn(self, call_id: str, speaker: str, seq: int, utterance: str, transcript: str) -> dict:
        body = {"speakerUserId": speaker, "seq": seq, "utterance": utterance, "transcript": transcript}
        return await self._post(f"/internal/huddles/calls/{call_id}/turns", body)

    async def end(self, call_id: str, usage_seconds: int) -> None:
        try:
            await self._post(f"/internal/huddles/calls/{call_id}/end", {"usageSeconds": usage_seconds})
        except BackendError as e:
            if e.status != 404:  # 404 = already closed
                raise

    async def presence(self, user_ids: list[str]) -> None:
        await self._post(f"/internal/huddles/{self._room_id}/presence", {"userIds": user_ids})

    async def aclose(self) -> None:
        await self._http.aclose()
