"""Pydantic body whitelist schemas for the Teams BFF.

Spec §5 invariant: every mutating endpoint that touches an agent uses
``model_config = ConfigDict(extra="forbid")`` so a request that includes
``adapterType``, ``adapterConfig``, ``url``, ``headers``, or any other
non-whitelisted field is rejected with 422 at the FastAPI boundary.
The BFF synthesizes the adapter block server-side via
``core.services.paperclip_adapter_config.synthesize_openclaw_adapter``.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
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


class CreateIssueBody(_Strict):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    project_id: Optional[str] = None
    assignee_agent_id: Optional[str] = None
    priority: Optional[str] = Field(default=None, pattern=r"^(critical|high|medium|low)$")


class PatchIssueBody(_Strict):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    project_id: Optional[str] = None
    assignee_agent_id: Optional[str] = None
    priority: Optional[str] = Field(default=None, pattern=r"^(critical|high|medium|low)$")
    status: Optional[str] = None
    column_id: Optional[str] = None
    # Full desired blocker set (replace semantics) — the issue-detail "Blocked
    # by" picker sends the complete array, matching upstream Paperclip. The
    # ``_Strict`` alias_generator emits this as ``blockedByIssueIds`` on the
    # wire (Paperclip's PATCH /api/issues/:id reads camelCase).
    blocked_by_issue_ids: Optional[list[str]] = None


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


class CreateRoutineBody(_Strict):
    """Body for ``POST /teams/routines``.

    Mirrors upstream's ``createRoutineSchema``
    (paperclip/packages/shared/src/validators/routine.ts:52-65): a routine
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


class CreateRoutineTriggerBody(_Strict):
    """Body for ``POST /teams/routines/{id}/triggers``.

    Schedule-only subset of upstream's ``createRoutineTriggerSchema``
    (paperclip/packages/shared/src/validators/routine.ts:118-132). The
    Teams create dialog only wires the ``schedule`` kind (cron); webhook /
    api triggers are deferred. ``kind`` is fixed to ``schedule`` so the
    discriminated union resolves cleanly upstream.
    """

    kind: str = Field(default="schedule", pattern=r"^schedule$")
    cron_expression: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    label: Optional[str] = Field(default=None, max_length=120)


class CreateGoalBody(_Strict):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    parent_id: Optional[str] = None


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


class ApproveApprovalBody(_Strict):
    note: Optional[str] = Field(default=None, max_length=2000)


class RejectApprovalBody(_Strict):
    reason: str = Field(min_length=1, max_length=2000)


class PatchCompanySettingsBody(_Strict):
    """Tenant-safe subset of company PATCH. No instance settings, no
    branding overrides that affect other tenants, no adapter fields."""

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=4000)
