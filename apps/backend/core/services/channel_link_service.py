"""Channel link service — member identity linking flow.

Reads OpenClaw's pairing file from EFS to extract the platform user ID
corresponding to a pairing code, adds the peer to the bot's allowFrom via
the locked EFS writer, and persists the link row in DynamoDB.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.repositories import channel_link_repo
from core.services.config_patcher import append_to_openclaw_config_list

logger = logging.getLogger(__name__)

_efs_mount_path = settings.EFS_MOUNT_PATH

PAIRING_CODE_TTL = timedelta(hours=1)


class ChannelLinkError(Exception):
    """Base class for channel link service errors."""


class PairingCodeNotFoundError(ChannelLinkError):
    """The pairing code was not found in the EFS pairing file or has expired."""


class PeerAlreadyLinkedError(ChannelLinkError):
    """The platform user ID is already linked to a different Clerk member."""


def _pairing_file_path(owner_id: str, provider: str) -> str:
    # OpenClaw writes this at `{workspace}/credentials/{provider}-pairing.json`.
    # Do NOT add a `.openclaw/` prefix — OpenClaw treats the workspace root
    # as the base and puts the credentials dir directly under it.
    return os.path.join(
        _efs_mount_path,
        owner_id,
        "credentials",
        f"{provider}-pairing.json",
    )


def _read_pairing_requests(owner_id: str, provider: str) -> list[dict]:
    """Read the pairing file and return its requests list.

    Returns [] if the file doesn't exist (pairing file is only created on
    first unknown DM). Non-existent is not an error — it means no unknown
    senders have DMed the bot yet.
    """
    path = _pairing_file_path(owner_id, provider)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            store = json.load(f)
    except OSError as e:
        logger.warning(
            "Pairing file I/O error for owner %s provider %s: %s",
            owner_id,
            provider,
            e,
        )
        return []
    except json.JSONDecodeError as e:
        logger.error(
            "Pairing file is not valid JSON for owner %s provider %s (will be treated as empty): %s",
            owner_id,
            provider,
            e,
        )
        return []
    requests = store.get("requests", [])
    return requests if isinstance(requests, list) else []


def _is_expired(created_at_iso: str) -> bool:
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    # Defensive: if OpenClaw ever emits a naive timestamp, treat it as UTC
    # rather than crashing on the offset-aware subtraction.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > PAIRING_CODE_TTL


async def complete_link(
    *,
    owner_id: str,
    provider: str,
    agent_id: str,
    code: str,
    member_id: str,
    linked_via: str = "settings",
) -> dict:
    """Complete the member-linking flow by matching a pairing code.

    Steps:
    1. Read the pairing file for this owner/provider from EFS.
    2. Find the entry with the given code (case-insensitive), unexpired.
    3. Extract the `id` field (the platform user ID).
    4. Append the platform user ID to the bot's allowFrom list.
    5. Write the link row to DynamoDB.

    Returns: {"status": "linked", "peer_id": <platform user id>}

    Raises:
        PairingCodeNotFoundError: if the code is missing or expired.
    """
    requests = await asyncio.to_thread(_read_pairing_requests, owner_id, provider)
    code_upper = code.strip().upper()

    match = None
    for req in requests:
        req_code = str(req.get("code", "")).strip().upper()
        if req_code != code_upper:
            continue
        created_at = str(req.get("createdAt", ""))
        if _is_expired(created_at):
            continue
        match = req
        break

    if match is None:
        raise PairingCodeNotFoundError(f"No pending pairing request for code {code_upper} on {provider}")

    peer_id = str(match.get("id", "")).strip()
    if not peer_id:
        raise PairingCodeNotFoundError(f"Pairing entry for code {code_upper} has no platform user id")

    # Check for an existing link row for this (owner, provider, agent, peer)
    existing = await channel_link_repo.get_by_peer(
        owner_id=owner_id,
        provider=provider,
        agent_id=agent_id,
        peer_id=peer_id,
    )
    if existing is not None:
        if existing.get("member_id") == member_id:
            logger.info(
                "Link already exists for member %s on %s peer %s — no-op",
                member_id,
                provider,
                peer_id,
            )
            return {"status": "already_linked", "peer_id": peer_id}
        raise PeerAlreadyLinkedError(f"Peer {peer_id} on {provider}/{agent_id} is already linked to another member")

    # Write allowFrom entry (dedup-aware append)
    await append_to_openclaw_config_list(
        owner_id,
        ["channels", provider, "accounts", agent_id, "allowFrom"],
        peer_id,
    )

    # Persist the link row
    await channel_link_repo.put(
        owner_id=owner_id,
        provider=provider,
        agent_id=agent_id,
        peer_id=peer_id,
        member_id=member_id,
        linked_via=linked_via,
    )

    logger.info(
        "Linked %s peer %s to member %s on owner %s (agent %s)",
        provider,
        peer_id,
        member_id,
        owner_id,
        agent_id,
    )

    return {"status": "linked", "peer_id": peer_id}
