"""Public platform-info catalog for container-side agents.

Serves the LIVE list of connectable **integrations** and **channels** so the
baked ``isol8`` skill never has to hardcode (and drift on) a catalog-shaped
list. An agent inside a per-owner container curls
``$ISOL8_API_URL/api/v1/platform-info`` and gets the current connectable
services straight from the same registry that drives the Integrations tab.

Auth: **none**. The connectable catalog is non-sensitive — it is the same set
of services any logged-in owner sees on the Integrations/Channels tabs, and it
carries no per-owner state. Per-owner *connected* state is deliberately NOT
served here: the agent already sees its connected integrations as tools.

Projection is mandatory. ``registry.all_providers()`` returns full ``Provider``
models carrying internal wiring (MCP commands/env, OAuth URLs, materializer
names, policy ARNs, package pins) that a public route must never disclose. The
handler builds each dict from a fixed field allowlist — never ``model_dump()``.
"""

from fastapi import APIRouter

from core.constants import CHANNEL_PROVIDER_IDS
from core.services.credentials import apply_connection, registry

router = APIRouter()


@router.get("")
async def get_platform_info() -> dict:
    """Public connectable catalog: integrations + channels. No auth."""
    integrations = [
        {
            "key": provider.key,
            "display_name": provider.display_name,
            "byo_required": provider.byo_required,
            # The integration's rail: "mcp" (tools appear in the agent's tool
            # list once connected), "cli" (authenticated binary in the agent's
            # shell — never appears as tools), or "skill". Agents use this to
            # verify the RIGHT way per rail instead of concluding a connected
            # CLI integration is broken because no tool namespace showed up.
            # Safe to disclose: a literal enum, no wiring detail.
            "actor": provider.actor,
            # Gated off this environment → the owner cannot connect it directly;
            # the Integrations tab shows Request access instead.
            #
            # This route is public and OWNERLESS, so it evaluates only the env
            # half of the gate. An owner on the provider's owner_allowlist
            # therefore reads requestable=true here while their own tab offers
            # Connect. The skill tells agents to send owners to the Integrations
            # tab and describe what they find, rather than naming a button — so
            # this skew changes the wording, never the destination.
            "requestable": not registry.visible_in_env(provider),
        }
        for provider in registry.all_providers().values()
        # A gate still means HIDDEN unless the provider opts into being requested.
        # Without this, adding a half-wired provider gated `environments:
        # [local, dev]` would publish it on this unauthenticated route and have
        # agents pitch it, with no config knob left to suppress it.
        if (registry.visible_in_env(provider) or provider.offer_request_access)
        and apply_connection.runtime_supported(provider)
    ]
    channels = [{"key": key, "display_name": key.replace("_", " ").title()} for key in CHANNEL_PROVIDER_IDS]
    return {"integrations": integrations, "channels": channels}
