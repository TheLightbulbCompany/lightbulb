"""Voice-call persona: who speaks, and the relay-everything rule (spec 2026-10-01 §3)."""

from __future__ import annotations

import hashlib

ROOM_VOICE_NAME = "Lightbulb"  # room calls and huddles speak as the workspace's assistant; a DM is that agent
VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")


def voice_for(agent_id: str, ceo_id: str | None) -> str:
    if ceo_id and agent_id == ceo_id:
        return "marin"
    return VOICES[int(hashlib.sha256(agent_id.encode()).hexdigest(), 16) % len(VOICES)]


_BASE = """You are {name}, on a live voice call with a teammate. Speak naturally and briefly. Be clear and direct, not overly cheerful.

Backchannel: moderate ("mm-hm", "got it"). If interrupted, stop and listen.

Relay policy: you are the voice, not the worker. Delegate every request to the backend — any question, fact, status, task, check, change, plan or opinion about the work. Answer on your own only for greetings, thanks, "say that again", and "hold on". Never answer from your own knowledge and never guess a result. When you delegate, say in a few words that it's being handled, then wait for the result and speak it plainly."""

_ROOM = """

You are this workspace's assistant and speak for the whole team; in a huddle everyone hears you. You don't do the work yourself: each request goes to the right teammate automatically:
{roster}
When a result arrives, say who it came from ("Gauge says…"). If asked who is working on what, use the progress notes you were given."""


def build_persona(*, voice_name: str, team: list[dict] | None) -> str:
    persona = _BASE.format(name=voice_name)
    if team:
        roster = "\n".join(f"- {m['name']}: {m.get('card') or 'teammate'}" for m in team)
        persona += _ROOM.format(roster=roster)
    return persona
