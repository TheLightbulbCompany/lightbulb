"""Server-owned bootstrap dispatch -- INV-5, spec PR-6 (BOOTSTRAP).

Friday's first-run kickoff (``FIRST_RUN_PROMPT``) is dispatched by the
BACKEND, exactly once per ``onboarding_generation``, with no browser tab
required. This replaces the client-side latch it used to depend on
(``useFirstRunKickoff`` + Clerk ``firstRun.started``/``completed``) and the
thread-emptiness reconcile that latch needed to self-heal (#1391) -- both
deleted in this same PR. See
``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md`` §2
("Server-owned bootstrap") and INV-5.

Call ``maybe_dispatch_bootstrap(owner_id)`` from any code path that already
knows the funnel might be actionable:

  - ``routers/onboarding.py:submit_brief`` -- right after the funnel claim
    lands, in case the runtime is ALREADY up (the common case: the funnel
    UI only renders inside a live chat surface, so by the time someone
    submits it the container has almost always already provisioned).
  - ``routers/container.py:container_status`` -- every poll already
    observes runtime-phase transitions; if the funnel was submitted while
    the runtime was still cold, this is the "next lifecycle -> ready
    transition" the spec requires waiting for instead of storing a stale
    handoff token.

Both call sites overlap: this function's own guards (funnel_state ==
"submitted", bootstrap_state == "not_started", runtime reachable) make a
redundant call a cheap no-op, so neither caller needs to reason about the
other, and neither needs to be the ONLY caller.

**Onboarding dict, not a repo round trip by default.** Every field this
module reads comes from the SAME bridge-dict shape
``PaperclipRepo.get_onboarding_state`` / ``readiness_state.
resolve_onboarding_state`` return (12 spec-fixed onboarding keys + the
row's own ``user_id`` PK + ``onboarding_submitted_by``) -- never a
``PaperclipCompany`` object. Callers that already resolved this dict for
their own purposes (``submit_brief`` builds one from its post-claim
knowledge; ``container_status`` fetches it via
``readiness_state.resolve_onboarding_state`` and also feeds it to
``compute_status``) pass it via the ``onboarding=`` kwarg so this function
never re-queries the paperclip-companies row -- load-bearing on the
``/status`` poll path, which would otherwise double the by-org-id GSI read
``compute_status`` already does, fleet-wide, on every ~3s ready poll (the
overwhelming majority of which have nothing to dispatch). Omit it to have
this function resolve it itself (the pattern every other guard here uses).

**Dispatch addresses the SUBMITTING member, not the row's PK.**
``/onboarding/brief`` is open to any org member, and the paperclip-companies
row is keyed by the ORG CREATOR's user_id (see ``resolve_owner_row``) --
NOT necessarily who submitted. The gateway demuxes streamed events by the
member segment of ``sessionKey``, and client history queries key off the
current user's own Clerk id, so dispatching under the row's PK when a
non-creator member submitted would land Friday's live kickoff (and its
``chat.history``) in the CREATOR's session -- invisible to the actual
submitter the prompt addresses ("I'm watching"). ``onboarding_submitted_by``
(stamped by ``claim_funnel_submission`` at claim time) is therefore what
``session_key``/``member_id`` are built from, falling back to the row's
``user_id`` only when absent (legacy/degraded rows). For a personal owner
the two are always equal, so this is a no-op distinction there. The row's
``user_id`` is STILL what every DDB write in this module addresses
(``claim_bootstrap_dispatch`` etc. key on it) -- only the gateway-facing
session/member identity moves to the submitter.

INV-5: dispatch is claimed via a conditional ``bootstrap_state:
not_started -> dispatched`` write fenced on ``onboarding_generation``
(``PaperclipRepo.claim_bootstrap_dispatch``) -- only the caller that wins
the conditional write ever calls chat.send. A caller that loses the race
(concurrent /status poll + /brief submit, two backend instances, two polls)
simply returns.

Completion is OBSERVED, not assumed: the gateway pool's run-completion
observer (``GatewayConnectionPool.register_run_completion_callback``,
fired from the SAME TRUE-terminal gate the run-completion push uses) is
what flips ``bootstrap_state`` to ``delivered`` on ``chat.final`` (and
``funnel_state`` to ``complete`` -- see
``PaperclipRepo.mark_bootstrap_delivered``). A ``chat`` terminal
``error``/``aborted``, or a chat.send that never reaches OpenClaw at all,
marks ``bootstrap_state`` ``failed`` with the reason recorded.

CRITICAL UX invariant this dispatcher must never break: dispatching
chat.send from the BACKEND instead of a browser tab does NOT change where
Friday's work is SEEN. The gateway pool's ``_forward_to_frontends`` /
``_transform_agent_event`` broadcast every agent event (streaming text,
tool_start/tool_end, chat.final) to watching frontends off Friday's OUTPUT
stream, regardless of who sent the input -- so a watching user still sees
Friday work live, and a not-watching user sees it via ``chat.history`` on
return. This module dispatches chat.send exactly the way
``routers/websocket_chat.py`` does (``ensure_connection`` +
``mark_run_active`` + ``send_rpc``), so that forwarding path is unaffected.

There is no retry/backoff loop in THIS PR -- that is the generalized
``reconciliation_worker`` (spec PR-8), which retries a dispatched-but-
undelivered (or failed) bootstrap on its own sweep. This PR only implements
the correct-path single dispatch + its own completion observer, bounded by
``bootstrap_attempts`` so a runaway caller can't spin forever.

If the runtime is not up yet (no running container / no task IP), this
function does NOTHING -- no dispatch, no partial write, no stale handoff
token. Per the spec: "If the runtime isn't up at brief-submit, WAIT for the
next lifecycle transition -- do NOT store a stale handoff token." The next
caller (a later ``/container/status`` poll observing the runtime come up)
re-evaluates the same guards from scratch and dispatches then.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from core.repositories.paperclip_repo import BootstrapClaimResult

logger = logging.getLogger(__name__)

FIRST_RUN_PROMPT = (
    "Friday — I just set up my company. Read BRIEF.md, then really dig into my business and "
    "market and write up what you learn (a market-research doc and a mission doc). Once you "
    "get it, put my three best next moves on the board as tasks. Talk me through it as you go — "
    "I'm watching."
)

# A caller checks this BEFORE claiming (not the repo's CAS itself, which has
# no retry loop in this PR -- see module docstring). Bounds how many times a
# stuck/failed dispatch can be re-claimed once the generalized
# reconciliation_worker (spec PR-8) starts resetting bootstrap_state back to
# not_started for a retry. A small number: a genuinely stuck dispatch needs
# investigation, not an unbounded retry storm against Friday's session.
MAX_BOOTSTRAP_ATTEMPTS = 3


def _paperclip_repo():
    """Local import so ``patch("core.repositories.paperclip_repo.PaperclipRepo", ...)``
    (the pattern every existing test double in this codebase uses) keeps
    working -- mirrors ``readiness_state._paperclip_repo``'s own idiom.
    """
    from core.repositories.paperclip_repo import PaperclipRepo

    return PaperclipRepo(table_name="paperclip-companies")


async def _resolve_default_agent_id(owner_id: str) -> str | None:
    """Friday's OpenClaw agent id, read straight off EFS's openclaw.json --
    no RPC round trip needed (and no live gateway connection required just
    to answer this). ``_build_agents_list`` (core/containers/config.py)
    marks exactly one ``agents.list[]`` entry ``"default": True`` -- the
    Paperclip-role="ceo" agent, i.e. Friday -- at provision time. Returns
    None if the config isn't staged yet or carries no default entry
    (shouldn't happen for an owner whose funnel was even submittable, but a
    dispatcher must degrade, never raise).
    """
    from core.containers.config import read_openclaw_config_from_efs

    config = await read_openclaw_config_from_efs(owner_id)
    if not config:
        return None
    agents = (config.get("agents") or {}).get("list") or []
    for entry in agents:
        if isinstance(entry, dict) and entry.get("default") is True:
            agent_id = entry.get("id")
            return agent_id if isinstance(agent_id, str) and agent_id else None
    return None


async def maybe_dispatch_bootstrap(owner_id: str, *, onboarding: dict | None = None) -> None:
    """Best-effort: dispatch Friday's first-run kickoff if it is this
    caller's turn to (INV-5) and the runtime is reachable right now.

    ``onboarding``: an already-resolved ``get_onboarding_state``-shaped
    dict (see module docstring) -- pass this to avoid a redundant
    paperclip-companies read when the caller already has one. Resolved via
    ``readiness_state.resolve_onboarding_state`` when omitted.

    Never raises -- every guard below is a plain early return, and the
    dispatch itself is wrapped so a failure marks ``bootstrap_state``
    ``failed`` (recording the reason) instead of propagating into the
    caller's request (``submit_brief`` / ``container_status``), neither of
    which should fail because a best-effort kickoff hiccupped.
    """
    repo = _paperclip_repo()
    if onboarding is None:
        from core.services.readiness_state import resolve_onboarding_state

        onboarding = await resolve_onboarding_state(owner_id)

    generation = onboarding.get("onboarding_generation")
    if not generation:
        return
    if onboarding.get("funnel_state") != "submitted":
        # not_started: nothing to dispatch yet. complete: already delivered.
        return
    if (onboarding.get("bootstrap_state") or "not_started") != "not_started":
        # Already dispatched/delivered/failed by a prior attempt -- this PR
        # dispatches at most once per onboarding_generation (INV-5); a later
        # PR's reconciliation_worker owns retrying a stuck/failed dispatch.
        return
    if (onboarding.get("bootstrap_attempts") or 0) >= MAX_BOOTSTRAP_ATTEMPTS:
        logger.warning(
            "bootstrap_dispatcher: max attempts (%d) reached for %s -- standing down",
            MAX_BOOTSTRAP_ATTEMPTS,
            owner_id,
        )
        return

    # The paperclip-companies row's PK -- the ORG CREATOR's user_id for an
    # org, else owner_id -- NOT necessarily the submitting member. Every DDB
    # write below addresses the row via this. Defensive: get_onboarding_state
    # only omits this for a genuinely-absent row, which the generation check
    # above should already have caught (a row with no onboarding_generation
    # can't be "submitted" either) -- but a dispatcher must degrade, never
    # KeyError, on a shape it doesn't fully trust.
    row_user_id = onboarding.get("user_id")
    if not row_user_id:
        logger.warning("bootstrap_dispatcher: onboarding dict for %s carries no row user_id", owner_id)
        return

    # Who actually submitted the brief (may differ from row_user_id for an
    # org -- see module docstring). Falls back to the row's own PK for
    # legacy/degraded rows that predate onboarding_submitted_by.
    submitted_by = onboarding.get("onboarding_submitted_by") or row_user_id

    from core.containers import get_ecs_manager, get_gateway_pool

    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    except Exception:  # noqa: BLE001 — a degraded read must never break the caller
        logger.info("bootstrap_dispatcher: container resolve failed for %s", owner_id, exc_info=True)
        return
    if not container or not ip:
        # Runtime isn't up yet. Per the spec: wait for the next lifecycle
        # transition (the next caller that observes the runtime is up
        # re-evaluates from scratch) -- do NOT claim/write anything here.
        return

    friday_agent_id = await _resolve_default_agent_id(owner_id)
    if not friday_agent_id:
        logger.warning("bootstrap_dispatcher: no default agent resolved for %s", owner_id)
        return

    claim_result, dispatch_id = await repo.claim_bootstrap_dispatch(row_user_id, onboarding_generation=generation)
    if claim_result is not BootstrapClaimResult.WON:
        # NOT_READY: someone else already won (or already resolved) this
        # dispatch. STALE_GENERATION: the company moved on underneath us
        # (INV-4 silent no-op). Either way, this caller has nothing to do.
        return

    from routers.websocket_chat import build_chat_session_key

    pool = get_gateway_pool()
    # Addressed at the SUBMITTING member (not row_user_id) -- see module
    # docstring: this is what makes the kickoff visible to whoever actually
    # submitted the brief, not just the org creator.
    session_key = build_chat_session_key(friday_agent_id, submitted_by, None)
    run_id = str(uuid4())

    async def _on_terminal(state: str, payload: dict) -> None:
        try:
            if state == "final":
                await repo.mark_bootstrap_delivered(
                    row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id
                )
            else:
                err_msg = payload.get("errorMessage") or payload.get("error") or f"chat.{state}"
                await repo.mark_bootstrap_failed(
                    row_user_id,
                    onboarding_generation=generation,
                    dispatch_id=dispatch_id,
                    reason=str(err_msg),
                )
        except Exception:  # noqa: BLE001 — a completion-observer callback must never raise
            logger.exception(
                "bootstrap_dispatcher: completion observer failed for %s dispatch_id=%s",
                owner_id,
                dispatch_id,
            )

    registered = False
    try:
        await pool.ensure_connection(owner_id, ip, container["gateway_token"])
        # Register the completion observer BEFORE mark_run_active/send_rpc so
        # a terminal that arrives immediately after send_rpc returns (fast
        # container) can never race past an unregistered callback.
        registered = pool.register_run_completion_callback(owner_id, run_id, _on_terminal)
        if not registered:
            # Connection vanished between ensure_connection and here (should
            # not happen -- ensure_connection just created/returned it) --
            # degrade rather than dispatch a kickoff with no completion path.
            raise RuntimeError("gateway connection unavailable for run-completion registration")
        pool.mark_run_active(owner_id, session_key, friday_agent_id, member_id=submitted_by)
        await pool.send_rpc(
            user_id=owner_id,
            req_id=f"bootstrap-dispatch-{dispatch_id}",
            method="chat.send",
            params={
                "sessionKey": session_key,
                "message": FIRST_RUN_PROMPT,
                "idempotencyKey": run_id,
                "deliver": False,
            },
            ip=ip,
            token=container["gateway_token"],
        )
        logger.info("bootstrap_dispatcher: dispatched kickoff for %s dispatch_id=%s", owner_id, dispatch_id)
    except Exception as e:  # noqa: BLE001 — chat.send never reached OpenClaw
        logger.warning("bootstrap_dispatcher: dispatch failed for %s: %s", owner_id, e)
        pool.clear_active_run(owner_id, session_key)
        if registered:
            # send_rpc (or mark_run_active) raised AFTER the completion
            # observer was registered -- no terminal event will ever arrive
            # to consume it (chat.send never reached OpenClaw), so the
            # callback (and its closure) would otherwise leak in
            # _run_completion_callbacks until the GatewayConnection itself
            # is torn down. Unregister it explicitly, mirroring the
            # clear_active_run cleanup on the line above.
            pool.unregister_run_completion_callback(owner_id, run_id)
        try:
            await repo.mark_bootstrap_failed(
                row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id, reason=str(e)
            )
        except Exception:  # noqa: BLE001 — best-effort failure recording
            logger.exception("bootstrap_dispatcher: failed to record dispatch failure for %s", owner_id)
