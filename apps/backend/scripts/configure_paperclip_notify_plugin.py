"""Push the isol8.notifications plugin's configJson into Paperclip.

Re-runnable ops tool (NOT a one-shot): run once per environment after the
plugin ships, and again whenever the webhook secret rotates. The plugin
worker reads config per event, so the new value takes effect immediately —
no Paperclip restart needed.

Why this exists: plugin workers cannot read env vars (five-key allowlist in
plugin-worker-manager) and plugin secret-refs are disabled in the fork, so
``POST /api/plugins/isol8.notifications/config`` — instance-admin only — is
the sole channel for handing the plugin its backend URL + bearer token.

Runs inside the backend container (``aws ecs execute-command``), which
already has ``PAPERCLIP_EVENTS_WEBHOOK_SECRET`` + ``PAPERCLIP_INTERNAL_URL``
+ ``ENVIRONMENT`` in env and the bootstrap-admin credentials reachable via
``paperclip_admin_session``::

    uv run python scripts/configure_paperclip_notify_plugin.py
"""

import asyncio
import logging
import sys

import httpx

from core.config import settings
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PLUGIN_KEY = "isol8.notifications"


def _backend_events_url(env: str) -> str:
    # Must be the PUBLIC hostname — the plugin's outbound fetch SSRF filter
    # rejects private/internal IPs, so PAPERCLIP_INTERNAL_URL-style hosts
    # can never work here.
    host = "api.isol8.co" if env == "prod" else f"api-{env}.isol8.co"
    return f"https://{host}/api/v1/webhooks/paperclip-events"


async def main() -> int:
    env = settings.ENVIRONMENT
    secret = settings.PAPERCLIP_EVENTS_WEBHOOK_SECRET
    if not env or not secret or not settings.PAPERCLIP_INTERNAL_URL:
        logger.error(
            "ENVIRONMENT, PAPERCLIP_EVENTS_WEBHOOK_SECRET and PAPERCLIP_INTERNAL_URL "
            "must all be set (run inside the backend container)"
        )
        return 1

    config = {
        "backendUrl": _backend_events_url(env),
        "bearerToken": secret,
    }
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        await admin.set_plugin_config(
            session_cookie=cookie,
            plugin_key=PLUGIN_KEY,
            config_json=config,
        )
    logger.info("plugin %s configured: backendUrl=%s", PLUGIN_KEY, config["backendUrl"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
