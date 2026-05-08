"""Tests for admin_service org-aware owner_id resolution.

Background: the DDB partition key ``owner_id`` equals Clerk ``org_id`` for
org-member resources and ``user_id`` for personal-mode resources. The admin
dashboard receives the target user_id from the URL, so it must resolve the
effective owner_id via Clerk before querying container_repo / billing_repo /
usage_repo — otherwise org-member users render as "no container provisioned".

These tests pin:
- get_overview queries DDB with org_id (not user_id) when Clerk reports org
  membership, and returns org_context in the ``org`` response field.
- get_overview falls back to user_id when Clerk reports no orgs, and returns
  ``org: None``.
- get_overview is defensive: when Clerk itself errors, we still render the
  personal-mode payload (fail-open) rather than 500 the whole dashboard.

The second half of this file (``TestAdminMutationOwnerResolution``) covers
Codex's follow-up P1: admin MUTATION endpoints must also resolve owner_id
before dispatching to downstream services. Previously only reads did; writes
(container stop/start/reprovision/resize, billing cancel/pause/credit/invoice,
PATCH /config, agent delete/clear-sessions) still targeted the raw
``{user_id}`` from the URL — so for org members the mutations hit a
non-existent ``openclaw-{user_id}-{hash}`` ECS service instead of the real
``openclaw-{org_id}-{hash}``. Account ops (suspend/reactivate/force-signout/
resend-verification) target the Clerk user directly and MUST NOT resolve.
"""

import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.mark.asyncio
async def test_get_overview_uses_org_id_as_owner_when_user_is_in_org():
    """User in an org → repos queried with org_id, response includes org."""
    from core.services import admin_service

    org = {
        "id": "org_abc",
        "slug": "acme",
        "name": "Acme Co.",
        "role": "org:admin",
    }

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value={"id": "user_abc", "email_addresses": []}),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running", "plan_tier": "pro"}),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"plan_tier": "pro"}),
        ) as mock_billing,
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={"total_spend_microdollars": 0}),
        ) as mock_usage,
    ):
        result = await admin_service.get_overview("user_abc")

    # Repos must be queried with the ORG id, not the USER id.
    mock_container.assert_awaited_once_with("org_abc")
    mock_billing.assert_awaited_once_with("org_abc")
    # usage_repo.get_period_usage takes (owner_id, period)
    assert mock_usage.await_args.args[0] == "org_abc"

    # Response carries org context.
    assert result["org"] == org
    assert result["identity"]["id"] == "user_abc"
    assert result["container"]["status"] == "running"


@pytest.mark.asyncio
async def test_get_overview_uses_user_id_when_personal_mode():
    """User in no orgs → repos queried with user_id, ``org`` is None."""
    from core.services import admin_service

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value={"id": "user_solo"}),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_billing,
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={}),
        ) as mock_usage,
    ):
        result = await admin_service.get_overview("user_solo")

    mock_container.assert_awaited_once_with("user_solo")
    mock_billing.assert_awaited_once_with("user_solo")
    assert mock_usage.await_args.args[0] == "user_solo"

    assert result["org"] is None


@pytest.mark.asyncio
async def test_get_overview_falls_back_to_user_id_when_clerk_errors():
    """Clerk org lookup raising must NOT 500 the dashboard — fall back to
    personal-mode lookup so the admin can still see something."""
    from core.services import admin_service

    async def raising_clerk(user_id: str):  # noqa: ARG001
        raise RuntimeError("clerk 503")

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            side_effect=raising_clerk,
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await admin_service.get_overview("user_flaky")

    mock_container.assert_awaited_once_with("user_flaky")
    assert result["org"] is None


