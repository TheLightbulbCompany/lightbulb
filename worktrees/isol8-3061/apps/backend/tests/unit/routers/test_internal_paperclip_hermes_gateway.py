"""Authenticated facade for Paperclip's deployed native ``hermes_gateway`` adapter."""

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from core.config import settings
from core.services import runtime_transport

OWNER = "org-1"
TOKEN = "hook-token"
GATEWAY_TOKEN = "gateway-token"
RUN = "run-123"
COMPANY = "company-1"
AGENT = "agent-1"
ISSUE = "issue-1"
SESSION = f"paperclip:company:{COMPANY}:agent:{AGENT}:issue:{ISSUE}"
BASE = f"/api/v1/internal/paperclip/hermes/{OWNER}/v1/runs"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Idempotency-Key": RUN,
    "X-Hermes-Session-Key": SESSION,
}
BODY = {
    "input": "# Native task\nPreserve **all** markdown.\n",
    "instructions": "Exact native instructions.\n",
    "session_id": SESSION,
    "wake_payload": {"reason": "comment", "nested": [1, 2]},
}


@contextmanager
def _dependencies(*, company_id=COMPANY, agent_company=COMPANY, issue_company=COMPANY):
    container = {
        "owner_id": OWNER,
        "hook_token": TOKEN,
        "gateway_token": GATEWAY_TOKEN,
        "runtime_host": "agentcore",
    }
    repo = SimpleNamespace(
        resolve_owner_row=AsyncMock(
            return_value=SimpleNamespace(company_id=company_id, status="active") if company_id else None
        )
    )
    admin = SimpleNamespace(
        _http=object(),
        get_agent=AsyncMock(return_value={"id": AGENT, "companyId": agent_company}),
        get_issue=AsyncMock(return_value={"id": ISSUE, "companyId": issue_company}),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.internal:3100"))
        stack.enter_context(
            patch("routers.messaging.container_repo.get_by_owner_id", AsyncMock(return_value=container))
        )
        stack.enter_context(patch("routers.messaging.PaperclipRepo", return_value=repo))
        stack.enter_context(patch("routers.messaging.shared_admin_client", return_value=admin))
        stack.enter_context(patch("routers.messaging.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")))
        yield container, repo, admin


def _runtime_response(status, body):
    import json

    return runtime_transport.RuntimeResponse(status, json.dumps(body), {"content-type": "application/json"})


def test_create_requires_hook_token_and_native_identity_headers(client):
    assert client.post(BASE, json=BODY).status_code == 401
    with _dependencies():
        missing_idempotency = client.post(
            BASE,
            json=BODY,
            headers={k: v for k, v in HEADERS.items() if k != "Idempotency-Key"},
        )
        missing_session = client.post(
            BASE,
            json=BODY,
            headers={k: v for k, v in HEADERS.items() if k != "X-Hermes-Session-Key"},
        )
    assert missing_idempotency.status_code == 400
    assert missing_session.status_code == 400


