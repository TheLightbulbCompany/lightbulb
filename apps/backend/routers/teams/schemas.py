"""Pydantic body whitelist schemas for the Teams BFF.

Spec §5 invariant: every mutating endpoint that touches an agent uses
``model_config = ConfigDict(extra="forbid")`` so a request that includes
``adapterType``, ``adapterConfig``, ``url``, ``headers``, or any other
non-whitelisted field is rejected with 422 at the FastAPI boundary.
The BFF synthesizes the adapter block server-side via
``core.services.paperclip_adapter_config.synthesize_openclaw_adapter``.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class _Strict(BaseModel):
    """Base for Teams BFF request bodies.

    All Teams BFF schemas use snake_case Python attribute names but
    must emit camelCase JSON to match Paperclip's wire shape. Pydantic's
    ``alias_generator=to_camel`` derives a camelCase alias for every
    field; ``populate_by_name=True`` keeps the snake_case attribute
    names usable from internal Python callers (and from the existing
    test fixtures). ``extra="forbid"`` still rejects unknown keys —
    aliasing does not weaken the security posture.

    Outbound: every router that forwards a body to Paperclip must call
    ``model_dump(..., by_alias=True)`` so the camelCase aliases land on
    the wire.
    """

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )


class CreateAgentBody(_Strict):
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=40)
    title: Optional[str] = Field(default=None, max_length=80)
    capabilities: Optional[str] = Field(default=None, max_length=4000)
    reports_to: Optional[str] = Field(default=None, max_length=80)
    budget_monthly_cents: int = Field(default=0, ge=0)


class PatchAgentBody(_Strict):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    title: Optional[str] = Field(default=None, max_length=80)
    capabilities: Optional[str] = Field(default=None, max_length=4000)
    reports_to: Optional[str] = Field(default=None, max_length=80)
    budget_monthly_cents: Optional[int] = Field(default=None, ge=0)


class ResetAgentSessionBody(_Strict):
    # Recovery op for a wedged agent — mirrors upstream ``resetAgentSessionSchema``
    # (paperclip/packages/shared/src/validators/agent.ts): the single optional
    # ``taskKey`` (``z.string().min(1).optional().nullable()``) scopes the reset to
    # one task's runtime session; omitting it resets ALL sessions. ``extra="forbid"``
    # (via ``_Strict``) rejects any smuggled adapter field with 422 at the BFF
    # boundary; ``task_key`` aliases to the camelCase ``taskKey`` Paperclip reads.
    task_key: Optional[str] = Field(default=None, min_length=1, max_length=200)


class CreateIssueBody(_Strict):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    project_id: Optional[str] = None
    # Goal linkage. ``issues.goal_id`` is a real upstream column and both
    # ``createIssueSchema`` and ``updateIssueSchema`` accept ``goalId`` — it was
    # simply never whitelisted here, which is why the UI could only hang
    # sub-goals and projects off a goal. Uses the same alias-generator path as
    # ``project_id``, so it goes out as ``goalId``.
    goal_id: Optional[str] = None
    assignee_agent_id: Optional[str] = None
    # A human owner: a Paperclip user id (``CompanyMember.paperclipUserId``).
    # Upstream refuses a non-member of the company and wakes no agent for it.
    assignee_user_id: Optional[str] = None
    priority: Optional[str] = Field(default=None, pattern=r"^(critical|high|medium|low)$")
    # Compose-time status preset + execution work mode — both forward to
    # upstream's createIssueSchema, so the composer can set the starting state
    # without a round-trip PATCH.
    #
    # Omitting ``status`` is NOT "defaults to backlog". Upstream's
    # ``resolveCreateIssueStatusDefault`` reads the assignee: with one the
    # default is ``todo`` (which DISPATCHES a run), without one it is
    # ``backlog``. An assigned create that omits ``status`` therefore starts an
    # agent — the misreading behind #1426. ``work_mode`` does default to
    # ``standard``.
    status: Optional[str] = Field(
        default=None,
        pattern=r"^(backlog|todo|in_progress|in_review|done|blocked|cancelled)$",
    )
    work_mode: Optional[str] = Field(default=None, pattern=r"^(standard|ask|planning)$")


class PatchIssueBody(_Strict):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    project_id: Optional[str] = None
    # Goal linkage — see CreateIssueBody.goal_id. This route forwards with
    # ``exclude_unset``, so an explicit ``null`` detaches the task from its goal
    # while omitting the key leaves the link untouched.
    goal_id: Optional[str] = None
    assignee_agent_id: Optional[str] = None
    priority: Optional[str] = Field(default=None, pattern=r"^(critical|high|medium|low)$")
    status: Optional[str] = None
    column_id: Optional[str] = None
    # Full desired blocker set (replace semantics) — the issue-detail "Blocked
    # by" picker sends the complete array, matching upstream Paperclip. The
    # ``_Strict`` alias_generator emits this as ``blockedByIssueIds`` on the
    # wire (Paperclip's PATCH /api/issues/:id reads camelCase).
    blocked_by_issue_ids: Optional[list[str]] = None
    # Soft-hide: upstream PATCH /api/issues/:id accepts ``hiddenAt`` (ISO
    # string) and excludes hiddenAt-set rows from the issues list. An ISO
    # timestamp hides the task; an explicit null restores it.
    hidden_at: Optional[str] = Field(default=None, max_length=40)
    # Execution work mode (standard / ask / planning) — upstream
    # updateIssueSchema accepts it; lets the detail surface change how a task
    # runs without recreating it.
    work_mode: Optional[str] = Field(default=None, pattern=r"^(standard|ask|planning)$")
    # Reparent / clear parent, human-user assignee (mutually exclusive with
    # assigneeAgentId — enforced in the picker UI), and the full desired
    # label-id set (replace semantics). All accepted by upstream
    # updateIssueSchema.
    parent_id: Optional[str] = None
    assignee_user_id: Optional[str] = None
    label_ids: Optional[list[str]] = None
    # Execution policy passthrough — carries the monitor schedule
    # {monitor: {nextCheckAt, notes, serviceName, ...}}. Forwarded verbatim;
    # upstream updateIssueSchema validates the nested shape. (Inner keys must
    # already be camelCase — the alias generator only renames model fields,
    # not nested dict contents.)
    execution_policy: Optional[dict] = None


class UpsertIssueWatchdogBody(_Strict):
    """Body for ``PUT /teams/issues/{id}/watchdog``.

    Mirrors upstream ``upsertIssueWatchdogSchema``
    (paperclip/packages/shared/src/validators/issue.ts), which is itself
    ``.strict()``: the agent that reviews the issue's subtree when it goes
    quiet, plus optional instructions for it. An explicit ``null`` clears the
    instructions, so the route forwards with ``exclude_unset``.
    """

    agent_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    instructions: Optional[str] = Field(default=None, max_length=20000)


class CreateIssueLabelBody(_Strict):
    """Body for ``POST /teams/issues/labels``.

    Mirrors upstream ``createIssueLabelSchema`` (name 1-48 chars, colour a
    required 6-digit hex). Colour is optional HERE and defaults to a value
    derived from the name in the router — labelling a task is the categorising
    gesture, and making the user pick a hex first is friction for no gain.
    """

    name: str = Field(min_length=1, max_length=48)
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        """Strip before the length check rather than after.

        ``min_length`` runs against the raw string, so a whitespace-only name
        passes validation and then reaches upstream as ``""`` once the router
        strips it. Normalising here makes the validated value the forwarded
        value. The picker already gates on a non-empty trimmed query; this is
        the boundary guard for every other BFF caller.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class AddIssueCommentBody(_Strict):
    """Body for ``POST /teams/issues/{id}/comments``.

    Whitelisted to ``body`` (the comment text) only — same defense-in-depth
    as ``CreateIssueBody`` to prevent ``adapterType``/``adapterConfig``
    smuggling through the comment payload. Upstream's
    ``addIssueCommentSchema`` also accepts ``reopen`` / ``resume`` /
    ``interrupt`` boolean action flags; we defer those until the
    IssueDetail comment composer wires the corresponding controls.
    """

    body: str = Field(min_length=1, max_length=20000)


