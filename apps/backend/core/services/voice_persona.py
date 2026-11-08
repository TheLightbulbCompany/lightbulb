"""Voice-call persona: who speaks, and the relay-everything rule (spec 2026-10-01 §3)."""

from __future__ import annotations

import hashlib


# ---- Option D: each agent is its own voice (spec 2026-10-02 §1, §2) ----------------------------

#: Never aster (not in the enum), bossa or tempo (Brazilian Portuguese), nor the nine older realtime
#: voices: untested on gpt-live-1, listen before they enter the pool.
VOICE_POOL = (
    "marin",
    "quartz",
    "ripple",
    "vesper",
    "willow",
    "stone",
    "gleam",
    "meridian",
    "beacon",
    "delta",
    "cinder",
)
STARTER_VOICES = {
    "ceo": "marin",
    "engineering": "beacon",
    "finance": "stone",
    "growth": "vesper",
    "analytics": "quartz",
    "customer_operations": "willow",
}
_HASH_POOL = tuple(v for v in VOICE_POOL if v not in STARTER_VOICES.values())


def _starter_role(agent_id: str, starter_ids: dict | None) -> str | None:
    return next((role for role, aid in (starter_ids or {}).items() if aid and aid == agent_id), None)


# A roster from before the starter six (the founder's) records no starter ids, but its agents hold the same
# jobs under their own titles ("Forge — Engineering & Reliability"). The first word found picks the role.
_TITLE_ROLES = (
    ("ceo", ("ceo", "chief executive")),
    ("customer_operations", ("customer",)),
    ("engineering", ("engineer",)),
    ("finance", ("financ",)),
    ("analytics", ("analytic",)),
    ("growth", ("growth", "distribution", "marketing")),
)


def _voice_role(agent_id: str, starter_ids: dict | None, title: str | None) -> str | None:
    """The starter role whose voice this agent takes: its recorded one; else, in a workspace that records no
    starters, the role its title names. ponytail: two such agents with one job share a voice."""
    if role := _starter_role(agent_id, starter_ids):
        return role
    if any((starter_ids or {}).values()) or not title:
        return None
    t = title.casefold()
    return next((role for role, words in _TITLE_ROLES if any(w in t for w in words)), None)


def agent_voice(agent_id: str, starter_ids: dict | None, title: str | None = None) -> str:
    """A starter's hand-picked role voice; any other agent hashes over the pool minus those."""
    role = _voice_role(agent_id, starter_ids, title)
    if role in STARTER_VOICES:
        return STARTER_VOICES[role]
    return _HASH_POOL[int(hashlib.sha256(agent_id.encode()).hexdigest(), 16) % len(_HASH_POOL)]
