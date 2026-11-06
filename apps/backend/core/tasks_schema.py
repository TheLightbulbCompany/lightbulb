"""The super-agent shared contract: every id builder, enum, row model, Router decision and GSI of the
`tasks` table (contracts-v2 §2). No I/O.

Specs cite names from here, never values. `scripts/export_tasks_schema.py` writes
`packages/shared/fixtures/super-agent/tasks-schema.json` from `export_schema()`; web, mobile and bridge tests
read that file, and `tests/unit/super_agent/test_tasks_schema.py` fails when it is stale.

Row keys: pk `WS#{ws}`, where `ws` is the workspace owner id (`org_…` / `user_…`). Rows (contracts-v2 §3, plus
`NEEDS#` restored by §14.2): Task, Attempt, Subscription + `THREAD#` lookup, Question, Needs, Effect, Trigger,
Schedule + `HOOK#` webhook pointer, Specialist (`AGENT#`), Project, Goal, Creation (`CAP#`), Observed (`OBS#`, §18 rules). Cut rows have no builder on purpose.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any, ClassVar, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = 1

# --- Enums -----------------------------------------------------------------------------------------

# §4 stored statuses. "stopped" is `resolved` with `statusReason`. Rows stored `proposed` before 5 Nov 2026
# (proposals removed) are read as `resolved`/`dismissed` (`tasks_access.summarize`).
TaskStatus = Literal["queued", "working", "waiting", "idle", "resolved", "failed"]
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
    "answer",
    "ask",
    "watch",  # lane A (A12): a task's own watch was met; the input kind is "watch"
    "idea",  # Ideas "Let's do it" and the proactive first run (core/services/proactive.py)
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
# §3 + §13: what Lightbulb made for the workspace. An app's `capId` is its app id; its state lives on the app
# record (Apps owns the lifecycle, spec 08 §3.1); the row says which request made it (founder, 5 Nov: Canvas =
# Creations). Skills next.
CreationKind = Literal["app", "skill"]
CreationStatus = Literal["draft", "active", "off"]
RouterDecisionKind = Literal["start_task", "pass_through", "reply", "ask", "ignore"]
# Spec 11 (watchdog): check that fired, the judge's verdict, the action it proposes, a platform admin's label.
WatchdogCheck = Literal["C1", "C2", "C3", "C4", "C5", "C6", "judge"]
WatchdogVerdict = Literal[
    "progressing", "looping", "drifting", "stalled_waiting", "blocked_external", "claims_done_without_evidence"
]
WatchdogAction = Literal["reconcile", "retry", "recheck", "steer", "question", "escalate", "none"]
WatchdogLabel = Literal["tp", "fp", "unsure"]


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


def router_run_id(ws: str, batch_id: str) -> str:
    """A Router batch's bridge run id: a retried `router_turn` re-POSTs the same run (a replay)."""
    return str(uuid.uuid5(RUN_NS, f"{ws}:router:{batch_id}"))


#: A Worker's session on the workspace's VM (spec 02 §2.2): one per task, so an attempt resumes the last one's
#: transcript. The bridge's run route checks keys against this pattern (bridge/runs.py SESSION_KEY_RE).
WORKER_SESSION_KEY_PATTERN = r"^agent:[0-9a-f-]{36}:lightbulb:task:[0-9a-f-]{36}$"


def worker_session_key(agent_id: str, task_id: str) -> str:
    return f"agent:{agent_id}:lightbulb:task:{task_id}"


#: The Router's session on the workspace VM: one per lane (`shared` or `router_lane_key(user)`) and conversation
#: (`router_room_key(trigger_scope(trig))`), on the CEO agent's profile. A Router run reads its session's previous
#: conversation, so one room's batch never sees another room's, a DM's or an outside sender's. The room part is
#: required: no key equals the old per-lane one, whose history mixed every room. The bridge checks keys against this
#: (bridge/runs.py ROUTER_SESSION_KEY_RE).
ROUTER_SESSION_KEY_PATTERN = r"^agent:[0-9a-f-]{36}:lightbulb:router:(shared|u:[0-9a-f]{16}):r:[0-9a-f]{16}$"


