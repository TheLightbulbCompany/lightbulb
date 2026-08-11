"""Tests for the Automations Phase 2 trigger-seed service.

Mirrors the ``_resolver_env`` fake-collaborator idiom from
``test_paperclip_trigger_proxy.py``: patch ``PaperclipRepo`` (company
resolution), ``get_admin_session_cookie``, and ``PaperclipAdminClient`` so
nothing here touches a real Paperclip deployment.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.inbound_triggers import seed_trigger_routine


@pytest.fixture(autouse=True)
def mock_secret_put():
    """Every seeded stripe_edge trigger writes a small durable record via
    integration_secret_store.put — the stripe-edge-seeded marker. Autouse
    keeps every OTHER test in this file off real boto3; tests that care about
    the write itself take this fixture as an argument."""
    with patch("core.services.inbound_triggers.integration_secret_store.put", AsyncMock(return_value="arn")) as m:
        yield m


def _fake_admin(*, friday_id="friday_1", routine_id="rt_1", public_id="pub_1"):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[{"id": friday_id, "role": "ceo"}])
    admin.create_routine = AsyncMock(return_value={"id": routine_id})
    admin.create_routine_trigger = AsyncMock(return_value={"trigger": {"publicId": public_id}})
    admin.delete_routine = AsyncMock(return_value={})
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
async def test_seed_trigger_routine_stripe_guided(mock_secret_put):
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

    # The stripe-edge-seeded marker — the ONLY thing PUT
    # /automations/{public_id}/webhook-secret (routers/automations.py) may
    # gate on, NOT Paperclip signingMode == "none" (also Phase 1's own
    # deliberately-unsigned option). Written to the same Secrets Manager
    # slot the whsec_ itself later occupies.
    mock_secret_put.assert_awaited_once_with("owner_1", "stripe-trigger-pub_1", {"stripe_edge_seeded": True})


@pytest.mark.asyncio
async def test_seed_trigger_routine_stripe_marker_write_failure_rolls_back_routine(mock_secret_put):
    """If the stripe-edge-seeded marker can't be written, the routine+trigger
    already created upstream are an orphan the owner could never legitimately
    arm (PUT would always 400 not_a_stripe_edge_trigger) — roll back like any
    other post-creation failure."""
    admin = _fake_admin(routine_id="rt_orphan_marker")
    mock_secret_put.side_effect = RuntimeError("Secrets Manager throttled")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(RuntimeError, match="Secrets Manager throttled"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_marker"


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
    # google has no `inbound:` block, so it isn't a trigger source.
    with pytest.raises(ValueError, match="not wired as an Automations trigger source"):
        await seed_trigger_routine("owner_1", "google", "whatever")


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


@pytest.mark.asyncio
async def test_seed_trigger_routine_trigger_create_failure_rolls_back_routine():
    """create_routine_trigger throwing (timeout, upstream error) must not
    leave the routine it already created orphaned."""
    admin = _fake_admin(routine_id="rt_orphan_trigger")
    admin.create_routine_trigger = AsyncMock(side_effect=RuntimeError("upstream 500"))
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(RuntimeError, match="upstream 500"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_trigger"


@pytest.mark.asyncio
async def test_seed_trigger_routine_trigger_missing_public_id_rolls_back_routine():
    """A malformed create_routine_trigger response (no trigger.publicId) is
    also a trigger-create failure — the routine it already created must be
    rolled back the same way an outright exception would be."""
    admin = _fake_admin(routine_id="rt_orphan_malformed")
    admin.create_routine_trigger = AsyncMock(return_value={"trigger": {}})
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(RuntimeError, match="no trigger.publicId"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_malformed"
