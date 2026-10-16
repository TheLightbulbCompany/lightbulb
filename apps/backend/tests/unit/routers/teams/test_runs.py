"""Tests for the Teams Runs BFF."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        session_cookie="cookie",
    )


@pytest.fixture
def client(teams_ctx):
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_get_run_detail(client, monkeypatch):
    """`GET /teams/heartbeat-runs/{id}` returns upstream's heartbeat-run detail."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(
        return_value={
            "id": "run_1",
            "companyId": "co_abc",
            "agentId": "agt_1",
            "status": "failed",
            "startedAt": "2026-05-04T01:00:00Z",
            "stderrExcerpt": "boom",
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "run_1"
    assert body["status"] == "failed"
    admin.get_heartbeat_run.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
    )


def test_get_run_log_defaults(client, monkeypatch):
    """`GET /teams/heartbeat-runs/{id}/log` proxies the upstream log slice
    with the default 64 KiB tail when no params are supplied."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    admin.get_run_log = AsyncMock(
        return_value={
            "runId": "run_1",
            "store": "local_file",
            "logRef": "ref",
            "content": '{"ts":"t","stream":"stdout","chunk":"Executing command"}\n',
            "nextOffset": 53,
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/log",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content"].startswith("{")
    assert body["nextOffset"] == 53
    admin.get_run_log.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        offset=0,
        limit_bytes=65_536,
    )


def test_get_run_log_passes_offset_and_limit(client, monkeypatch):
    """The route forwards `offset` and the `limitBytes` alias verbatim."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    admin.get_run_log = AsyncMock(return_value={"runId": "run_1", "content": ""})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/log?offset=128&limitBytes=4096",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_run_log.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        offset=128,
        limit_bytes=4096,
    )


def test_get_run_log_rejects_oversized_limit(client, monkeypatch):
    """`limitBytes` above the route ceiling is rejected by validation."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    admin.get_run_log = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/log?limitBytes=9999999",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.get_run_log.assert_not_awaited()


def test_get_run_events_defaults(client, monkeypatch):
    """`GET /teams/heartbeat-runs/{id}/events` wraps upstream's raw event
    array in the standard list envelope, with afterSeq=0 / limit=200."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    admin.get_run_events = AsyncMock(
        return_value=[
            {
                "id": 1,
                "runId": "run_1",
                "seq": 1,
                "eventType": "lifecycle",
                "level": "info",
                "message": "run started",
            }
        ]
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/events",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["events"][0]["eventType"] == "lifecycle"
    admin.get_run_events.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        after_seq=0,
        limit=200,
    )


def test_get_run_events_passes_after_seq_and_limit(client, monkeypatch):
    """The route forwards `afterSeq` and `limit` and clamps limit to 1000."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    admin.get_run_events = AsyncMock(return_value=[])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/events?afterSeq=17&limit=50",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    admin.get_run_events.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        after_seq=17,
        limit=50,
    )

    r = client.get(
        "/api/v1/teams/heartbeat-runs/run_1/events?limit=99999",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


def test_cancel_run(client, monkeypatch):
    """`POST /teams/heartbeat-runs/{id}/cancel` proxies upstream cancel and
    returns the cancelled run row verbatim. A queued run has no turn: the runtime is not called."""
    import routers.messaging as messaging

    monkeypatch.setattr(messaging, "stop_paperclip_hermes_turn", AsyncMock(side_effect=AssertionError("woke runtime")))
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    admin.cancel_heartbeat_run = AsyncMock(return_value={"id": "run_1", "status": "cancelled"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/heartbeat-runs/run_1/cancel",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    admin.cancel_heartbeat_run.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
    )


def test_cancel_run_also_stops_the_hermes_turn(client, monkeypatch):
    """Paperclip's cancel never reaches the hermes_gateway adapter, so the BFF stops the turn on
    the runtime itself, with the identity the adapter's issue session key names."""
    import routers.messaging as messaging
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(
        return_value={
            "id": "run_1",
            "status": "running",
            "companyId": "co_abc",
            "agentId": "agt_1",
            "contextSnapshot": {"issueId": "iss_1"},
        }
    )
    admin.cancel_heartbeat_run = AsyncMock(return_value={"id": "run_1", "status": "cancelled"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(messaging.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "g"}))
    monkeypatch.setattr(messaging.settings, "PAPERCLIP_INTERNAL_URL", "http://pc:3100/")
    runtime = AsyncMock(side_effect=RuntimeError("parked"))  # a failure never fails the cancel
    monkeypatch.setattr(messaging, "_paperclip_hermes_runtime_response", runtime)

    r = client.post("/api/v1/teams/heartbeat-runs/run_1/cancel", headers={"Authorization": "Bearer x"})

    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    (container, path), kwargs = runtime.await_args
    assert path == "/internal/paperclip/v1/runs/run_1/stop" and kwargs["method"] == "POST"
    assert kwargs["json_body"]["identity"] == {
        "runId": "run_1",
        "companyId": "co_abc",
        "agentId": "agt_1",
        "sessionId": "paperclip:company:co_abc:agent:agt_1:issue:iss_1",
        "paperclipApiUrl": "http://pc:3100",
        "issueId": "iss_1",
    }


