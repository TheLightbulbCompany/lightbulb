"""Tests for the Automations Phase 2 trigger-seeding router.

Mirrors ``test_connections.py``'s org-admin-gate idiom (``async_client`` =
personal owner user_test_123, ``async_client_org_admin`` / ``_org_member`` =
org_test_456 admin/non-admin).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest


class TestFromIntegration:
    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("routers.teams._guards.billing_repo")
    async def test_success_personal_owner(self, mock_billing, mock_seed, async_client):
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.return_value = {
            "routine_id": "rt_1",
            "trigger_public_id": "pub_1",
            "proxy_url": "http://localhost:8000/api/v1/hooks/t/user_test_123/pub_1",
            "guided": True,
            "instructions": "paste this",
        }

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={"provider": "stripe", "event_id": "invoice.payment_failed"},
        )
        assert response.status_code == 200
        assert response.json()["guided"] is True
        mock_seed.assert_awaited_once_with("user_test_123", "stripe", "invoice.payment_failed", resource=None)

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    async def test_non_admin_org_member_forbidden(self, mock_seed, async_client_org_member):
        response = await async_client_org_member.post(
            "/api/v1/automations/from-integration",
            json={"provider": "stripe", "event_id": "invoice.payment_failed"},
        )
        assert response.status_code == 403
        mock_seed.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("routers.teams._guards.billing_repo")
    async def test_trial_owner_blocked_before_seeding(self, mock_billing, mock_seed, async_client):
        """Preserves the trial-gate: no billing row means no always-on
        entitlement, and seeding must never run for a blocked owner — a
        webhook trigger is always-on automation the same as a routine's cron
        trigger (routers/teams/work.py's create_routine_trigger)."""
        mock_billing.get_by_owner_id = AsyncMock(return_value=None)

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={"provider": "stripe", "event_id": "invoice.payment_failed"},
        )
        assert response.status_code == 402
        mock_seed.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("routers.teams._guards.billing_repo")
    async def test_bad_input_surfaces_as_400(self, mock_billing, mock_seed, async_client):
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.side_effect = ValueError("unknown event 'nope'")

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={"provider": "stripe", "event_id": "nope"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("routers.teams._guards.billing_repo")
    async def test_resource_passes_through_to_seed(self, mock_billing, mock_seed, async_client):
        """The auto_github shape rides the same route — `resource` reaches the
        seed untouched (validation lives there, next to the GitHub call)."""
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.return_value = {
            "routine_id": "rt_1",
            "trigger_public_id": "pub_1",
            "proxy_url": "http://localhost:8000/api/v1/hooks/t/user_test_123/pub_1",
            "guided": False,
            "instructions": None,
            "secret_hint": None,
            "resource": "acme/site",
        }

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={"provider": "github", "event_id": "pull_request", "resource": "acme/site"},
        )
        assert response.status_code == 200
        assert response.json()["resource"] == "acme/site"
        mock_seed.assert_awaited_once_with("user_test_123", "github", "pull_request", resource="acme/site")


class TestGithubResources:
    """The repo-picker feed for auto_github. Org-admin gated like
    from-integration, but with NO always-on gate — listing repos is read-only,
    so no billing_repo patch is needed for the happy path."""

    @pytest.mark.asyncio
    @patch("routers.automations.list_github_repos", new_callable=AsyncMock)
    async def test_lists_repos(self, mock_repos, async_client):
        mock_repos.return_value = [
            {"id": "acme/site", "label": "acme/site"},
            {"id": "acme/api", "label": "acme/api"},
        ]
        response = await async_client.get("/api/v1/automations/providers/github/resources")
        assert response.status_code == 200
        assert response.json() == mock_repos.return_value
        mock_repos.assert_awaited_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.automations.list_github_repos", new_callable=AsyncMock)
    async def test_github_not_connected_404s(self, mock_repos, async_client):
        mock_repos.side_effect = LookupError("GitHub is not connected")
        response = await async_client.get("/api/v1/automations/providers/github/resources")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @patch("routers.automations.list_github_repos", new_callable=AsyncMock)
    async def test_github_api_failure_502s(self, mock_repos, async_client):
        mock_repos.side_effect = httpx.HTTPError("github down")
        response = await async_client.get("/api/v1/automations/providers/github/resources")
        assert response.status_code == 502

    @pytest.mark.asyncio
    @patch("routers.automations.list_github_repos", new_callable=AsyncMock)
    async def test_non_admin_org_member_forbidden(self, mock_repos, async_client_org_member):
        response = await async_client_org_member.get("/api/v1/automations/providers/github/resources")
        assert response.status_code == 403
        mock_repos.assert_not_awaited()