@pytest.mark.asyncio
async def test_get_overview_picks_first_org_when_clerk_returns_multiple():
    """project_single_org_per_user: one org per user. If Clerk returns
    multiple (shouldn't happen) we use the first and log a warning."""
    from core.services import admin_service

    orgs = [
        {"id": "org_first", "slug": "first", "name": "First", "role": "org:admin"},
        {"id": "org_second", "slug": "second", "name": "Second", "role": "org:member"},
    ]

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=orgs),
        ),
        patch(
            "core.services.admin_service.clerk_admin.get_user",
            new=AsyncMock(return_value={"id": "user_multi"}),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_container,
        patch(
            "core.services.admin_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.admin_service.usage_repo.get_period_usage",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await admin_service.get_overview("user_multi")

    mock_container.assert_awaited_once_with("org_first")
    assert result["org"]["id"] == "org_first"


@pytest.mark.asyncio
async def test_list_user_agents_uses_org_id_for_container_lookup():
    """Agents-list container lookup must hit org_id when user is in an org."""
    from core.services import admin_service

    org = {"id": "org_abc", "slug": "acme", "name": "Acme", "role": "org:admin"}

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ) as mock_container,
    ):
        result = await admin_service.list_user_agents("user_abc")

    mock_container.assert_awaited_once_with("org_abc")
    assert result["org"] == org
    assert result["container_status"] == "none"


@pytest.mark.asyncio
async def test_get_agent_detail_uses_org_id_for_container_lookup():
    """Agent detail container lookup must hit org_id when user is in an org."""
    from core.services import admin_service

    org = {"id": "org_abc", "slug": "acme", "name": "Acme", "role": "org:admin"}

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "stopped"}),
        ) as mock_container,
    ):
        result = await admin_service.get_agent_detail("user_abc", "agt_1")

    mock_container.assert_awaited_once_with("org_abc")
    assert result["error"] == "container_not_running"
    assert result["org"] == org


# -----------------------------------------------------------------------------
# Codex P1+P1+P2 (PR #376) regression tests
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_user_agents_uses_unique_req_ids_per_call():
    """P1: concurrent admin agents.list calls on the same org must NOT
    overwrite each other in the gateway's pending-RPC dict. The deterministic
    req_id (admin-agents-list-{owner_id}) collided; add uuid4 entropy."""
    from core.services import admin_service

    org = {"id": "org_abc", "slug": "acme", "name": "Acme", "role": "org:admin"}

    captured_req_ids: list[str] = []

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        captured_req_ids.append(req_id)
        return {"agents": [], "cursor": None}

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
    ):
        results = await asyncio.gather(
            admin_service.list_user_agents("user_abc"),
            admin_service.list_user_agents("user_abc"),
        )

    assert len(results) == 2
    assert len(captured_req_ids) == 2
    # Both req_ids must share the admin-agents-list-{owner_id} prefix but
    # differ in the trailing uuid4 slug.
    for rid in captured_req_ids:
        assert rid.startswith("admin-agents-list-org_abc-")
    assert captured_req_ids[0] != captured_req_ids[1]


@pytest.mark.asyncio
async def test_list_user_agents_sends_empty_params_to_openclaw():
    """Regression: OpenClaw's agents.list schema rejects unknown keys with
    INVALID_REQUEST (observed in prod on cursor/limit). Match the main-app
    call site (useAgents.ts) which passes no params. cursor/limit on the
    service signature stay for forward compatibility but aren't forwarded.
    """
    from core.services import admin_service

    captured_params: list[dict] = []

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        captured_params.append(params)
        return {"agents": [{"id": "agt_1"}], "cursor": None}

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
    ):
        result = await admin_service.list_user_agents("user_abc", cursor="abc", limit=25)

    assert len(captured_params) == 1
    assert captured_params[0] == {}, (
        f"agents.list params must be empty (OpenClaw rejects unknown keys); got {captured_params[0]}"
    )
    assert result["agents"] == [{"id": "agt_1"}]
    assert result["container_status"] == "running"


