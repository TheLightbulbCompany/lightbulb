"""The first grant reader stays bound to one active room run and one route."""

import time
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import jwt
import pytest

from core.services import hook_grants
from core.services.messaging import RunEventResponse

OWNER = "user_a"
ROW = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
ROOM = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
BODY = {"runId": RUN, "type": "run.heartbeat", "at": "2026-09-12T03:15:00Z"}
KEY = "test-only-hook-grant-signing-key"


def grant(**changes):
    now = int(time.time())
    claims = {
        "kind": hook_grants.PURPOSE,
        "aud": hook_grants.AUDIENCE,
        "ownerId": OWNER,
        "rowId": ROW,
        "agentId": AGENT,
        "runId": RUN,
        "roomId": ROOM,
        "actions": ["run.event"],
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
        **changes,
    }
    return jwt.encode(claims, KEY, algorithm="HS256", headers={"kid": "1"})


def _state(*, status="started", deadline="future", aborted=False):
    run = {
        "ownerId": OWNER,
        "runId": RUN,
        "agentId": AGENT,
        "roomId": ROOM,
        "status": status,
    }
    if deadline == "future":
        run["deadlineAt"] = int(time.time()) + 60
    elif deadline == "expired":
        run["deadlineAt"] = int(time.time()) - 1
    elif deadline is not None:
        run["deadlineAt"] = deadline
    if aborted:
        run["abortRequestedAt"] = "2026-09-29T00:00:00Z"
    return (
        patch.dict("os.environ", {"HOOK_GRANT_SIGNING_KEY": KEY}),
        patch(
            "routers.messaging.container_repo.get_by_owner_id",
            AsyncMock(return_value={"id": ROW, "hook_token": "legacy"}),
        ),
        patch(
            "core.services.hook_grants.messaging_repo.get_run",
            AsyncMock(return_value=run),
        ),
        patch("core.services.hook_grants.messaging_repo.get_room", AsyncMock(return_value={"ownerId": OWNER})),
        patch("core.services.hook_grants.messaging_repo.get_member", AsyncMock(return_value={"id": AGENT})),
    )


def test_run_grant_accepts_only_run_event(client):
    with ExitStack() as stack:
        for context in _state():
            stack.enter_context(context)
        handle = stack.enter_context(
            patch(
                "routers.messaging.service.handle_run_event",
                AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied")),
            )
        )
        headers = {"Authorization": f"Bearer {grant()}"}
        accepted = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY, headers=headers)
        refused = client.post(f"/api/v1/internal/messaging/runtime/{OWNER}/recycle", headers=headers)
    assert accepted.status_code == 200
    assert refused.status_code == 401
    handle.assert_awaited_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"ownerId": "user_other"},
        {"rowId": "55555555-5555-4555-8555-555555555555"},
        {"agentId": "55555555-5555-4555-8555-555555555555"},
        {"roomId": "55555555-5555-4555-8555-555555555555"},
        {"runId": "55555555-5555-4555-8555-555555555555"},
        {"actions": ["run.event", "runtime.recycle"]},
        {"exp": 1},
    ],
)
def test_mismatched_grant_never_reaches_service(client, changes):
    with ExitStack() as stack:
        for context in _state():
            stack.enter_context(context)
        handle = stack.enter_context(patch("routers.messaging.service.handle_run_event", AsyncMock()))
        response = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json=BODY,
            headers={"Authorization": f"Bearer {grant(**changes)}"},
        )
    assert response.status_code in {401, 403}
    handle.assert_not_awaited()


def test_terminal_run_rejected(client):
    with ExitStack() as stack:
        for context in _state(status="ended"):
            stack.enter_context(context)
        handle = stack.enter_context(patch("routers.messaging.service.handle_run_event", AsyncMock()))
        response = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json=BODY,
            headers={"Authorization": f"Bearer {grant()}"},
        )
    assert response.status_code == 403
    handle.assert_not_awaited()


@pytest.mark.parametrize(
    "state",
    [{"deadline": None}, {"deadline": "expired"}, {"deadline": "futureish"}, {"aborted": True}],
)
def test_stopped_or_expired_run_rejected(client, state):
    with ExitStack() as stack:
        for context in _state(**state):
            stack.enter_context(context)
        handle = stack.enter_context(patch("routers.messaging.service.handle_run_event", AsyncMock()))
        response = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json=BODY,
            headers={"Authorization": f"Bearer {grant()}"},
        )
    assert response.status_code == 403
    handle.assert_not_awaited()


def test_a_feed_authors_run_reports_without_a_seat(client):
    """The feed's agents are authors, never seated: the grant asks messaging.room_agent, which
    stands a roster agent in for the feed (and answers None for anyone else)."""
    with ExitStack() as stack:
        for context in _state():
            stack.enter_context(context)
        stack.enter_context(patch("core.services.hook_grants.messaging_repo.get_member", AsyncMock(return_value=None)))
        stack.enter_context(patch("core.services.messaging.room_agent", AsyncMock(return_value={"id": AGENT})))
        handle = stack.enter_context(
            patch(
                "routers.messaging.service.handle_run_event",
                AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied")),
            )
        )
        response = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json=BODY,
            headers={"Authorization": f"Bearer {grant()}"},
        )
    assert response.status_code == 200
    handle.assert_awaited_once()