def router_session_key(agent_id: str, lane: str, room: str) -> str:
    return f"agent:{agent_id}:lightbulb:router:{lane}:{room}"


def router_room_key(room_id: str) -> str:
    """The room part of a Router session key (no raw room id in session keys, like the lane)."""
    return "r:" + hashlib.sha256(room_id.encode()).hexdigest()[:16]


def trigger_room(trig: dict) -> str | None:
    """The room a trigger belongs to and is answered in: where it was said (chat), else its deliver room. The one
    rule admission (the Router batch key) and router_turn (memory audience, open tasks, delivery) share."""
    ref = trig.get("ref") or {}
    return ref.get("roomId") or ref.get("deliverRoomId")


def trigger_scope(trig: dict) -> str:
    """The conversation a trigger belongs to: its room (`trigger_room`), else its own outside conversation (a mail
    thread's session, else its sender, else the trigger alone), hashed. The Router batch key and its session: two
    outside senders never share a Router prompt or session."""
    room = trigger_room(trig)
    if room:
        return room
    ref = trig.get("ref") or {}
    return router_room_key(f"{trig.get('source')}:{ref.get('sessionKey') or ref.get('from') or trig['triggerId']}")


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
    "answer": "answer:{qid}:{clientAnswerId}",  # a person's free-text answer; the Temporal update carries ids only
    "ask": "ask:{qid}",  # a Worker's ask_person / request_approval text; the `ask` update carries ids only
    "watch": "watch:{taskId}:{watchId}:{n}",  # the n-th check of a task's watch found it met (A12)
    "idea": "idea:{key}",  # an idea start (hash of idea, person, start token) or the workspace's first run
    # §18: one observe/auto match per rule per thread, ever. Not a TriggerSource: the row's `source` is chat/webhook.
    "rule": "rule:{scheduleId}:{threadHash}",
}


def trigger_id(source: str, *parts: object) -> str:
    fmt = TRIGGER_ID_FORMATS[source]
    prefix, *names = fmt.split(":")
    if len(parts) != len(names):
        raise ValueError(f"{source} trigger id takes {len(names)} part(s): {fmt}")
    return ":".join([prefix, *map(str, parts)])


def rule_trigger_id(sched_id: str, thread_key: str) -> str:
    """§18: the dedupe key of a rule match; the thread key is hashed so ids stay short and carry no room ids."""
    return trigger_id("rule", sched_id, hashlib.sha256(thread_key.encode()).hexdigest()[:16])


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


def learn_sk(specialist_id: str, task_id: str, attempt: int) -> str:
    """One learning merge of one attempt into one specialist (spec 07 §3.5; lane V)."""
    return f"LEARN#{specialist_id}#{task_id}#{attempt:04d}"


def trigger_sk(trigger_id: str) -> str:
    return f"TRIG#{trigger_id}"


def observed_sk(thread_key: str) -> str:
    """§18: a rule observed this thread (spec 12 §3.4); a task later started on it counts as did-it-yourself."""
    return f"OBS#{thread_key}"


def schedule_sk(schedule_id: str) -> str:
    return f"SCHED#{schedule_id}"


def hook_sk(public_id: str) -> str:
    """spec 06 §3.5: webhook lookup pointer; the key itself binds the public id to the workspace."""
    return f"HOOK#{public_id}"


def agent_sk(agent_id: str) -> str:
    return f"AGENT#{agent_id}"


def agent_owner_pk(agent_id: str) -> str:
    """Reverse pointer agent id → workspace (spec 03 §5.1): sk `AGENT_OWNER_SK`, attr `ws`. Global id uniqueness."""
    return f"AGENTID#{agent_id}"


AGENT_OWNER_SK = "WS"


def project_sk(project_id: str) -> str:
    return f"PROJECT#{project_id}"


def goal_sk(goal_id: str) -> str:
    return f"GOAL#{goal_id}"


