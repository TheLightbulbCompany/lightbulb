"""The engine's contract types (engine-service plan; research `2026-11-20-engine-boundary.md` §6).

Products (Lightbulb Phone, Lightbulb Meetings) reach the engine only through these shapes: the operations in
`core.engine.api` take and return them, and the HTTP transport sends them as camelCase JSON. Pure data: nothing here
imports engine or product code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

#: Where a message came from when not the chat surface itself; carried on the stored message.
#: voice: said in a Live talk; meeting_notes: a meeting's action items, filed from its notes
MessageVia = Literal["lightbulb_mcp", "ideas", "front_desk", "text", "apps", "voice", "meeting_notes", "meeting_chat"]

#: The room `META` attributes the products write (call and meeting records, the call's token and live slot, a talk's
#: owner). The engine stores them and never reads them.
PRODUCT_META_FIELDS: frozenset[str] = frozenset({"call", "callToken", "liveSlot", "meeting", "ownerUserId"})
#: The trigger `ref` keys only products give meaning to. A trigger carrying one is a product's (`product_ref`): the
#: engine asks that product's hooks about it and otherwise leaves the keys alone.
PRODUCT_REF_KEYS: frozenset[str] = frozenset(
    {"callId", "from", "meetingBotId", "meetingFollowups", "meetingInCallAt", "meetingNotes", "utteranceId"}
)

#: The header that carries a run's `authorize` grant (`ContextAnswer.grant`) to the agent's product tools.
GRANT_HEADER = "X-Lightbulb-Meeting-Context-Grant"
#: The longest voice note the `voice_profile` tool takes (its route's request model checks it first).
VOICE_NOTE_TEXT_MAX = 300


def product_ref(trigger: dict | None) -> bool:
    """The trigger carries a product's `ref` keys."""
    return bool(PRODUCT_REF_KEYS & ((trigger or {}).get("ref") or {}).keys())


class EngineError(Exception):
    """Raised by every operation; the HTTP layer maps it 1:1 to the status and body the routes return today."""

    def __init__(self, code: str, status: int, detail: dict | None = None) -> None:
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail or {}


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Principal(Model):
    kind: Literal["member", "contact", "system"]
    id: str
    display_name: str | None = None


class QuestionRef(Model):
    task_id: str
    qid: str


# --- operations (`core.engine.api`, Task 4) ----------------------------------------------------------------------


class LineIn(Model):
    tenant_id: str
    conversation_id: str
    principal: Principal
    text: str
    idempotency_key: str
    via: MessageVia | None = None
    respond: Literal["auto", "none"] = "auto"
    answers: QuestionRef | None = None
    requester_user_id: str | None = None
    trigger_key: str | None = None
    hint: str | None = None
    origin: str | None = None
    product_ref: dict = {}
    in_reply_to: str | None = None
    system_attrs: dict | None = None
    #: a contact line's place in its conversation (a phone call's turn n): with the conversation and the text it is
    #: the line's request hash, so a retry of the same turn replays and a different text under its key conflicts
    turn: int | None = None
    #: a system line goes into room memory (`post_as_lightbulb`'s `remember`)
    remember: bool = True


class LineOut(Model):
    message_id: str
    seq: int | None = None  # not reported for a system line
    trigger_id: str | None = None
    task_id: str | None = None
    answered: bool = False


class StartIn(Model):
    tenant_id: str
    principal: Principal | None
    conversation_id: str
    trigger_id: str
    source: Literal["voice", "webhook"]
    lane: Literal["auto", "shared"] = "auto"
    title: str
    brief: str
    specialist: str | None = None
    ack: str | None = None
    origin: str | None = None
    confirm_all: bool = False
    ask: dict | None = None
    product_ref: dict = {}


class StartOut(Model):
    task_id: str
    trigger_id: str
    created: bool
    qid: str | None = None


class AnswerIn(Model):
    tenant_id: str
    task_id: str
    qid: str
    principal: Principal
    option_id: str
    text: str | None = None
    client_answer_id: str
    expect_version: int | None = None


class ActIn(Model):
    tenant_id: str
    task_id: str
    action: Literal["stop", "resolve", "reopen", "retry"]
    principal: Principal
    version: int | None = None


class EventIn(Model):
    tenant_id: str
    trigger_id: str
    source: str
    admission: Literal["respond", "candidate"]
    principal: Principal | None
    text: str
    conversation_id: str | None = None
    origin: str | None = None
    confirm_all: bool = False
    product_ref: dict = {}


class EventOut(Model):
    trigger_id: str
    created: bool
