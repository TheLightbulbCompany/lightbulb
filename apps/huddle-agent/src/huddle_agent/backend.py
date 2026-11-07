"""The worker's client for the backend's /internal/huddles routes (apps/backend/routers/huddles.py)."""

from __future__ import annotations

import asyncio
import os

import httpx

ROUTE_TIMEOUT_S = 10  # /route is one Jev call; /turns keeps the client's long timeout


class BackendError(Exception):
    def __init__(self, status: int, code: str, reason: str | None):
        super().__init__(f"{status} {code} {reason or ''}".strip())
        self.status, self.code, self.reason = status, code, reason


class Backend:
    """Every call carries the room's ownerId and roomToken (the dispatch binds the worker to its room)."""

    def __init__(
        self,
        room_id: str,
        owner_id: str,
        room_token: str,
        client: httpx.AsyncClient | None = None,
    ):
        self._room_id, self._owner_id, self._room_token = room_id, owner_id, room_token
        # Accept LIGHTBULB_API_URL with or without the /api/v1 suffix.
        base = (
            os.environ["LIGHTBULB_API_URL"].rstrip("/").removesuffix("/api/v1")
            + "/api/v1"
        )
        self._http = (
            client
            or httpx.AsyncClient(
                base_url=base,
                headers={
                    "X-Lightbulb-Huddle-Secret": os.environ["LIGHTBULB_HUDDLE_SECRET"]
                },
                timeout=100,  # a turn POST waits on a cold workspace's invoke (backend budget: 90 s)
            )
        )

    async def _post(self, path: str, body: dict, method: str = "POST", timeout: float | None = None) -> dict:
        """`timeout`: this request's own, in place of the client's (sized for /turns)."""
        try:
            r = await self._http.request(
                method,
                path,
                json={**body, "ownerId": self._owner_id, "roomToken": self._room_token},
                **({"timeout": timeout} if timeout else {}),
            )
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
        if isinstance(detail, dict) and isinstance(
            detail.get("code"), str
        ):  # HTTPException {code, reason}
            raise BackendError(r.status_code, detail["code"], detail.get("reason"))
        if isinstance(
            body.get("code"), str
        ):  # ApiProblem {code: "INVALID_REQUEST", message}
            raise BackendError(r.status_code, body["code"].lower(), None)
        fallback = {400: "invalid_request", 401: "unauthorized"}.get(
            r.status_code, f"http_{r.status_code}"
        )
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
        setting: str = "live",
        unclaimed: bool = False,
        open: list[dict] = (),
        engagements: list[dict] = (),
        turn: int | None = None,
    ) -> dict:
        """{agentId, agentIds, hold?}: agentIds empty is nobody; `hold` is an unfinished line. `humans` (people
        in the call) lets Jev pick nobody when 2+ are talking; None is the wake word. `floor`/`voiceRuns` are
        Live's open requests; `at` is when each of the transcript's last lines was said (epoch s, oldest first).
        `setting` is "live" or "meeting"; `unclaimed`: a meeting question nobody took within UNCLAIMED_QUESTION_S.
        `open`: the call's request rows (RequestList.for_route), each with its `asker` and how far its answer
        has been said (`delivery`); `floor` may carry `mine` (understanding §4.4). `engagements`: who the speaker
        is talking with (`Huddle._evidence`), the evidence a meeting line with no name is routed on. `turn`: the
        speaker's turn this line is (all of) so far: a turn said in pieces is routed again as it grows."""
        body = {
            "speakerUserId": speaker,
            "utterance": utterance,
            "transcript": transcript,
            "humans": humans,
            "floor": floor,
            "voiceRuns": list(voice_runs),
            "at": list(at),
            "setting": setting,
            "unclaimed": unclaimed,
            "open": list(open),
            "engagements": list(engagements),
            **({"turn": turn} if turn is not None else {}),
        }
        # understanding §4.6: a delegation may wait on this answer; a slow one is a failure like any other
        return await self._post(f"/internal/huddles/{self._room_id}/route", body, timeout=ROUTE_TIMEOUT_S)

    @staticmethod
    def _label(speaker_name: str | None) -> dict:
        return {"speakerName": speaker_name[:120]} if speaker_name else {}

    async def summon(self, speaker: str, speaker_name: str | None = None) -> dict:
        """speaker_name: a meeting line's Meet speaker label; the backend shows it, never trusts it."""
        return await self._post(
            f"/internal/huddles/{self._room_id}/summon",
            {"speakerUserId": speaker, **self._label(speaker_name)},
        )

    async def session(self, agent_id: str) -> dict:
        """The agent's voice and persona (the small-talk writer's)."""
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
        speaker_name: str | None = None,
        requests: list[str] | tuple = (),
    ) -> dict:
        """steer_only: steer `steer_run_id` or do nothing (never dispatch a new run)."""
        body = {
            "speakerUserId": speaker,
            **self._label(speaker_name),
            "agentId": agent_id,
            "runId": run_id,
            "utterance": utterance,
            "transcript": transcript,
            "steerRunId": steer_run_id,
            **({"steerOnly": True} if steer_only else {}),
            **({"requests": list(requests)} if requests else {}),
        }
        return await self._post(f"/internal/huddles/calls/{call_id}/turns", body)

    async def abort(self, run_id: str) -> dict:
        return await self._post(
            f"/internal/huddles/{self._room_id}/runs/{run_id}/abort", {}
        )

    async def no_post(self, run_id: str) -> dict:
        """"Don't post that": the backend never posts this run's answer late (either door)."""
        return await self._post(f"/internal/huddles/{self._room_id}/runs/{run_id}/no-post", {})

    async def end(self, call_id: str, usage_seconds: int) -> None:
        # An unsent /end leaves the call's hold or free claim open: retry transport failures (status 0) and
        # a gateway 502/503/504 during a backend deploy. The close is idempotent.
        for attempt in range(4):
            try:
                await self._post(
                    f"/internal/huddles/calls/{call_id}/end",
                    {"usageSeconds": usage_seconds},
                )
                return
            except BackendError as e:
                if e.status == 404:  # already closed
                    return
                if e.status not in (0, 502, 503, 504) or attempt == 3:
                    raise
            await asyncio.sleep(2**attempt)

    async def late_answer(
        self, agent_id: str, run_id: str, request: str, answer: str
    ) -> None:
        body = {
            "agentId": agent_id,
            "runId": run_id,
            "request": request,
            "answer": answer,
        }
        await self._post(f"/internal/huddles/{self._room_id}/late-answer", body)

    async def transcript(
        self, huddle_id: str, lines: list[dict], final: bool = False
    ) -> None:
        """final: a meeting's whole transcript, sent once as the worker leaves."""
        await self._post(
            f"/internal/huddles/{self._room_id}/transcript",
            {"huddleId": huddle_id, "lines": lines, "final": final},
        )

    async def leave_meeting(self) -> None:
        """The meeting bot leaves the Google Meet (Recall), then this worker leaves the room."""
        await self._post(f"/internal/meetings/{self._room_id}", {}, method="DELETE")

    async def presence(self, user_ids: list[str], version: int = 0) -> None:
        body = {"userIds": user_ids, "version": version}
        await self._post(f"/internal/huddles/{self._room_id}/presence", body)

    async def meet(self, op: str, body: dict) -> dict:
        """The meeting agent's routes (apps/backend/routers/voice_meet.py)."""
        # an integration read may first fetch a cold Google grant (backend TOKEN_S = 20 s)
        timeout = 25 if op == "integration-read" else ROUTE_TIMEOUT_S
        return await self._post(f"/internal/voice/meet/{self._room_id}/{op}", body, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()