@pytest.mark.asyncio
async def test_get_agent_detail_redacts_secrets_in_config_raw_envelope():
    """Codex P1 on PR #379: config.get returns either a plain dict or the
    ConfigPanel envelope {raw: "<openclaw.json as string>", hash}. The
    redactor only walks dict/list nodes; strings pass through unchanged.
    If we redact the envelope directly, config_redacted.raw still
    contains BYOK secrets verbatim. Parse+redact the raw envelope.
    """
    import json as _json

    from core.services import admin_service

    secret_config = {
        "providers": {
            "anthropic_api_key": "sk-ant-REAL-SECRET-DO-NOT-LEAK",
            "openai_api_key": "sk-openai-ALSO-SECRET",
        },
        "agents": {"list": [{"id": "agt_1", "name": "A"}]},
    }
    raw_envelope = {"raw": _json.dumps(secret_config), "hash": "abc123"}

    rpc_results = {
        "agent.identity.get": {"name": "A"},
        "sessions.list": {"sessions": []},
        "skills.status": {"skills": []},
        "config.get": raw_envelope,  # envelope form — the bug path
    }

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        return rpc_results[method]

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
    ):
        result = await admin_service.get_agent_detail("user_abc", "agt_1")

    # Belt-and-suspenders: secrets must not appear anywhere in the response.
    response_str = _json.dumps(result)
    assert "sk-ant-REAL-SECRET-DO-NOT-LEAK" not in response_str, "anthropic key leaked"
    assert "sk-openai-ALSO-SECRET" not in response_str, "openai key leaked"

    # Structural check: envelope was parsed, so config_redacted is the parsed
    # + redacted config (not a wrapper containing the raw string).
    assert result["config_redacted"]["providers"]["anthropic_api_key"] == "***redacted***"
    assert result["config_redacted"]["providers"]["openai_api_key"] == "***redacted***"


@pytest.mark.asyncio
async def test_get_agent_detail_handles_malformed_config_raw_envelope():
    """If config.get returns {raw: "not-json"}, don't crash and don't leak
    the unparseable blob. Return an error marker instead."""
    from core.services import admin_service

    rpc_results = {
        "agent.identity.get": {"name": "A"},
        "sessions.list": {"sessions": []},
        "skills.status": {"skills": []},
        "config.get": {"raw": "not valid json {{{", "hash": "abc"},
    }

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        return rpc_results[method]

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
    ):
        result = await admin_service.get_agent_detail("user_abc", "agt_1")

    assert result["config_redacted"] == {"error": "malformed_config_raw"}


@pytest.mark.asyncio
async def test_get_agent_detail_normalizes_sessions_list_array_response():
    """Codex P2 (PR #379 follow-up): sessions.list returns either
    {sessions: [...]} or a raw array depending on OpenClaw version, same as
    skills.status. Admin must surface both; previously only the dict form
    was handled and array responses silently rendered 'no recent sessions'.
    """
    from core.services import admin_service

    rpc_results = {
        "agent.identity.get": {"name": "A"},
        "sessions.list": [  # array form — the bug path
            {"id": "s_keep", "agentId": "agt_1"},
            {"id": "s_drop", "agentId": "agt_OTHER"},
            {"id": "s_keep2", "agent_id": "agt_1"},
        ],
        "skills.status": {"skills": []},
        "config.get": {},
    }

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        return rpc_results[method]

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
    ):
        result = await admin_service.get_agent_detail("user_abc", "agt_1")

    ids = [s["id"] for s in result["sessions"]]
    assert ids == ["s_keep", "s_keep2"], (
        f"array-shape sessions.list + agent filter must yield only agt_1 sessions; got {ids}"
    )


