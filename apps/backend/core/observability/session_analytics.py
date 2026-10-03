"""Content-free PostHog facts for a local session's durable transitions."""

from __future__ import annotations

import uuid
from typing import Literal

from core.observability.product_analytics import capture_product_event

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:local-session-journey")
Event = Literal[
    "session_opened",
    "local_session_reconnected",
    "local_session_command_admitted",
    "local_session_command_settled",
    "native_send_outcome",
    "local_session_team_control_changed",
]
Origin = Literal["web", "ios", "direct", "native"]
State = Literal["on", "off", "pending", "applied", "rejected", "unknown", "cancelled"]


def capture_session_event(
    row: dict,
    event: Event,
    key: str,
    *,
    actor_user_id: str | None = None,
    start_id: str | None = None,
    request_id: str | None = None,
    source_session_id: str | None = None,
    command_kind: Literal["send", "interrupt", "approve"] | None = None,
    origin: Origin | None = None,
    state: State | None = None,
    destination_kind: Literal["room", "person", "session"] | None = None,
    offline_queued: bool | None = None,
    own: bool | None = None,
    outcome_reason: Literal["cancelled", "expired", "rejected"] | None = None,
) -> None:
    """Only these opaque IDs and bounded enums can cross the analytics boundary.

    ``key`` distinguishes durable occurrences and is used only to derive the
    deterministic PostHog UUID. It never becomes an event property.
    """
    properties = {
        "workspace_id": row["ownerId"],
        "session_id": row["sessionId"],
        "provider": row["provider"],
    }
    for name, value in {
        "start_id": start_id,
        "request_id": request_id,
        "source_session_id": source_session_id,
        "command_kind": command_kind,
        "origin": origin,
        "state": state,
        "destination_kind": destination_kind,
        "offline_queued": offline_queued,
        "own": own,
        "outcome_reason": outcome_reason,
    }.items():
        if value is not None:
            properties[name] = value
    capture_product_event(
        distinct_id=actor_user_id or row["humanUserId"],
        event=event,
        properties=properties,
        event_uuid=str(uuid.uuid5(_NS, f"{row['ownerId']}:{row['sessionId']}:{event}:{key}")),
    )
