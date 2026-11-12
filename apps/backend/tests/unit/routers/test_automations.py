"""Tests for the Automations Phase 2 trigger-seeding router.

Mirrors ``test_connections.py``'s org-admin-gate idiom (``async_client`` =
personal owner user_test_123, ``async_client_org_admin`` / ``_org_member`` =
org_test_456 admin/non-admin).
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestFromIntegration:
    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
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
        mock_seed.assert_awaited_once_with(
            "org_test_456",
            "stripe",
            ["invoice.payment_failed"],
            resource=None,
            topic=None,
            assignee_agent_id=None,
            routine_id=None,
            title=None,
            description=None,
            actor_user_id="user_test_123",
        )

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
    @patch("core.services.entitlement_guards.billing_repo")
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
        # The copy names what was gated: a webhook automation, not a scheduled routine.
        message = response.json()["detail"]["message"]
        assert "webhook" in message.lower() and "Scheduled routines" not in message
        mock_seed.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
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
    @patch("core.services.entitlement_guards.billing_repo")
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
        mock_seed.assert_awaited_once_with(
            "org_test_456",
            "github",
            ["pull_request"],
            resource="acme/site",
            topic=None,
            assignee_agent_id=None,
            routine_id=None,
            title=None,
            description=None,
            actor_user_id="user_test_123",
        )

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_topic_passes_through_to_seed(self, mock_billing, mock_seed, async_client):
        """The owner_setup shape (gmail.message) rides the same route —
        `topic` reaches the seed untouched (validation lives there, next to
        the gog call)."""
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.return_value = {
            "routine_id": "rt_1",
            "trigger_public_id": "pub_1",
            "proxy_url": "http://localhost:8000/api/v1/hooks/t/user_test_123/pub_1",
            "guided": False,
            "instructions": "one-time Pub/Sub setup",
            "secret_hint": None,
            "resource": "ada@example.com",
        }

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={
                "provider": "google",
                "event_id": "gmail.message",
                "resource": "ada@example.com",
                "topic": "projects/my-project/topics/isol8-gmail",
            },
        )
        assert response.status_code == 200
        assert response.json()["instructions"] == "one-time Pub/Sub setup"
        mock_seed.assert_awaited_once_with(
            "org_test_456",
            "google",
            ["gmail.message"],
            resource="ada@example.com",
            topic="projects/my-project/topics/isol8-gmail",
            assignee_agent_id=None,
            routine_id=None,
            title=None,
            description=None,
            actor_user_id="user_test_123",
        )

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_assignee_passes_through_to_seed(self, mock_billing, mock_seed, async_client):
        """An explicit assignee reaches the seed untouched — the tenancy check
        (is this agent in the caller's own company) lives there, next to the
        agent roster the seed already fetches."""
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.return_value = {"routine_id": "rt_1", "trigger_public_id": "pub_1"}

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={
                "provider": "stripe",
                "event_id": "invoice.payment_failed",
                "assignee_agent_id": "agent_support",
            },
        )
        assert response.status_code == 200
        mock_seed.assert_awaited_once_with(
            "org_test_456",
            "stripe",
            ["invoice.payment_failed"],
            resource=None,
            topic=None,
            assignee_agent_id="agent_support",
            routine_id=None,
            title=None,
            description=None,
            actor_user_id="user_test_123",
        )

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_routine_id_passes_through_to_seed(self, mock_billing, mock_seed, async_client):
        """The attach branch rides the same route under the same gates — the
        target routine reaches the seed untouched, where the tenancy check
        (is this routine in the caller's own company) lives next to the
        routine list it already fetches."""
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.return_value = {"routine_id": "rt_existing", "trigger_public_id": "pub_1", "guided": True}

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={
                "provider": "stripe",
                "event_id": "charge.refunded",
                "routine_id": "rt_existing",
            },
        )
        assert response.status_code == 200
        assert response.json()["routine_id"] == "rt_existing"
        mock_seed.assert_awaited_once_with(
            "org_test_456",
            "stripe",
            ["charge.refunded"],
            resource=None,
            topic=None,
            assignee_agent_id=None,
            routine_id="rt_existing",
            title=None,
            description=None,
            actor_user_id="user_test_123",
        )

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_edited_draft_passes_through_as_an_override(self, mock_billing, mock_seed, async_client):
        """What the owner typed over the pre-filled title/description reaches
        the seed; the untouched-draft path (every other test here) sends
        neither, so the registry template still wins there."""
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.return_value = {"routine_id": "rt_1", "trigger_public_id": "pub_1", "guided": True}

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={
                "provider": "stripe",
                "event_id": "invoice.payment_failed",
                "title": "Chase the failed invoice",
                "description": "Email the customer, then flag it to me.",
            },
        )
        assert response.status_code == 200
        assert mock_seed.await_args.kwargs["title"] == "Chase the failed invoice"
        assert mock_seed.await_args.kwargs["description"] == "Email the customer, then flag it to me."

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    async def test_attach_is_org_admin_gated_like_create(self, mock_seed, async_client_org_member):
        """Attaching a trigger to an existing automation is the same admin
        action as creating one — a non-admin member can't do it either."""
        response = await async_client_org_member.post(
            "/api/v1/automations/from-integration",
            json={"provider": "stripe", "event_id": "charge.refunded", "routine_id": "rt_x"},
        )
        assert response.status_code == 403
        mock_seed.assert_not_awaited()


class TestSetWebhookSecret:
    """Arming a trigger stores the secret and arms its HOOK# row (``automation_hooks.arm``);
    no HOOK# row is an unknown trigger. Paperclip is never called (Phase 1 task 7)."""

    @pytest.mark.asyncio
    async def test_arms_the_hook_row(self, async_client):
        with patch("routers.automations.automation_hooks.arm", AsyncMock(return_value=True)) as arm:
            response = await async_client.put(
                "/api/v1/automations/pubabc123456/webhook-secret", json={"webhook_secret": "whsec_abc123"}
            )
        assert response.status_code == 200 and response.json() == {"status": "stored"}
        arm.assert_awaited_once_with("org_test_456", "pubabc123456", "whsec_abc123")

    @pytest.mark.asyncio
    async def test_no_hook_row_404s(self, async_client):
        with patch("routers.automations.automation_hooks.arm", AsyncMock(return_value=False)):
            response = await async_client.put(
                "/api/v1/automations/notminepub1234/webhook-secret", json={"webhook_secret": "whsec_abc123"}
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_malformed_public_id_400s_before_any_lookup(self, async_client):
        with patch("routers.automations.automation_hooks.arm", AsyncMock()) as arm:
            response = await async_client.put(
                "/api/v1/automations/not-alnum!/webhook-secret", json={"webhook_secret": "whsec_abc123"}
            )
        assert response.status_code == 400
        arm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_admin_org_member_forbidden(self, async_client_org_member):
        with patch("routers.automations.automation_hooks.arm", AsyncMock()) as arm:
            response = await async_client_org_member.put(
                "/api/v1/automations/pubabc123456/webhook-secret", json={"webhook_secret": "whsec_abc123"}
            )
        assert response.status_code == 403
        arm.assert_not_awaited()


class TestMultiEventBody:
    """``event_ids`` is the multi-select shape; ``event_id`` is the deprecated
    one-event alias that maps into it. Sending both is ambiguous, so it is
    refused rather than silently picking one."""

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_event_ids_reaches_the_seed_as_a_list(self, mock_billing, mock_seed, async_client):
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.return_value = {"routine_id": "rt_1", "guided": True}

        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={
                "provider": "stripe",
                "event_ids": ["invoice.payment_failed", "charge.refunded", "invoice.payment_failed"],
            },
        )
        assert response.status_code == 200
        # Deduped, caller's order preserved — the first is the primary event.
        assert mock_seed.await_args.args[2] == ["invoice.payment_failed", "charge.refunded"]

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_both_spellings_at_once_is_refused(self, mock_billing, mock_seed, async_client):
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={"provider": "stripe", "event_id": "charge.refunded", "event_ids": ["charge.refunded"]},
        )
        assert response.status_code == 422
        mock_seed.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_neither_spelling_is_refused(self, mock_billing, mock_seed, async_client):
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        response = await async_client.post("/api/v1/automations/from-integration", json={"provider": "stripe"})
        assert response.status_code == 422
        mock_seed.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_empty_event_ids_is_refused(self, mock_billing, mock_seed, async_client):
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        response = await async_client.post(
            "/api/v1/automations/from-integration", json={"provider": "stripe", "event_ids": []}
        )
        assert response.status_code == 422
        mock_seed.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("routers.automations.seed_trigger_routine", new_callable=AsyncMock)
    @patch("core.services.entitlement_guards.billing_repo")
    async def test_conflicting_shapes_surface_as_400(self, mock_billing, mock_seed, async_client):
        """The seed's registration-shape conflict is caller-facing input, so
        it reaches the owner as a 400 naming what to split."""
        mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        mock_seed.side_effect = ValueError(
            "these events wire up differently and can't share one trigger: "
            "gmail.poll (poll), calendar.changed (auto_google_watch) — add them as separate triggers"
        )
        response = await async_client.post(
            "/api/v1/automations/from-integration",
            json={"provider": "google", "event_ids": ["gmail.poll", "calendar.changed"]},
        )
        assert response.status_code == 400
        assert "separate triggers" in response.json()["detail"]


@pytest.fixture(autouse=True)
def _no_hook_row(monkeypatch):
    """set_webhook_secret's Paperclip fallback: a trigger with no `HOOK#` row."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services import automation_hooks

    monkeypatch.setattr(automation_hooks, "arm", _AsyncMock(return_value=False))