class UpsertFeedbackVoteBody(_Strict):
    """Body for ``POST /teams/issues/{id}/feedback-votes``.

    Mirrors upstream ``upsertIssueFeedbackVoteSchema``
    (paperclip/packages/shared/src/validators/feedback.ts): thumbs up/down on
    an agent output (a comment or a document revision). ``extra="forbid"``
    (via ``_Strict``) rejects any smuggled adapter field with 422 at the BFF
    boundary. The enums are constrained here so junk 422s locally instead of
    being forwarded for upstream to reject opaquely.

    ``allow_sharing`` defaults to omitted (local-only): sharing the voted
    output with Paperclip Labs is opt-in, and the Isol8 UI casts votes
    local-only. ``target_type`` aliases to ``targetType`` etc. via ``_Strict``.
    """

    target_type: str = Field(pattern=r"^(issue_comment|issue_document_revision)$")
    target_id: str = Field(min_length=1, max_length=200)
    vote: str = Field(pattern=r"^(up|down)$")
    reason: Optional[str] = Field(default=None, max_length=1000)
    allow_sharing: Optional[bool] = None


class DecideDecisionBody(_Strict):
    """Body for ``POST /teams/decisions/{id}/decide``.

    Mirrors upstream ``decideSchema`` (paperclip/server/src/routes/decisions.ts):
    the chosen option, the answers to the decision's inputs, and a client key
    that makes a retried click replay instead of 409.
    """

    option_id: str = Field(min_length=1, max_length=120)
    input_values: Optional[dict[str, Annotated[str, Field(max_length=20000)]]] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=500)


