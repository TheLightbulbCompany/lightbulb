"""A Meet answer is read aloud by a voice fetched on its own route: `/session` would use up the agent's
one-time naming prompt (Codex on #3774)."""

import pytest

from core.services import voice_calls, voice_persona


@pytest.mark.asyncio
async def test_the_meeting_voice_is_read_without_claiming_the_naming_prompt(monkeypatch):
    async def owned(owner_id, room_id):
        return {"name": "general"}

    async def starters(owner_id):
        return {}

    async def roster(owner_id):
        return [{"id": "forge", "title": "Forge — Engineering & Reliability"}]

    async def reads(owner_id, agent_id):
        raise AssertionError("SOUL.md and the voice notes are not this route's business")

    async def claim(*a, **k):
        raise AssertionError("the naming prompt was claimed")

    monkeypatch.setattr(voice_calls, "_owned_room", owned)
    monkeypatch.setattr(voice_calls, "_starter_ids", starters)
    monkeypatch.setattr(voice_calls, "_session_reads", reads)
    monkeypatch.setattr(voice_calls.messaging, "load_agent_roster", roster)
    monkeypatch.setattr(voice_calls, "_claim_voice_asked", claim)
    out = await voice_calls.agent_meet_voice(owner_id="org_1", room_id="r1", agent_id="forge")
    assert out == {"voice": voice_persona.STARTER_MEET_VOICES["engineering"]}
