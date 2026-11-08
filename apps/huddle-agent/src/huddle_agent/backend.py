"""The worker's client for the backend's /internal/huddles routes (apps/backend/routers/huddles.py)."""

from __future__ import annotations

import os

import httpx

ROUTE_TIMEOUT_S = 10  # the voice routes' own timeout; the client keeps its long one


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

    async def meet(self, op: str, body: dict, seat: str = "meet") -> dict:
        """The meeting agent's routes (apps/backend/routers/voice_meet.py), or owner mode's (voice_owner.py)."""
        # an integration read may first fetch a cold Google grant (backend TOKEN_S = 20 s)
        timeout = 25 if op == "integration-read" else ROUTE_TIMEOUT_S
        return await self._post(f"/internal/voice/{seat}/{self._room_id}/{op}", body, timeout=timeout)

    async def owner(self, op: str, body: dict) -> dict:
        return await self.meet(op, body, seat="owner")

    async def aclose(self) -> None:
        await self._http.aclose()