class DismissDecisionBody(_Strict):
    """Body for ``POST /teams/decisions/{id}/dismiss`` (upstream ``dismissSchema``)."""

    reason: Optional[str] = Field(default=None, max_length=20000)


class CreateRoutineBody(_Strict):
    """Body for ``POST /teams/routines``.

    Mirrors upstream's ``createRoutineSchema``
    (paperclip/packages/shared/src/validators/routine.ts): a routine
    is a titled, assignable unit of recurring work. The cron schedule is
    NOT part of the routine row — it lives on a separate schedule trigger
    created via ``POST /routines/{id}/triggers`` (``CreateRoutineTriggerBody``).
    The old ``{name, cron, agent_id, prompt, enabled}`` shape 422'd at the
    upstream boundary; ``description`` carries the prompt copy.
    """

    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    assignee_agent_id: Optional[str] = None
    status: Optional[str] = Field(default=None, pattern=r"^(active|paused|draft|archived)$")
    priority: Optional[str] = Field(default=None, pattern=r"^(critical|high|medium|low)$")
    project_id: Optional[str] = None
    # --- A2 (editable routines): full-shape create fields --------------------
    # Scheduling policies + variables + env + circuit-breaker (autoPause) —
    # upstream ``createRoutineSchema`` accepts all of these
    # (paperclip/packages/shared/src/validators/routine.ts, deployed fork
    # 2026.7 = vanilla 936215693 + circuit-breaker patch). ``variables`` carries
    # the routine's typed variable definitions INCLUDING the ``date`` type added
    # by upstream #8655; forwarded verbatim (upstream validates each entry via
    # ``routineVariableSchema``). ``env`` is upstream ``envConfigSchema`` (a
    # free-form secret/env map). Non-strict upstream schemas strip anything they
    # don't know, so the passthrough is safe.
    goal_id: Optional[str] = None
    concurrency_policy: Optional[str] = Field(
        default=None, pattern=r"^(coalesce_if_active|always_enqueue|skip_if_active)$"
    )
    catch_up_policy: Optional[str] = Field(default=None, pattern=r"^(skip_missed|enqueue_missed_with_cap)$")
    auto_pause_enabled: Optional[bool] = None
    auto_pause_threshold: Optional[int] = Field(default=None, ge=1, le=100)
    variables: Optional[list[dict]] = None
    env: Optional[dict] = None
    # Upstream ``issueExecutionPolicySchema``, carried on the routine so every
    # run issue it generates is born with review stages and flows through
    # Paperclip's existing reviewer lifecycle. Forwarded verbatim — upstream
    # validates the shape AND that every agent participant belongs to the
    # company, so this layer must not second-guess either.
    execution_policy: Optional[dict] = None


