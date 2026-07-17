"""Public platform-info catalog for container-side agents.

Serves the LIVE list of connectable **integrations** and **channels** so the
baked ``isol8`` skill never has to hardcode (and drift on) a catalog-shaped
list. An agent inside a per-owner container curls
``$ISOL8_API_URL/api/v1/platform-info`` and gets the current connectable
services straight from the same registry that drives the Integrations tab.

Auth: **none**. The connectable catalog is non-sensitive — it is the same set
of services any logged-in owner sees on the Integrations/Channels tabs, and it
carries no per-owner state. Per-owner *connected* state is deliberately NOT
served here (see docs/superpowers/plans/2026-07-17-platform-info-live-read.md):
the agent already sees its connected integrations as tools.

Projection is mandatory. ``registry.all_providers()`` returns full ``Provider``
models carrying internal wiring (MCP commands/env, OAuth URLs, materializer
names, policy ARNs, package pins) that a public route must never disclose. The
handler builds each dict from a fixed field allowlist — never ``model_dump()``.
"""

from fastapi import APIRouter

from core.containers.config import CHANNEL_PROVIDER_IDS
from core.services.credentials import registry

router = APIRouter()


@router.get("")
async def get_platform_info() -> dict:
    """Public connectable catalog: integrations + channels. No auth."""
    integrations = [
        {
            "key": provider.key,
            "display_name": provider.display_name,
            "byo_required": provider.byo_required,
        }
        for provider in registry.all_providers().values()
    ]
    channels = [{"key": key, "display_name": key.title()} for key in CHANNEL_PROVIDER_IDS]
    return {"integrations": integrations, "channels": channels}