def creation_sk(cap_id: str) -> str:
    return f"CAP#{cap_id}"


def slots_sk() -> str:
    """The workspace's Worker slot leases (contracts §9: cap per VM kind)."""
    return "SLOTS"


def task_seq_sk() -> str:
    """The workspace's task identifier counter (`LB-{n}`, spec 01 `next_identifier`)."""
    return "TASKSEQ"


# Spec 03 §5.1: the workspace's own rows under `WS#{ws}` (Lane R, wave1-R Task 6).
META_SK = "META"
PENDING_BRIEF_SK = "PENDING_BRIEF"


def status_key(ws: str, status: str) -> str:
    return f"{ws}#{status}"


def task_workflow_id(ws: str, task_id: str) -> str:
    return f"task:{ws}:{task_id}"


def learning_workflow_id(ws: str, specialist_id: str) -> str:
    """The one merge writer per specialist (spec 07 §3.5)."""
    return f"learn:{ws}:{specialist_id}"


def router_workflow_id(ws: str) -> str:
    return f"router:{ws}"


def schedule_workflow_id(ws: str, schedule_id: str) -> str:
    return f"sched:{ws}:{schedule_id}"


# Watchdog rows (spec 11 §5.3). Findings and caps live under platform pks; Actions under the workspace so a
# task's counters survive any change to the Task row. `WD#` never matches `get_task_tree`'s `TASK#` prefix.
WD_PLATFORM_PK = "PLATFORM#watchdog"


def wd_finding_pk(day: str) -> str:
    return f"{WD_PLATFORM_PK}#F#{day}"


def wd_finding_sk(ws: str, task_id: str, check: str, attempt_n: int, seq: int | None = None) -> str:
    sk = f"{ws}#{task_id}#{check}#{attempt_n}"
    return sk if seq is None else f"{sk}#J{seq:02d}"


def wd_action_sk(task_id: str, kind: str, n: int) -> str:
    return f"WD#{task_id}#ACT#{kind}#{n:02d}"


def wd_cap_sk(kind: Literal["act", "judge"], bucket: str) -> str:
    return f"CAP#{kind}#{bucket}"


def task_room_id(task_id: str) -> str:
    """§6: every task's hidden conversation room. A uuid (message routes take uuid room ids), fixed per task."""
    return str(uuid.uuid5(TASK_NS, f"room:{task_id}"))


