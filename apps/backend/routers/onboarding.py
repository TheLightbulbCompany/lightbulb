"""First-run onboarding endpoints.

POST /api/v1/onboarding/brief is INTAKE ONLY: it records the funnel answer
under the owner id (put-if-absent, ``PaperclipRepo.put_pending_brief``) and
returns. It needs auth and nothing else — no Teams context, no Paperclip, no
owner row.

FULFILMENT — the generation-fenced ``funnel_state: not_started -> submitted``
claim, the company goal + project seed, staging BRIEF.md into Friday's
pre-boot workspace, and Friday's kickoff — is
``core.services.onboarding_brief.reconcile_pending_brief``, driven off the
``/container/status`` poll (and fired once from here, for the case where the
workspace is already up).

The split exists because the funnel deliberately renders DURING provisioning,
to fill the dead time: the answer routinely arrives ~30s before there is an
owner row to claim against. Fused, this endpoint 202'd until Paperclip came
up and the BROWSER retried it — for ~16s, then it showed an error and the
answer, which lived nowhere else, was gone. Everything downstream of intake is
now a server-owned reconciler like its neighbours (``ensure_user_ready``,
``maybe_dispatch_bootstrap``): idempotent, claim-fenced, retried by the poll.

Onboarding truth is company-scoped, not per-container and not per-user: an
org shares one container AND one owner row, so onboarding happens once for
the whole org regardless of which member's tab submits. Both halves key off
``resolve_owner_id(auth)`` (org_id for an org, else the user_id) — intake for
the pending item, the reconciler via ``PaperclipRepo.resolve_owner_row`` for
the row ``provision_org`` wrote and fenced with ``onboarding_generation``.

Per the 2026-07-24 architecture decision (see
``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md`` §2),
this replaces the old ``containers.onboarded_at`` stamp (S2) and the
``__onboarded__`` sentinel (S3) entirely: ``GET /container/status`` reads
``funnel_state == "complete"`` off the owner row. The funnel claim only
advances it to ``submitted``; ``complete`` is set once the bootstrap
dispatcher (``core.services.bootstrap_dispatcher``) observes Friday's
first-run kickoff actually deliver (``chat.final``).

There is no first-run BOOTSTRAP.md any more — OpenClaw's boot-time
reconcile (``reconcileWorkspaceBootstrapCompletionState``) never surfaces a
seeded BOOTSTRAP.md into Friday's context once ``setupCompletedAt`` is
pre-marked (see ``core/services/friday_identity.py``'s docstring and
research §5 of the architecture doc's companion) — the file was inert.
The server-dispatched ``chat.send`` replaces it as a first-class, observed
operation instead of a file Friday is guaranteed never to read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.observability.product_analytics import capture_product_event
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.onboarding_brief import build_onboarding_brief_text, reconcile_pending_brief
from core.services.status_events import publish_status_changed

logger = logging.getLogger(__name__)

router = APIRouter()


class BriefBody(BaseModel):
    path: Literal["new", "grow"]
    input: str | None = Field(default=None, max_length=2000)
    detail: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_input(self) -> "BriefBody":
        """`new` may omit the idea; `grow` may not omit the website.

        An absent `input` on the `new` path is the "I don't know yet — you
        pick" button: the missing idea IS the signal, and BRIEF.md tells
        Friday to choose one. Blank-but-present is normalized to absent so a
        client posting "" gets that same brief rather than one quoting an
        empty string back at her.
        """
        cleaned = (self.input or "").strip()
        if not cleaned:
            if self.path == "grow":
                raise ValueError("input is required on the grow path")
            self.input = None
        elif len(cleaned) < 3:
            raise ValueError("input must be at least 3 characters")
        else:
            self.input = cleaned
        return self


@router.post("/brief")
async def submit_brief(
    body: BriefBody,
    auth: AuthContext = Depends(get_current_user),
):
    """Record the funnel answer. That is the whole endpoint.

    INTAKE ONLY -- deliberately depends on nothing but auth. It does not
    resolve a Teams context, does not touch Paperclip, does not read the owner
    row. Everything that needs the workspace to be up (the funnel claim, the
    goal/project seed, staging BRIEF.md, Friday's kickoff) happens in
    ``onboarding_brief.reconcile_pending_brief``, fired below and again from
    every ``/container/status`` poll.

    Why the split: the funnel renders DURING provisioning -- on purpose, to
    fill the dead time -- so the answer routinely arrives ~30s before there is
    anything to write it to. When fulfilment lived here, the endpoint 202'd
    (``_ctx``: "team workspace provisioning") until Paperclip came up and the
    BROWSER was the retry engine. It gave up after ~16s, showed "Couldn't save
    that just yet", and -- worse than the error -- the brief existed nowhere
    but that tab, so closing it lost the answer with no server-side trace.

    One-shot per COMPANY: the pending item is written put-if-absent under the
    OWNER id, so a second org member submitting concurrently gets
    ``already_submitted``. Open to any org member, not just admins -- the
    dedupe makes a non-admin winning harmless (they supply the provisional
    project name, which Friday renames a turn later), and an admin-only gate
    would leave an org whose admin never opens the app permanently stuck.
    """
    owner_id = resolve_owner_id(auth)
    repo = PaperclipRepo(table_name="paperclip-companies")

    # The user's own words, in the natural-language form the bootstrap
    # dispatcher weaves into Friday's visible first message
    # (build_first_run_message). None on the "you pick" path. Built HERE
    # rather than at drain time so the phrasing that reaches Friday is the
    # one this submission produced.
    brief_text = build_onboarding_brief_text(path=body.path, user_input=body.input, detail=body.detail)

    landed = await repo.put_pending_brief(
        owner_id,
        path=body.path,
        user_input=body.input,
        detail=body.detail,
        brief_text=brief_text,
        submitted_by=auth.user_id,
    )
    if not landed:
        # An answer is already pending for this company (a peer member, or a
        # double submit). Same terminal verdict the client gets from a
        # already-claimed funnel -- stop showing them the funnel.
        return {"status": "already_submitted"}

    # Nudge the owner's tabs: `compute_status` now reports funnel_state
    # "submitted" off this pending item, so every tab on this company (not
    # just the submitter's) can drop the funnel without waiting out the poll.
    publish_status_changed(owner_id)

    # Fire the drain in case the workspace is ALREADY up -- makes the split
    # invisible for a user who took their time on the funnel. If it is still
    # provisioning this is a cheap no-op and the /container/status poll drains
    # it later; reconcile_pending_brief never raises.
    asyncio.create_task(reconcile_pending_brief(owner_id))

    capture_product_event(
        distinct_id=auth.user_id,
        event="first_run_brief_submitted",
        properties={"path": body.path, "no_idea": body.input is None},
    )
    # "staged" is kept as the wire word for a fresh intake: it already means
    # "the server has your brief", so a frontend/backend deploy skew is a
    # non-event.
    return {"status": "staged"}
