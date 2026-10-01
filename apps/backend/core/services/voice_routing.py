"""Who handles a spoken request: a name you say → Jev → CEO (spec 2026-10-01 §4)."""

from __future__ import annotations

import re

from core.services import jev

_LEAD = r"^(?:(?:hey|ok|okay|so)[\s,]+)?"
_VERB = r"\b(?:ask|tell|get|have)\s+"


def named_agent(text: str, team: list[dict]) -> str | None:
    t = " ".join(text.lower().split())
    for m in sorted(team, key=lambda m: -len(m["name"])):  # longest name wins ("Customer Operations" over "Customer")
        name = re.escape(" ".join(m["name"].lower().split())).replace("\\ ", " ")
        if re.match(_LEAD + name + r"(?!\w)", t) or re.search(_VERB + name + r"(?!\w)", t):
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
