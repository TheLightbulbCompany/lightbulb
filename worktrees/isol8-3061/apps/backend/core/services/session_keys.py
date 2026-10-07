"""Parsers for OpenClaw session keys."""

from __future__ import annotations

import uuid


def parse_lightbulb_session_key(session_key: str) -> dict[str, str] | None:
    """Parse exactly ``agent:<uuid>:lightbulb:channel:<room-uuid>``."""
    parts = session_key.split(":") if isinstance(session_key, str) else []
    if len(parts) != 5 or parts[0] != "agent" or parts[2:4] != ["lightbulb", "channel"]:
        return None
    try:
        agent_id = str(uuid.UUID(parts[1]))
        room_id = str(uuid.UUID(parts[4]))
    except ValueError:
        return None
    if agent_id != parts[1].lower() or room_id != parts[4].lower():
        return None
    return {"agent_id": agent_id, "room_id": room_id}


def parse_session_key(session_key: str) -> dict:
    """Parse an OpenClaw session key into its components.

    Shapes (from openclaw/src/routing/session-key.ts with dmScope=per-account-channel-peer):
      Personal webchat:  agent:<agentId>:main
      Org webchat:       agent:<agentId>:<clerk_user_id>
      Cron chat:         agent:<agentId>:cron:<cronId>
      Cron run event:    agent:<agentId>:cron:<cronId>:run:<runId>
      Channel DM:        agent:<agentId>:<channel>:<accountId>:direct:<peerId>
      Channel group:     agent:<agentId>:<channel>:group:<id>(:topic:<topicId>)?
      Channel room:      agent:<agentId>:<channel>:channel:<id>(:thread:<threadId>)?

    Returns dict with:
      - empty {} for malformed input
      - {agent_id, source} for webchat personal
      - {agent_id, source, member_id} for org webchat (member_id is the clerk user_id)
      - {agent_id, source, cron_id} for cron sessions (source="cron")
      - {agent_id, source, channel, peer_id} for channel DMs (source="dm")
      - {agent_id, source, channel, group_id} for channel groups (source="group")
      - {agent_id, source, channel, channel_id} for channel rooms (source="channel")
    """
    parts = session_key.split(":")
    # Hermes native Paperclip runs: paperclip:company:<co>:agent:<id>:(issue|run):<id>, the same
    # shape the bridge and routers/messaging.py validate. The only Paperclip shape still emitted.
    if len(parts) == 7 and parts[0] == "paperclip" and parts[1] == "company" and parts[3] == "agent":
        if parts[5] in ("issue", "run"):
            return {"agent_id": parts[4], "source": "paperclip", f"{parts[5]}_id": parts[6]}
        return {}
    if len(parts) < 3 or parts[0] != "agent":
        return {}
    agent_id = parts[1]

    # Cron sessions: agent:<agentId>:cron:<cronId>(:run:<runId>)?
    # Must check BEFORE webchat since 4-part cron keys would otherwise
    # fall through to channel parsing.
    if parts[2] == "cron":
        result = {
            "agent_id": agent_id,
            "source": "cron",
        }
        if len(parts) >= 4:
            result["cron_id"] = parts[3]
        return result

    # Paperclip adapter sessions: agent:<agentId>:paperclip(:issue:<id>|:run:<id>)?
    # (paperclip/packages/adapters/openclaw-gateway/src/server/execute.ts
    # resolveSessionKey). A real member id is always a Clerk "user_..." id,
    # never the literal "paperclip", so this marker is unambiguous. Must check
    # BEFORE webchat so the 3-part bare "agent:<id>:paperclip" fallback key
    # isn't misread as an org member named "paperclip". These sessions are
    # delivered to Teams via Paperclip's own event stream, NOT the web /chat
    # fan-out — see the source gate in the agent/chat event handlers.
    if parts[2] == "paperclip":
        result = {"agent_id": agent_id, "source": "paperclip"}
        if len(parts) >= 5 and parts[3] == "issue":
            result["issue_id"] = parts[4]
        elif len(parts) >= 5 and parts[3] == "run":
            result["run_id"] = parts[4]
        return result

    # Webchat: 3 parts (agent:<agentId>:<sessionName>)
    if len(parts) == 3:
        if parts[2] == "main":
            return {"agent_id": agent_id, "source": "webchat"}
        # Org webchat — parts[2] is a Clerk user_id
        return {
            "agent_id": agent_id,
            "source": "webchat",
            "member_id": parts[2],
        }

    # Webchat named session: agent:<agentId>:<userId>:<opaqueSessionId>
    # (4-part, server-built by build_chat_session_key). parts[2] is the Clerk
    # user_id (member routing target); parts[3] is the client-minted UUID.
    # Cron/paperclip 4-part keys are already handled above, and channel keys are
    # >=5 parts (group/room) or 6 (DM), so an exactly-4-part key here is
    # unambiguously a webchat session. Must classify as webchat so it is NOT
    # treated as isolated in _handle_message (which would drop streamed frames).
    if len(parts) == 4:
        return {
            "agent_id": agent_id,
            "source": "webchat",
            "member_id": parts[2],
        }

    # Channel DM (per-account-channel-peer):
    # agent:<agentId>:<channel>:<accountId>:direct:<peerId>
    # In our design accountId == agentId so we don't extract parts[3] separately.
    if len(parts) == 6 and parts[4] == "direct":
        return {
            "agent_id": agent_id,
            "source": "dm",
            "channel": parts[2],
            "peer_id": parts[5],
        }

    # Channel group: agent:<agentId>:<channel>:group:<id>(:topic:<topicId>)?
    if len(parts) >= 5 and parts[3] == "group":
        return {
            "agent_id": agent_id,
            "source": "group",
            "channel": parts[2],
            "group_id": parts[4],
        }

    # Channel room: agent:<agentId>:<channel>:channel:<id>(:thread:<threadId>)?
    if len(parts) >= 5 and parts[3] == "channel":
        return {
            "agent_id": agent_id,
            "source": "channel",
            "channel": parts[2],
            "channel_id": parts[4],
        }

    return {"agent_id": agent_id, "source": "unknown"}
