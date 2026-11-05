"""A0 — parity: with the switch off (``sa-dev-off``) the API behaves as today (spec 09 §8.3; wave2-README G1).

Sam posts in ``#general``; the room agent answers as today (a cold runtime's first reply can take a minute);
``live-config`` carries no ``features``; the workspace has zero ``tasks`` rows. The CloudWatch side
(zero ``Lightbulb/SuperAgent`` metrics for that owner) stays a by-hand check, recorded as evidence text.
"""

from __future__ import annotations

import time
import uuid

from scripts.super_agent.acceptance.ctx import Result

LEGACY_REPLY_S = 120


async def a00(ctx) -> Result:
    off = ctx.for_ws("sa-dev-off")
    api = off.api("sam")
    cfg = (await api.get("/api/v1/messaging/live-config")).raise_for_status().json()
    rooms = (await api.get("/api/v1/messaging/rooms")).raise_for_status().json().get("rooms") or []
    general = next((r for r in rooms if (r.get("name") or "").lower() == "general"), None)
    if general is None:
        return Result(False, {"reason": "sa-dev-off has no #general"})
    room_id = general["roomId"]
    body = {
        "clientMessageId": str(uuid.uuid4()),
        "text": "Hi, we're launching the workshop in March.",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
    }
    posted = time.monotonic()
    first = (await api.post(f"/api/v1/messaging/rooms/{room_id}/messages", json=body)).raise_for_status().json()
    reply = None
    while reply is None and time.monotonic() - posted < LEGACY_REPLY_S:
        data = (await api.get(f"/api/v1/messaging/rooms/{room_id}/messages", params={"limit": 50})).raise_for_status()
        page = data.json()
        reply = next(
            (
                m
                for m in page.get("messages") or page.get("items") or []
                if (m.get("author") or {}).get("kind") == "agent"
                and m.get("messageSeq", 0) > first.get("messageSeq", 0)
            ),
            None,
        )
        if reply is None:
            await off.sleep(2)
    rows = await off.tasks_rows(off.ws)
    checks = {
        "live_config_has_no_features": "features" not in cfg,
        "room_agent_replied_as_today": reply is not None,
        "zero_tasks_rows": len(rows) == 0,
    }
    return Result(
        all(checks.values()),
        {
            "ws": off.ws,
            "roomId": room_id,
            "messageId": first.get("messageId"),
            "replyMessageId": (reply or {}).get("messageId"),
            "replyAuthorId": ((reply or {}).get("author") or {}).get("id"),
            "replySeconds": round(time.monotonic() - posted, 1),
            "tasksRows": len(rows),
            "metrics": "by hand: CloudWatch Lightbulb/SuperAgent has no datapoint for this owner during the run",
            **checks,
        },
    )
