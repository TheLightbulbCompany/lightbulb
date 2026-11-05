"""The super-agent shared contract: every id builder, enum, row model, Router decision and GSI of the
`tasks` table (contracts-v2 §2). No I/O.

Specs cite names from here, never values. `scripts/export_tasks_schema.py` writes
`packages/shared/fixtures/super-agent/tasks-schema.json` from `export_schema()`; web, mobile and bridge tests
read that file, and `tests/unit/super_agent/test_tasks_schema.py` fails when it is stale.

Row keys: pk `WS#{ws}`, where `ws` is the workspace owner id (`org_…` / `user_…`). Rows (contracts-v2 §3, plus
`NEEDS#` restored by §14.2): Task, Attempt, Subscription + `THREAD#` lookup, Question, Needs, Effect, Trigger,
Schedule + `HOOK#` webhook pointer, Specialist (`AGENT#`), Project, Goal, Creation (`CAP#`). Cut rows have no builder on purpose.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any, ClassVar, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = 1

# --- Enums -----------------------------------------------------------------------------------------

# §4 stored statuses. "stopped" is `resolved` with `statusReason`.
TaskStatus = Literal["proposed", "queued", "working", "waiting", "idle", "resolved", "failed"]
# §4 shown groups: derived by clients from status + open questions + effects, never stored.
ShownGroup = Literal["waiting_on_you", "ready_for_review", "landing", "working", "idle", "resolved"]
# §5
AudienceKind = Literal["person", "room", "workspace"]
# C12: the Task row's lane; the Router's session lane key is `router_lane_key()`.
Lane = Literal["shared", "private"]
# C17 (union of 01/02/05)
AttemptOutcome = Literal[
    "completed",
    "failed",
    "cancelled",
    "runtime_lost",
    "refused",
    "waiting_approval",
    "checkpoint_failed",
    "unavailable",
]
AttemptStatus = Literal["dispatched", "running", "ended"]
SubscriptionCreator = Literal["router", "person", "schedule"]
# §3 Question: denied is an answer, not a state.
QuestionKind = Literal["question", "approval", "review"]
QuestionState = Literal["open", "answered", "cancelled"]
AskerKind = Literal["worker", "router", "system"]
# §3 Effect: proposed → approved/denied → intent → done/unknown.
EffectState = Literal["proposed", "approved", "denied", "intent", "done", "unknown"]
# §8 answer of POST /api/v1/internal/runtime/{owner}/effects
EffectDecision = Literal["proceed", "replay", "refuse", "needs_approval", "denied"]
# C17 (`preview` = app). An Output is the draft payload of a proposed Effect.
OutputKind = Literal["document", "file", "app", "pr", "mail", "link", "capability"]
# C5 + §14.1 voice + §14.6 text messages
TriggerSource = Literal[
    "chat",
    "task_result",
    "job_result",
    "schedule",
    "webhook",
    "manual",
    "mail",
    "a2a",
    "network",
    "voice",
    "text",
]
Admission = Literal["respond", "candidate"]
# C13
TaskInputKind = Literal["message", "job_completed", "delegation_completed", "answer", "watch", "network_result"]
# spec 06 §3.1
ScheduleStatus = Literal["draft", "active", "paused", "paused_system"]
# §18: a proactive rule is an automation row with a mode; it always starts in observe.
RuleMode = Literal["off", "observe", "auto"]
# §14.7: active = Jev candidate, observe = never runs.
ListenMode = Literal["active", "observe"]
# §3 + §13: skills only in v1.
CreationKind = Literal["skill"]
CreationStatus = Literal["draft", "active", "off"]
RouterDecisionKind = Literal["start_task", "propose_tasks", "pass_through", "reply", "ask", "ignore"]


# --- Ids and keys ----------------------------------------------------------------------------------

# Fixed forever: changing either re-keys every run and every Router-made task.
RUN_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://thelightbulb.company/ns/super-agent/run")
TASK_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://thelightbulb.company/ns/super-agent/task")


def new_task_id() -> str:
    return str(uuid.uuid4())


def router_task_id(batch_id: str, decision_index: int, k: int) -> str:
    """A task made by a Router decision: same id on an Activity retry."""
    return str(uuid.uuid5(TASK_NS, f"{batch_id}:{decision_index}:{k}"))


def run_id(ws: str, task_id: str, n: int) -> str:
    """Attempt n's bridge run id: a re-POST of the same attempt is a replay, not a second run."""
    return str(uuid.uuid5(RUN_NS, f"{ws}:{task_id}:{n}"))


