"""Channel management router.

Exposes:
- GET /links/me — list caller's channel link status across bots
- POST /link/{provider}/complete — member self-link flow
- DELETE /link/{provider}/{agent_id} — member self-unlink
- DELETE /{provider}/{agent_id} — admin bot delete
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.auth import (
    AuthContext,
    get_current_user,
    require_org_admin,
    resolve_owner_id,
)
from core.observability.metrics import put_metric
from core.containers.config import read_openclaw_config_from_efs
from core.repositories import channel_link_repo
from core.services import channel_link_service
from core.services.admin_audit import audit_admin_action
from core.services.config_patcher import (
    delete_openclaw_config_path,
    delete_provider_level_channel_secrets,
    remove_from_openclaw_config_list,
)

logger = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_PROVIDERS = {"telegram", "discord", "slack"}
PAIRING_PROVIDER = "slack"


def _validate_provider(provider: str) -> str:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
    return provider


class LinkCompleteBody(BaseModel):
    agent_id: str
    code: str
    linked_via: str = "settings"


class PairingDecisionBody(BaseModel):
    channel: str
    account_id: str
    request_id: str


class PairingAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    channel: str
    channel_label: str = Field(alias="channelLabel")
    account_id: str = Field(alias="accountId")
    account_label: str | None = Field(default=None, alias="accountLabel")
    notify_supported: bool = Field(alias="notifySupported")


class PairingRequestSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    request_id: str = Field(alias="requestId")
    channel: str
    channel_label: str = Field(alias="channelLabel")
    account_id: str = Field(alias="accountId")
    account_label: str | None = Field(default=None, alias="accountLabel")
    sender_id: str = Field(alias="senderId")
    sender_label: str = Field(alias="senderLabel")
    metadata: dict[str, str] | None = None
    created_at: str = Field(alias="createdAt")
    last_seen_at: str = Field(alias="lastSeenAt")
    expires_at: str = Field(alias="expiresAt")
    notify_supported: bool = Field(alias="notifySupported")


class PairingLimits(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    pending_per_account: int = Field(alias="pendingPerAccount")
    ttl_ms: int = Field(alias="ttlMs")


class PairingRequestsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    accounts: list[PairingAccountSnapshot]
    requests: list[PairingRequestSnapshot]
    command_owner_configured: bool = Field(alias="commandOwnerConfigured")
    limits: PairingLimits


class PairingApproveResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    request_id: str = Field(alias="requestId")
    sender_id: str = Field(alias="senderId")
    notification: str
    command_owner_bootstrap: str = Field(alias="commandOwnerBootstrap")
    audit_status: str | None = None


class PairingDismissResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    request_id: str = Field(alias="requestId")
    sender_id: str = Field(alias="senderId")
    audit_status: str | None = None


def _authorized_channel_owner(auth: AuthContext = Depends(get_current_user)) -> str:
    require_org_admin(auth)
    return resolve_owner_id(auth)


async def _pairing_rpc(owner_id: str, method: str, params: dict) -> dict:
    """Call a channel-pairing RPC without exposing its transient code.

    The gateway's public pairing queue is intentionally keyed by an opaque
    request id plus immutable channel/account context.  The pairing code is
    never returned by these RPCs or accepted by this API.
    """
    from core.containers import get_ecs_manager, get_gateway_pool

    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if not container or not ip:
        raise HTTPException(status_code=503, detail="Agent container is not running")

    try:
        result = await get_gateway_pool().send_rpc(
            user_id=owner_id,
            req_id=f"channel-pairing-{uuid.uuid4().hex}",
            method=method,
            params=params,
            ip=ip,
            token=container["gateway_token"],
        )
    except Exception as exc:
        logger.warning("Channel pairing RPC %s failed for owner %s: %s", method, owner_id, exc)
        raise HTTPException(status_code=503, detail="Could not reach the agent pairing queue") from exc
    return result or {}


@router.get(
    "/pairing/requests",
    response_model=PairingRequestsResponse,
    summary="List pending channel pairing requests",
)
async def list_pairing_requests(owner_id: str = Depends(_authorized_channel_owner)):
    """Admin-only, code-free view of pending channel identity requests."""
    return await _pairing_rpc(owner_id, "channels.pairing.list", {"channel": PAIRING_PROVIDER})


@router.post(
    "/pairing/approve",
    response_model=PairingApproveResponse,
    summary="Approve a pending channel pairing request",
)
@audit_admin_action(
    "channel_pairing.approve",
    target_param="owner_id",
    capture_params=["owner_id", "body"],
)
async def approve_pairing_request(
    body: PairingDecisionBody,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    owner_id: str = Depends(_authorized_channel_owner),
):
    """Approve by opaque request id; no pairing code crosses this boundary."""
    if body.channel != PAIRING_PROVIDER:
        raise HTTPException(status_code=400, detail="Only Slack pairing requests are supported")
    return await _pairing_rpc(
        owner_id,
        "channels.pairing.approve",
        {
            "channel": PAIRING_PROVIDER,
            "accountId": body.account_id,
            "requestId": body.request_id,
            "notify": True,
        },
    )


@router.post(
    "/pairing/reject",
    response_model=PairingDismissResponse,
    summary="Reject a pending channel pairing request",
)
@audit_admin_action(
    "channel_pairing.reject",
    target_param="owner_id",
    capture_params=["owner_id", "body"],
)
async def reject_pairing_request(
    body: PairingDecisionBody,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    owner_id: str = Depends(_authorized_channel_owner),
):
    """Dismiss by opaque request id; the sender may request access again."""
    if body.channel != PAIRING_PROVIDER:
        raise HTTPException(status_code=400, detail="Only Slack pairing requests are supported")
    return await _pairing_rpc(
        owner_id,
        "channels.pairing.dismiss",
        {
            "channel": PAIRING_PROVIDER,
            "accountId": body.account_id,
            "requestId": body.request_id,
        },
    )


@router.get("/links/me", summary="List the caller's channel link status across all bots")
async def get_links_me(auth: AuthContext = Depends(get_current_user)):
    """List the caller's channel link status for every bot in their org.

    Reads the openclaw.json `channels.<provider>.accounts` map for each
    supported provider (telegram, discord, slack), then joins it against
    the caller's own channel-link rows to mark each bot as `linked` or not.

    Returns a dict shaped as:
        {
          "can_create_bots": bool,
          "telegram": [{"agent_id", "bot_username", "linked"}, ...],
          "discord":  [...],
          "slack":    [...],
        }
    """
    owner_id = resolve_owner_id(auth)
    member_id = auth.user_id

    config = await read_openclaw_config_from_efs(owner_id) or {}
    channels_cfg = config.get("channels", {}) if isinstance(config, dict) else {}

    # Look up all link rows for this member
    all_member_links = await channel_link_repo.query_by_member(member_id)
    links_for_owner = {
        (link["provider"], link["agent_id"]): link for link in all_member_links if link.get("owner_id") == owner_id
    }

    can_create_bots = (not auth.is_org_context) or auth.is_org_admin

    # Try to get live bot usernames from channels.status probe via the
    # gateway pool. This calls getMe() / /users/@me on each provider and
    # returns the actual bot handle (e.g. @Isol8DevBot). Falls back to
    # agent_id if the container isn't reachable or the probe fails.
    bot_usernames: dict[str, dict[str, str]] = {}  # provider -> accountId -> username
    try:
        from core.containers import get_ecs_manager, get_gateway_pool

        ecs_manager = get_ecs_manager()
        container, ip = await ecs_manager.resolve_running_container(owner_id)
        if container and ip:
            pool = get_gateway_pool()
            status = await pool.send_rpc(
                user_id=owner_id,
                # Unique per call — a constant id (this was previously
                # `links-me-{owner_id}`) collides in the per-owner pending-RPC
                # map when two of the owner's tabs probe at once. The pool now
                # namespaces the wire id defensively too, but don't ship a
                # constant one for future direct callers to trip over.
                req_id=f"links-me-{uuid.uuid4().hex}",
                method="channels.status",
                params={"probe": True},
                ip=ip,
                token=container["gateway_token"],
            )
            for prov, accts in (status or {}).get("channelAccounts", {}).items():
                if not isinstance(accts, list):
                    continue
                bot_usernames[prov] = {}
                for acct in accts:
                    if not isinstance(acct, dict):
                        continue
                    acct_id = acct.get("accountId", "")
                    probe = acct.get("probe") or {}
                    bot = probe.get("bot") or {} if isinstance(probe, dict) else {}
                    username = bot.get("username", "") if isinstance(bot, dict) else ""
                    if acct_id and username:
                        bot_usernames[prov][acct_id] = username
    except Exception as e:
        put_metric("channel.rpc", dimensions={"provider": "all", "status": "error"})
        logger.debug("channels.status probe failed for links/me (using fallback): %s", e)

    result: dict = {"can_create_bots": can_create_bots}
    for provider in ("telegram", "discord", "slack"):
        provider_cfg = channels_cfg.get(provider, {}) if isinstance(channels_cfg, dict) else {}
        accounts = provider_cfg.get("accounts", {}) if isinstance(provider_cfg, dict) else {}
        bots = []
        if isinstance(accounts, dict):
            for agent_id in accounts.keys():
                linked = (provider, agent_id) in links_for_owner
                live_name = bot_usernames.get(provider, {}).get(agent_id, "")
                bots.append(
                    {
                        "agent_id": agent_id,
                        "bot_username": live_name or agent_id,
                        "linked": linked,
                    }
                )
        result[provider] = bots
    return result


@router.post(
    "/link/{provider}/complete",
    summary="Complete the member-link flow by pasting the pairing code",
)
async def link_complete(
    provider: str,
    body: LinkCompleteBody,
    auth: AuthContext = Depends(get_current_user),
):
    """Consume a one-time pairing code to link the caller's identity to a bot.

    The user gets the pairing code by DMing the bot from a fresh channel
    account; OpenClaw writes the code to a per-owner pairing file on EFS.
    This endpoint validates the code, writes a `channel-links` row keyed by
    `(owner_id, provider, agent_id, peer_id)`, and appends the peer to the
    bot's `allowFrom` list in `openclaw.json` so the per-account-channel-peer
    DM scope routes future messages to the right member.

    Errors:
        404 PairingCodeNotFoundError — code missing/expired/already used.
        409 PeerAlreadyLinkedError — peer is already linked to another member.
    """
    _validate_provider(provider)
    owner_id = resolve_owner_id(auth)
    member_id = auth.user_id

    try:
        result = await channel_link_service.complete_link(
            owner_id=owner_id,
            provider=provider,
            agent_id=body.agent_id,
            code=body.code,
            member_id=member_id,
            linked_via=body.linked_via,
        )
    except channel_link_service.PairingCodeNotFoundError as e:
        put_metric("channel.configure", dimensions={"provider": provider, "status": "error"})
        raise HTTPException(status_code=404, detail=str(e))
    except channel_link_service.PeerAlreadyLinkedError as e:
        put_metric("channel.configure", dimensions={"provider": provider, "status": "error"})
        raise HTTPException(status_code=409, detail=str(e))
    put_metric("channel.configure", dimensions={"provider": provider, "status": "ok"})
    return result


@router.delete(
    "/link/{provider}/{agent_id}",
    summary="Unlink the caller's identity from a bot",
)
async def link_delete(
    provider: str,
    agent_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Remove the caller's own channel-link row for a specific bot.

    Looks up the caller's link row for `(owner_id, provider, agent_id)`,
    then removes the matching `peer_id` from the bot's `allowFrom` list in
    `openclaw.json` and deletes the DynamoDB row. Idempotent: returns
    `{"status": "not_linked"}` if no row exists for the caller.
    """
    _validate_provider(provider)
    owner_id = resolve_owner_id(auth)
    member_id = auth.user_id

    # Find the member's own row for this bot
    member_rows = await channel_link_repo.query_by_member(member_id)
    match = next(
        (
            row
            for row in member_rows
            if row.get("owner_id") == owner_id and row.get("provider") == provider and row.get("agent_id") == agent_id
        ),
        None,
    )
    if match is None:
        return {"status": "not_linked"}

    peer_id = match["peer_id"]

    await remove_from_openclaw_config_list(
        owner_id,
        ["channels", provider, "accounts", agent_id, "allowFrom"],
        predicate=lambda v: v == peer_id,
    )
    await channel_link_repo.delete(
        owner_id=owner_id,
        provider=provider,
        agent_id=agent_id,
        peer_id=peer_id,
    )
    return {"status": "unlinked"}


