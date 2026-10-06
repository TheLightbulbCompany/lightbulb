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

    async def members(room_id):
        return [{"kind": "agent", "id": "forge"}, {"kind": "human", "id": "u1"}]

    monkeypatch.setattr(voice_calls.messaging_repo, "list_members", members)
    monkeypatch.setattr(voice_calls, "_claim_voice_asked", claim)
    out = await voice_calls.agent_meet_voice(owner_id="org_1", room_id="r1", agent_id="forge")
    assert out == {"voice": voice_persona.STARTER_MEET_VOICES["engineering"]}
    # an agent removed from the room while its run finished gets no voice: the worker then says nothing
    with pytest.raises(Exception) as refused:
        await voice_calls.agent_meet_voice(owner_id="org_1", room_id="r1", agent_id="gone")
    assert "agent_not_in_room" in str(getattr(refused.value, "detail", refused.value))


@pytest.mark.asyncio
async def test_a_silent_session_keeps_the_naming_prompt_for_a_real_conversation(monkeypatch):
    """Codex on #3776: a raised-hand voice only says what it is given; it must not use the prompt up."""

    async def owned(owner_id, room_id):
        return {"name": "general"}

    async def members(room_id):
        return [{"kind": "agent", "id": "g1", "displayName": "Growth"}]

    async def starters(owner_id):
        return {"growth": "g1"}

    async def reads(owner_id, agent_id):
        return {"name": "Growth", "title": "Growth"}, None, None

    claims = []

    async def claim(owner_id, agent_id):
        claims.append(agent_id)
        return True

    monkeypatch.setattr(voice_calls, "_owned_room", owned)
    monkeypatch.setattr(voice_calls.messaging_repo, "list_members", members)
    monkeypatch.setattr(voice_calls, "_starter_ids", starters)
    monkeypatch.setattr(voice_calls, "_session_reads", reads)
    monkeypatch.setattr(voice_calls, "_claim_voice_asked", claim)
    silent = await voice_calls.agent_session(owner_id="org_1", room_id="r1", agent_id="g1", ask_name=False)
    assert silent["askName"] is False and claims == []
    asked = await voice_calls.agent_session(owner_id="org_1", room_id="r1", agent_id="g1")
    assert asked["askName"] is True and claims == ["g1"]
