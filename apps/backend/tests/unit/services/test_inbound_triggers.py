"""Tests for the Automations Phase 2 trigger-seed service.

Mirrors the ``_resolver_env`` fake-collaborator idiom from
``test_paperclip_trigger_proxy.py``: patch ``PaperclipRepo`` (company
resolution), ``get_admin_session_cookie``, and ``PaperclipAdminClient`` so
nothing here touches a real Paperclip deployment.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.inbound_triggers import seed_trigger_routine


def _fake_admin(*, friday_id="friday_1", routine_id="rt_1", public_id="pub_1"):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[{"id": friday_id, "role": "ceo"}])
    admin.create_routine = AsyncMock(return_value={"id": routine_id})
    admin.create_routine_trigger = AsyncMock(
        return_value={"trigger": {"publicId": public_id}, "secretMaterial": {"secret": "s"}}
    )
    return admin


def _env(admin, *, company_id="c_1", company_status="active"):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id=company_id, status=company_status))
    repo.get_org_company_id = AsyncMock(return_value=company_id)
    return (
        patch("core.services.inbound_triggers.PaperclipRepo", return_value=repo),
        patch("core.services.inbound_triggers.get_admin_session_cookie", AsyncMock(return_value="s=1")),
        patch("core.services.inbound_triggers.PaperclipAdminClient", return_value=admin),
    )


@pytest.mark.asyncio
async def test_seed_trigger_routine_stripe_guided():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")

    assert out["guided"] is True
    assert out["routine_id"] == "rt_1"
    assert out["trigger_public_id"] == "pub_1"
    assert out["proxy_url"].endswith("/api/v1/hooks/t/owner_1/pub_1")
    assert "invoice.payment_failed" in out["instructions"]
    assert out["proxy_url"] in out["instructions"]


@pytest.mark.asyncio
async def test_seed_trigger_routine_builds_expected_bodies():
    """The routine and trigger bodies sent upstream — title/description carry
    the interpolated event label, the routine assigns to the resolved Friday
    id, and the webhook trigger is armed with Paperclip signingMode "none"
    (stripe_edge verifies at the Isol8 edge instead — see stripe_edge.py)."""
    admin = _fake_admin(friday_id="friday_xyz")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", "charge.refunded")

    routine_body = admin.create_routine.await_args.kwargs["body"]
    assert routine_body["title"] == "Stripe: Charge refunded"
    assert "Charge refunded" in routine_body["description"]
    assert routine_body["assigneeAgentId"] == "friday_xyz"
    assert admin.create_routine.await_args.kwargs["company_id"] == "c_1"

    trigger_call = admin.create_routine_trigger.await_args.kwargs
    assert trigger_call["routine_id"] == "rt_1"
    trigger_body = trigger_call["body"]
    assert trigger_body["kind"] == "webhook"
    # Registry signing_mode "stripe_edge" must NOT reach Paperclip verbatim —
    # it arms "none" because verification happens at the Isol8 edge instead.
    assert trigger_body["signingMode"] == "none"
    assert trigger_body["replayWindowSec"] == 300
    assert trigger_body["label"] == "Stripe: Charge refunded"


@pytest.mark.asyncio
async def test_seed_trigger_routine_bails_when_provider_has_no_inbound():
    with pytest.raises(ValueError, match="not wired as an Automations trigger source"):
        await seed_trigger_routine("owner_1", "github", "workflow_run")


@pytest.mark.asyncio
async def test_seed_trigger_routine_unknown_event_id():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="unknown event"):
        await seed_trigger_routine("owner_1", "stripe", "not.a.real.event")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_no_active_company():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin, company_status="seeding")
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="no active Paperclip company"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_uses_org_company_lookup_for_org_owner():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin, company_id="c_org")
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("org_abc", "stripe", "invoice.payment_failed")
    assert out["proxy_url"].endswith("/api/v1/hooks/t/org_abc/pub_1")
    assert admin.create_routine.await_args.kwargs["company_id"] == "c_org"


@pytest.mark.asyncio
async def test_seed_trigger_routine_no_friday_agent_found():
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[])
    admin.create_routine = AsyncMock()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="no CEO agent"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.create_routine.assert_not_awaited()
