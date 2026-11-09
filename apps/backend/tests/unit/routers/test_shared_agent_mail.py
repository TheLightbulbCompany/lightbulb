"""The shared agent address routes by authenticated membership, never a founder alias."""

from unittest.mock import AsyncMock

import pytest

from routers import ses_inbound as inbound
from tests.unit.routers import test_ses_inbound as fixtures

client = fixtures.client
_team_mailbox = fixtures._team_mailbox


@pytest.fixture
def shared(monkeypatch):
    fixtures._enable(monkeypatch)
    fixtures._dedup_recorded(monkeypatch)
    monkeypatch.setattr(inbound.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(inbound, "is_team_address", AsyncMock(return_value=None))
    lookup = AsyncMock(
        return_value={
            "id": "user_sender",
            "email_addresses": [{"email_address": fixtures.SENDER, "verification": {"status": "verified"}}],
        }
    )
    orgs = AsyncMock(return_value=[{"id": "org_business"}])
    monkeypatch.setattr(inbound.clerk_admin, "find_user_by_email", lookup)
    monkeypatch.setattr(inbound.clerk_admin, "list_user_organizations", orgs)
    monkeypatch.setattr(
        inbound, "ensure_team_address", AsyncMock(return_value=("agents@business.thelightbulb.company", "ok"))
    )
    monkeypatch.setattr(
        inbound,
        "get_by_address",
        AsyncMock(
            return_value={**fixtures._row(address="agents@business.thelightbulb.company"), "owner_id": "org_business"}
        ),
    )
    handler = AsyncMock(return_value="meeting_scheduled")
    monkeypatch.setattr(inbound.meeting_invites, "handle_team_mail", handler)
    monkeypatch.setattr(inbound, "delete_event", AsyncMock())
    return lookup, orgs, handler


def payload(**extra):
    return fixtures._body(to=["agents@thelightbulb.company"], calendar="original calendar invite", **extra)


def test_shared_routes_exactly_one_verified_membership(client, shared):
    lookup, orgs, handler = shared
    response = fixtures._post(client, payload())
    assert response.json()["status"] == "meeting_scheduled"
    lookup.assert_awaited_once_with(fixtures.SENDER, strict=True)
    orgs.assert_awaited_once_with("user_sender", limit=100, strict=True)
    assert handler.await_args.args[0] == "org_business"


def test_shared_ambiguous_memberships_are_explicit_and_never_delivered(client, shared):
    _, orgs, handler = shared
    orgs.return_value = [{"id": "org_business"}, {"id": "org_other"}]
    assert fixtures._post(client, payload()).json()["status"] == "dropped_ambiguous_workspace"
    handler.assert_not_awaited()


@pytest.mark.parametrize("verdicts", [{"dmarc": "FAIL"}, {"spf": "PASS", "spf_domain": "unrelated.example"}, {}])
def test_unproven_sender_never_queries_membership(client, shared, verdicts):
    lookup, orgs, handler = shared
    assert fixtures._post(client, payload(verdicts=verdicts)).json()["status"] == "dropped_failed_authentication"
    lookup.assert_not_awaited()
    orgs.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.parametrize(
    "user",
    [
        None,
        {
            "id": "user_sender",
            "email_addresses": [{"email_address": fixtures.SENDER, "verification": {"status": "unverified"}}],
        },
    ],
)
def test_unknown_or_unverified_sender_never_selects_workspace(client, shared, user):
    lookup, orgs, handler = shared
    lookup.return_value = user
    assert fixtures._post(client, payload()).json()["status"] == "dropped_unregistered_sender"
    orgs.assert_not_awaited()
    handler.assert_not_awaited()


def test_membership_outage_retries_and_rolls_back_dedup(client, shared):
    _, orgs, handler = shared
    orgs.side_effect = RuntimeError("Clerk temporary outage")
    response = fixtures._post(client, payload())
    assert response.status_code == 503
    inbound.delete_event.assert_awaited_once_with(fixtures.MESSAGE_ID)
    handler.assert_not_awaited()


def test_workspace_specific_address_does_not_use_shared_lookup(client, shared, monkeypatch):
    lookup, _, handler = shared
    monkeypatch.setattr(inbound, "is_team_address", AsyncMock(return_value=("org_business", "business")))
    assert (
        fixtures._post(client, fixtures._body(to=["agents@business.thelightbulb.company"], calendar="original")).json()[
            "status"
        ]
        == "meeting_scheduled"
    )
    lookup.assert_not_awaited()


def test_shared_routes_no_workspace_instead_of_personal_fallback(client, shared):
    _, orgs, handler = shared
    orgs.return_value = []
    assert fixtures._post(client, payload()).json()["status"] == "dropped_unregistered_sender"
    handler.assert_not_awaited()


def test_payload_cannot_forge_verified_sender_identity(client, shared):
    _, _, handler = shared
    response = fixtures._post(client, payload(verified_sender_user_id="user_attacker"))
    assert response.json()["status"] == "meeting_scheduled"
    assert handler.await_args.kwargs == {"verified_sender_user_id": "user_sender"}


def test_workspace_path_does_not_pass_payload_identity(client, shared, monkeypatch):
    _, _, handler = shared
    monkeypatch.setattr(inbound, "is_team_address", AsyncMock(return_value=("org_business", "business")))
    body = fixtures._body(
        to=["agents@business.thelightbulb.company"], calendar="original", verified_sender_user_id="user_attacker"
    )
    assert fixtures._post(client, body).json()["status"] == "meeting_scheduled"
    assert handler.await_args.kwargs == {}


def test_wrong_mailbox_owner_is_retryable_and_never_delivered(client, shared):
    _, _, handler = shared
    inbound.get_by_address.return_value = {"owner_id": "org_wrong"}
    response = fixtures._post(client, payload())
    assert response.status_code == 503
    handler.assert_not_awaited()
    inbound.delete_event.assert_awaited_once_with(fixtures.MESSAGE_ID)


def test_shared_alias_cannot_pin_every_sender_to_founder(client, shared):
    _, orgs, handler = shared
    orgs.return_value = [{"id": "org_other_business"}]
    inbound.get_by_address.return_value = {**fixtures._row(), "owner_id": "org_other_business"}
    assert fixtures._post(client, payload()).json()["status"] == "meeting_scheduled"
    assert handler.await_args.args[0] == "org_other_business"
    inbound.get_by_address.assert_awaited_once_with("agents@business.thelightbulb.company")


def test_first_shared_sender_cannot_self_authorize_a_closed_legacy_mailbox(client, shared, monkeypatch):
    from core.services import agent_email

    address = "agents@business.thelightbulb.company"
    state = {}

    async def put(**row):
        state.update(row)
        return dict(row)

    monkeypatch.setattr(inbound, "ensure_team_address", agent_email.ensure_team_address)
    monkeypatch.setattr(agent_email, "team_address", AsyncMock(return_value=address))
    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_email.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {
                    "send_to_anyone": False,
                    "verified_senders": ["owner@business.example"],
                }
            ]
        ),
    )
    monkeypatch.setattr(agent_email.agent_email_repo, "put", AsyncMock(side_effect=put))
    monkeypatch.setattr(inbound, "get_by_address", AsyncMock(side_effect=lambda _address: dict(state)))
    monkeypatch.setattr(inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver = AsyncMock(return_value="delivered")
    monkeypatch.setattr(inbound, "deliver_to_agent", deliver)

    response = fixtures._post(client, {**payload(), "calendar": None})
    assert response.json()["status"] == "stored_not_woken"
    assert state["verified_senders"] == ["owner@business.example"]
    deliver.assert_not_awaited()
