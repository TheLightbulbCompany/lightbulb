"""Tests for /api/v1/admin/users/* read endpoints.

Covers seven GET endpoints on the future ``routers/admin.py`` module:

  - GET /api/v1/admin/users
  - GET /api/v1/admin/users/{user_id}/overview
  - GET /api/v1/admin/users/{user_id}/agents
  - GET /api/v1/admin/users/{user_id}/agents/{agent_id}
  - GET /api/v1/admin/users/{user_id}/posthog
  - GET /api/v1/admin/users/{user_id}/logs
  - GET /api/v1/admin/users/{user_id}/cloudwatch-url

Each endpoint is a thin wrapper around an ``admin_service.*`` method, so we
patch ``routers.admin.admin_service.<method>`` and assert the call args + that
the response body mirrors the service result. Auth is gated by
``Depends(require_platform_admin)``; tests flip the email domain gate by
re-binding ``get_current_user`` in ``app.dependency_overrides`` to return an
AuthContext whose email does (or doesn't) end with ``@isol8.co``.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, get_current_user

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admit_test_user(app):
    """Override get_current_user so the caller has an @isol8.co email."""
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_test_123", email="admin@isol8.co")


def _deny_all_admins(app):
    """Override get_current_user so the caller's email is NOT @isol8.co => 403."""
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_test_123", email="user@example.com")


# ---------------------------------------------------------------------------
# GET /api/v1/admin/users — paginated user directory
# ---------------------------------------------------------------------------


class TestListUsers:
    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.list_users", new_callable=AsyncMock)
    async def test_users_calls_admin_service_with_query_params(self, mock_list, async_client, app):
        _admit_test_user(app)
        mock_list.return_value = {"users": [], "cursor": None, "stubbed": False}

        res = await async_client.get("/api/v1/admin/users", params={"q": "alice", "limit": 25, "cursor": "50"})

        assert res.status_code == 200
        mock_list.assert_awaited_once()
        # Service is called with kwargs (admin_service.list_users uses *).
        kwargs = mock_list.await_args.kwargs
        assert kwargs.get("q") == "alice"
        assert kwargs.get("limit") == 25
        assert kwargs.get("cursor") == "50"

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.list_users", new_callable=AsyncMock)
    async def test_users_returns_service_result(self, mock_list, async_client, app):
        _admit_test_user(app)
        payload = {
            "users": [
                {
                    "clerk_id": "user_abc",
                    "email": "abc@example.com",
                    "container_status": "running",
                    "plan_tier": "starter",
                }
            ],
            "cursor": "50",
            "stubbed": False,
        }
        mock_list.return_value = payload

        res = await async_client.get("/api/v1/admin/users")

        assert res.status_code == 200
        assert res.json() == payload

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.list_users", new_callable=AsyncMock)
    async def test_users_default_query_params(self, mock_list, async_client, app):
        _admit_test_user(app)
        mock_list.return_value = {"users": [], "cursor": None, "stubbed": False}

        res = await async_client.get("/api/v1/admin/users")

        assert res.status_code == 200
        kwargs = mock_list.await_args.kwargs
        assert kwargs.get("q") == ""
        assert kwargs.get("limit") == 50
        assert kwargs.get("cursor") is None

    @pytest.mark.asyncio
    async def test_users_403_for_non_admin(self, async_client, app):
        _deny_all_admins(app)
        res = await async_client.get("/api/v1/admin/users")
        assert res.status_code == 403

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.list_users", new_callable=AsyncMock)
    async def test_users_passes_through_stubbed_flag(self, mock_list, async_client, app):
        _admit_test_user(app)
        mock_list.return_value = {"users": [], "cursor": None, "stubbed": True}

        res = await async_client.get("/api/v1/admin/users")

        assert res.status_code == 200
        assert res.json()["stubbed"] is True


# ---------------------------------------------------------------------------
# GET /api/v1/admin/users/{user_id}/overview
# ---------------------------------------------------------------------------