class PatchRoutineBody(_Strict):
    """Body for ``PATCH /teams/routines/{id}``.

    Mirrors upstream's ``updateRoutineSchema`` (a partial of
    ``createRoutineSchema``). The enable/disable toggle PATCHes
    ``{status: "active" | "paused"}`` — there is no ``enabled`` field
    on the upstream routine row.
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    assignee_agent_id: Optional[str] = None
    status: Optional[str] = Field(default=None, pattern=r"^(active|paused|draft|archived)$")
    priority: Optional[str] = Field(default=None, pattern=r"^(critical|high|medium|low)$")
    project_id: Optional[str] = None
    # --- A2 (editable routines): full-shape update fields --------------------
    # Same superset as ``CreateRoutineBody`` plus ``base_revision_id`` (upstream
    # ``updateRoutineSchema`` = createRoutineSchema.partial().extend({baseRevisionId})),
    # so the routine-detail edit form can change policies / variables (incl. the
    # ``date`` type from #8655) / env, and optimistic-concurrency against a base
    # revision. Activation stays gated in the router (status="active").
    goal_id: Optional[str] = None
    concurrency_policy: Optional[str] = Field(
        default=None, pattern=r"^(coalesce_if_active|always_enqueue|skip_if_active)$"
    )
    catch_up_policy: Optional[str] = Field(default=None, pattern=r"^(skip_missed|enqueue_missed_with_cap)$")
    auto_pause_enabled: Optional[bool] = None
    auto_pause_threshold: Optional[int] = Field(default=None, ge=1, le=100)
    variables: Optional[list[dict]] = None
    env: Optional[dict] = None
    # Same field as ``CreateRoutineBody``. The PATCH handler dumps with
    # ``exclude_unset`` (not ``exclude_none``), so sending an explicit null
    # here is how a reviewer is removed — an omitted key leaves it alone.
    execution_policy: Optional[dict] = None
    base_revision_id: Optional[str] = None


class CreateRoutineTriggerBody(_Strict):
    """Body for ``POST /teams/routines/{id}/triggers``.

    Mirrors upstream's ``createRoutineTriggerSchema`` discriminated union
    (paperclip/packages/shared/src/validators/routine.ts): ``kind`` selects
    ``schedule`` (cron), ``webhook`` (signing config), or ``api``. The Teams
    create dialog wires schedule + webhook; ``api`` triggers still have no UI
    but are accepted here so the union stays in sync with upstream instead of
    422ing a legitimate kind.

    ``cron_expression`` is Optional at the type level (kind now varies) but
    still required in practice for a schedule trigger — enforced by
    ``_require_cron_for_schedule`` below so an empty body doesn't silently
    create a cron-less schedule trigger. ``signing_mode`` / ``replay_window_sec``
    are the webhook branch's knobs, mirroring the same constraints
    ``UpdateRoutineTriggerBody`` already enforces (Paperclip's four signing
    modes, 30–86400s replay window) so junk 422s locally instead of
    round-tripping to upstream.
    """

    kind: str = Field(default="schedule", pattern=r"^(schedule|webhook|api)$")
    cron_expression: Optional[str] = Field(default=None, min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    label: Optional[str] = Field(default=None, max_length=120)
    # A2: schedule triggers may be created already disabled (upstream
    # ``baseTriggerSchema.enabled`` defaults true) — lets the detail UI stage a
    # trigger without arming it.
    enabled: Optional[bool] = None
    # Webhook-only — Paperclip's createRoutineTriggerSchema "webhook" branch.
    # Stripped server-side by upstream's non-strict schema for other kinds, so
    # forwarding them unconditionally is safe; still constrained here for the
    # local-422 defense-in-depth the rest of this file uses.
    signing_mode: Optional[str] = Field(default=None, pattern=r"^(bearer|hmac_sha256|github_hmac|none)$")
    replay_window_sec: Optional[int] = Field(default=None, ge=30, le=86_400)

    @model_validator(mode="after")
    def _require_cron_for_schedule(self) -> "CreateRoutineTriggerBody":
        if self.kind == "schedule" and not self.cron_expression:
            raise ValueError("cron_expression is required for a schedule trigger")
        return self


class UpdateRoutineTriggerBody(_Strict):
    """Body for ``PATCH /teams/routines/{id}/triggers/{trigger_id}`` (A2).

    Mirrors upstream's ``updateRoutineTriggerSchema``
    (paperclip/packages/shared/src/validators/routine.ts) — every field
    optional. ``cron_expression`` / ``timezone`` edit a schedule trigger;
    ``signing_mode`` / ``replay_window_sec`` edit a webhook trigger that
    already exists (created upstream / via CLI). ``enabled`` toggles a
    trigger on/off — enabling is gated in the router (it arms the schedule).
    """

    label: Optional[str] = Field(default=None, max_length=120)
    enabled: Optional[bool] = None
    cron_expression: Optional[str] = Field(default=None, min_length=1, max_length=200)
    timezone: Optional[str] = Field(default=None, min_length=1, max_length=64)
    signing_mode: Optional[str] = Field(default=None, pattern=r"^(bearer|hmac_sha256|github_hmac|none)$")
    replay_window_sec: Optional[int] = Field(default=None, ge=30, le=86_400)


class CreateGoalBody(_Strict):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    parent_id: Optional[str] = None
    # Level + status on create — upstream createGoalSchema accepts both
    # (defaults level="task", status="planned"). Without these every goal
    # lands at the default level/status with no way to set them at creation.
    level: Optional[str] = Field(default=None, pattern=r"^(company|team|agent|task)$")
    status: Optional[str] = Field(default=None, pattern=r"^(planned|active|achieved|cancelled)$")


class PatchGoalBody(_Strict):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    parent_id: Optional[str] = None
    # Upstream GOAL_STATUSES / GOAL_LEVELS — constrain to the enums so an
    # invalid value is rejected at the BFF boundary (422) instead of being
    # forwarded for upstream to reject opaquely. ``updateGoalSchema`` exposes
    # ``level`` via the goal-detail Properties Level picker.
    status: Optional[str] = Field(default=None, pattern=r"^(planned|active|achieved|cancelled)$")
    level: Optional[str] = Field(default=None, pattern=r"^(company|team|agent|task)$")


class CreateProjectBody(_Strict):
    name: str = Field(min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=20000)


class PatchProjectBody(_Strict):
    # NOTE: no project-budget field. Upstream Paperclip has no
    # ``budgetMonthlyCents`` on the project row — budgets live behind a
    # separate ``budget_policies`` API the BFF does not proxy, so a
    # ``budget_monthly_cents`` here would be silently stripped. Removed
    # rather than left as a no-op that misleads the UI.
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=20000)
    # Status + goal links + lead — all accepted by upstream updateProjectSchema.
    # (Still no budget field — see the note above.)
    status: Optional[str] = Field(default=None, pattern=r"^(backlog|planned|in_progress|completed|cancelled)$")
    goal_ids: Optional[list[str]] = None
    lead_agent_id: Optional[str] = None


class ApproveApprovalBody(_Strict):
    note: Optional[str] = Field(default=None, max_length=2000)


class RejectApprovalBody(_Strict):
    reason: str = Field(min_length=1, max_length=2000)


class AddApprovalCommentBody(_Strict):
    """Mirrors upstream ``addApprovalCommentSchema`` (``{body}``, non-empty)."""

    body: str = Field(min_length=1, max_length=20000)


class RequestApprovalRevisionBody(_Strict):
    """Mirrors upstream ``requestApprovalRevisionSchema``; ``decision_note``
    aliases to ``decisionNote``."""

    decision_note: Optional[str] = Field(default=None, max_length=2000)


class ResubmitApprovalBody(_Strict):
    """Upstream ``resubmitApprovalSchema`` also takes a replacement ``payload``;
    the BFF refuses it. A ``hire_agent`` payload carries ``adapterType``, the
    same smuggling carrier the approve/reject bodies keep closed, and stock's
    "Mark resubmitted" button sends none."""


# ---------------------------------------------------------------------------
# Issue-thread interactions (accept / reject / respond / cancel)
#
# Whitelisted bodies for the board-only interaction resolution routes. As with
# the approval bodies, ``extra="forbid"`` rejects any smuggled field (e.g.
# ``adapterType``) with 422 at the FastAPI boundary. The snake_case attributes
# alias to the camelCase shape upstream expects via ``_Strict``'s
# ``to_camel`` generator — routers MUST forward with
# ``model_dump(by_alias=True, exclude_none=True)``.
# ---------------------------------------------------------------------------


class AcceptInteractionBody(_Strict):
    """Body for ``POST /teams/issues/{id}/interactions/{iid}/accept``.

    Mirrors upstream ``acceptIssueThreadInteractionSchema``: both fields are
    optional and an empty ``{}`` accepts all/defaults. ``selected_client_keys``
    aliases to ``selectedClientKeys`` and ``selected_option_ids`` to
    ``selectedOptionIds``.
    """

    selected_client_keys: Optional[list[str]] = Field(default=None, max_length=50)
    selected_option_ids: Optional[list[str]] = Field(default=None, max_length=50)


class RejectInteractionBody(_Strict):
    """Body for ``POST /teams/issues/{id}/interactions/{iid}/reject``.

    Mirrors upstream ``rejectIssueThreadInteractionSchema``: ``reason`` is
    optional.
    """

    reason: Optional[str] = Field(default=None, max_length=4000)


class InteractionAnswer(_Strict):
    """A single answer in a ``respond`` body.

    Mirrors upstream ``askUserQuestionsAnswerSchema``: ``question_id`` aliases
    to ``questionId``, ``option_ids`` to ``optionIds``, ``other_text`` to
    ``otherText``.
    """

    question_id: str = Field(min_length=1, max_length=120)
    option_ids: list[str] = Field(max_length=20)
    other_text: Optional[str] = Field(default=None, max_length=4000)


class RespondInteractionBody(_Strict):
    """Body for ``POST /teams/issues/{id}/interactions/{iid}/respond``
    (``ask_user_questions`` only).

    Mirrors upstream ``respondIssueThreadInteractionSchema``: ``answers`` is
    required, ``summary_markdown`` (alias ``summaryMarkdown``) is optional.
    """

    answers: list[InteractionAnswer] = Field(max_length=20)
    summary_markdown: Optional[str] = Field(default=None, max_length=20000)


class CancelInteractionBody(_Strict):
    """Body for ``POST /teams/issues/{id}/interactions/{iid}/cancel``
    (``ask_user_questions`` only).

    Mirrors upstream ``cancelIssueThreadInteractionSchema``: ``reason`` is
    optional.
    """

    reason: Optional[str] = Field(default=None, max_length=4000)


class SkipInteractionBody(_Strict):
    """Body for ``POST /teams/issues/{id}/interactions/{iid}/skip``.

    Mirrors upstream ``skipIssueThreadInteractionSchema``: ``reason`` is
    optional (stock's composer Skip sends ``{}``).
    """

    reason: Optional[str] = Field(default=None, max_length=4000)


class InteractionVerdict(_Strict):
    """One entry of a verdicts body — upstream's ``{id, verdict, reason?}``."""

    id: str = Field(min_length=1, max_length=120)
    verdict: Literal["approve", "reject", "defer"]
    reason: Optional[str] = Field(default=None, max_length=4000)


class SubmitInteractionVerdictsBody(_Strict):
    """Body for ``POST /teams/issues/{id}/interactions/{iid}/verdicts``
    (``request_item_verdicts`` only).

    Mirrors upstream ``submitIssueThreadInteractionVerdictsSchema``: 1..200
    verdicts (``REQUEST_ITEM_VERDICTS_ITEM_LIMIT``), item ids unique.
    """

    verdicts: list[InteractionVerdict] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [v.id for v in self.verdicts]
        if len(ids) != len(set(ids)):
            raise ValueError("verdict item ids must be unique")
        return self


class PatchCompanySettingsBody(_Strict):
    """Tenant-safe subset of company PATCH. No instance settings, no
    branding overrides that affect other tenants, no adapter fields.

    ``name`` must match Paperclip's company field exactly. Paperclip's
    ``updateCompanySchema`` is a default-mode ``z.object``, which
    *silently strips* unknown keys — so a mismatched key (this field was
    once ``display_name`` → ``displayName``) is dropped downstream and
    the rename no-ops with no error anywhere. Keep this named ``name``.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=4000)


class WatchdogDecisionBody(_Strict):
    # Mirrors upstream's watchdog-decisions route contract: the decision enum
    # is validated here so junk 422s at the BFF boundary; ``snoozedUntil``'s
    # required-future-when-snoozing rule stays upstream (it owns the clock).
    decision: str = Field(pattern=r"^(snooze|continue|dismissed_false_positive)$")
    evaluation_issue_id: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=4000)
    snoozed_until: Optional[str] = None


# ---------------------------------------------------------------------------
# Issue work-products (typed deliverables + review lifecycle)
#
# Whitelisted bodies for creating a work-product and driving its review
# transition. As with every mutating Teams body, ``extra="forbid"`` (from
# ``_Strict``) rejects a smuggled ``adapterType``/``adapterConfig`` with 422
# at the FastAPI boundary. The enums mirror upstream
# ``createIssueWorkProductSchema`` / ``updateIssueWorkProductSchema``
# (paperclip/packages/shared/src/validators/work-product.ts) so junk 422s
# here rather than being forwarded for upstream to reject opaquely. Routers
# forward with ``model_dump(by_alias=True, ...)`` so the camelCase aliases
# (``reviewState``, ``isPrimary``, ``projectId``) land on the wire.
# ---------------------------------------------------------------------------

_WORK_PRODUCT_TYPE = r"^(preview_url|runtime_service|pull_request|branch|commit|artifact|document)$"
_WORK_PRODUCT_STATUS = r"^(active|ready_for_review|approved|changes_requested|merged|closed|failed|archived|draft)$"
_WORK_PRODUCT_REVIEW_STATE = r"^(none|needs_board_review|approved|changes_requested)$"


class CreateWorkProductBody(_Strict):
    """Body for ``POST /teams/issues/{id}/work-products``.

    Subset of upstream ``createIssueWorkProductSchema``. ``type`` / ``provider``
    / ``title`` are required; the rest default upstream. ``metadata`` is a
    free-form passthrough dict (upstream validates the nested shape per type).
    """

    type: str = Field(pattern=_WORK_PRODUCT_TYPE)
    provider: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    url: Optional[str] = Field(default=None, max_length=2000)
    summary: Optional[str] = Field(default=None, max_length=20000)
    status: Optional[str] = Field(default=None, pattern=_WORK_PRODUCT_STATUS)
    review_state: Optional[str] = Field(default=None, pattern=_WORK_PRODUCT_REVIEW_STATE)
    is_primary: Optional[bool] = None
    project_id: Optional[str] = None
    external_id: Optional[str] = Field(default=None, max_length=200)
    metadata: Optional[dict] = None


_RECOVERY_ACTION_ID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class ResolveIssueRecoveryActionBody(_Strict):
    """Body for ``POST /teams/issues/{id}/recovery-actions/resolve``.

    Mirrors upstream's ``resolveIssueRecoveryActionSchema`` (``.strict()``,
    packages/shared/src/validators/issue.ts @ v2026.916.1-isol8.2), including its
    outcome ↔ ``sourceIssueStatus`` pairing. ``executionReconciliation`` is
    deliberately NOT accepted: it is the provider-stopped evidence attestation
    for reconciliation-class causes, and the ported card hides itself for those
    (``requiresExecutionReconciliation``) — so nothing here ever sends it.
    """

    action_id: Optional[str] = Field(default=None, pattern=_RECOVERY_ACTION_ID)
    outcome: str = Field(pattern=r"^(restored|false_positive|blocked|cancelled)$")
    source_issue_status: str = Field(pattern=r"^(todo|done|in_review|blocked)$")
    resolution_note: Optional[str] = None

    @model_validator(mode="after")
    def _outcome_matches_status(self) -> "ResolveIssueRecoveryActionBody":
        allowed = {
            "restored": {"todo", "done", "in_review"},
            "blocked": {"blocked"},
            "false_positive": {"done", "in_review"},
            "cancelled": {"done", "in_review"},
        }[self.outcome]
        if self.source_issue_status not in allowed:
            raise ValueError(f"outcome {self.outcome!r} requires sourceIssueStatus in {sorted(allowed)}")
        return self


_Uuid = Annotated[str, Field(pattern=_RECOVERY_ACTION_ID)]


class RefreshExternalObjectsBody(_Strict):
    """Body for ``POST /teams/issues/{id}/external-objects/refresh``. Mirrors
    upstream's ``refreshExternalObjectsSchema`` (``.strict()``, ≤50 uuids)."""

    object_ids: Optional[list[_Uuid]] = Field(default=None, max_length=50)


