"""A spoken "yes" to a Needs-you card (voice-agents Task 15; audit I): it never answers the card. It marks the card
`pendingConfirm` and pushes a one-tap confirm to the approving member's app; the tap on the answer route
(`routers/questions.py`) is still the only answer. In a meeting anyone's yes is the inviting member's (the route
resolves who); in the app orb it is the signed-in member's.
"""

from typing import Literal

from botocore.exceptions import ClientError

from core.dynamodb import utc_now_iso
from core.repositories import questions_repo
from core.services import questions
from core.temporal.task_questions import _push_once


async def _pick(ws: str, user_id: str, hint: str) -> dict | None:
    """The open card the member may answer now that shares most words with `hint`; the newest on a tie."""
    needs, _ = await questions_repo.needs_for_user(ws, user_id)
    found = [await questions_repo.get_question(ws, n["taskId"], n["qid"]) for n in needs]
    open_qs = [q for q in found if q and q["state"] == "open" and await questions.can_answer_now(ws, q, user_id)]

    # ponytail: 5-letter word stems ("supply" ~ "supplies"); an embedding match if cards get mixed up
    def stems(text: str) -> set[str]:
        return {w[:5] for w in text.lower().split()}

    words = stems(hint)

    def score(q: dict) -> tuple[int, str]:
        return len(words & stems(f"{q['title']} {q.get('text', '')}")), q["createdAt"]

    return max(open_qs, key=score, default=None)


async def approve(
    ws: str,
    seat: Literal["app", "meet", "phone_owner"],
    speaker: dict,
    phrase: Literal["yes", "no", "undo"],
    hint: str,
    room_id: str | None = None,
) -> dict:
    """`speaker`: {user_id: the member whose yes this is, name: who said it, where: the meeting's title or ""}."""
    if phrase == "no":
        return {"say": "Say no in the app to decline."}  # a no needs no confirm card: the card stays open as it is
    if phrase == "undo":
        return {"say": "Nothing to undo."}  # ponytail: main has no undo window yet; wire it when the 10 s undo lands
    q = await _pick(ws, speaker["user_id"], hint)
    if q is None:
        return {"none": True}
    pending = {"by": speaker.get("name") or "", "seat": seat, "roomId": room_id, "at": utc_now_iso()}
    try:
        await questions_repo.set_pending_confirm(ws, q["taskId"], q["qid"], pending)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return {"none": True}  # the card closed between the pick and the mark
    where = f" in {speaker['where']}" if speaker.get("where") else ""
    await _push_once(
        f"qconfirm:{ws}:{q['qid']}:{speaker['user_id']}",  # one push per card; a second yes only refreshes the mark
        speaker["user_id"],
        "Confirm in one tap",
        f"Approve in one tap: {q['title']} (approved by voice{where})",
        {"source": "task_question", "taskId": q["taskId"], "qid": q["qid"]},
    )
    return {"confirm_sent": True, "title": q["title"]}
