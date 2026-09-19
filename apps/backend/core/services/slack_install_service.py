"""Install/uninstall of the platform Slack app for one owner (spec §3.2–3.5).

Order on install: secret → routing row → status. Each is idempotent and a
partial failure unwinds through ``revoke_install``.

**The container half is gone, and Slack transport with it (M5/LATER).** The
install used to also roll the owner's ECS task definition (so the container
resolved ``process.env.SLACK_BOT_TOKEN_{TEAM}``) and write a
``channels.slack.accounts.<team>`` block plus a route binding into
``openclaw.json``. A Hermes profile has neither, so what survives here is the
owner-visible half: the bot token in Secrets Manager, the install row (which
carries the agent this workspace routes to) and the Connected/Disconnected
status. Connect and Disconnect stay honest and reversible; nothing delivers a
Slack message to an agent until the Hermes-side channel rail exists.
"""

from __future__ import annotations

import logging

import httpx

from core.repositories import channel_link_repo, integrations_repo, slack_installs_repo
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "slack"

# The platform app's bot scopes = the OpenClaw plugin's canonical manifest
# (extensions/slack/src/setup-shared.ts buildSlackManifest), all 23.
# scripts/slack-app-manifest.yaml MUST list the same set (a test enforces it).
# DO NOT trim scopes without ALSO disabling the matching agent action in
# account_block()'s `actions` — the plugin's action gates check config, not
# granted scopes (createActionGate defaults every action to true), so a
# dropped scope fails `missing_scope` at first use instead of degrading.
BOT_SCOPES: list[str] = [
    "app_mentions:read",
    "assistant:write",
    "channels:history",
    "channels:read",
    "chat:write",
    "commands",
    "emoji:read",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "mpim:write",
    "pins:read",
    "pins:write",
    "reactions:read",
    "reactions:write",
    "usergroups:read",
    "users:read",
]


class SlackInstallConflict(Exception):
    """Another owner holds an active install for this workspace."""


def secret_provider(team_id: str) -> str:
    """``integration_secret_store`` provider segment — nested under ``slack/`` so
    the bare ``slack`` key is never claimed (the integrations rail's rule)."""
    return f"{PROVIDER}/{team_id}"


# Progress drafts: one channel message edited live during long turns instead of
# minutes of silence. `toolProgress` is OFF because it posts each tool's OUTPUT
# into the conversation — a Read of a bundled SKILL.md put the entire file,
# including the maintenance notes addressed to our own engineers, into the
# owner's shared company channel. The draft card still shows that work is
# happening; only the raw tool payloads are withheld.
async def _mark_connected(owner_id: str) -> None:
    await integrations_repo.put_status(owner_id, PROVIDER, "connected")


async def _mark_disconnected(owner_id: str) -> None:
    await integrations_repo.delete(owner_id, PROVIDER)


SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# Slack surfaces an app in a member's sidebar only once a conversation with it
# exists, so an install that posts nothing is invisible to the human who just
# performed it: they land back on the web app, see "Connected", switch to
# Slack, and find nothing. This DM IS the install's visible half — it opens the
# conversation, puts the app in the installer's sidebar, and proves the token
# works. Posting to a user id opens the IM (we hold chat:write + im:write).
INSTALL_GREETING = (
    "Lightbulb is connected to this workspace. :zap:\n\n"
    "Your agent is listening right here — send it a message in this DM. "
    "To use it in a channel, run `/invite @Lightbulb` there and @-mention it."
)


async def _greet_installer(bot_token: str, slack_user_id: str) -> None:
    """Best-effort welcome DM to whoever clicked Add to Slack. A greeting that
    fails must never fail the install — the workspace is already connected."""
    if not slack_user_id:
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(
                SLACK_POST_MESSAGE_URL,
                headers={"Authorization": f"Bearer {bot_token}"},
                json={"channel": slack_user_id, "text": INSTALL_GREETING},
            )
        body = resp.json()
        if not body.get("ok"):
            logger.warning("slack install: greeting rejected: %s", body.get("error"))
    except Exception:  # noqa: BLE001 — cosmetic step, never worth failing an install
        logger.exception("slack install: greeting failed")


async def apply_install(
    *,
    owner_id: str,
    agent_id: str,
    team_id: str,
    team_name: str,
    bot_token: str,
    bot_user_id: str,
    app_id: str,
    scopes: list[str],
    installer_slack_user_id: str,
    installer_member_id: str,
) -> dict:
    provider = secret_provider(team_id)
    await integration_secret_store.put(owner_id, provider, {"bot_token": bot_token, "bot_user_id": bot_user_id})
    secret_name = integration_secret_store._secret_name(owner_id, provider)
    row = await slack_installs_repo.put(
        team_id=team_id,
        owner_id=owner_id,
        agent_id=agent_id,
        team_name=team_name,
        bot_user_id=bot_user_id,
        app_id=app_id,
        scopes=scopes,
        installer_slack_user_id=installer_slack_user_id,
        installer_member_id=installer_member_id,
        token_secret_name=secret_name,
    )
    if row is None:
        # Somebody else's workspace. Don't leave our copy of their token behind.
        await integration_secret_store.delete(owner_id, provider)
        raise SlackInstallConflict(team_id)
    await _mark_connected(owner_id)

    try:
        await _greet_installer(bot_token, installer_slack_user_id)
    except Exception:
        # A partial failure must not leave an active install row + a "Connected" card behind
        # — unwind to the same state as if the install never happened.
        logger.exception("slack install: greet failed owner=%s team=%s, rolling back", owner_id, team_id)
        await revoke_install(team_id)
        raise
    logger.info("slack install applied owner=%s team=%s agent=%s", owner_id, team_id, agent_id)
    return row


async def revoke_install(team_id: str) -> bool:
    """``app_uninstalled`` / ``tokens_revoked`` / owner Disconnect: mark the row, drop the
    secret, sweep pairing rows. Clerk and billing are untouched."""
    row = await slack_installs_repo.get(team_id)
    if not row:
        return False
    owner_id, agent_id = row["owner_id"], row.get("agent_id") or ""
    await slack_installs_repo.set_status(team_id, slack_installs_repo.STATUS_REVOKED)
    # No task-def entry to drop first any more: the token is ours alone, so the delete is
    # unconditional.
    await integration_secret_store.delete(owner_id, secret_provider(team_id))
    if agent_id:
        await channel_link_repo.sweep_by_owner_provider_agent(owner_id=owner_id, provider=PROVIDER, agent_id=agent_id)
    # The owner has ONE integrations status row for Slack, shared by every
    # workspace. Only flip it to disconnected once no active install is left,
    # or a second workspace keeps transporting while catalog requirement
    # checks report Slack as missing.
    # list_for_owner reads the by-owner GSI, which is eventually consistent:
    # the row just flipped to revoked can still come back as active, so it is
    # excluded by team id rather than trusted.
    remaining = [
        r
        for r in await slack_installs_repo.list_for_owner(owner_id)
        if r.get("team_id") != team_id and r.get("status") == slack_installs_repo.STATUS_ACTIVE
    ]
    if not remaining:
        await _mark_disconnected(owner_id)
    logger.info("slack install revoked owner=%s team=%s remaining=%d", owner_id, team_id, len(remaining))
    return True