class ExternalObjectSummariesBody(_Strict):
    """Body for ``POST /teams/issues/external-object-summaries``. Mirrors
    upstream's ``externalObjectSummariesSchema`` (``.strict()``, ≤1000 uuids)."""

    issue_ids: list[_Uuid] = Field(max_length=1000)


class PatchWorkProductBody(_Strict):
    """Body for ``PATCH /teams/issues/{id}/work-products/{wpid}``.

    Narrowed to the board review DECISION only. The board UI makes exactly two
    decisions — Approve / Request-changes — so ``review_state`` (alias
    ``reviewState``) is REQUIRED and constrained to ``approved`` |
    ``changes_requested``. The descriptive/mutable fields the UI never sets
    (status / title / url / summary / isPrimary) are intentionally dropped:
    this route can only record a review verdict, never rewrite a deliverable.
    """

    review_state: str = Field(pattern=r"^(approved|changes_requested)$")


# ---------------------------------------------------------------------------
# Issue tree-control (pause / resume / cancel a runaway subtree)
#
# Whitelisted bodies for the operator safety lever. Mirror the upstream Zod
# ``.strict()`` schemas (paperclip/packages/shared/src/validators/
# issue-tree-control.ts): a smuggled field (e.g. ``adapterType``) is 422'd at
# the FastAPI boundary. ``_Strict``'s ``to_camel`` generator emits the
# camelCase shape upstream expects — routers forward with
# ``model_dump(by_alias=True, exclude_none=True)``.
# ---------------------------------------------------------------------------

