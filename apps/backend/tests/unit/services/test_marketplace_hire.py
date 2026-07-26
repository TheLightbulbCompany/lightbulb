"""Marketplace hires routed through Paperclip's approval surface.

A headless Friday cannot hire; she can only ask. The ask becomes a Paperclip
``hire_agent`` approval linked to the issue that prompted it, which lands in
the Teams Inbox "Needs Attention" band. The roster changes only on approve.

This is separate from — and does not disturb — the direct-create flow that
seeds Friday/Atlas and lands marketplace deploys.
"""

from core.services.marketplace_hire import (
    LISTING_ID_KEY,
    build_hire_approval_payload,
    listing_slug_of,
)

_LISTING = {
    "listing_id": "81ab74f8-efc2-464c-8c62-706274b69639",
    "slug": "pulse",
    "name": "Pulse",
    "summary": "Marketing intel and content research.",
    "tags": ["marketing", "content"],
    "price_cents": 0,
}

# Stand-in for synthesize_openclaw_adapter's output. The device keypair is the
# load-bearing part.
_ADAPTER = {
    "url": "wss://ws-prod.isol8.co",
    "authToken": "tok",
    "devicePrivateKeyPem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
    "sessionKey": "user_abc",
}


def _payload(**over):
    listing = {**_LISTING, **over.pop("listing", {})}
    return build_hire_approval_payload(listing=listing, adapter_config=over.pop("adapter", _ADAPTER))


def test_payload_carries_the_full_adapter_config_including_the_device_key():
    """THE bug this whole path turns on.

    Paperclip builds the agent FROM this payload on approve
    (``agentsSvc.create(..., adapterConfig: payload.adapterConfig)``). Isol8
    mints and owns the ed25519 device keypair so the deviceId is stable and can
    be pre-registered in the container's paired.json; an operator device that
    is not pre-paired is rejected NOT_PAIRED and the agent sits broken until the
    next reprovision. Passing a bare {"url": ...} here is exactly that bug.
    """
    payload = _payload()
    assert payload["adapterConfig"] == _ADAPTER
    assert payload["adapterConfig"]["devicePrivateKeyPem"]


def test_payload_pins_the_openclaw_gateway_adapter():
    """Paperclip's approve path does ``String(payload.adapterType ?? "process")``,
    and a "process" agent is unreachable over the openclaw gateway."""
    assert _payload()["adapterType"] == "openclaw_gateway"


def test_payload_omits_agent_id():
    """With agentId absent, approve CREATES the agent; with it set, approve
    merely activates a pre-created one. Absent keeps nothing in the roster
    before the owner says yes."""
    assert "agentId" not in _payload()


def test_payload_carries_listing_id_for_the_deploy_step():
    """Slugs are non-unique — the post-approve artifact deploy resolves by
    listing_id."""
    assert _payload()["metadata"][LISTING_ID_KEY] == _LISTING["listing_id"]


def test_payload_names_and_describes_the_agent_from_the_listing():
    payload = _payload()
    assert payload["name"] == "Pulse"
    assert payload["title"] == "Marketing intel and content research."
    assert payload["capabilities"] == "marketing, content"


def test_payload_title_is_bounded():
    assert len(_payload(listing={"summary": "x" * 400})["title"]) <= 120


def test_payload_tolerates_a_listing_with_no_tags():
    payload = build_hire_approval_payload(listing={"slug": "x", "name": "X"}, adapter_config=_ADAPTER)
    assert payload["capabilities"] == ""
    assert payload["title"] == ""


def test_listing_slug_of_returns_none_for_a_non_marketplace_approval():
    """Every other approval type must flow through the approve handler
    untouched — the deploy hook is a no-op for them."""
    assert listing_slug_of({}) is None
    assert listing_slug_of({"metadata": {}}) is None
    assert listing_slug_of({"metadata": {"something_else": "x"}}) is None


def test_listing_slug_of_finds_a_marketplace_hire():
    assert listing_slug_of(_payload()) == "pulse"
