"""What a voice says in a room, shared by Phone, Meetings and Talk: the assistant's name and the question a line
answers."""

from pydantic import BaseModel, Field

from core.voice import store


async def assistant_name(owner_id: str) -> str:
    name = (((await store.get_voice_agent(owner_id)) or {}).get("name") or "").strip()
    return name or "Lightbulb"


class QuestionRef(BaseModel):
    taskId: str = Field(min_length=1, max_length=64)
    qid: str = Field(min_length=1, max_length=64)