# Upstream ISSUE_TREE_CONTROL_MODES also includes ``restore``, which — with
# ``metadata.wakeAgents === true`` — spawns ``heartbeat.wakeup`` agent runs
# (inference spend) that the BFF forwards ungated (no ``require_trial_runway``).
# The UI defers restore, so the BFF whitelist deliberately omits it: pause /
# resume / cancel only. (ISSUE_TREE_HOLD_RELEASE_POLICY_STRATEGIES also upstream.)
_TREE_CONTROL_MODE = r"^(pause|resume|cancel)$"


class TreeHoldReleasePolicyBody(_Strict):
    """Nested release policy — mirrors ``issueTreeHoldReleasePolicySchema``.

    ``strategy`` defaults to ``manual`` upstream; both fields optional here so a
    bare hold is valid. ``note`` aliases straight through (single word).
    """

    strategy: Optional[str] = Field(default=None, pattern=r"^(manual|after_active_runs_finish)$")
    note: Optional[str] = Field(default=None, min_length=1, max_length=500)


class PreviewTreeControlBody(_Strict):
    """Body for ``POST /teams/issues/{id}/tree-control/preview``.

    Mirrors upstream ``previewIssueTreeControlSchema``: ``mode`` required,
    ``release_policy`` optional.
    """

    mode: str = Field(pattern=_TREE_CONTROL_MODE)
    release_policy: Optional[TreeHoldReleasePolicyBody] = None