#: A Worker's session on the workspace's VM (spec 02 §2.2): one per task, so an attempt resumes the last one's
#: transcript. The bridge's run route checks keys against this pattern (bridge/runs.py SESSION_KEY_RE).
WORKER_SESSION_KEY_PATTERN = r"^agent:[0-9a-f-]{36}:lightbulb:task:[0-9a-f-]{36}$"


def worker_session_key(agent_id: str, task_id: str) -> str:
    return f"agent:{agent_id}:lightbulb:task:{task_id}"


def router_lane_key(user_id: str) -> str:
    """The Router's private lane for one person; no raw user id in session keys (C12)."""
    return "u:" + hashlib.sha256(user_id.encode()).hexdigest()[:16]


# C5: the prefix and the parts each source's id is built from.
TRIGGER_ID_FORMATS: dict[str, str] = {
    "chat": "chat:{messageId}",
    "task_result": "task_result:{taskId}:{attempt}",
    "job_result": "job:{deliveryId}",
    "schedule": "sched:{scheduleId}:{epoch}",
    "webhook": "hook:{publicId}:{dedupeKey}",
    "manual": "manual:{scheduleId}:{ulid}",
    "mail": "mail:{messageId}",
    "a2a": "a2a:{a2aTaskId}",
    "network": "net:{recordId}",
    "voice": "voice:{callId}:{utteranceId}",
    "text": "text:{messageId}",
}


def trigger_id(source: str, *parts: object) -> str:
    fmt = TRIGGER_ID_FORMATS[source]
    prefix, *names = fmt.split(":")
    if len(parts) != len(names):
        raise ValueError(f"{source} trigger id takes {len(names)} part(s): {fmt}")
    return ":".join([prefix, *map(str, parts)])


def ws_pk(ws: str) -> str:
    return f"WS#{ws}"


def task_sk(task_id: str) -> str:
    return f"TASK#{task_id}"


def attempt_sk(task_id: str, n: int) -> str:
    return f"TASK#{task_id}#ATT#{n:04d}"


def subscription_sk(task_id: str, thread_key: str) -> str:
    return f"TASK#{task_id}#SUB#{thread_key}"


def thread_sk(thread_key: str) -> str:
    return f"THREAD#{thread_key}"


def question_sk(task_id: str, qid: str) -> str:
    return f"TASK#{task_id}#Q#{qid}"


def needs_sk(user_id: str, task_id: str, qid: str) -> str:
    return f"NEEDS#{user_id}#{task_id}#{qid}"


def effect_sk(idem_key: str) -> str:
    return f"EFFECT#{idem_key}"


def trigger_sk(trigger_id: str) -> str:
    return f"TRIG#{trigger_id}"


def schedule_sk(schedule_id: str) -> str:
    return f"SCHED#{schedule_id}"


def hook_sk(public_id: str) -> str:
    """spec 06 §3.5: webhook lookup pointer; the key itself binds the public id to the workspace."""
    return f"HOOK#{public_id}"


def agent_sk(agent_id: str) -> str:
    return f"AGENT#{agent_id}"


def project_sk(project_id: str) -> str:
    return f"PROJECT#{project_id}"


def goal_sk(goal_id: str) -> str:
    return f"GOAL#{goal_id}"


def creation_sk(cap_id: str) -> str:
    return f"CAP#{cap_id}"


def status_key(ws: str, status: str) -> str:
    return f"{ws}#{status}"


def task_workflow_id(ws: str, task_id: str) -> str:
    return f"task:{ws}:{task_id}"


def router_workflow_id(ws: str) -> str:
    return f"router:{ws}"


def schedule_workflow_id(ws: str, schedule_id: str) -> str:
    return f"sched:{ws}:{schedule_id}"


def task_room_id(task_id: str) -> str:
    """§6: every task's hidden conversation room."""
    return f"task:{task_id}"


KEY_FORMATS: dict[str, str] = {
    "pk": "WS#{ws}",
    "task": "TASK#{taskId}",
    "attempt": "TASK#{taskId}#ATT#{n:04}",
    "subscription": "TASK#{taskId}#SUB#{threadKey}",
    "thread": "THREAD#{threadKey}",
    "question": "TASK#{taskId}#Q#{qid}",
    "needs": "NEEDS#{userId}#{taskId}#{qid}",
    "effect": "EFFECT#{idemKey}",
    "trigger": "TRIG#{triggerId}",
    "schedule": "SCHED#{scheduleId}",
    "hook": "HOOK#{publicId}",
    "agent": "AGENT#{agentId}",
    "project": "PROJECT#{projectId}",
    "goal": "GOAL#{goalId}",
    "creation": "CAP#{capId}",
    "statusKey": "{ws}#{status}",
    "taskWorkflowId": "task:{ws}:{taskId}",
    "routerWorkflowId": "router:{ws}",
    "scheduleWorkflowId": "sched:{ws}:{scheduleId}",
    "taskRoomId": "task:{taskId}",
    "routerLaneKey": "u:{sha256(userId)[:16]}",
}


