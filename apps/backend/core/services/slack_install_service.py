"""Install/uninstall of the platform Slack app for one owner (spec §3.2–3.5).

Order on install matters: secret → routing row → task-def roll → config. The
container's SecretRef resolves ``process.env.SLACK_BOT_TOKEN_{TEAM}``; writing
the account block before the task-def carries the env var just leaves the
account ``configured_unavailable`` on the OLD task until the roll lands, which
is harmless — but rolling first means the NEW task boots with everything.
"""

from __future__ import annotations

import logging

import httpx

from core.containers import get_ecs_manager
from core.containers.ecs_manager import slack_env_var_name, slack_token_value_from
from core.repositories import channel_link_repo, integrations_repo, slack_installs_repo
from core.services.config_patcher import (
    delete_openclaw_config_path,
    patch_openclaw_config,
    remove_from_openclaw_config_list,
)
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


def account_block(team_id: str) -> dict:
    """``channels.slack.accounts.<team_id>`` for the platform app: http mode, one
    webhook path per account (the plugin's route registry is first-writer-
    wins), and SecretRefs — the bot token from its task-def env var, the
    signing secret from ``OPENCLAW_HOOK_TOKEN`` because the backend re-signs
    every forwarded request with the owner's hook_token (spec §3.2)."""
    return {
        "mode": "http",
        "webhookPath": f"/slack/events/{team_id}",
        "botToken": {"source": "env", "provider": "default", "id": slack_env_var_name(team_id)},
        "signingSecret": {"source": "env", "provider": "default", "id": "OPENCLAW_HOOK_TOKEN"},
        # Decision 3: Slack peers, not members — anyone in the workspace may DM.
        "dmPolicy": "open",
        "allowFrom": ["*"],
        # Channels. `ChannelAccountPolicyDefaults` materializes
        # groupPolicy="allowlist" onto every ACCOUNT that leaves it unset
        # (upstream src/config/zod-schema.channel-messaging-common.ts), and we
        # list no channels — so every channel message was dropped with an
        # ephemeral "not in its allowlist" notice only the mentioner ever saw.
        # That is the same materialize-on-account rule `dmPolicy` above already
        # has to override.
        "groupPolicy": "open",
        # Pinned, not inherited. "open" alone would make the bot answer EVERY
        # message in every channel it is in; what keeps it to @-mentions is
        # requireMention, whose true is a runtime `?? true` in the plugin
        # (monitor/context.ts), not a schema default. Leaving the safety half of
        # this pair implicit is the exact shape of the bug above.
        "requireMention": True,
        # Upstream default is disabled; the manifest declares /lightbulb.
        "slashCommand": {"enabled": True, "name": "lightbulb"},
    }


def route_binding(team_id: str, agent_id: str) -> dict:
    return {"type": "route", "agentId": agent_id, "match": {"channel": PROVIDER, "accountId": team_id}}


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
    arn = await integration_secret_store.put(owner_id, provider, {"bot_token": bot_token, "bot_user_id": bot_user_id})
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
        await get_ecs_manager().refresh_task_definition(
            owner_id,
            extra_secrets=[{"name": slack_env_var_name(team_id), "valueFrom": slack_token_value_from(arn)}],
        )
        await patch_openclaw_config(
            owner_id,
            {
                "channels": {
                    PROVIDER: {
                        "enabled": True,
                        "streaming": {"mode": "progress"},
                        "accounts": {team_id: account_block(team_id)},
                    }
                }
            },
            agent_for_account={team_id: agent_id},
        )
    except Exception:
        # Partial failure (task-def roll or config patch) must not leave an
        # active install row + a "Connected" card with no working config
        # behind — unwind to the same state as if the install never happened.
        # revoke_install already gets the ordering right (drop the task-def
        # secret entry before force-deleting the secret) — a hand-rolled
        # rollback here would risk deleting a secret the roll's own new task-
        # def revision still references.
        logger.exception("slack install: roll/patch failed owner=%s team=%s, rolling back", owner_id, team_id)
        await revoke_install(team_id)
        raise
    await _greet_installer(bot_token, installer_slack_user_id)
    logger.info("slack install applied owner=%s team=%s agent=%s", owner_id, team_id, agent_id)
    return row


async def revoke_install(team_id: str) -> bool:
    """``app_uninstalled`` / ``tokens_revoked`` / owner Disconnect: mark the row,
    drop the secret, remove the account + binding, sweep pairing rows. The
    container keeps running; Clerk and billing are untouched."""
    row = await slack_installs_repo.get(team_id)
    if not row:
        return False
    owner_id, agent_id = row["owner_id"], row.get("agent_id") or ""
    await slack_installs_repo.set_status(team_id, slack_installs_repo.STATUS_REVOKED)
    # Drop the task-def secrets: entry BEFORE the secret is force-deleted —
    # otherwise every later relaunch of this owner's task (Spot interruption,
    # OOM, resize) fails on a dead ARN. If the ECS roll itself fails, skip
    # ONLY the secret delete (an orphaned secret is harmless; a dead ARN in a
    # still-referencing task-def is not) — the rest of teardown must still run.
    try:
        await get_ecs_manager().refresh_task_definition(owner_id, drop_secrets=[slack_env_var_name(team_id)])
    except Exception:
        logger.exception(
            "slack revoke: task-def refresh failed owner=%s team=%s; skipping secret delete", owner_id, team_id
        )
    else:
        await integration_secret_store.delete(owner_id, secret_provider(team_id))
    await delete_openclaw_config_path(owner_id, ["channels", PROVIDER, "accounts", team_id])
    await remove_from_openclaw_config_list(
        owner_id,
        ["bindings"],
        predicate=lambda b: (
            isinstance(b, dict)
            and b.get("match", {}).get("channel") == PROVIDER
            and b.get("match", {}).get("accountId") == team_id
        ),
    )
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