class TestVercelResources:
    """The project-picker feed for auto_vercel — the same parameterized
    resources route TestGithubResources exercises, dispatched by provider."""

    @pytest.mark.asyncio
    @patch("routers.automations.list_vercel_projects", new_callable=AsyncMock)
    async def test_lists_projects(self, mock_projects, async_client):
        mock_projects.return_value = [
            {"id": "prj_1", "label": "site"},
            {"id": "prj_2", "label": "api"},
        ]
        response = await async_client.get("/api/v1/automations/providers/vercel_cli/resources")
        assert response.status_code == 200
        assert response.json() == mock_projects.return_value
        mock_projects.assert_awaited_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.automations.list_vercel_projects", new_callable=AsyncMock)
    async def test_vercel_not_connected_404s(self, mock_projects, async_client):
        mock_projects.side_effect = LookupError("Vercel is not connected")
        response = await async_client.get("/api/v1/automations/providers/vercel_cli/resources")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @patch("routers.automations.list_vercel_projects", new_callable=AsyncMock)
    async def test_vercel_api_failure_502s(self, mock_projects, async_client):
        mock_projects.side_effect = httpx.HTTPError("vercel down")
        response = await async_client.get("/api/v1/automations/providers/vercel_cli/resources")
        assert response.status_code == 502

    @pytest.mark.asyncio
    async def test_unknown_provider_404s(self, async_client):
        response = await async_client.get("/api/v1/automations/providers/notaprovider/resources")
        assert response.status_code == 404
        assert response.json()["detail"] == "unknown_provider"