class Gsi(NamedTuple):
    name: str
    pk_attr: str
    sk_attr: str


# §3: both created with the table (CloudFormation adds at most one GSI per later update). `ws-meta` is
# sparse: spec 03's workspace META rows carry `metaKind` (Lane R, wave1-R Task 6).
GSIS: tuple[Gsi, ...] = (Gsi("by-status", "statusKey", "updatedAt"), Gsi("ws-meta", "metaKind", "pk"))


# --- Models ----------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


TaskId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
TriggerIds = Annotated[list[str], Field(min_length=1)]


class Audience(_Model):
    """§5: set at creation; question answerers are added when a question opens."""

    kind: AudienceKind
    user_ids: list[str] = []
    room_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Audience:
        if self.kind == "person" and not self.user_ids:
            raise ValueError("a person audience names at least one user")
        if self.kind == "room" and not self.room_id:
            raise ValueError("a room audience names its room")
        return self


class Usage(_Model):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class _Row(_Model):
    """Common row shape. `ws` becomes the pk; `type` names the row; writers own `version`."""

    ws: str = Field(exclude=True)
    version: int = 1
    created_at: str
    updated_at: str

    def sk(self) -> str:
        raise NotImplementedError

    def derived(self) -> dict[str, Any]:
        return {}

    def to_item(self) -> dict[str, Any]:
        return {
            "pk": ws_pk(self.ws),
            "sk": self.sk(),
            **self.model_dump(by_alias=True),
            **self.derived(),
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]):
        drop = {"pk", "sk", *cls.DERIVED}
        return cls.model_validate({k: v for k, v in item.items() if k not in drop} | {"ws": item["pk"][3:]})

    DERIVED: ClassVar[tuple[str, ...]] = ()


class Task(_Row):
    type: Literal["task"] = "task"
    task_id: TaskId
    identifier: str
    title: str = Field(max_length=200)
    description: str = Field("", max_length=8192)
    status: TaskStatus
    status_reason: str | None = None
    audience: Audience
    lane: Lane
    owner_user_id: str | None = None
    specialist: str | None = None
    project_id: str | None = None
    goal_id: str | None = None
    parent_task_id: str | None = None
    trigger_id: str
    policy_ref: str | None = None  # SCHED#{id} or CAP#{id}
    budget_usd: float | None = None  # §13: per-run automation budget
    hidden: bool = False  # §18 speculative work
    watch_until: str | None = None  # "Watching" when set
    attempt_count: int = 0
    usage: Usage | None = None
    resolved_at: str | None = None
    a2a_task_id: str | None = None
    ws_channel: str | None = None

    DERIVED: ClassVar[tuple[str, ...]] = ("statusKey", "workflowId")

    def sk(self) -> str:
        return task_sk(self.task_id)

    def derived(self) -> dict[str, Any]:
        return {"statusKey": status_key(self.ws, self.status), "workflowId": task_workflow_id(self.ws, self.task_id)}


class Attempt(_Row):
    type: Literal["attempt"] = "attempt"
    task_id: TaskId
    n: int = Field(ge=1)
    run_id: str
    agent_id: str
    session_id: str
    status: AttemptStatus
    outcome: AttemptOutcome | None = None
    error: str | None = None  # error class only, no text
    input_trigger_ids: list[str] = []
    usage: Usage | None = None
    started_at: str
    ended_at: str | None = None
    last_heartbeat_at: str | None = None

    def sk(self) -> str:
        return attempt_sk(self.task_id, self.n)


class Subscription(_Row):
    type: Literal["subscription"] = "subscription"
    task_id: TaskId
    # room:{roomId}:{rootMsgId} · room:{roomId} · mail:{threadId} · pr:{repo}#{n} · app:{appId}
    thread_key: str
    active: bool = True
    created_by: SubscriptionCreator

    def sk(self) -> str:
        return subscription_sk(self.task_id, self.thread_key)


class Thread(_Row):
    """`THREAD#` lookup, written with its Subscription: thread → task without GSI lag (C4)."""

    type: Literal["thread"] = "thread"
    thread_key: str
    task_id: TaskId
    active: bool = True
    history: list[str] = []  # prior task ids on this thread

    def sk(self) -> str:
        return thread_sk(self.thread_key)