class CreateTreeHoldBody(_Strict):
    """Body for ``POST /teams/issues/{id}/tree-holds``.

    Mirrors upstream ``createIssueTreeHoldSchema``: ``mode`` required;
    ``reason`` / ``release_policy`` optional. Upstream's free-form ``metadata``
    (``record(string, unknown)``) is deliberately NOT accepted here: the restore
    path reads ``metadata.wakeAgents`` to spawn ``heartbeat.wakeup`` agent runs
    (inference spend) that the BFF forwards ungated, and the UI never sends it.
    Dropping the field means ``extra="forbid"`` 422s any smuggled ``wakeAgents``
    at the FastAPI boundary (defense-in-depth alongside the narrowed ``mode``
    whitelist, which already rejects ``restore``).
    """

    mode: str = Field(pattern=_TREE_CONTROL_MODE)
    reason: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    release_policy: Optional[TreeHoldReleasePolicyBody] = None


class ReleaseTreeHoldBody(_Strict):
    """Body for ``POST /teams/issues/{id}/tree-holds/{holdId}/release``.

    Mirrors upstream ``releaseIssueTreeHoldSchema``: every field optional.
    """

    reason: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    release_policy: Optional[TreeHoldReleasePolicyBody] = None
    metadata: Optional[dict] = None


# ---------------------------------------------------------------------------
# Plan-document review — upsert + inline annotation threads.
#
# Whitelisted bodies for the issue-document review surface. As with every
# mutating Teams body, ``extra="forbid"`` (from ``_Strict``) rejects a smuggled
# field with 422 at the FastAPI boundary. Enums / limits mirror upstream
# ``upsertIssueDocumentSchema`` (paperclip/packages/shared/src/validators/
# issue.ts) and ``createDocumentAnnotationThreadSchema`` /
# ``createDocumentAnnotationCommentSchema`` /
# ``updateDocumentAnnotationThreadSchema``
# (paperclip/packages/shared/src/validators/document-annotation.ts) so junk
# 422s here rather than being forwarded for upstream to reject opaquely.
# Routers forward with ``model_dump(by_alias=True, ...)`` so the camelCase
# aliases (``changeSummary``, ``baseRevisionId``, ``baseRevisionNumber``,
# ``issueCommentId``) land on the wire.
# ---------------------------------------------------------------------------

