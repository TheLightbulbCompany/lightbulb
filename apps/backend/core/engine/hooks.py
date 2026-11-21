"""The engine's three calls into the products (research §6.2): `tool`, `context`, `event`.

Every process that runs engine code (the API, the Temporal worker, the work and provisioner Lambdas) registers the
products' implementations at start (`core.voice.engine_hooks.register()`); a hook called before that raises
`HookNotRegistered`, so a process that forgot fails loudly instead of quietly skipping a product. In process today;
the same shapes go over HTTP later. A hook's exception reaches the engine call site unchanged: inside an activity it
fails the activity (retried, so product handlers are idempotent on the ids they get), never an empty answer.

`tool`: the agent runtime's product tool routes (`routers/runtime_internal.py`, `routers/messaging.py`). The route
checks the hook token and, where the tool speaks for a task run, the attempt and its secret, then calls the tool; the
answer is the response body. A refusal is the HTTPException the route raised before. `routedTo` in an answer is not
sent: it names the `routedTo` keys the tool's line fills on `trigger_ids`, which the engine writes (the Router posts no
second line for those triggers).

`context` purposes and what an answer means:

- `router`, no `trigger`: the products' part of the Router's instructions. `text` goes where their rules stand,
  `flags["tools"]` after the engine's tool names, `flags["heads"]` maps a `ref` key to words added to the line of a
  trigger whose `ref` holds a truthy value under it.
- `router`, with the batch's first phone caller `trigger`: `text` is its Business profile, or None.
- `worker`: `task` (and its `trigger` for a caller's task): `text` is added to the brief (a caller's is fenced as the
  Business profile), `flags["memorySync"]` overrides the brief's memory sync; `allow=False` means no run, with
  `text` posted in the task room as the reason.
- `authorize`: `trigger` (asked only for a `product_ref` trigger): `allow=False` refuses it (`text` is why);
  `flags["grant"]` puts it in the Router run's grant. With `trigger_ids` and `run_id` instead: `grant` is the header
  value for the agent's product tools in that run.
- `clarification`: an `ask` on a `product_ref` trigger, `task` its task id, `message` the ask's text and options.
  `text` set: the product's clarification task (`text` its brief; `flags` its `question`, `title`, and the `line`
  said in the room); None: the plain ask line.
- `result_line`: a finished task's line in `conversation_id` (`message["text"]`): `text` is the line to post.
- `admit`: a message in a room whose META carries `PRODUCT_META_FIELDS` (`annotations`): `flags` carries `origin`
  and product `ref` keys for its trigger, and `respond` ("respond" or "candidate") when the product decides that.
- `voice_run`: `run_id`, a voice-start's run: `conversation_id` its room, `requester_user_id` who asked, `agent_id`;
  all None for a run the product does not know.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from core.engine.contract import Model

ToolName = Literal[
    "front_desk",
    "business_facts",
    "transfer_call",
    "meeting_context",
    "meeting_chat",
    "meeting_stop_transcribing",
    "voice_profile",
]


class ToolCall(Model):
    tenant_id: str
    task_id: str | None = None
    trigger_ids: list[str] = []
    #: the engine's rows for `trigger_ids`, read by the route after its checks (None for an unknown id)
    triggers: list[dict | None] = []
    run_id: str | None = None
    agent_id: str | None = None
    args: dict = {}


class ContextAsk(Model):
    tenant_id: str
    purpose: Literal[
        "router",
        "worker",
        "clarification",
        "authorize",
        "result_line",
        "admit",
        "post_target",
        "voice_run",
    ]
    conversation_id: str | None = None
    trigger: dict | None = None
    task: dict | None = None
    message: dict | None = None
    run_id: str | None = None
    agent_id: str | None = None
    trigger_ids: list[str] = []
    annotations: dict = {}


class ContextAnswer(Model):
    allow: bool = True
    text: str | None = None
    grant: str | None = None
    flags: dict = {}
    conversation_id: str | None = None
    agent_id: str | None = None
    requester_user_id: str | None = None


class EngineEvent(Model):
    type: Literal[
        "task.moved",
        "task.created",
        "trigger.dropped",
        "question.answered",
        "line.posted",
        "conversation.archived",
        "member.removed",
    ]
    tenant_id: str
    conversation_id: str | None = None
    task: dict | None = None
    trigger: dict | None = None
    to: str | None = None
    message: dict | None = None
    member_id: str | None = None
    question: dict | None = None
    answer: dict | None = None
    actor_user_id: str | None = None
    #: `task.moved`: the move gets a lifecycle line (not a person's Close/Stop/Reopen, not a watch going idle).
    announced: bool = False


class HookNotRegistered(RuntimeError):
    """A hook was called in a process that never registered the products' implementations."""


class NotAuthorized(ValueError):
    """A product refused a trigger (`authorize` answered allow=False) where the refusal fails the work."""


Tool = Callable[[ToolName, ToolCall], Awaitable[dict]]
Context = Callable[[ContextAsk], Awaitable[ContextAnswer]]
Event = Callable[[EngineEvent], Awaitable[None]]

_registered: tuple[Tool, Context, Event] | None = None


def register(*, tool: Tool, context: Context, event: Event) -> None:
    global _registered
    _registered = (tool, context, event)


def _hooks() -> tuple[Tool, Context, Event]:
    if _registered is None:
        raise HookNotRegistered("core.engine.hooks: no product hooks registered in this process")
    return _registered


async def tool(name: ToolName, call: ToolCall) -> dict:
    return await _hooks()[0](name, call)


async def context(ask: ContextAsk) -> ContextAnswer:
    return await _hooks()[1](ask)


async def event(e: EngineEvent) -> None:
    await _hooks()[2](e)