def test_create_preserves_native_body_and_uses_validated_identity_for_routing(client, monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.internal:3100")
    captured = {}

    async def fake_request(container, path, **kwargs):
        captured.update(container=container, path=path, **kwargs)
        return _runtime_response(202, {"run_id": RUN, "status": "queued", "session_id": SESSION})

    with (
        _dependencies() as (_container, repo, admin),
        patch("routers.messaging.runtime_transport.request", fake_request),
    ):
        response = client.post(BASE, json=BODY, headers=HEADERS)

    assert response.status_code == 202
    assert response.json() == {"run_id": RUN, "status": "queued", "session_id": SESSION}
    assert captured["path"] == "/internal/paperclip/v1/runs"
    assert captured["method"] == "POST"
    assert captured["json_body"] == {
        "identity": {
            "runId": RUN,
            "companyId": COMPANY,
            "agentId": AGENT,
            "sessionId": SESSION,
            "issueId": ISSUE,
            "paperclipApiUrl": "http://paperclip.internal:3100",
        },
        "request": BODY,
    }
    assert captured["headers"] == {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
    assert captured["timeout_s"] == 25.0  # under the 30s public-gateway ceiling, see below
    repo.resolve_owner_row.assert_awaited_once_with(OWNER)
    admin.get_agent.assert_awaited_once_with(session_cookie="admin-cookie", agent_id=AGENT)
    admin.get_issue.assert_awaited_once_with(session_cookie="admin-cookie", issue_id=ISSUE)


def test_a_poll_rechecks_the_company_binding_without_a_paperclip_admin_round_trip(client):
    """The adapter polls this facade for the WHOLE run. The company binding is the security
    boundary and is re-read every time (one DynamoDB get), but agent/issue membership is a
    Paperclip admin call each -- doing those per poll would put 2-3 calls per second per live
    run on the shared BFF. Create already verified them, and the bridge only answers a poll
    whose full identity tuple matches the entry that verified create wrote."""

    async def fake_request(*_args, **_kwargs):
        return _runtime_response(200, {"run_id": RUN, "status": "running", "session_id": SESSION})

    with (
        _dependencies() as (_container, repo, admin),
        patch("routers.messaging.runtime_transport.request", fake_request),
    ):
        assert client.get(f"{BASE}/{RUN}", headers=HEADERS).status_code == 200
        assert client.post(f"{BASE}/{RUN}/stop", headers=HEADERS).status_code == 200

    assert repo.resolve_owner_row.await_count == 2  # the binding, on both
    admin.get_agent.assert_not_awaited()
    admin.get_issue.assert_not_awaited()


def test_a_poll_for_another_companys_session_is_refused(client):
    """A stolen/typo'd session key naming a company this owner does not own must never route,
    on the poll path as much as on create."""

    async def boom(*_args, **_kwargs):
        raise AssertionError("must not reach the runtime with an unverified identity")

    with _dependencies(company_id="company-other"), patch("routers.messaging.runtime_transport.request", boom):
        assert client.get(f"{BASE}/{RUN}", headers=HEADERS).status_code == 403
        assert client.post(f"{BASE}/{RUN}/stop", headers=HEADERS).status_code == 403


def test_create_rejects_body_session_or_owner_agent_issue_mismatches(client):
    with _dependencies():
        wrong_body = client.post(BASE, json={**BODY, "session_id": SESSION + "-other"}, headers=HEADERS)
    assert wrong_body.status_code == 400

    for kwargs in (
        {"company_id": "company-other"},
        {"agent_company": "company-other"},
        {"issue_company": "company-other"},
    ):
        with _dependencies(**kwargs):
            response = client.post(BASE, json=BODY, headers=HEADERS)
        assert response.status_code == 403


def test_run_scoped_session_must_match_the_idempotency_key(client):
    session = f"paperclip:company:{COMPANY}:agent:{AGENT}:run:different-run"
    with _dependencies():
        response = client.post(
            BASE,
            json={**BODY, "session_id": session},
            headers={**HEADERS, "X-Hermes-Session-Key": session},
        )
    assert response.status_code == 400


def test_status_returns_real_terminal_result_and_lost_registry_fails_terminally(client):
    terminal = {
        "run_id": RUN,
        "status": "completed",
        "session_id": SESSION,
        "output": "actual answer",
        "usage": {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 2},
    }
    calls = []

    async def fake_request(_container, path, **kwargs):
        calls.append((path, kwargs))
        return _runtime_response(200, terminal)

    with _dependencies(), patch("routers.messaging.runtime_transport.request", fake_request):
        response = client.get(f"{BASE}/{RUN}", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == terminal
    assert calls[0][0] == f"/internal/paperclip/v1/runs/{RUN}"
    assert calls[0][1]["method"] == "GET"

    async def missing(*_args, **_kwargs):
        return _runtime_response(404, {"error": "run_not_found"})

    with _dependencies(), patch("routers.messaging.runtime_transport.request", missing):
        lost = client.get(f"{BASE}/{RUN}", headers=HEADERS)
    assert lost.status_code == 200
    assert lost.json() == {
        "run_id": RUN,
        "status": "failed",
        "session_id": SESSION,
        "error": "runtime_lost",
    }


def test_an_unreachable_paperclip_answers_a_retryable_503_not_an_unhandled_error(client):
    """``ApiProblem`` validates its code against ``API_ERROR_STATUS`` and raises ValueError on
    an unknown one, so an invented code turns a transient Paperclip blip into a 500 from
    inside the error path itself. A create that cannot validate identity must be RETRYABLE:
    the adapter classifies >=500 as ``transient_upstream`` and Paperclip retries the run,
    whereas a 4xx would burn it."""
    from core.services.paperclip_admin_client import PaperclipApiError

    with _dependencies() as (_container, _repo, admin):
        admin.get_agent = AsyncMock(side_effect=PaperclipApiError("GET /api/agents/x -> 500", 500, None))
        response = client.post(BASE, json=BODY, headers=HEADERS)

    assert response.status_code == 503
    assert response.json()["retryable"] is True


def test_the_runtime_budget_stays_under_the_public_gateway_ceiling(client):
    """Paperclip reaches this over the public API Gateway, whose ALB integration is capped at
    30 s (`api-stack.ts` `timeoutInMillis: 30000`). A 90 s budget cannot be waited out — the
    gateway 504s over us first, and the adapter gets an answer we did not write."""
    captured = {}

    async def fake_request(_container, _path, **kwargs):
        captured.update(kwargs)
        return _runtime_response(202, {"run_id": RUN, "status": "queued", "session_id": SESSION})

    with _dependencies(), patch("routers.messaging.runtime_transport.request", fake_request):
        assert client.post(BASE, json=BODY, headers=HEADERS).status_code == 202

    assert captured["timeout_s"] < 30.0


def test_a_cold_microvm_timeout_answers_a_retryable_503_not_a_gateway_504(client):
    """A cold microVM is 54-57 s, so the first call to a parked workspace times out no matter
    what. It must come back as our own retryable 503 — the adapter classifies >=500 as
    `transient_upstream` and retries onto the now-warming microVM. `httpx.ReadTimeout` is an
    `httpx.HTTPError`, so this is already the behaviour; this test is what keeps it."""

    async def cold(*_args, **_kwargs):
        raise httpx.ReadTimeout("agentcore invoke exceeded 25s")

    with _dependencies(), patch("routers.messaging.runtime_transport.request", cold):
        response = client.post(BASE, json=BODY, headers=HEADERS)

    assert response.status_code == 503
    assert response.json()["retryable"] is True


def test_a_row_claiming_agentcore_with_no_runtime_arn_is_classified_not_a_500(client):
    """`runtime_transport._request_agentcore` raises a bare ValueError for that row state."""

    async def missing_arn(*_args, **_kwargs):
        raise ValueError("agentcore transport requires containers.agentcore_runtime_arn")

    with _dependencies(), patch("routers.messaging.runtime_transport.request", missing_arn):
        assert client.post(BASE, json=BODY, headers=HEADERS).status_code == 503


def test_a_retry_with_the_same_idempotency_key_reaches_the_bridge_as_the_same_run(client):
    """The facade holds no run state: it derives `runId` from the `Idempotency-Key` header and
    forwards it verbatim, so an adapter retry of the SAME Paperclip run arrives at the bridge
    under the key that already has an entry and is replayed (200) rather than started twice.

    This does NOT cover the orphan window: Paperclip retrying a FAILED run mints a NEW run id,
    hence a new key, hence a second turn — see the report."""
    seen = []
    replies = [
        _runtime_response(202, {"run_id": RUN, "status": "queued", "session_id": SESSION}),
        _runtime_response(200, {"run_id": RUN, "status": "running", "session_id": SESSION}),
    ]

    async def fake_request(_container, _path, **kwargs):
        seen.append(kwargs["json_body"]["identity"]["runId"])
        return replies[len(seen) - 1]

    with _dependencies(), patch("routers.messaging.runtime_transport.request", fake_request):
        first = client.post(BASE, json=BODY, headers=HEADERS)
        retry = client.post(BASE, json=BODY, headers=HEADERS)

    assert seen == [RUN, RUN]  # one run id, not two
    assert (first.status_code, retry.status_code) == (202, 200)


def test_stop_forwards_to_real_interrupt_route(client):
    calls = []

    async def fake_request(_container, path, **kwargs):
        calls.append((path, kwargs))
        return _runtime_response(202, {"run_id": RUN, "status": "stopping", "session_id": SESSION})

    with _dependencies(), patch("routers.messaging.runtime_transport.request", fake_request):
        response = client.post(f"{BASE}/{RUN}/stop", headers=HEADERS)
    assert response.status_code == 202
    assert calls[0][0] == f"/internal/paperclip/v1/runs/{RUN}/stop"
    assert calls[0][1]["method"] == "POST"
