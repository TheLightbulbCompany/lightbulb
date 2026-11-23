"""The shared agent address routes by the authenticated sender: their own tenant once they have claimed a team
address, else their one org membership; never a founder alias.

Rule before: shared mail went to the sender's one org. Rule after: to the sender's own tenant when they have one."""

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
    monkeypatch.setattr(inbound, "team_address", AsyncMock(return_value=None))  # no person has claimed an address
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


def test_workspace_path_does_not_pass_payload_identity(client, shared, monkeypatch):
    _, _, handler = shared
    monkeypatch.setattr(inbound, "is_team_address", AsyncMock(return_value=("org_business", "business")))
    body = fixtures._body(
        to=["agents@business.thelightbulb.company"], calendar="original", verified_sender_user_id="user_attacker"
    )
    assert fixtures._post(client, body).json()["status"] == "meeting_scheduled"
    assert handler.await_args.kwargs == {}


def test_shared_address_routes_to_sender_tenant(client, shared, monkeypatch):
    """A person who claimed a team address gets the shared mail they send in their own tenant; no org is read."""
    _, orgs, handler = shared
    own = {"user_a": "agents@alice.thelightbulb.company", "user_sender": "agents@bob.thelightbulb.company"}
    monkeypatch.setattr(inbound, "team_address", AsyncMock(side_effect=own.get))
    inbound.ensure_team_address.return_value = (own["user_sender"], "ok")
    inbound.get_by_address.return_value = {**fixtures._row(address=own["user_sender"]), "owner_id": "user_sender"}
    assert fixtures._post(client, payload()).json()["status"] == "meeting_scheduled"
    inbound.ensure_team_address.assert_awaited_once_with("user_sender", None)
    assert handler.await_args.args[0] == "user_sender"
    assert handler.await_args.kwargs == {"verified_sender_user_id": "user_sender"}
    orgs.assert_not_awaited()


def test_shared_address_never_routes_to_other_tenant(client, shared, monkeypatch):
    """B, with no address of their own and no org, mails the shared address: nothing reaches A's tenant."""
    _, orgs, handler = shared
    orgs.return_value = []
    monkeypatch.setattr(
        inbound, "team_address", AsyncMock(side_effect={"user_a": "agents@alice.thelightbulb.company"}.get)
    )
    assert fixtures._post(client, payload()).json()["status"] == "dropped_unregistered_sender"
    inbound.team_address.assert_awaited_once_with("user_sender")
    inbound.ensure_team_address.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.fixture
def own_address(monkeypatch, shared):
    """The sender has claimed a team address of their own: their tenant is the route."""
    address = "agents@bob.thelightbulb.company"
    monkeypatch.setattr(inbound, "team_address", AsyncMock(return_value=address))
    inbound.ensure_team_address.return_value = (address, "ok")
    inbound.get_by_address.return_value = {**fixtures._row(address=address), "owner_id": "user_sender"}
    return address


def test_payload_cannot_forge_verified_sender_identity(client, shared, own_address):
    _, _, handler = shared
    response = fixtures._post(client, payload(verified_sender_user_id="user_attacker"))
    assert response.json()["status"] == "meeting_scheduled"
    assert handler.await_args.kwargs == {"verified_sender_user_id": "user_sender"}


def test_wrong_mailbox_owner_is_retryable_and_never_delivered(client, shared, own_address):
    _, _, handler = shared
    inbound.get_by_address.return_value = {"owner_id": "user_wrong"}
    response = fixtures._post(client, payload())
    assert response.status_code == 503
    handler.assert_not_awaited()
    inbound.delete_event.assert_awaited_once_with(fixtures.MESSAGE_ID)