KEY_FORMATS: dict[str, str] = {
    "pk": "WS#{ws}",
    "task": "TASK#{taskId}",
    "attempt": "TASK#{taskId}#ATT#{n:04}",
    "subscription": "TASK#{taskId}#SUB#{threadKey}",
    "thread": "THREAD#{threadKey}",
    "question": "TASK#{taskId}#Q#{qid}",
    "needs": "NEEDS#{userId}#{taskId}#{qid}",
    "effect": "EFFECT#{idemKey}",
    "learn": "LEARN#{specialistId}#{taskId}#{attempt:04}",
    "trigger": "TRIG#{triggerId}",
    "schedule": "SCHED#{scheduleId}",
    "observed": "OBS#{threadKey}",
    "hook": "HOOK#{publicId}",
    "agent": "AGENT#{agentId}",
    "agentOwner": "AGENTID#{agentId}",
    "project": "PROJECT#{projectId}",
    "goal": "GOAL#{goalId}",
    "creation": "CAP#{capId}",
    "slots": "SLOTS",
    "meta": "META",
    "pendingBrief": "PENDING_BRIEF",
    "statusKey": "{ws}#{status}",
    "taskWorkflowId": "task:{ws}:{taskId}",
    "routerWorkflowId": "router:{ws}",
    "scheduleWorkflowId": "sched:{ws}:{scheduleId}",
    "taskRoomId": "uuid5(task, 'room:{taskId}')",
    "routerLaneKey": "u:{sha256(userId)[:16]}",
    "wdFindingPk": "PLATFORM#watchdog#F#{yyyymmdd}",
    "wdFinding": "{ws}#{taskId}#{check}#{attemptN}[#J{seq:02}]",
    "wdAction": "WD#{taskId}#ACT#{kind}#{n:02}",
    "wdCapPk": "PLATFORM#watchdog",
    "wdCap": "CAP#{act|judge}#{bucket}",
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


class Output(_Model):
    kind: OutputKind
    title: str = Field(max_length=200)
    ref: dict[str, str] = {}  # {s3Key} / {appId} / {repo, pr} …


class TaskOutput(Output):
    """What a task made, kept on its row for every status (UX slice 5): a landed effect (mail, PR, published app),
    a file its result attached, an app it handed to Apps. `id` dedupes a retried write: `effect:{idemKey}`,
    `file:{attachmentId}`, `app:{appId}`."""

    id: str
    created_at: str


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
    previous_run_task_id: str | None = None  # an automation run's previous run (lane A chaining)
    hidden: bool = False  # §18 speculative work
    watch_until: str | None = None  # "Watching" when set
    attempt_count: int = 0
    usage: Usage | None = None
    resolved_at: str | None = None
    # Explicit: `to_camel` would make it `a2ATaskId`; the wire name everywhere else (the `a2a:{a2aTaskId}` trigger
    # id, lane Q's A2A admission) is `a2aTaskId`.
    a2a_task_id: str | None = Field(None, alias="a2aTaskId")
    ws_channel: str | None = None
    stop_requested_by: str | None = None  # who pressed Stop (attributes the stop)
    landing_effect: str | None = None  # Effect idemKey while `apply_effect` runs (Lane Q writes)
    outputs: list[TaskOutput] = []  # what it made, oldest first (`tasks_repo.add_task_output`)

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
    task_token: str | None = None  # b64 Temporal task token of the async `worker_turn`; never in a response
    run_secret: str | None = None  # per-attempt secret the runtime presents on run events/inputs; never in a response
    terminal_status: str | None = None  # the bridge's terminal run status, once delivered
    output: str | None = Field(None, max_length=65536)  # the Worker's result text (never in a payload)
    recent_tools: list[dict[str, Any]] = []  # last heartbeat's tool calls (trace, progress)
    progress_posted: bool = False  # the attempt's one progress line went up
    attachments: list[dict[str, Any]] = []  # uploadedAttachments for the result post (task room keys)

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
    trigger_id: str | None = None  # `TRIG#answer:…` holding a free-text answer (payloads carry ids only)


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
    reason: str | None = None  # why it was cancelled (stopped, superseded)
    cancelled_by: str | None = None
    cancelled_at: str | None = None
    answerer_rule: str | None = None  # questions.answerers() rule, re-checked at answer time
    base_answerers: list[str] = []  # the non-admin part of `answerers` (requester, app creator, holders)
    client_answer_id: str | None = None  # the answer press's retry key
    operands_hash: str | None = None
    effect_idem_key: str | None = None
    preview: dict[str, Any] | None = None  # spec 05 §5.1 card preview (mail body, diff, price), ≤16 KB
    relay: dict[str, str] | None = None  # {roomId, messageId} of the posted card
    gov_ref: dict[str, str] | None = None  # {kind, id} of a governance hold

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


class LearnIntent(_Row):
    """A learning merge's intent row (spec 07 §3.5 crash safety 2-3): `planned` ({rel: sha256} of
    every put) lands before the S3 writes, `applied` + the merge report after; a replay of an
    applied row is a no-op."""

    type: Literal["learn"] = "learn"
    specialist_id: str
    task_id: TaskId
    attempt: int = Field(ge=1)
    state: Literal["planned", "applied"]
    planned: dict[str, str] = {}
    report: dict[str, Any] | None = None

    def sk(self) -> str:
        return learn_sk(self.specialist_id, self.task_id, self.attempt)


class Effect(_Row):
    """External, irreversible effects only (send, pay, merge, publish, provision, A2A)."""

    type: Literal["effect"] = "effect"
    idem_key: str
    task_id: TaskId
    attempt: int
    kind: str  # questions.EFFECT_KINDS
    tool: str
    operands: dict[str, Any] = {}  # the call's parsed operands (≤16 KB): the Output draft while proposed
    operands_hash: str
    executor_ok: bool = False  # a backend Landing executor can run it from `operands`
    door_used_at: str | None = None  # the one backend door call this intent let through (Lane Q door gating)
    state: EffectState
    question_id: str | None = None
    allowed_by: str | None = None  # why it went ahead with no card: `mode:auto`, `mode:never_ask` or `rule:<id>`
    output: Output | None = None  # the draft payload while proposed
    result_ref: str | None = None

    def sk(self) -> str:
        return effect_sk(self.idem_key)


class RuleMatch(_Model):
    """§18: what a rule would have done (observe) or did (auto) for this trigger."""

    sched_id: str
    version: int  # the rule's `ruleVersion`
    p: float | None = None  # Jev's probability; None for code-matched (calendar) rules
    decision: str  # "observed" in wave 2
    would_do: str


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
    rule_match: RuleMatch | None = None

    def sk(self) -> str:
        return trigger_sk(self.trigger_id)


class RuleMatchScope(_Model):
    """spec 12 §3.1: one source per rule; `room` names its rooms (no "everywhere" rule), `calendar` its account."""

    sources: list[Literal["room", "calendar"]]
    room_ids: list[str] = []
    account: str | None = None
    repo: str | None = None  # owner/name; builtin=repro_fix only


class MatchEntry(_Model):
    at: str
    thread_key: str
    would_do: str
    version: int  # the rule's `ruleVersion` when it matched
    done_by_human: bool = False


class RuleStats(_Model):
    """Capped lists on the rule row (spec 12 §3.1): no new row type."""

    day: str | None = None
    spent_usd: float = 0.0
    deliveries: list[bool] = []  # last 10 delivery verdicts (wave 3)
    matches: list[MatchEntry] = []  # newest last, at most 50


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
    # §18 rule fields: a row with `match` is a proactive rule; rows without it are ordinary automations.
    match: RuleMatchScope | None = None
    # The rule's own version: bumped by a text, scope or specialist edit (which also drops it to observe). Row
    # `version` is the write lock and moves on every stats append, so promotion counts on this one.
    rule_version: int = 1
    speculative: bool = False
    time_limit_s: int | None = None
    daily_cap_usd: float = 10.0
    builtin: Literal["invite_prep", "repro_fix"] | None = None
    promoted_by: str | None = None
    promoted_at: str | None = None
    stats: RuleStats | None = None

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
    resource: str | None = None  # the provider-side thing hooked: repo, project id, asset id, account
    account: str | None = None  # Google watch fields (spec 06 §3.6); `page_token` is also S7's sync token
    channel_id: str | None = None
    resource_id: str | None = None
    expires_at: str | None = None
    page_token: str | None = None
    topic: str | None = None  # Gmail push: the Pub/Sub topic the watch publishes to
    sig_fail_count: int = 0
    last_sig_fail_at: str | None = None

    def sk(self) -> str:
        return hook_sk(self.public_id)


class Agent(_Row):
    """Specialist; ids are today's agent ids (spec 03 owns the backfill, §5.1 the fields). `persona` is
    Paperclip's `capabilities`; `listing` is `{listingId, listingSlug}`; `companyId` until Paperclip is gone."""

    type: Literal["agent"] = "agent"
    agent_id: str
    name: str = Field(max_length=256)
    handle: str | None = None
    listen_mode: ListenMode = "active"
    skills: list[str] = []
    status: Literal["active", "paused", "deleted"] = "active"
    role: str | None = None
    title: str | None = None
    persona: str | None = None
    icon: str | None = None
    appearance: dict[str, Any] | None = None
    reports_to: str | None = None
    listing: dict[str, str] | None = None
    budget_monthly_cents: int = 0
    company_id: str | None = None

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
    task_id: str | None = None  # the request that made it (the first one; later changes are their own requests)

    def sk(self) -> str:
        return creation_sk(self.cap_id)


class Observed(_Row):
    """`OBS#{threadKey}`: a rule observed this thread (§18). Written once by `record_match`; never a THREAD# row."""

    type: Literal["observed"] = "observed"
    thread_key: str
    sched_id: str
    trigger_id: str
    at: str

    def sk(self) -> str:
        return observed_sk(self.thread_key)


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
        LearnIntent,
        Trigger,
        Schedule,
        Hook,
        Agent,
        Project,
        Goal,
        Creation,
        Observed,
    )
}