def test_record_watchdog_decision(client, monkeypatch):
    """Forwards the camelCase decision body verbatim; returns the row."""
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    admin.record_watchdog_decision = AsyncMock(return_value={"id": "wd_1", "decision": "snooze"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.post(
        "/api/v1/teams/heartbeat-runs/run_1/watchdog-decisions",
        json={
            "decision": "snooze",
            "snoozedUntil": "2027-01-01T00:00:00Z",
            "reason": "long research run",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "snooze"
    admin.record_watchdog_decision.assert_awaited_once_with(
        run_id="run_1",
        session_cookie="cookie",
        body={
            "decision": "snooze",
            "snoozedUntil": "2027-01-01T00:00:00Z",
            "reason": "long research run",
        },
    )


def test_watchdog_decision_rejects_bad_decision(client, monkeypatch):
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "companyId": "co_abc"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.post(
        "/api/v1/teams/heartbeat-runs/run_1/watchdog-decisions",
        json={"decision": "explode"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422


# --- agent runs list (ported stock agent Dashboard / Runs tabs) --------------

AGT = "22222222-2222-2222-2222-222222222222"
H = {"Authorization": "Bearer x"}


@pytest.fixture
def admin(client, monkeypatch):
    from routers.teams import agents as agents_mod

    fake = MagicMock()
    fake.get_agent = AsyncMock(return_value={"id": AGT, "companyId": "co_abc", "name": "CEO"})
    fake.list_company_heartbeat_runs = AsyncMock(return_value=[{"id": "run_1", "status": "succeeded"}, "junk"])
    monkeypatch.setattr(agents_mod, "_admin", lambda: fake)
    return fake


def test_agent_runs_forwards_own_company_and_resolved_agent_id(client, admin):
    # A url-key resolves to the agent row; the upstream filter carries its uuid.
    r = client.get("/api/v1/teams/agents/ceo/runs", headers=H)
    assert r.status_code == 200
    assert r.json() == {"runs": [{"id": "run_1", "status": "succeeded"}]}
    admin.get_agent.assert_awaited_once_with(agent_id="ceo", session_cookie="cookie")
    admin.list_company_heartbeat_runs.assert_awaited_once_with(
        session_cookie="cookie", company_id="co_abc", agent_id=AGT, limit=200
    )


def test_agent_runs_forwards_limit(client, admin):
    assert client.get(f"/api/v1/teams/agents/{AGT}/runs?limit=5", headers=H).status_code == 200
    assert admin.list_company_heartbeat_runs.await_args.kwargs["limit"] == 5


@pytest.mark.parametrize("agent", [{"id": AGT, "companyId": "co_OTHER"}, {"id": AGT}, None, []])
def test_agent_runs_404_for_another_companys_agent(client, admin, agent):
    admin.get_agent.return_value = agent
    assert client.get(f"/api/v1/teams/agents/{AGT}/runs", headers=H).status_code == 404
    admin.list_company_heartbeat_runs.assert_not_awaited()


@pytest.mark.parametrize("url", ["/api/v1/teams/agents/a.b/runs", f"/api/v1/teams/agents/{AGT}/runs?limit=0"])
def test_agent_runs_bad_input_is_422(client, admin, url):
    assert client.get(url, headers=H).status_code == 422
    admin.get_agent.assert_not_awaited()


def test_cancel_of_a_run_that_just_ended_does_not_wake_the_runtime(client, monkeypatch):
    import routers.messaging as messaging
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "run_1", "status": "running", "companyId": "co_abc"})
    admin.cancel_heartbeat_run = AsyncMock(return_value=None)  # finished between the read and the cancel
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(messaging, "stop_paperclip_hermes_turn", AsyncMock(side_effect=AssertionError("woke runtime")))

    assert (
        client.post("/api/v1/teams/heartbeat-runs/run_1/cancel", headers={"Authorization": "Bearer x"}).status_code
        == 200
    )