@pytest.mark.asyncio
async def test_get_agent_detail_normalizes_skills_status_array_response():
    """Codex P2 (PR #379): skills.status returns either {skills: [...]} or a
    raw array depending on OpenClaw version. Admin must surface both shapes;
    previously `skills.get("skills", [])` returned [] for array responses,
    silently showing "no skills" on environments returning the array form."""
    from core.services import admin_service

    rpc_results = {
        "agent.identity.get": {"name": "A"},
        "sessions.list": {"sessions": []},
        "skills.status": [  # array form — the bug path
            {"id": "skill_shell", "enabled": True},
            {"id": "skill_browser", "enabled": False},
        ],
        "config.get": {},
    }

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        return rpc_results[method]

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
    ):
        result = await admin_service.get_agent_detail("user_abc", "agt_1")

    assert len(result["skills"]) == 2, f"array-shape skills.status must surface; got {result['skills']}"
    assert result["skills"][0]["id"] == "skill_shell"


@pytest.mark.asyncio
async def test_get_agent_detail_uses_unique_req_ids_per_call():
    """P1: _rpc in get_agent_detail issued 4 RPCs with deterministic
    admin-{suffix}-{agent_id} ids. Concurrent detail loads on the same agent
    would clobber each other. Each call must carry uuid4 entropy."""
    from core.services import admin_service

    org = {"id": "org_abc", "slug": "acme", "name": "Acme", "role": "org:admin"}

    captured_req_ids: list[str] = []

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        captured_req_ids.append(req_id)
        # Return a plausibly-shaped dict so get_agent_detail doesn't error.
        return {"agent": {}, "sessions": [], "skills": [], "config": {}}

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[org]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
        patch(
            "core.services.admin_service.redact_openclaw_config",
            side_effect=lambda x: x,
        ),
    ):
        await asyncio.gather(
            admin_service.get_agent_detail("user_abc", "agt_1"),
            admin_service.get_agent_detail("user_abc", "agt_1"),
        )

    # 4 RPCs per detail call * 2 concurrent calls = 8 unique ids.
    assert len(captured_req_ids) == 8
    assert len(set(captured_req_ids)) == 8, f"req_ids collided: {captured_req_ids}"
    # All still carry the admin-{suffix}-{agent_id} prefix.
    for rid in captured_req_ids:
        assert rid.startswith("admin-") and "-agt_1-" in rid


@pytest.mark.asyncio
async def test_resolve_owner_falls_back_to_personal_mode_on_clerk_timeout(caplog):
    """P2: _resolve_owner_for_admin must bound the Clerk call with the same
    per-panel timeout budget. When Clerk is slow/unreachable, fall back to
    personal-mode (user_id, None) and log a warning — don't block the
    dashboard on upstream latency before _with_timeout-wrapped reads."""
    from core.services import admin_service

    async def never_returns(user_id: str):  # noqa: ARG001
        # Sleep far longer than the patched timeout to force asyncio.TimeoutError.
        await asyncio.sleep(10)
        return [{"id": "org_never"}]

    with (
        patch(
            "core.services.admin_service._PARALLEL_TIMEOUT_S",
            0.05,  # 50ms — enough time for wait_for to trip, too short for the sleep.
        ),
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            side_effect=never_returns,
        ),
        caplog.at_level(logging.WARNING, logger="core.services.admin_service"),
    ):
        owner_id, org_context = await admin_service._resolve_owner_for_admin("user_slow")

    assert owner_id == "user_slow"
    assert org_context is None
    # Warning must mention timeout so operators know Clerk is slow.
    assert any("timeout" in rec.message.lower() for rec in caplog.records)


# =============================================================================
# Codex P1 (PR #376 follow-up): admin MUTATION endpoints must resolve owner_id
# =============================================================================
#
# These tests drive the admin router (routers/admin.py) through FastAPI's
# TestClient and verify each mutation endpoint's internal dispatch switches
# from the raw URL path param to the Clerk-resolved owner_id when the target
# user is in an org. They use the same dependency_overrides pattern as
# tests/unit/routers/test_admin_actions_writes.py — the ``app`` and
# ``async_client`` fixtures from conftest.py provide a full FastAPI app with
# auth shimmed to an @isol8.co admin.
#
# Org-mode fixtures mock ``clerk_admin.list_user_organizations`` to return a
# single org so ``resolve_admin_owner_id`` yields the org_id. Personal-mode
# tests return []. Account-ops tests assert the raw user_id still flows
# through (regression guard — suspending an org-member's account must ban
# the specific user, not the org).


