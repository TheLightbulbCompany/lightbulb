"""Who handles a spoken request: a name you say → Jev → CEO (spec 2026-10-01 §4)."""

from __future__ import annotations

import re

from core.services import jev

_LEAD = r"^(?:hey|ok|okay|so)?[\s,]*"
_VERB = r"\b(?:ask|tell|get|have)\s+"


def named_agent(text: str, team: list[dict]) -> str | None:
    t = text.strip().lower()
    for m in team:
        name = re.escape(m["name"].lower())
        if re.match(_LEAD + name + r"\b", t) or re.search(_VERB + name + r"\b", t):
            return m["id"]
    return None


async def pick(owner_id: str, call: dict, utterance: str, transcript: str, ceo_id: str | None) -> str:
    team = call.get("team") or []
    if call.get("kind") != "room":
        return call["voiceAgentId"]
    ids = {m["id"] for m in team}
    named = named_agent(utterance, team)
    if named:
        return named
    chosen = await jev.pick_agent(
        owner_id, room_name=call.get("roomName") or "", team=team, text=utterance, recent=transcript
    )
    if chosen in ids:
        return chosen
    return ceo_id if ceo_id in ids else call["voiceAgentId"]
