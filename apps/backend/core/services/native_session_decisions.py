"""The owner's own Mac answers from the Claude terminal or a notification (spec
2026-10-04-session-sharing-mod §4.3): a join or pick-up request, or the session's sharing switch.
Session sharing stays on the backend; agents' messages never wait on a decision."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field, model_validator

from core.auth import AuthContext
from core.repositories import local_sessions_repo as sessions_repo
from core.services import local_sessions
from core.services.machine_auth import MachineIdentity
from core.services.messaging import ApiProblem, ContractModel


class NativeDecisionRequest(ContractModel):
    session_id: uuid.UUID
    generation: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "decline", "pause", "unpause"]
    user_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def names_one_request(self):
        answers = self.decision in ("allow", "decline")
        if answers != (self.user_id is not None) or answers != (self.request_id is not None):
            raise ValueError("allow and decline name one request; pause and unpause name none")
        return self


async def decide(device: MachineIdentity, body: NativeDecisionRequest) -> local_sessions.LocalSession:
    """The one write a device token reaches (owner approval 2026-10-04): only for the owner's own
    session on this Mac, at its current generation, while this Mac holds its relay connection."""
    sid = str(body.session_id)
    row = await sessions_repo.get_session(device.owner_id, sid)
    if (
        not row
        or row.get("humanUserId") != device.user_id
        or row.get("deviceId") != device.device_id
        or row.get("generation") != body.generation
    ):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Not this Mac's current session.")
    if row.get("endedAt") is not None:
        # The sharing switch still reaches an ended run (a stop the Mac retries, or a paused run
        # shared again before its conversation resumes), and nothing else does.
        if body.decision not in ("pause", "unpause"):
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Not this Mac's current session.")
    else:
        from core.services.local_session_relay import get_local_session_relay

        connection, _ = await get_local_session_relay().connect(row.get("connectionId") or "")
        if connection.get("device_id") != device.device_id or connection.get("user_id") != device.user_id:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "This Mac is not connected.")
    auth = AuthContext(user_id=device.user_id, org_id=device.org_id or None)
    if body.decision in ("pause", "unpause"):
        session = await local_sessions.set_sharing(auth, sid, body.decision == "unpause", generation=body.generation)
        from core.services.local_session_relay import publish_resources_changed

        # Same notice the web sharing route sends, so other clients' session views refresh.
        await publish_resources_changed(device.owner_id, device.user_id, session_id=sid)
        return session
    # Join and pick-up requests share one presence list; the request id says which one this answers.
    if local_sessions.pending_pickups(row).get(body.user_id) == body.request_id:
        return await local_sessions.decide_pickup_request(
            auth, sid, body.user_id, body.decision, request_id=body.request_id
        )
    return await local_sessions.decide_join(auth, sid, body.user_id, body.decision, request_id=body.request_id)