class QuestionOption(_Model):
    id: str
    label: str


class AskedBy(_Model):
    kind: AskerKind
    specialist_id: str | None = None
    attempt: int | None = None


class Answer(_Model):
    option_id: str | None = None
    text: str | None = None


class Question(_Row):
    type: Literal["question"] = "question"
    task_id: TaskId
    qid: str
    kind: QuestionKind
    subkind: str | None = None
    title: str = Field(max_length=200)
    text: str = Field("", max_length=500)
    options: list[QuestionOption] = []
    asked_by: AskedBy
    answerers: list[str] = Field(min_length=1)  # stored at ask time
    state: QuestionState = "open"
    answer: Answer | None = None
    answered_by: str | None = None
    answered_at: str | None = None
    operands_hash: str | None = None
    effect_idem_key: str | None = None

    def sk(self) -> str:
        return question_sk(self.task_id, self.qid)


class Needs(_Row):
    """§14.2: one per answerer while a Question is open; Waiting on you, Activity, push and away email read it."""

    type: Literal["needs"] = "needs"
    user_id: str
    task_id: TaskId
    qid: str
    kind: QuestionKind
    subkind: str | None = None

    def sk(self) -> str:
        return needs_sk(self.user_id, self.task_id, self.qid)


class Output(_Model):
    kind: OutputKind
    title: str = Field(max_length=200)
    ref: dict[str, str] = {}  # {s3Key} / {appId} / {repo, pr} …


class Effect(_Row):
    """External, irreversible effects only (send, pay, merge, publish, provision, A2A)."""

    type: Literal["effect"] = "effect"
    idem_key: str
    task_id: TaskId
    attempt: int
    tool: str
    operands_hash: str
    state: EffectState
    question_id: str | None = None
    output: Output | None = None  # the draft payload while proposed
    result_ref: str | None = None

    def sk(self) -> str:
        return effect_sk(self.idem_key)


class Trigger(_Row):
    type: Literal["trigger"] = "trigger"
    trigger_id: str
    source: TriggerSource
    ref: dict[str, Any] = {}
    admission: Admission
    lane: str  # "shared" or router_lane_key(user)
    actor_user_id: str | None = None
    routed_to: dict[str, str] | None = None
    received_at: str

    def sk(self) -> str:
        return trigger_sk(self.trigger_id)


class Schedule(_Row):
    """An automation (spec 06); a proactive rule is the same row with a `mode` (§18)."""

    type: Literal["schedule"] = "schedule"
    schedule_id: str
    cron: list[str] = []  # 5-field, wall clock in `tz`; empty for event-only automations
    tz: str
    policy_text: str
    specialist: str | None = None
    budget_per_run_usd: float | None = None
    status: ScheduleStatus = "draft"
    paused_reason: str | None = None
    mode: RuleMode | None = None
    last_task_id: str | None = None
    name: str = Field(max_length=200)
    owner_user_id: str
    deliver_to: str = "none"  # room:{id} | dm:{userId} | none
    remaining_runs: int | None = None  # 1 = one-off
    active_run_task_id: str | None = None  # the run in flight (SKIP overlap claim)
    # History = the run task ids, newest first.
    # ponytail: capped at 50 on the row; a by-policy GSI if History ever needs more.
    run_task_ids: list[str] = []
    consecutive_failures: int = 0
    hook_public_ids: list[str] = []
    created_by: Literal["user", "router"] = "user"

    def sk(self) -> str:
        return schedule_sk(self.schedule_id)


class Hook(_Row):
    """spec 06 §3.5: one webhook trigger of an automation; append-only pointer, secret in Secrets Manager."""

    type: Literal["hook"] = "hook"
    public_id: str
    schedule_id: str
    provider: str
    events: list[str] = []  # empty = every event
    signing_mode: str
    signature_header: str | None = None
    replay_window_s: int = 300
    secret_ref: str  # Secrets Manager name, never the value
    armed: bool = False
    provider_hook_id: str | None = None
    account: str | None = None  # Google watch fields
    channel_id: str | None = None
    resource_id: str | None = None
    expires_at: str | None = None
    sig_fail_count: int = 0
    last_sig_fail_at: str | None = None

    def sk(self) -> str:
        return hook_sk(self.public_id)


class Agent(_Row):
    """Specialist; ids are today's agent ids (spec 03 owns the backfill)."""

    type: Literal["agent"] = "agent"
    agent_id: str
    name: str
    handle: str | None = None
    listen_mode: ListenMode = "active"
    skills: list[str] = []
    status: str = "active"

    def sk(self) -> str:
        return agent_sk(self.agent_id)