ORG_FIXTURE = {
    "id": "org_abc",
    "slug": "acme",
    "name": "Acme Co.",
    "role": "org:admin",
}


@pytest.fixture
def admin_env(app):
    """Caller has an @isol8.co email — require_platform_admin admits them."""
    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_test_123", email="admin@isol8.co")
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def org_mode():
    """Patch resolve_admin_owner_id to return ('org_abc', ORG_FIXTURE).

    Patching at the router's import path (``routers.admin.resolve_admin_owner_id``)
    is enough — that's the symbol the mutation handlers call. We avoid
    patching ``clerk_admin.list_user_organizations`` directly so the test
    stays tight: a future refactor that moves resolution elsewhere still fails
    loudly if it stops calling the resolver.
    """
    with patch(
        "routers.admin.resolve_admin_owner_id",
        new=AsyncMock(return_value=("org_abc", ORG_FIXTURE)),
    ) as mock:
        yield mock


@pytest.fixture
def personal_mode():
    """Patch resolve_admin_owner_id to return ('user_123', None) — personal mode."""
    with patch(
        "routers.admin.resolve_admin_owner_id",
        new=AsyncMock(return_value=("user_123", None)),
    ) as mock:
        yield mock


class TestAdminMutationOwnerResolution:
    """Each admin mutation endpoint dispatches to the resolved owner_id, not
    the raw URL path param."""

    # -- Container mutations -------------------------------------------------

    @pytest.mark.asyncio
    async def test_container_stop_uses_org_owner_id(self, async_client, admin_env, org_mode):
        ecs = MagicMock()
        ecs.stop_user_service = AsyncMock(return_value={"status": "stopped"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/user_123/container/stop")
        assert res.status_code == 200
        ecs.stop_user_service.assert_awaited_once_with("org_abc")
        org_mode.assert_awaited_once_with("user_123")

    @pytest.mark.asyncio
    async def test_container_stop_personal_mode_uses_user_id(self, async_client, admin_env, personal_mode):
        """Negative test: personal-mode user (no orgs) → dispatch targets the
        Clerk user_id unchanged."""
        ecs = MagicMock()
        ecs.stop_user_service = AsyncMock(return_value={"status": "stopped"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/user_123/container/stop")
        assert res.status_code == 200
        ecs.stop_user_service.assert_awaited_once_with("user_123")

    @pytest.mark.asyncio
    async def test_container_start_uses_org_owner_id(self, async_client, admin_env, org_mode):
        ecs = MagicMock()
        ecs.start_user_service = AsyncMock(return_value={"status": "started"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/user_123/container/start")
        assert res.status_code == 200
        ecs.start_user_service.assert_awaited_once_with("org_abc")

    @pytest.mark.asyncio
    async def test_container_reprovision_uses_org_owner_id(self, async_client, admin_env, org_mode):
        ecs = MagicMock()
        ecs.reprovision_for_user = AsyncMock(return_value={"status": "reprovisioned"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/user_123/container/reprovision")
        assert res.status_code == 200
        ecs.reprovision_for_user.assert_awaited_once_with("org_abc")

    # -- Billing mutations ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_billing_cancel_subscription_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.cancel_subscription_for_owner = AsyncMock(return_value={"status": "canceled"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/user_123/billing/cancel-subscription")
        assert res.status_code == 200
        billing.cancel_subscription_for_owner.assert_awaited_once_with("org_abc")

    @pytest.mark.asyncio
    async def test_billing_pause_subscription_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.pause_subscription_for_owner = AsyncMock(return_value={"status": "paused"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/user_123/billing/pause-subscription")
        assert res.status_code == 200
        billing.pause_subscription_for_owner.assert_awaited_once_with("org_abc")

    @pytest.mark.asyncio
    async def test_billing_issue_credit_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.issue_credit_for_owner = AsyncMock(return_value={"credit_id": "cn_1"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/user_123/billing/issue-credit",
                json={"amount_cents": 500, "reason": "incident_comp"},
            )
        assert res.status_code == 200
        billing.issue_credit_for_owner.assert_awaited_once_with(
            "org_abc",
            amount_cents=500,
            reason="incident_comp",
        )

    @pytest.mark.asyncio
    async def test_billing_mark_invoice_resolved_uses_org_owner_id(self, async_client, admin_env, org_mode):
        billing = MagicMock()
        billing.mark_invoice_resolved = AsyncMock(return_value={"status": "resolved"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/user_123/billing/mark-invoice-resolved",
                json={"invoice_id": "in_1"},
            )
        assert res.status_code == 200
        billing.mark_invoice_resolved.assert_awaited_once_with("org_abc", "in_1")

    # -- Config + agent mutations -------------------------------------------

    @pytest.mark.asyncio
    async def test_config_patch_uses_org_owner_id(self, async_client, admin_env, org_mode):
        with patch(
            "routers.admin.patch_openclaw_config",
            new=AsyncMock(return_value=None),
        ) as mock_patch:
            res = await async_client.patch(
                "/api/v1/admin/users/user_123/config",
                json={"patch": {"agents": {"defaults": {}}}},
            )
        assert res.status_code == 200
        mock_patch.assert_awaited_once()
        # owner_id keyword — patch_openclaw_config is called with kwargs.
        assert mock_patch.await_args.kwargs.get("owner_id") == "org_abc"

    @pytest.mark.asyncio
    async def test_agent_delete_uses_org_owner_id_and_unique_req_id(self, async_client, admin_env, org_mode):
        """Dispatch targets org_id AND the req_id carries a uuid4 nonce so
        concurrent deletes on the same shared agent don't collide in the
        gateway pending-RPC dict."""
        import re

        pool = MagicMock()
        pool.send_rpc = AsyncMock(return_value={"deleted": True})
        ecs = MagicMock()
        ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok"}, "10.0.0.1"))
        with (
            patch("routers.admin.get_gateway_pool", return_value=pool),
            patch("routers.admin.get_ecs_manager", return_value=ecs),
        ):
            res = await async_client.post("/api/v1/admin/users/user_123/agents/agt_42/delete")

        assert res.status_code == 200
        # ECS container lookup uses org_id.
        ecs.resolve_running_container.assert_awaited_once_with("org_abc")
        # RPC user_id is org_id; req_id carries an 8-hex-char uuid nonce.
        kwargs = pool.send_rpc.await_args.kwargs
        assert kwargs["user_id"] == "org_abc"
        assert re.fullmatch(r"admin-agent-delete-agt_42-[0-9a-f]{8}", kwargs["req_id"])

    @pytest.mark.asyncio
    async def test_agent_delete_req_ids_are_unique_across_calls(self, async_client, admin_env, org_mode):
        """P1: two concurrent delete calls on the same agent must produce
        different req_ids so the gateway's pending-RPC dict keyed by req_id
        doesn't clobber one future with another."""
        pool = MagicMock()
        pool.send_rpc = AsyncMock(return_value={"deleted": True})
        ecs = MagicMock()
        ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok"}, "10.0.0.1"))
        with (
            patch("routers.admin.get_gateway_pool", return_value=pool),
            patch("routers.admin.get_ecs_manager", return_value=ecs),
        ):
            r1 = await async_client.post("/api/v1/admin/users/user_123/agents/agt_42/delete")
            r2 = await async_client.post("/api/v1/admin/users/user_123/agents/agt_42/delete")
        assert r1.status_code == r2.status_code == 200
        req_ids = [c.kwargs["req_id"] for c in pool.send_rpc.await_args_list]
        assert len(req_ids) == 2
        assert req_ids[0] != req_ids[1]

    @pytest.mark.asyncio
    async def test_agent_clear_sessions_uses_org_owner_id_and_unique_req_id(self, async_client, admin_env, org_mode):
        import re

        pool = MagicMock()
        pool.send_rpc = AsyncMock(return_value={"cleared": 3})
        ecs = MagicMock()
        ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok"}, "10.0.0.1"))
        with (
            patch("routers.admin.get_gateway_pool", return_value=pool),
            patch("routers.admin.get_ecs_manager", return_value=ecs),
        ):
            res = await async_client.post("/api/v1/admin/users/user_123/agents/agt_42/clear-sessions")

        assert res.status_code == 200
        ecs.resolve_running_container.assert_awaited_once_with("org_abc")
        kwargs = pool.send_rpc.await_args.kwargs
        assert kwargs["user_id"] == "org_abc"
        assert re.fullmatch(r"admin-agent-clear-agt_42-[0-9a-f]{8}", kwargs["req_id"])

    # -- Account mutations MUST NOT resolve (regression guard) ---------------

    @pytest.mark.asyncio
    async def test_account_suspend_uses_raw_user_id_not_org(self, async_client, admin_env):
        """Regression guard: account/* endpoints target the Clerk user
        directly, not the org. If these ever start calling
        resolve_admin_owner_id an admin would end up banning "org_abc" (which
        Clerk would 404) instead of the actual misbehaving user.

        We patch resolve_admin_owner_id so, if the handler DID call it, we'd
        see ``org_abc`` land in the ban payload and fail the assertion.
        """
        with (
            patch(
                "routers.admin.resolve_admin_owner_id",
                new=AsyncMock(return_value=("org_abc", ORG_FIXTURE)),
            ) as mock_resolve,
            patch(
                "routers.admin.clerk_admin.ban_user",
                new=AsyncMock(return_value={"banned": True}),
            ) as mock_ban,
        ):
            res = await async_client.post("/api/v1/admin/users/user_123/account/suspend")

        assert res.status_code == 200
        mock_ban.assert_awaited_once_with("user_123")
        mock_resolve.assert_not_awaited()


# =============================================================================
# PR #379: get_agent_detail RPC method names + camelCase params
# =============================================================================
#
# Background: prod returned `{"code":"INVALID_REQUEST","message":"unknown
# method: agents.get"}` — admin_service was calling non-existent RPCs with
# snake_case params. The correct OpenClaw RPCs, anchored to the main-app
# call sites:
#   agent.identity.get {agentId}            (AgentOverviewTab.tsx:27)
#   sessions.list     {includeGlobal, ...} (SessionsPanel.tsx:123-137)
#   skills.status     {agentId}            (SkillsPanel.tsx:219)
#   config.get        {}                    (ConfigPanel.tsx:14)
#
# sessions.list isn't agent-filterable server-side — main app narrows
# client-side and we must too.


@pytest.mark.asyncio
async def test_get_agent_detail_uses_correct_rpc_methods():
    """Regression: the four RPC method names must match OpenClaw's actual
    handlers. `agents.get` and `skills.list` DO NOT EXIST — OpenClaw exposes
    `agent.identity.get` and `skills.status`. This guards against a silent
    rename regression in admin_service.get_agent_detail."""
    from core.services import admin_service

    captured_methods: list[str] = []

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        captured_methods.append(method)
        return {"sessions": [], "skills": []}

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
        patch(
            "core.services.admin_service.redact_openclaw_config",
            side_effect=lambda x: x,
        ),
    ):
        await admin_service.get_agent_detail("user_abc", "agt_1")

    # Order is gather-dependent but the set must be exact.
    assert set(captured_methods) == {
        "agent.identity.get",
        "sessions.list",
        "skills.status",
        "config.get",
    }, f"wrong RPC methods: {captured_methods}"


@pytest.mark.asyncio
async def test_get_agent_detail_uses_camelcase_params():
    """Regression: OpenClaw schemas use camelCase. Admin was sending
    snake_case (`agent_id`) so every call 400'd. Each RPC's params shape
    must match the main-app call site exactly."""
    from core.services import admin_service

    # method -> params, captured per-call.
    captured: dict[str, dict] = {}

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        captured[method] = params
        return {"sessions": [], "skills": []}

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
        patch(
            "core.services.admin_service.redact_openclaw_config",
            side_effect=lambda x: x,
        ),
    ):
        await admin_service.get_agent_detail("user_abc", "agt_1")

    # agent.identity.get: only {agentId}.
    assert captured["agent.identity.get"] == {"agentId": "agt_1"}
    # skills.status: only {agentId}.
    assert captured["skills.status"] == {"agentId": "agt_1"}
    # config.get: no params (matches ConfigPanel.tsx:14).
    assert captured["config.get"] == {}
    # sessions.list: include-* flags, and NO agent filter of any form (main
    # app filters client-side — OpenClaw's schema would reject agentId here).
    sessions_params = captured["sessions.list"]
    assert sessions_params.get("includeGlobal") is True
    assert sessions_params.get("includeUnknown") is True
    assert sessions_params.get("includeDerivedTitles") is True
    assert sessions_params.get("includeLastMessage") is True
    assert "agentId" not in sessions_params
    assert "agent_id" not in sessions_params
    assert "limit" not in sessions_params

    # Blanket camelCase audit across ALL params: no snake_case agent key
    # should have leaked through anywhere.
    for method, params in captured.items():
        assert "agent_id" not in params, f"{method} sent snake_case agent_id: {params}"


@pytest.mark.asyncio
async def test_get_agent_detail_filters_sessions_by_agent_client_side():
    """OpenClaw's sessions.list returns all sessions regardless of agent;
    admin_service must narrow to the target agent client-side. Both
    `agentId` (canonical) and `agent_id` (legacy payloads) are accepted."""
    from core.services import admin_service

    seeded_sessions = {
        "sessions": [
            {"id": "s1", "agentId": "agt_1"},
            {"id": "s2", "agentId": "agt_OTHER"},
            {"id": "s3", "agent_id": "agt_1"},
            {"id": "s4"},  # no agent key at all — must be filtered out
        ]
    }

    async def fake_send_rpc(*, user_id, req_id, method, params, ip, token):  # noqa: ARG001
        if method == "sessions.list":
            return seeded_sessions
        if method == "skills.status":
            return {"skills": []}
        if method == "config.get":
            return {"raw": "{}"}
        if method == "agent.identity.get":
            return {"id": "agt_1", "name": "Test Agent"}
        return {}

    fake_pool = type("FakePool", (), {"send_rpc": staticmethod(fake_send_rpc)})()
    fake_ecs = type(
        "FakeECS",
        (),
        {"resolve_running_container": AsyncMock(return_value=({"gateway_token": "tok"}, "1.2.3.4"))},
    )()

    with (
        patch(
            "core.services.admin_service.clerk_admin.list_user_organizations",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "core.services.admin_service.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("core.services.admin_service.get_ecs_manager", return_value=fake_ecs),
        patch("core.services.admin_service.get_gateway_pool", return_value=fake_pool),
        patch(
            "core.services.admin_service.redact_openclaw_config",
            side_effect=lambda x: x,
        ),
    ):
        result = await admin_service.get_agent_detail("user_abc", "agt_1")

    ids = [s["id"] for s in result["sessions"]]
    assert ids == ["s1", "s3"], f"expected only s1+s3 (both match forms); got {ids}"
    # Sanity: the other payloads survived the filter pipeline.
    assert result["agent"] == {"id": "agt_1", "name": "Test Agent"}
    assert result["skills"] == []