@router.delete(
    "/{provider}/{agent_id}",
    summary="Admin: delete a bot from an agent entirely",
)
async def admin_delete_bot(
    provider: str,
    agent_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Admin-only: completely remove a bot from an agent and sweep its links.

    Requires org admin (or personal-account caller). Performs:

    1. Deletes `channels.<provider>.accounts.<agent_id>` from `openclaw.json`.
    2. Strips any legacy provider-level orphan credentials on
       `channels.<provider>` (botToken/appToken/token/signingSecret) so a
       revoked "[default]" account can't keep crash-looping.
    3. Removes the routing binding that points `(provider, accountId)` at the
       agent.
    4. Sweeps every `channel-links` row for the same `(owner, provider,
       agent_id)` so member identities don't dangle on a bot that no longer
       exists.

    Returns the number of link rows swept, for observability.
    """
    _validate_provider(provider)
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)

    # Remove the account block
    await delete_openclaw_config_path(
        owner_id,
        ["channels", provider, "accounts", agent_id],
    )
    # Strip any legacy provider-level orphan credentials (botToken/appToken/…
    # written directly on channels.<provider>). OpenClaw reads those as a
    # synthetic "[default]" account that keeps crash-looping auth on revoked
    # creds; nothing else ever clears them.
    await delete_provider_level_channel_secrets(owner_id, provider)
    # Remove the binding that routes this (provider, accountId) to the agent
    await remove_from_openclaw_config_list(
        owner_id,
        ["bindings"],
        predicate=lambda b: (
            isinstance(b, dict)
            and b.get("match", {}).get("channel") == provider
            and b.get("match", {}).get("accountId") == agent_id
        ),
    )
    # Sweep channel-link rows for this bot
    count = await channel_link_repo.sweep_by_owner_provider_agent(
        owner_id=owner_id,
        provider=provider,
        agent_id=agent_id,
    )
    logger.info(
        "Admin %s deleted %s bot for agent %s in owner %s (swept %d links)",
        auth.user_id,
        provider,
        agent_id,
        owner_id,
        count,
    )
    return {"status": "deleted", "links_swept": count}
