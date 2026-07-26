"""Marketplace hires routed through Paperclip's approval surface.

A headless Friday cannot hire; she can only ask. The ask becomes a Paperclip
``hire_agent`` approval linked to the issue that prompted it, which lands in
the Teams Inbox "Needs Attention" band. The roster changes only on approve.
"""

from core.services.marketplace_hire import build_hire_approval_payload, listing_slug_of

_LISTING = {
    "slug": "pulse",
    "name": "Pulse",
    "summary": "Marketing intel and content research.",
    "tags": ["marketing", "content"],
    "price_cents": 0,
}


def test_payload_pins_the_openclaw_gateway_adapter():
    """Paperclip's approve path does ``String(payload.adapterType ?? "process")``.

    An agent created as "process" is unreachable over the openclaw gateway and
    fails silently at its first wake — long after the approval looked like it
    succeeded. This is the single most breakable thing in the flow.
    """
    payload = build_hire_approval_payload(listing=_LISTING, env="prod")
    assert payload["adapterType"] == "openclaw_gateway"
    assert payload["adapterConfig"]["url"] == "wss://ws-prod.isol8.co"


def test_payload_url_tracks_the_environment():
    assert build_hire_approval_payload(listing=_LISTING, env="dev")["adapterConfig"]["url"] == ("wss://ws-dev.isol8.co")


def test_payload_omits_agent_id():
    """With agentId absent, Paperclip CREATES the agent on approve rather than
    activating a pre-created one — so nothing exists in the roster until a
    human says yes."""
    assert "agentId" not in build_hire_approval_payload(listing=_LISTING, env="prod")


def test_payload_carries_the_listing_slug_for_the_deploy_step():
    """Paperclip makes the roster entry; the container artifact is ours. The
    slug is how the approve handler knows which artifact to deploy."""
    payload = build_hire_approval_payload(listing=_LISTING, env="prod")
    assert listing_slug_of(payload) == "pulse"


def test_payload_names_and_describes_the_agent_from_the_listing():
    payload = build_hire_approval_payload(listing=_LISTING, env="prod")
    assert payload["name"] == "Pulse"
    assert payload["title"] == "Marketing intel and content research."
    assert payload["capabilities"] == "marketing, content"


def test_payload_title_is_bounded():
    payload = build_hire_approval_payload(listing={**_LISTING, "summary": "x" * 400}, env="prod")
    assert len(payload["title"]) <= 120


def test_payload_tolerates_a_listing_with_no_tags():
    payload = build_hire_approval_payload(listing={"slug": "x", "name": "X"}, env="prod")
    assert payload["capabilities"] == ""
    assert payload["title"] == ""


def test_listing_slug_of_returns_none_for_a_non_marketplace_approval():
    """Every other approval type must flow through the approve handler
    untouched — the deploy hook is a no-op for them."""
    assert listing_slug_of({}) is None
    assert listing_slug_of({"metadata": {}}) is None
    assert listing_slug_of({"metadata": {"something_else": "x"}}) is None