# --- Views (C15) ------------------------------------------------------------------------------------


class SummaryQuestion(_Model):
    qid: str
    kind: QuestionKind
    subkind: str | None = None
    title: str
    answerers: list[str]
    answerer_names: list[str]
    can_answer: bool
    options: list[QuestionOption] = []


class SummaryOutput(_Model):
    idem_key: str
    kind: OutputKind
    title: str
    next_step: str | None = None


class SummaryActivity(_Model):
    n: int
    status: AttemptStatus
    outcome: AttemptOutcome | None = None
    last_heartbeat_at: str | None = None


class TaskSummary(_Model):
    """One task as one viewer sees it. `tasks_access.summarize` builds it; clients render it and never
    re-derive `shownGroup` (viewer-specific; None = proposed, shown in chat's Suggested block)."""

    task_id: TaskId
    identifier: str
    title: str
    status: TaskStatus
    status_reason: str | None = None
    shown_group: ShownGroup | None
    specialist: str | None = None
    owner_user_id: str | None = None
    audience: Audience
    audience_kind: AudienceKind
    origin_room_id: str | None = None
    # What came of a finished task, one plain line for its card in the origin room; None while it works or when
    # someone in that room can't open it (`tasks_access.result_line`).
    result_line: str | None = None
    task_room_id: str
    open_question: SummaryQuestion | None = None
    landing: str | None = None
    output: SummaryOutput | None = None  # the draft waiting on a review, else None
    outputs: list[TaskOutput] = []  # what it made, for every status
    watch_until: str | None = None
    watching: bool
    runs_on: Literal["task", "workspace"] | None = None
    activity: SummaryActivity | None = None
    recent_inputs: list[str] = []
    attempt_count: int = 0
    usage: Usage | None = None
    cost_usd: float = 0.0
    budget_usd: float | None = None
    updated_at: str
    version: int


