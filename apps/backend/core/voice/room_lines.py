"""What a voice says in a room, shared by Phone, Meetings and Talk: the assistant's name, the room's cursor and the
agent lines after it."""

from pydantic import BaseModel, Field

from core.engine import api
from core.repositories import messaging_repo


async def assistant_name(owner_id: str) -> str:
    name = (((await messaging_repo.get_voice_agent(owner_id)) or {}).get("name") or "").strip()
    return name or "Lightbulb"


async def latest_seq(room_id: str) -> int:
    """The room's newest message seq: the first `/events` cursor (`core.engine.api.latest_seq`)."""
    return await api.latest_seq(room_id)


async def agent_lines(room_id: str, after: int) -> dict:
    """The agent lines a voice says in the room after seq `after`, and the next cursor (`core.engine.api`)."""
    return await api.agent_lines(room_id, after)


class QuestionRef(BaseModel):
    taskId: str = Field(min_length=1, max_length=64)
    qid: str = Field(min_length=1, max_length=64)
