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

    async def team(self) -> dict:
        return await self._post(f"/internal/huddles/{self._room_id}/team", {})

    async def route(
        self,
        speaker: str,
        utterance: str,
        transcript: str,
        humans: int | None = None,
        *,
        floor: dict | None = None,
        voice_runs: list[dict] = (),
        at: list[int] = (),
    ) -> dict:
        """{agentId, agentIds, hold?}: agentIds empty is nobody; `hold` is an unfinished line. `humans` (people
        in the call) lets Jev pick nobody when 2+ are talking; None is the wake word. `floor`/`voiceRuns` are
        Live's open requests; `at` is when each of the transcript's last lines was said (epoch s, oldest first)."""
        body = {
            "speakerUserId": speaker,
            "utterance": utterance,
            "transcript": transcript,
            "humans": humans,
            "floor": floor,
            "voiceRuns": list(voice_runs),
            "at": list(at),
        }
        return await self._post(f"/internal/huddles/{self._room_id}/route", body)

    async def summon(self, speaker: str) -> dict:
        return await self._post(f"/internal/huddles/{self._room_id}/summon", {"speakerUserId": speaker})

    async def session(self, agent_id: str) -> dict:
        return await self._post(f"/internal/huddles/{self._room_id}/agents/{agent_id}/session", {})

    async def turn(
        self,
        call_id: str,
        speaker: str,
        agent_id: str,
        run_id: str,
        utterance: str,
        transcript: str,
        steer_run_id: str | None = None,
        steer_only: bool = False,
    ) -> dict:
        """steer_only: steer `steer_run_id` or do nothing (never dispatch a new run)."""
        body = {
            "speakerUserId": speaker,
            "agentId": agent_id,
            "runId": run_id,
            "utterance": utterance,
            "transcript": transcript,
            "steerRunId": steer_run_id,
            **({"steerOnly": True} if steer_only else {}),
        }
        return await self._post(f"/internal/huddles/calls/{call_id}/turns", body)

    async def abort(self, run_id: str) -> dict:
        return await self._post(f"/internal/huddles/{self._room_id}/runs/{run_id}/abort", {})

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

    async def transcript(self, huddle_id: str, lines: list[dict]) -> None:
        await self._post(f"/internal/huddles/{self._room_id}/transcript", {"huddleId": huddle_id, "lines": lines})

    async def presence(self, user_ids: list[str], version: int = 0) -> None:
        body = {"userIds": user_ids, "version": version}
        await self._post(f"/internal/huddles/{self._room_id}/presence", body)

    async def aclose(self) -> None:
        await self._http.aclose()