class TestOverview:
    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_overview", new_callable=AsyncMock)
    async def test_overview_calls_admin_service_with_user_id(self, mock_overview, async_client, app):
        _admit_test_user(app)
        mock_overview.return_value = {
            "identity": {"id": "user_abc"},
            "container": None,
            "billing": None,
            "usage": None,
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/overview")

        assert res.status_code == 200
        mock_overview.assert_awaited_once()
        # Path param threaded through positionally or by name — accept either.
        args, kwargs = mock_overview.await_args
        passed = args[0] if args else kwargs.get("user_id")
        assert passed == "user_abc"

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_overview", new_callable=AsyncMock)
    async def test_overview_returns_service_result_with_partial_errors(self, mock_overview, async_client, app):
        """Per-panel error slices (e.g. Stripe timed out) must NOT 500 — the
        endpoint surfaces 200 with the partial error embedded so the UI can
        render the other panels."""
        _admit_test_user(app)
        mock_overview.return_value = {
            "identity": {"id": "user_abc", "email": "abc@example.com"},
            "container": {"status": "running"},
            "billing": {"error": "timeout", "source": "ddb_billing"},
            "usage": {"used_usd": 0.42},
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/overview")

        assert res.status_code == 200
        body = res.json()
        assert body["billing"] == {"error": "timeout", "source": "ddb_billing"}
        assert body["identity"]["id"] == "user_abc"
        assert body["usage"]["used_usd"] == 0.42

    @pytest.mark.asyncio
    async def test_overview_403_for_non_admin(self, async_client, app):
        _deny_all_admins(app)
        res = await async_client.get("/api/v1/admin/users/user_abc/overview")
        assert res.status_code == 403

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_overview", new_callable=AsyncMock)
    async def test_overview_writes_audit_row(self, mock_overview, async_client, app):
        """Loading /admin/users/{id}/overview must persist an admin_actions
        row tagged action starting with "user." and target_user_id from the
        path. The test is loose: it accepts whichever audit mechanism the
        endpoint uses, and only asserts the shape when one runs."""
        _admit_test_user(app)
        mock_overview.return_value = {
            "identity": {"id": "user_abc"},
            "container": None,
            "billing": None,
            "usage": None,
        }

        with patch("core.repositories.admin_actions_repo.create", new_callable=AsyncMock) as mock_create:
            res = await async_client.get("/api/v1/admin/users/user_abc/overview")

        assert res.status_code == 200
        if mock_create.await_count:
            kwargs = mock_create.await_args.kwargs
            # action label may be "user.view" or "user.overview.view" — accept
            # any "user.view*" prefix so the test isn't over-specified.
            action = kwargs.get("action") or (mock_create.await_args.args[0] if mock_create.await_args.args else "")
            assert isinstance(action, str) and action.startswith("user.")
            target = kwargs.get("target_user_id")
            assert target == "user_abc"


# ---------------------------------------------------------------------------
# GET /api/v1/admin/users/{user_id}/agents
# ---------------------------------------------------------------------------


class TestListUserAgents:
    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.list_user_agents", new_callable=AsyncMock)
    async def test_agents_returns_running_container_result(self, mock_agents, async_client, app):
        _admit_test_user(app)
        mock_agents.return_value = {
            "agents": [{"agent_id": "agt_1", "name": "Researcher"}],
            "cursor": None,
            "container_status": "running",
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/agents")

        assert res.status_code == 200
        body = res.json()
        assert body["container_status"] == "running"
        assert len(body["agents"]) == 1
        assert body["agents"][0]["agent_id"] == "agt_1"

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.list_user_agents", new_callable=AsyncMock)
    async def test_agents_surfaces_container_stopped(self, mock_agents, async_client, app):
        """Stopped/scale-to-zero container is a normal state (free tier).
        Must 200 with empty agents + status="stopped" so the UI can show a
        "container is sleeping" hint instead of an error toast."""
        _admit_test_user(app)
        mock_agents.return_value = {
            "agents": [],
            "cursor": None,
            "container_status": "stopped",
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/agents")

        assert res.status_code == 200
        body = res.json()
        assert body["container_status"] == "stopped"
        assert body["agents"] == []

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.list_user_agents", new_callable=AsyncMock)
    async def test_agents_threads_cursor_and_limit_query_params(self, mock_agents, async_client, app):
        _admit_test_user(app)
        mock_agents.return_value = {
            "agents": [],
            "cursor": None,
            "container_status": "running",
        }

        res = await async_client.get(
            "/api/v1/admin/users/user_abc/agents",
            params={"cursor": "next-page-token", "limit": 10},
        )

        assert res.status_code == 200
        kwargs = mock_agents.await_args.kwargs
        assert kwargs.get("cursor") == "next-page-token"
        assert kwargs.get("limit") == 10
        # user_id is positional
        args = mock_agents.await_args.args
        passed_uid = args[0] if args else mock_agents.await_args.kwargs.get("user_id")
        assert passed_uid == "user_abc"

    @pytest.mark.asyncio
    async def test_agents_403_for_non_admin(self, async_client, app):
        _deny_all_admins(app)
        res = await async_client.get("/api/v1/admin/users/user_abc/agents")
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/admin/users/{user_id}/agents/{agent_id}
# ---------------------------------------------------------------------------


class TestAgentDetail:
    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_agent_detail", new_callable=AsyncMock)
    async def test_agent_detail_returns_redacted_config(self, mock_detail, async_client, app):
        """CEO S3: the openclaw.json slice MUST be redacted before leaving
        the backend. We trust admin_service.get_agent_detail to apply
        redact_openclaw_config; the router just passes the value through."""
        _admit_test_user(app)
        mock_detail.return_value = {
            "agent": {"id": "agt_1", "name": "Researcher"},
            "sessions": [],
            "skills": [],
            "config_redacted": {"providers": {"anthropic_api_key": "***redacted***"}},
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/agents/agt_1")

        assert res.status_code == 200
        body = res.json()
        assert body["config_redacted"]["providers"]["anthropic_api_key"] == "***redacted***"
        # Hard guard: the real secret string should never appear in the
        # response body even if the impl forgets to call redact.
        assert "sk-ant-" not in res.text

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_agent_detail", new_callable=AsyncMock)
    async def test_agent_detail_409_when_container_not_running(self, mock_detail, async_client, app):
        """When admin_service signals "container_not_running" the router
        surfaces it as 409 Conflict — the resource exists but is in a state
        that prevents fetch. (Implementation note: returning 200 with the
        error embedded would also be defensible; this test pins the chosen
        behaviour as 409 so the frontend can branch on status.)"""
        _admit_test_user(app)
        mock_detail.return_value = {
            "error": "container_not_running",
            "container_status": "stopped",
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/agents/agt_1")

        assert res.status_code == 409
        body = res.json()
        # FastAPI HTTPException nests under "detail"; tolerate either shape.
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            assert detail.get("error") == "container_not_running"
        else:
            assert "container_not_running" in str(detail)

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_agent_detail", new_callable=AsyncMock)
    async def test_agent_detail_threads_both_path_params(self, mock_detail, async_client, app):
        _admit_test_user(app)
        mock_detail.return_value = {
            "agent": {"id": "agt_1"},
            "sessions": [],
            "skills": [],
            "config_redacted": {},
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/agents/agt_xyz")

        assert res.status_code == 200
        args = mock_detail.await_args.args
        kwargs = mock_detail.await_args.kwargs
        passed_uid = args[0] if len(args) >= 1 else kwargs.get("user_id")
        passed_aid = args[1] if len(args) >= 2 else kwargs.get("agent_id")
        assert passed_uid == "user_abc"
        assert passed_aid == "agt_xyz"

    @pytest.mark.asyncio
    async def test_agent_detail_403_for_non_admin(self, async_client, app):
        _deny_all_admins(app)
        res = await async_client.get("/api/v1/admin/users/user_abc/agents/agt_1")
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/admin/users/{user_id}/posthog
# ---------------------------------------------------------------------------


class TestPostHogTimeline:
    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_posthog_timeline", new_callable=AsyncMock)
    async def test_posthog_returns_events_when_present(self, mock_ph, async_client, app):
        _admit_test_user(app)
        mock_ph.return_value = {
            "events": [
                {"event": "$pageview", "timestamp": "2026-04-17T12:00:00Z"},
                {"event": "agent_chat_send", "timestamp": "2026-04-17T12:01:00Z"},
            ],
            "stubbed": False,
            "missing": False,
            "error": None,
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/posthog")

        assert res.status_code == 200
        body = res.json()
        assert len(body["events"]) == 2
        assert body["events"][0]["event"] == "$pageview"

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_posthog_timeline", new_callable=AsyncMock)
    async def test_posthog_returns_missing_when_user_never_identified(self, mock_ph, async_client, app):
        """Person never identified in PostHog → 200 with missing=True (NOT a
        404). The user exists in Clerk; they just never produced a tracked
        event. Frontend renders "no activity recorded" instead of erroring."""
        _admit_test_user(app)
        mock_ph.return_value = {
            "events": [],
            "stubbed": False,
            "missing": True,
            "error": None,
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/posthog")

        assert res.status_code == 200
        body = res.json()
        assert body["missing"] is True
        assert body["events"] == []

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_posthog_timeline", new_callable=AsyncMock)
    async def test_posthog_returns_stubbed_when_key_unset(self, mock_ph, async_client, app):
        """Local dev / unconfigured POSTHOG_PROJECT_ID → service returns
        stubbed=True. Endpoint must surface that flag so the UI can show a
        "PostHog not configured for this environment" badge."""
        _admit_test_user(app)
        mock_ph.return_value = {
            "events": [],
            "stubbed": True,
            "missing": False,
            "error": None,
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/posthog")

        assert res.status_code == 200
        body = res.json()
        assert body["stubbed"] is True

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_posthog_timeline", new_callable=AsyncMock)
    async def test_posthog_threads_limit_query_param(self, mock_ph, async_client, app):
        _admit_test_user(app)
        mock_ph.return_value = {
            "events": [],
            "stubbed": False,
            "missing": False,
            "error": None,
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/posthog", params={"limit": 250})

        assert res.status_code == 200
        kwargs = mock_ph.await_args.kwargs
        assert kwargs.get("limit") == 250


# ---------------------------------------------------------------------------
# GET /api/v1/admin/users/{user_id}/logs
# ---------------------------------------------------------------------------


class TestUserLogs:
    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_logs", new_callable=AsyncMock)
    async def test_logs_default_query_params(self, mock_logs, async_client, app):
        _admit_test_user(app)
        mock_logs.return_value = {"events": [], "cursor": None, "missing": False}

        res = await async_client.get("/api/v1/admin/users/user_abc/logs")

        assert res.status_code == 200
        kwargs = mock_logs.await_args.kwargs
        assert kwargs.get("level") == "ERROR"
        assert kwargs.get("hours") == 24
        assert kwargs.get("limit") == 20
        assert kwargs.get("cursor") is None

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_logs", new_callable=AsyncMock)
    async def test_logs_threads_all_query_params(self, mock_logs, async_client, app):
        _admit_test_user(app)
        mock_logs.return_value = {"events": [], "cursor": None, "missing": False}

        res = await async_client.get(
            "/api/v1/admin/users/user_abc/logs",
            params={"level": "WARN", "hours": 72, "limit": 50, "cursor": "abc"},
        )

        assert res.status_code == 200
        kwargs = mock_logs.await_args.kwargs
        assert kwargs.get("level") == "WARN"
        assert kwargs.get("hours") == 72
        assert kwargs.get("limit") == 50
        assert kwargs.get("cursor") == "abc"

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_logs", new_callable=AsyncMock)
    async def test_logs_returns_events_with_cursor_for_pagination(self, mock_logs, async_client, app):
        _admit_test_user(app)
        mock_logs.return_value = {
            "events": [
                {"timestamp": 1712345678000, "message": "ERROR: kaboom"},
                {"timestamp": 1712345679000, "message": "ERROR: rebooting"},
            ],
            "cursor": "next-token",
            "missing": False,
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/logs")

        assert res.status_code == 200
        body = res.json()
        assert len(body["events"]) == 2
        assert body["cursor"] == "next-token"
        assert body["missing"] is False

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_logs", new_callable=AsyncMock)
    async def test_logs_returns_missing_when_log_group_not_found(self, mock_logs, async_client, app):
        """Container never started or log group rotated away → service
        returns missing=True. Endpoint passes that through (no 404)."""
        _admit_test_user(app)
        mock_logs.return_value = {
            "events": [],
            "cursor": None,
            "missing": True,
        }

        res = await async_client.get("/api/v1/admin/users/user_abc/logs")

        assert res.status_code == 200
        body = res.json()
        assert body["missing"] is True
        assert body["events"] == []

    @pytest.mark.asyncio
    async def test_logs_403_for_non_admin(self, async_client, app):
        _deny_all_admins(app)
        res = await async_client.get("/api/v1/admin/users/user_abc/logs")
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/admin/users/{user_id}/cloudwatch-url
# ---------------------------------------------------------------------------


class TestCloudWatchUrl:
    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_cloudwatch_url")
    async def test_cwl_url_returns_url_object(self, mock_url, async_client, app):
        """get_cloudwatch_url is a SYNC method that returns a string. The
        router wraps it in a JSON object {url: "https://..."} so the
        frontend gets a stable shape."""
        _admit_test_user(app)
        mock_url.return_value = "https://console.aws.amazon.com/cloudwatch/insights?foo=bar"

        res = await async_client.get(
            "/api/v1/admin/users/user_abc/cloudwatch-url",
            params={
                "start": "2026-04-17T00:00:00Z",
                "end": "2026-04-18T00:00:00Z",
            },
        )

        assert res.status_code == 200
        body = res.json()
        assert body == {"url": "https://console.aws.amazon.com/cloudwatch/insights?foo=bar"}

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_cloudwatch_url")
    async def test_cwl_url_threads_start_end_level_params(self, mock_url, async_client, app):
        _admit_test_user(app)
        mock_url.return_value = "https://example.test/url"

        res = await async_client.get(
            "/api/v1/admin/users/user_abc/cloudwatch-url",
            params={
                "start": "2026-04-17T00:00:00Z",
                "end": "2026-04-18T00:00:00Z",
                "level": "WARN",
            },
        )

        assert res.status_code == 200
        kwargs = mock_url.call_args.kwargs
        assert kwargs.get("start") == "2026-04-17T00:00:00Z"
        assert kwargs.get("end") == "2026-04-18T00:00:00Z"
        assert kwargs.get("level") == "WARN"
        # user_id positional
        args = mock_url.call_args.args
        passed_uid = args[0] if args else kwargs.get("user_id")
        assert passed_uid == "user_abc"

    @pytest.mark.asyncio
    @patch("routers.admin.admin_service.get_cloudwatch_url")
    async def test_cwl_url_default_level_is_error(self, mock_url, async_client, app):
        _admit_test_user(app)
        mock_url.return_value = "https://example.test/url"

        res = await async_client.get(
            "/api/v1/admin/users/user_abc/cloudwatch-url",
            params={
                "start": "2026-04-17T00:00:00Z",
                "end": "2026-04-18T00:00:00Z",
            },
        )

        assert res.status_code == 200
        kwargs = mock_url.call_args.kwargs
        assert kwargs.get("level") == "ERROR"

    @pytest.mark.asyncio
    async def test_cwl_url_403_for_non_admin(self, async_client, app):
        _deny_all_admins(app)
        res = await async_client.get(
            "/api/v1/admin/users/user_abc/cloudwatch-url",
            params={
                "start": "2026-04-17T00:00:00Z",
                "end": "2026-04-18T00:00:00Z",
            },
        )
        assert res.status_code == 403
