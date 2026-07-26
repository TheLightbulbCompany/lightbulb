"""Marketplace hires routed through Paperclip's approval surface.

A headless Friday cannot hire; she can only ask. The ask becomes a Paperclip
``hire_agent`` approval over an agent Isol8 pre-created and parked inert, linked
to the issue that prompted it. The roster moves only on approve.

Separate from — and does not disturb — the direct-create flow that seeds
Friday/Atlas and lands marketplace deploys.
"""

import pytest

from core.services import marketplace_hire
from core.services.marketplace_hire import (
    LISTING_ID_KEY,
    LISTING_SLUG_KEY,
    build_hire_approval_payload,
    hired_agent_id_of,
    listing_id_of,
    listing_slug_of,
)

_AGENT_ID = "3f1c9a20-0000-4000-8000-000000000001"
_LISTING = {
    "listing_id": "81ab74f8-efc2-464c-8c62-706274b69639",
    "slug": "pulse",
    "name": "Pulse",
    "summary": "Marketing intel and content research.",
    "tags": ["marketing", "content"],
    "price_cents": 0,
}


def _payload(**over):
    listing = {**_LISTING, **over.pop("listing", {})}
    return build_hire_approval_payload(listing=listing, agent_id=over.pop("agent_id", _AGENT_ID))


def test_payload_carries_agent_id():
    """The load-bearing field. Paperclip's approve RESPONSE never returns the id
    of an agent it created, so an approval without agentId would leave the
    install step unable to say which agent to give a workspace to. With it,
    approve runs activatePendingApproval and reject runs terminate."""
    assert _payload()["agentId"] == _AGENT_ID


def test_payload_omits_the_adapter_config():
    """Read only on the create branch, which agentId bypasses — and it holds a
    private key that should not get a second at-rest copy."""
    payload = _payload()
    assert "adapterConfig" not in payload
    assert "adapterType" not in payload


def test_payload_carries_listing_id_for_the_install():
    """Slugs are non-unique; the install resolves the artifact by listing_id."""
    assert _payload()["metadata"][LISTING_ID_KEY] == _LISTING["listing_id"]


def test_payload_names_and_describes_the_agent_from_the_listing():
    payload = _payload()
    assert payload["name"] == "Pulse"
    assert payload["title"] == "Marketing intel and content research."
    assert payload["capabilities"] == "marketing, content"


def test_payload_title_is_bounded():
    assert len(_payload(listing={"summary": "x" * 400})["title"]) <= 120


def test_payload_tolerates_a_listing_with_no_tags():
    payload = build_hire_approval_payload(listing={"slug": "x", "name": "X"}, agent_id=_AGENT_ID)
    assert payload["capabilities"] == ""
    assert payload["title"] == ""


def test_accessors_find_a_marketplace_hire():
    payload = _payload()
    assert listing_id_of(payload) == _LISTING["listing_id"]
    assert listing_slug_of(payload) == "pulse"
    assert hired_agent_id_of(payload) == _AGENT_ID


def test_accessors_return_none_for_a_non_marketplace_approval():
    """Every other approval type must flow through the approve handler
    untouched — the install is a no-op for them."""
    for payload in ({}, {"metadata": {}}, {"metadata": {"something_else": "x"}}, None):
        assert listing_id_of(payload) is None
        assert listing_slug_of(payload) is None
    assert hired_agent_id_of({"agentId": ""}) is None
    assert hired_agent_id_of({"agentId": 42}) is None


@pytest.mark.asyncio
async def test_deploy_for_approval_is_a_noop_for_other_approval_types():
    """A budget-override or CEO-strategy approval must not touch the container."""
    await marketplace_hire.deploy_for_approval(
        approval={"id": "a1", "payload": {"agentId": _AGENT_ID}}, owner_id="user_x"
    )


@pytest.mark.asyncio
async def test_deploy_for_approval_logs_loudly_on_a_mangled_marketplace_payload(caplog):
    """A hire recognisable as ours but missing what the install needs must NOT
    pass as a silent no-op — the owner would see "approved" and get an agent
    with no workspace. That silent-failure shape has bitten this codebase
    before."""
    await marketplace_hire.deploy_for_approval(
        approval={"id": "a1", "payload": {"metadata": {LISTING_SLUG_KEY: "pulse"}}},
        owner_id="user_x",
    )
    assert "NOT installed" in caplog.text
