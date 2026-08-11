"""Tests for the Automations Phase 2 trigger-seeding router.

Mirrors ``test_connections.py``'s org-admin-gate idiom (``async_client`` =
personal owner user_test_123, ``async_client_org_admin`` / ``_org_member`` =
org_test_456 admin/non-admin).
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.services.credentials.stripe_edge import WEBHOOK_SECRET_FIELD, stripe_trigger_secret_provider
from routers import paperclip_trigger_proxy


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
        mock_seed.assert_awaited_once_with("user_test_123", "stripe", "invoice.payment_failed", repo=None)

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


class TestSetWebhookSecret:
    @pytest.mark.asyncio
    async def test_stores_secret_and_invalidates_proxy_cache(self, async_client):
        owner_id = "user_test_123"
        public_id = "pub_abc"
        # Seed a stale cache entry, as if a fire had already resolved (and
        # cached) "nothing stored" before the owner pasted their whsec_.
        paperclip_trigger_proxy._STRIPE_SECRET_CACHE[(owner_id, public_id)] = (time.monotonic(), None)
        try:
            with patch("routers.automations.integration_secret_store") as mock_store:
                mock_store.put = AsyncMock()
                response = await async_client.put(
                    f"/api/v1/automations/{public_id}/webhook-secret",
                    json={"webhook_secret": "whsec_abc123"},
                )

            assert response.status_code == 200
            mock_store.put.assert_awaited_once_with(
                owner_id,
                stripe_trigger_secret_provider(public_id),
                {WEBHOOK_SECRET_FIELD: "whsec_abc123"},
            )
            # The write must invalidate the SAME cache object the proxy reads
            # — otherwise a just-pasted secret is fail-open (unsigned-forward)
            # for up to the cache's TTL. This is the shared-object aliasing
            # in stripe_edge.py: both modules point at ONE dict.
            assert (owner_id, public_id) not in paperclip_trigger_proxy._STRIPE_SECRET_CACHE
        finally:
            paperclip_trigger_proxy._STRIPE_SECRET_CACHE.pop((owner_id, public_id), None)

    @pytest.mark.asyncio
    async def test_non_admin_org_member_forbidden(self, async_client_org_member):
        with patch("routers.automations.integration_secret_store") as mock_store:
            mock_store.put = AsyncMock()
            response = await async_client_org_member.put(
                "/api/v1/automations/pub_1/webhook-secret",
                json={"webhook_secret": "whsec_abc123"},
            )
        assert response.status_code == 403
        mock_store.put.assert_not_called()