class Project(_Row):
    type: Literal["project"] = "project"
    project_id: str
    name: str
    status: str = "active"
    description: str = ""

    def sk(self) -> str:
        return project_sk(self.project_id)


class Goal(_Row):
    type: Literal["goal"] = "goal"
    goal_id: str
    name: str
    status: str = "active"
    description: str = ""

    def sk(self) -> str:
        return goal_sk(self.goal_id)


class Creation(_Row):
    type: Literal["creation"] = "creation"
    cap_id: str
    kind: CreationKind = "skill"
    status: CreationStatus = "draft"
    s3_key: str | None = None
    activated_by: str | None = None

    def sk(self) -> str:
        return creation_sk(self.cap_id)


ROW_MODELS: dict[str, type[_Row]] = {
    m.model_fields["type"].default: m
    for m in (
        Task,
        Attempt,
        Subscription,
        Thread,
        Question,
        Needs,
        Effect,
        Trigger,
        Schedule,
        Hook,
        Agent,
        Project,
        Goal,
        Creation,
    )
}


# --- Router decisions (01 §5.3 + C11) ---------------------------------------------------------------


class _Decision(_Model):
    trigger_ids: TriggerIds
    reason: str | None = None


class ProposedTask(_Model):
    title: str = Field(max_length=200)
    brief: str
    specialist: str | None = None


class StartTask(_Decision):
    kind: Literal["start_task"]
    title: str = Field(max_length=200)
    brief: str
    specialist: str | None = None
    ack: str
    visibility: AudienceKind | None = None
    parent_task_id: TaskId | None = None
    policy_ref: str | None = None
    budget_usd: float | None = None
    previous_run_task_id: TaskId | None = None


class ProposeTasks(_Decision):
    kind: Literal["propose_tasks"]
    tasks: list[ProposedTask] = Field(min_length=1)
    ack: str


class PassThrough(_Decision):
    kind: Literal["pass_through"]
    task_id: TaskId
    ack: str | None = None


class Reply(_Decision):
    kind: Literal["reply"]
    text: str


class Ask(_Decision):
    """A clarifying question in chat. It never approves anything (§8: only answering a Question row does)."""

    kind: Literal["ask"]
    text: str
    options: list[str] = []


class Ignore(_Decision):
    kind: Literal["ignore"]


RouterDecision = Annotated[
    StartTask | ProposeTasks | PassThrough | Reply | Ask | Ignore,
    Field(discriminator="kind"),
]


class RouterDecisions(_Model):
    decisions: list[RouterDecision]


# --- Export ----------------------------------------------------------------------------------------


def _values(literal: Any) -> list[str]:
    return list(literal.__args__)


def export_schema() -> dict[str, Any]:
    """The JSON clients and the bridge assert against. Plain data, stable ordering."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "enums": {
            name: _values(lit)
            for name, lit in {
                "TaskStatus": TaskStatus,
                "ShownGroup": ShownGroup,
                "AudienceKind": AudienceKind,
                "Lane": Lane,
                "AttemptOutcome": AttemptOutcome,
                "AttemptStatus": AttemptStatus,
                "SubscriptionCreator": SubscriptionCreator,
                "QuestionKind": QuestionKind,
                "QuestionState": QuestionState,
                "AskerKind": AskerKind,
                "EffectState": EffectState,
                "EffectDecision": EffectDecision,
                "OutputKind": OutputKind,
                "TriggerSource": TriggerSource,
                "Admission": Admission,
                "TaskInputKind": TaskInputKind,
                "ScheduleStatus": ScheduleStatus,
                "RuleMode": RuleMode,
                "ListenMode": ListenMode,
                "CreationKind": CreationKind,
                "CreationStatus": CreationStatus,
                "RouterDecisionKind": RouterDecisionKind,
            }.items()
        },
        "keys": KEY_FORMATS,
        "triggerIds": TRIGGER_ID_FORMATS,
        "namespaces": {"run": str(RUN_NS), "task": str(TASK_NS)},
        "sessionKeys": {"worker": WORKER_SESSION_KEY_PATTERN},
        "gsis": [{"name": g.name, "pk": g.pk_attr, "sk": g.sk_attr} for g in GSIS],
        "rows": {
            name: model.model_json_schema(by_alias=True, mode="serialization") for name, model in ROW_MODELS.items()
        },
        "routerDecisions": TypeAdapter(RouterDecisions).json_schema(by_alias=True),
    }