# --- Router decisions (01 §5.3 + C11) ---------------------------------------------------------------


class _Decision(_Model):
    trigger_ids: TriggerIds
    reason: str | None = None


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
    # Same explicit alias as Task.a2a_task_id: one wire name, `a2aTaskId` (`to_camel` would say `a2ATaskId`).
    a2a_task_id: str | None = Field(None, alias="a2aTaskId")  # an inbound A2A request's governance row


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
    StartTask | PassThrough | Reply | Ask | Ignore,
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
                "WatchdogCheck": WatchdogCheck,
                "WatchdogVerdict": WatchdogVerdict,
                "WatchdogAction": WatchdogAction,
                "WatchdogLabel": WatchdogLabel,
            }.items()
        },
        "keys": KEY_FORMATS,
        "triggerIds": TRIGGER_ID_FORMATS,
        "namespaces": {"run": str(RUN_NS), "task": str(TASK_NS)},
        "sessionKeys": {"worker": WORKER_SESSION_KEY_PATTERN, "router": ROUTER_SESSION_KEY_PATTERN},
        "gsis": [{"name": g.name, "pk": g.pk_attr, "sk": g.sk_attr} for g in GSIS],
        "rows": {
            name: model.model_json_schema(by_alias=True, mode="serialization") for name, model in ROW_MODELS.items()
        },
        "views": {"TaskSummary": TaskSummary.model_json_schema(by_alias=True, mode="serialization")},
        "routerDecisions": TypeAdapter(RouterDecisions).json_schema(by_alias=True),
    }
