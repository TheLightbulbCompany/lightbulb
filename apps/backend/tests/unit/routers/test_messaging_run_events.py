"""Hook-token gate + wiring for the run event callback (mirrors test_agent_mail.py)."""

from unittest.mock import AsyncMock, patch

from core.services.messaging import RunEventResponse

OWNER, TOKEN = "user_a", "hook-token-for-a"
RUN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
H = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"runId": RUN, "type": "run.heartbeat", "at": "2026-09-12T03:15:00Z"}
USAGE = {
    "model": "m",
    "inputTokens": 1,
    "outputTokens": 2,
    "cacheReadTokens": 3,
    "cacheWriteTokens": 4,
    "estimatedCostUsd": 0.01,
}


def _container():
    return patch(
        "routers.messaging.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}),
    )


def test_missing_bearer_is_401(client):
    assert client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY).status_code == 401


def test_wrong_token_is_401(client):
    with _container():
        r = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY, headers={"Authorization": "Bearer WRONG"}
        )
    assert r.status_code == 401


def test_valid_token_reaches_the_service(client):
    with (
        _container(),
        patch(
            "routers.messaging.service.handle_run_event",
            AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied")),
        ) as handle,
    ):
        r = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY, headers=H)
    assert r.status_code == 200 and r.json() == {"runId": RUN, "status": "applied"}
    assert handle.call_args.args[0] == OWNER and handle.call_args.args[1].type == "run.heartbeat"


def test_ended_requires_outcome_and_usage_for_a_started_run(client):
    with _container():
        no_outcome = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events", json={**BODY, "type": "run.ended"}, headers=H
        )
        no_usage = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json={**BODY, "type": "run.ended", "outcome": "completed"},
            headers=H,
        )
    # main.py's messaging_validation_error_handler rewrites every RequestValidationError
    # under /api/v1/internal/messaging into ApiProblem("INVALID_REQUEST") -> 400, the same
    # as every other messaging/runtime route; it does not leave FastAPI's default 422.
    assert no_outcome.status_code == 400 and no_usage.status_code == 400
    with (
        _container(),
        patch(
            "routers.messaging.service.handle_run_event",
            AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied")),
        ),
    ):
        ignored = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json={**BODY, "type": "run.ended", "outcome": "ignored"},
            headers=H,
        )
        completed = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json={**BODY, "type": "run.ended", "outcome": "completed", "usage": USAGE},
            headers=H,
        )
    assert ignored.status_code == 200 and completed.status_code == 200


def test_a_room_turn_heartbeat_keeps_its_session_live(client):
    """Room turns heartbeat here, not on the run route: each one slides the drain estimate too."""
    with (
        _container(),
        patch(
            "routers.messaging.service.handle_run_event",
            AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied")),
        ),
        patch("routers.messaging.runtime_transport.note_alive") as alive,
    ):
        client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY, headers=H)
        client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json={**BODY, "type": "run.ended", "outcome": "completed", "usage": USAGE},
            headers=H,
        )
    assert [c.args for c in alive.call_args_list] == [(OWNER,)]
