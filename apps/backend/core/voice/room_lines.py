"""What a voice says in a room, shared by Phone, Meetings and Talk: the assistant's name, the room's cursor and the
agent lines after it."""

from pydantic import BaseModel, Field

from core.repositories import messaging_repo


async def assistant_name(owner_id: str) -> str:
    name = (((await messaging_repo.get_voice_agent(owner_id)) or {}).get("name") or "").strip()
    return name or "Lightbulb"


async def latest_seq(room_id: str) -> int:
    """The room's newest message seq: the first `/events` cursor, so a talk says only what comes after it."""
    rows = await messaging_repo.list_messages_before(room_id, 10**19, 1)
    return int(rows[-1]["messageSeq"]) if rows else 0


def _question_ref(row: dict) -> dict | None:
    """{taskId, qid} of a Worker's question line (task_questions.relay_question posts it in the origin thread)."""
    if row.get("kind") != "question":
        return None
    ref = next((t for t in row.get("taskRefs") or [] if t.get("qid") and t.get("taskId")), None)
    return {"taskId": ref["taskId"], "qid": ref["qid"]} if ref else None


async def agent_lines(room_id: str, after: int) -> dict:
    """The agent messages in the room's main chat after seq `after`, oldest first, and the next cursor. A thread's
    lines and progress lines are not said, except a Worker's question in a thread this room started (with its
    `question` ref: the next line said to the voice answers it, `answer_said_question`); the meeting never heard
    one and the task waited on it (8 Nov prod Meet). The voice says these word for word (huddle-agent mouth.py).
    `inReplyTo`: the human message a line answers (the voice matches it to that spoken ask); `action`: what the voice
    does with it (the phone's "transfer")."""
    from core.repositories import questions_repo

    rows, _ = await messaging_repo.list_messages(room_id, after, 100)
    said, owner = [], None
    for r in rows:
        text, question = r.get("editedText") or r.get("text") or "", _question_ref(r)
        if (r.get("author") or {}).get("kind") != "agent" or r.get("progress") or r.get("deletedAt"):
            continue
        if question:  # answered or closed (on its card, or by voice) before the voice got to it: never said
            owner = owner or ((await messaging_repo.get_room(room_id)) or {}).get("ownerId")
            q = await questions_repo.get_question(owner, question["taskId"], question["qid"]) if owner else None
            if (q or {}).get("state") != "open":
                continue
        if text.strip() and (question or not r.get("threadRootMessageId")):
            said.append({"id": r["messageId"], "seq": int(r["messageSeq"]), "text": text,
                         "waiting": bool(r.get("voiceWaiting", r.get("taskRefs") and r.get("kind") != "task_line")) if not question else False,
                         **({"taskIds": r.get("voiceTaskIds") or [ref["taskId"] for ref in r["taskRefs"]]} if r.get("voiceTaskIds") or r.get("taskRefs") else {}),
                         **({"question": question} if question else {}),
                         **{k: r[k] for k in ("inReplyTo", "action", "voiceDelivery") if r.get(k)}})  # fmt: skip
    return {"messages": said, "after": int(rows[-1]["messageSeq"]) if rows else after}


class QuestionRef(BaseModel):
    taskId: str = Field(min_length=1, max_length=64)
    qid: str = Field(min_length=1, max_length=64)