class TestSetWebhookSecret:
    """Verification is native in Paperclip now — arming a trigger means
    resolving public_id -> Paperclip's internal trigger id
    (``_lookup_trigger_detail``) and rotating that trigger's Paperclip
    secret to the caller-supplied value. No Isol8-side secret store,
    marker, or cache is touched anymore."""

    @pytest.fixture(autouse=True)
    def _paperclip_url(self, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.internal:3100")

    @pytest.mark.asyncio
    async def test_stores_secret_and_rotates_paperclip_trigger(self, async_client):
        owner_id = "user_test_123"
        public_id = "pubabc123456"

        with (
            patch(
                "routers.automations.paperclip_trigger_proxy._lookup_trigger_detail",
                AsyncMock(return_value={"id": "trig_internal_1", "publicId": public_id, "kind": "webhook"}),
            ) as mock_lookup,
            patch("routers.automations.get_admin_session_cookie", AsyncMock(return_value="s=1")),
            patch("routers.automations.PaperclipAdminClient") as mock_client_cls,
        ):
            mock_admin = mock_client_cls.return_value
            mock_admin.rotate_routine_trigger_secret = AsyncMock(return_value={})
            response = await async_client.put(
                f"/api/v1/automations/{public_id}/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )

        assert response.status_code == 200
        assert response.json() == {"status": "stored"}
        mock_lookup.assert_awaited_once_with(owner_id, public_id)
        mock_admin.rotate_routine_trigger_secret.assert_awaited_once_with(
            session_cookie="s=1", trigger_id="trig_internal_1", secret="whsec_abc123"
        )

    @pytest.mark.asyncio
    async def test_unknown_trigger_404s(self, async_client):
        """A public_id that doesn't resolve to a webhook trigger in the
        caller's own company (foreign owner, typo, deleted trigger) 404s
        instead of silently reporting success."""
        owner_id = "user_test_123"
        public_id = "notminepub1234"

        with (
            patch(
                "routers.automations.paperclip_trigger_proxy._lookup_trigger_detail",
                AsyncMock(return_value=None),
            ) as mock_lookup,
            patch("routers.automations.PaperclipAdminClient") as mock_client_cls,
        ):
            response = await async_client.put(
                f"/api/v1/automations/{public_id}/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )

        assert response.status_code == 404
        mock_lookup.assert_awaited_once_with(owner_id, public_id)
        mock_client_cls.return_value.rotate_routine_trigger_secret.assert_not_called()

    @pytest.mark.asyncio
    async def test_lookup_failure_returns_503(self, async_client):
        """The resolver failing (Paperclip down / admin session broken)
        fails closed, same posture as the fire-proxy's own binding gate."""
        public_id = "pubabc123456"

        with patch(
            "routers.automations.paperclip_trigger_proxy._lookup_trigger_detail",
            AsyncMock(side_effect=RuntimeError("paperclip down")),
        ):
            response = await async_client.put(
                f"/api/v1/automations/{public_id}/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_rotate_failure_returns_503(self, async_client):
        public_id = "pubabc123456"

        with (
            patch(
                "routers.automations.paperclip_trigger_proxy._lookup_trigger_detail",
                AsyncMock(return_value={"id": "trig_internal_1", "publicId": public_id, "kind": "webhook"}),
            ),
            patch("routers.automations.get_admin_session_cookie", AsyncMock(return_value="s=1")),
            patch("routers.automations.PaperclipAdminClient") as mock_client_cls,
        ):
            mock_admin = mock_client_cls.return_value
            mock_admin.rotate_routine_trigger_secret = AsyncMock(side_effect=RuntimeError("upstream 500"))
            response = await async_client.put(
                f"/api/v1/automations/{public_id}/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_expired_admin_session_retried_then_succeeds(self, async_client):
        """A 401 from the lookup (expired admin session) invalidates the cached
        session and retries once — same posture as the fire proxy — so a
        transient expiry doesn't surface as a 503 the owner has to retry."""
        from core.services.paperclip_admin_client import PaperclipApiError

        public_id = "pubabc123456"
        lookup = AsyncMock(
            side_effect=[
                PaperclipApiError("unauthorized", 401, {}),
                {"id": "trig_internal_1", "publicId": public_id, "kind": "webhook"},
            ]
        )
        with (
            patch("routers.automations.paperclip_trigger_proxy._lookup_trigger_detail", lookup),
            patch("routers.automations.invalidate_admin_session") as mock_invalidate,
            patch("routers.automations.get_admin_session_cookie", AsyncMock(return_value="s=1")),
            patch("routers.automations.PaperclipAdminClient") as mock_client_cls,
        ):
            mock_admin = mock_client_cls.return_value
            mock_admin.rotate_routine_trigger_secret = AsyncMock(return_value={})
            response = await async_client.put(
                f"/api/v1/automations/{public_id}/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )

        assert response.status_code == 200
        assert lookup.await_count == 2
        mock_invalidate.assert_called_once()
        mock_admin.rotate_routine_trigger_secret.assert_awaited_once_with(
            session_cookie="s=1", trigger_id="trig_internal_1", secret="whsec_abc123"
        )

    @pytest.mark.asyncio
    async def test_malformed_public_id_400s_before_any_lookup(self, async_client):
        with patch(
            "routers.automations.paperclip_trigger_proxy._lookup_trigger_detail",
            AsyncMock(),
        ) as mock_lookup:
            response = await async_client.put(
                "/api/v1/automations/not-alnum!/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )

        assert response.status_code == 400
        mock_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_admin_org_member_forbidden(self, async_client_org_member):
        with patch(
            "routers.automations.paperclip_trigger_proxy._lookup_trigger_detail",
            AsyncMock(),
        ) as mock_lookup:
            response = await async_client_org_member.put(
                "/api/v1/automations/pubabc123456/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )
        assert response.status_code == 403
        mock_lookup.assert_not_awaited()


class TestGoogleResources:
    """The account-picker feed for auto_google_watch — the same parameterized
    resources route TestGithubResources exercises, dispatched by provider."""

    @pytest.mark.asyncio
    @patch("routers.automations.list_google_accounts", new_callable=AsyncMock)
    async def test_lists_accounts(self, mock_accounts, async_client):
        mock_accounts.return_value = [
            {"id": "ada@example.com", "label": "ada@example.com"},
        ]
        response = await async_client.get("/api/v1/automations/providers/google/resources")
        assert response.status_code == 200
        assert response.json() == mock_accounts.return_value
        mock_accounts.assert_awaited_once_with("user_test_123")

    @pytest.mark.asyncio
    @patch("routers.automations.list_google_accounts", new_callable=AsyncMock)
    async def test_google_not_connected_404s(self, mock_accounts, async_client):
        mock_accounts.side_effect = LookupError("Google is not connected")
        response = await async_client.get("/api/v1/automations/providers/google/resources")
        assert response.status_code == 404