_DOCUMENT_FORMAT = r"^(markdown)$"
_ANNOTATION_THREAD_STATUS = r"^(open|resolved)$"


class UpsertDocumentBody(_Strict):
    """Body for ``PUT /teams/issues/{id}/documents/{key}``.

    Subset of upstream ``upsertIssueDocumentSchema``. ``format`` + ``body`` are
    required; ``title`` / ``change_summary`` / ``base_revision_id`` are optional.
    Board edits go through this route and mint a new revision upstream.
    """

    format: str = Field(pattern=_DOCUMENT_FORMAT)
    body: str = Field(max_length=524288)
    title: Optional[str] = Field(default=None, max_length=200)
    change_summary: Optional[str] = Field(default=None, max_length=500)
    base_revision_id: Optional[str] = Field(default=None, max_length=64)


class AnnotationTextQuoteSelector(_Strict):
    """The text-quote half of an annotation anchor (exact + surrounding context)."""

    exact: str = Field(min_length=1, max_length=10000)
    prefix: str = Field(default="", max_length=1000)
    suffix: str = Field(default="", max_length=1000)


class AnnotationTextPositionSelector(_Strict):
    """The character-offset half of an annotation anchor.

    Mirrors upstream ``documentAnnotationTextPositionSelectorSchema`` — both the
    normalized (whitespace-collapsed) and raw-markdown spans must have
    ``end > start`` (enforced upstream; the non-negativity is enforced here via
    ``ge=0``).
    """

    normalized_start: int = Field(ge=0)
    normalized_end: int = Field(ge=0)
    markdown_start: int = Field(ge=0)
    markdown_end: int = Field(ge=0)


class AnnotationAnchorSelector(_Strict):
    """The full annotation anchor: a text-quote selector + a position selector."""

    quote: AnnotationTextQuoteSelector
    position: AnnotationTextPositionSelector


class CreateDocumentAnnotationThreadBody(_Strict):
    """Body for ``POST /teams/issues/{id}/documents/{key}/annotations``.

    Mirrors upstream ``createDocumentAnnotationThreadSchema``: the anchor is
    pinned to a specific base revision (id + number), the ``selector`` carries
    the text-quote + position anchor, and ``body`` is the first comment.
    """

    base_revision_id: str = Field(min_length=1, max_length=64)
    base_revision_number: int = Field(gt=0)
    selector: AnnotationAnchorSelector
    body: str = Field(min_length=1, max_length=20000)
    issue_comment_id: Optional[str] = Field(default=None, max_length=64)


class AddDocumentAnnotationCommentBody(_Strict):
    """Body for ``POST /teams/issues/{id}/documents/{key}/annotations/{threadId}/comments``.

    Mirrors upstream ``createDocumentAnnotationCommentSchema``.
    """

    body: str = Field(min_length=1, max_length=20000)
    issue_comment_id: Optional[str] = Field(default=None, max_length=64)


class UpdateDocumentAnnotationThreadBody(_Strict):
    """Body for ``PATCH /teams/issues/{id}/documents/{key}/annotations/{threadId}``.

    Mirrors upstream ``updateDocumentAnnotationThreadSchema``: the board UI
    makes exactly one change here — resolve or reopen — so ``status`` is
    REQUIRED and constrained to ``open`` | ``resolved``.
    """

    status: str = Field(pattern=_ANNOTATION_THREAD_STATUS)


class PatchCaseBody(_Strict):
    """Body for ``PATCH /teams/cases/{id}``.

    Upstream ``patchCaseSchema`` accepts more (title, summary, fields, parent,
    project), but those are agent-authored: the board UI's only writes are the
    status and the full desired label-id set (replace semantics).
    """

    status: Optional[str] = Field(default=None, pattern=r"^(draft|in_progress|in_review|approved|done|cancelled)$")
    label_ids: Optional[list[str]] = Field(default=None, max_length=100)


# Upstream ``ITEM_KEY_RE`` in ``server/src/routes/inbox-dismissals.ts``.
INBOX_ITEM_KEY_PATTERN = r"^(approval|join|run|attention):.+$"


class InboxDismissalBody(_Strict):
    """``POST /teams/inbox-dismissals``. Upstream validates the snooze/dismiss
    pairing (``snoozedUntil`` required for, and only for, a snooze)."""

    item_key: str = Field(min_length=1, max_length=500, pattern=INBOX_ITEM_KEY_PATTERN)
    kind: Optional[Literal["dismiss", "snooze"]] = None
    snoozed_until: Optional[str] = Field(default=None, max_length=64)
