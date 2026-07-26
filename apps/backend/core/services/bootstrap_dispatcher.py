"""Server-owned bootstrap dispatch -- INV-5, spec PR-6 (BOOTSTRAP).

Friday's first-run kickoff is dispatched by the BACKEND, exactly once per
``onboarding_generation``, with no browser tab required. The kickoff message
is the USER'S OWN BRIEF (``onboarding_brief.build_first_run_message`` woven
from the ``onboarding_brief_text`` persisted at ``/onboarding/brief`` submit),
so Friday's chat opens with the user's ask -- not a canned prompt. See
``docs/superpowers/specs/2026-07-25-onboarding-first-message-design.md``
(Option A). This replaces the client-side latch it used to depend on
(``useFirstRunKickoff`` + Clerk ``firstRun.started``/``completed``) and the
thread-emptiness reconcile that latch needed to self-heal -- both
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

Failure handling is TWO-TIERED (both bounded by ``bootstrap_attempts``):

* **In-dispatch connect retry.** The container row goes ``running`` when
  the ECS task starts, but OpenClaw's gateway can take 1-3 more minutes to
  actually listen on its port -- a dispatch fired in that window used to
  die on the first ``[Errno 111]`` connect refusal and mark the bootstrap
  failed (observed live 2026-07-24). Connection-level failures on the
  gateway WebSocket connect (``OSError``/timeouts -- NEVER chat-level
  errors) are therefore retried in place with backoff
  (``_CONNECT_RETRY_DELAYS_S``, ~3-4 min total budget), all within the ONE
  dispatch this caller claimed -- the claim/dispatch_id fencing is
  untouched, and concurrent pollers keep no-oping on
  ``bootstrap_state="dispatched"`` while we wait.
* **Cross-poll re-arm -- CONNECT-PHASE FAILURES ONLY.** When even that
  budget is exhausted, the failure is CLASSIFIED, and re-arm is admitted
  ONLY when the gateway connect never succeeded (nothing was ever written
  to the socket). A TRANSIENT connect-phase failure (connection errors/
  timeouts, ``_TRANSIENT_DISPATCH_ERRORS``) re-arms the row via
  ``PaperclipRepo.rearm_bootstrap_dispatch`` (``bootstrap_state`` back to
  ``not_started``, attempts + last_error kept) so the next
  ``/container/status`` poll re-dispatches -- but only while
  ``bootstrap_attempts`` is under ``MAX_BOOTSTRAP_ATTEMPTS``. At the cap,
  for any DETERMINISTIC connect failure (auth rejections, anything that
  would fail identically on a retry), OR for ANY failure AFTER a successful
  connect, it marks ``failed`` for real.

  The post-connect exclusion is the load-bearing safety property: once the
  connect succeeds, a ``chat.send`` may already have been written and
  ACCEPTED by OpenClaw when its ack merely times out or the socket drops
  WHILE WAITING (``connection_pool.send_rpc``). Re-arming there would mint a
  NEW ``run_id`` / ``idempotencyKey`` on the next poll (the dispatch derives
  both fresh, NOT from the stable ``bootstrap_dispatch_id``), which OpenClaw
  cannot dedup against a run it already took -- so Friday's kickoff could
  execute more than once. Marking ``failed`` instead keeps that ambiguous
  case recoverable by a human / the next ``onboarding_generation`` without
  ever risking a double kickoff. Without ANY re-arm, ``failed`` was terminal
  (``claim_bootstrap_dispatch`` admits only ``not_started``) and one boot
  race killed the onboarding forever; scoping re-arm to the provably-pre-send
  connect phase recovers the boot-window blip without reintroducing that risk.

If the runtime is not up yet (no running container / no task IP), this
function does NOTHING -- no dispatch, no partial write, no stale handoff
token. Per the spec: "If the runtime isn't up at brief-submit, WAIT for the
next lifecycle transition -- do NOT store a stale handoff token." The next
caller (a later ``/container/status`` poll observing the runtime come up)
re-evaluates the same guards from scratch and dispatches then.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from core.repositories.paperclip_repo import BootstrapClaimResult
from core.services.onboarding_brief import build_first_run_message

logger = logging.getLogger(__name__)

# A caller checks this BEFORE claiming (the repo's CAS itself carries no
# attempts guard). Bounds the total number of dispatches a transient re-arm
# (``rearm_bootstrap_dispatch`` -- see module docstring) can produce: each
# claim increments ``bootstrap_attempts``, the re-arm keeps it, and the
# failure classifier marks ``failed`` for real once the attempt that just
# died was the MAX_BOOTSTRAP_ATTEMPTS-th. A small number: a dispatch still
# failing after five spaced-out attempts (each with its own multi-minute
# connect-retry budget) needs investigation, not an unbounded retry storm
# against Friday's session.
MAX_BOOTSTRAP_ATTEMPTS = 5

# Backoff schedule (seconds) for connection-level failures on the gateway
# WebSocket connect, all inside ONE claimed dispatch. The ECS task reports
# RUNNING 1-3 minutes before OpenClaw's gateway listens on its port; this
# ladder (~3 min of waiting across 9 connect attempts) is sized to outlast
# that boot window so a normal cold boot never burns a bootstrap attempt.
_CONNECT_RETRY_DELAYS_S: tuple[float, ...] = (5, 10, 20, 30, 30, 30, 30, 30)

# Failure classes the dispatcher treats as TRANSIENT: connection-level
# failures (``OSError`` covers ConnectionRefusedError/ConnectionResetError/
# the raw [Errno 111] connect refusal) and timeouts (``asyncio.TimeoutError``
# IS ``TimeoutError`` on >=3.11). Everything else -- including
# ``GatewayConnectAuthError`` (a RuntimeError: same credentials cannot
# succeed on retry) and chat-level errors -- is DETERMINISTIC and marks the
# bootstrap failed for real.
#
# NB: being of a transient TYPE is NECESSARY but NOT SUFFICIENT for a
# cross-poll re-arm. The re-arm also requires that the failure occurred
# BEFORE a successful gateway connect (``connected is False`` in the
# classifier) -- a transient-typed error raised AFTER the connect (e.g. a
# ``send_rpc`` ack timeout) is NOT re-armed, because a chat.send may already
# have been accepted by OpenClaw and a fresh re-dispatch could double-run
# Friday. See ``maybe_dispatch_bootstrap``'s classifier for the full rule.
_TRANSIENT_DISPATCH_ERRORS: tuple[type[BaseException], ...] = (OSError, TimeoutError)


def _paperclip_repo():
    """Local import so ``patch("core.repositories.paperclip_repo.PaperclipRepo", ...)``
    (the pattern every existing test double in this codebase uses) keeps
    working -- mirrors ``readiness_state._paperclip_repo``'s own idiom.
    """
    from core.repositories.paperclip_repo import PaperclipRepo

    return PaperclipRepo(table_name="paperclip-companies")


async def _ensure_connection_with_retry(pool, owner_id: str, ip: str, token: str) -> None:
    """Bounded connect-retry around the gateway WebSocket connect.

    Retries ONLY connection-level failures (``_TRANSIENT_DISPATCH_ERRORS``)
    on the backoff ladder in ``_CONNECT_RETRY_DELAYS_S`` -- the "container
    row says running but OpenClaw isn't listening yet" boot window. Any
    other exception (auth rejections, chat-level errors -- which can't even
    occur here, but the principle holds) propagates immediately, as does the
    final connection failure once the ladder is exhausted; the caller's
    failure classifier then decides re-arm vs. failed. Runs inside one
    claimed dispatch: the row sits at ``bootstrap_state="dispatched"`` the
    whole time, so concurrent pollers no-op rather than pile on.
    """
    for attempt, delay_s in enumerate(_CONNECT_RETRY_DELAYS_S, start=1):
        try:
            await pool.ensure_connection(owner_id, ip, token)
            return
        except _TRANSIENT_DISPATCH_ERRORS as e:
            logger.info(
                "bootstrap_dispatcher: gateway connect failed for %s (attempt %d/%d, retrying in %.0fs): %s",
                owner_id,
                attempt,
                len(_CONNECT_RETRY_DELAYS_S) + 1,
                delay_s,
                e,
            )
            await asyncio.sleep(delay_s)
    # Last attempt: let whatever it raises propagate to the classifier.
    await pool.ensure_connection(owner_id, ip, token)


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


FIRST_CONTACT_MAX_ATTEMPTS = 3

NUDGE_MESSAGE = (
    "Before you stop: you haven't sent me your first email yet. Name the "
    "company if you haven't, then send me a short note in your own voice "
    "about what you found — the command is in your instructions."
)

# States that mean first contact is still owed. `failed` is included so a bad
# mint or a failed SES send stays retryable; `sending` and `sent` are not.
_FIRST_CONTACT_PENDING = ("not_started", "failed")


async def _maybe_nudge_first_contact(
    repo,
    row_user_id: str,
    *,
    owner_id: str,
    generation: str,
    session_key: str,
    friday_agent_id: str,
    submitted_by: str,
) -> None:
    """Remind the agent if its run ended without sending the owner's first email.

    This observer NEVER composes or sends mail — the send stays the agent's own
    tool call against ``/agent-mail/send-to-owner``. It only notices she hasn't
    and reminds her.

    The ``/container/status`` poll cannot drive this: ``useProvisioningState``
    documents that cadence as "0/off once ready", and first contact happens
    AFTER ready, so a retry hung off the poll would never fire. This reuses the
    run-completion callback the dispatcher already registers for every first
    run instead — no new machinery.

    Nudge runs re-register this same callback, because the bootstrap run
    terminates exactly once and attempts 2 and 3 would otherwise have no
    trigger. ``bump_first_contact_attempts`` is therefore the SOLE bound on the
    loop, and it is checked BEFORE dispatch — dispatching first would be an
    unbounded chain of agent runs.
    """
    try:
        onboarding = await repo.get_onboarding_state(owner_id) or {}
        state = onboarding.get("first_contact_state") or "not_started"
        if state not in _FIRST_CONTACT_PENDING:
            return

        if not await repo.bump_first_contact_attempts(
            row_user_id, onboarding_generation=generation, limit=FIRST_CONTACT_MAX_ATTEMPTS
        ):
            # Budget spent. Record it terminally so this stops being invisible —
            # the previous incarnation of this feature failed silently for its
            # entire life.
            await repo.settle_first_contact(
                row_user_id,
                onboarding_generation=generation,
                state="failed",
                reason="nudge_attempts_exhausted",
            )
            logger.warning("bootstrap_dispatcher: first-contact nudges exhausted for %s", owner_id)
            return

        from core.containers import get_ecs_manager, get_gateway_pool

        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        if not container or not ip or not container.get("gateway_token"):
            return

        pool = get_gateway_pool()
        nudge_run_id = str(uuid4())

        async def _on_nudge_terminal(state: str, payload: dict) -> None:
            # Re-enter so attempts 2 and 3 have a trigger at all. The counter
            # bump above is what terminates this chain.
            if state == "final":
                await _maybe_nudge_first_contact(
                    repo,
                    row_user_id,
                    owner_id=owner_id,
                    generation=generation,
                    session_key=session_key,
                    friday_agent_id=friday_agent_id,
                    submitted_by=submitted_by,
                )

        if not pool.register_run_completion_callback(owner_id, nudge_run_id, _on_nudge_terminal):
            return
        pool.mark_run_active(owner_id, session_key, friday_agent_id, member_id=submitted_by)
        try:
            await pool.send_rpc(
                user_id=owner_id,
                req_id=f"first-contact-nudge-{nudge_run_id}",
                method="chat.send",
                params={
                    "sessionKey": session_key,
                    "message": NUDGE_MESSAGE,
                    "idempotencyKey": nudge_run_id,
                    "deliver": False,
                },
                ip=ip,
                token=container["gateway_token"],
            )
        except Exception:
            # Mirror the kickoff's cleanup: an un-consumed callback would leak
            # in _run_completion_callbacks until the connection is torn down.
            pool.clear_active_run(owner_id, session_key)
            pool.unregister_run_completion_callback(owner_id, nudge_run_id)
            raise
    except Exception:  # noqa: BLE001 — a completion observer must never raise
        logger.exception("bootstrap_dispatcher: first-contact nudge failed for %s", owner_id)


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
        # dispatched: a peer holds the claim (possibly mid connect-retry).
        # delivered: done. failed: terminal -- a DETERMINISTIC connect
        # failure, the attempts cap, OR an AMBIGUOUS post-connect failure
        # (chat.send may already have been accepted; re-arming could double
        # -run Friday -- see the failure classifier below). Only TRANSIENT
        # connect-phase failures re-arm back to not_started, so "failed"
        # means a re-dispatch is unsafe or pointless and it needs a human /
        # the next onboarding_generation, not another poll.
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
                await _maybe_nudge_first_contact(
                    repo,
                    row_user_id,
                    owner_id=owner_id,
                    generation=generation,
                    session_key=session_key,
                    friday_agent_id=friday_agent_id,
                    submitted_by=submitted_by,
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
    # Tracks whether the gateway WebSocket connect SUCCEEDED. It is the pivot
    # of the failure classifier below: only a failure that occurred BEFORE a
    # successful connect (connected is still False) provably happened before
    # any chat.send was ever written to the socket, and is therefore safe to
    # re-arm. Once connected is True, a chat.send may already be in flight /
    # accepted by OpenClaw, so a later failure is NEVER re-armed (see the
    # classifier's docstring). Flipped True the instant the connect returns.
    connected = False
    try:
        # Connection-level boot races retry in place (see module docstring);
        # everything past this line fails fast into the classifier below.
        await _ensure_connection_with_retry(pool, owner_id, ip, container["gateway_token"])
        connected = True
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
        # The ONLY thing that changes per-owner: the message CONTENT is the
        # user's own brief (build_first_run_message), not a constant. The
        # claim/CAS/connect-retry/re-arm machinery and the session_key routing
        # above are unchanged. Absent brief_text (the "you pick" path, or a
        # legacy row) degrades to FIRST_RUN_NO_BRIEF_MESSAGE inside the builder.
        message = build_first_run_message(onboarding.get("onboarding_brief_text"))
        await pool.send_rpc(
            user_id=owner_id,
            req_id=f"bootstrap-dispatch-{dispatch_id}",
            method="chat.send",
            params={
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": run_id,
                "deliver": False,
            },
            ip=ip,
            token=container["gateway_token"],
        )
        logger.info("bootstrap_dispatcher: dispatched kickoff for %s dispatch_id=%s", owner_id, dispatch_id)
    except Exception as e:  # noqa: BLE001 — dispatch failed at connect or send
        logger.warning("bootstrap_dispatcher: dispatch failed for %s: %s", owner_id, e)
        pool.clear_active_run(owner_id, session_key)
        if registered:
            # send_rpc (or mark_run_active) raised AFTER the completion
            # observer was registered -- no terminal event will ever arrive
            # to consume it via THIS callback (a new run_id is minted per
            # dispatch), so the callback (and its closure) would otherwise
            # leak in _run_completion_callbacks until the GatewayConnection
            # itself is torn down. Unregister it explicitly, mirroring the
            # clear_active_run cleanup on the line above.
            pool.unregister_run_completion_callback(owner_id, run_id)
        # Classify the failure. RE-ARM IS SAFE ONLY FOR CONNECT-PHASE
        # FAILURES -- ones that provably occurred BEFORE any chat.send was
        # written to the socket. ``connected`` is the pivot:
        #
        #   * connected is False -> the gateway WebSocket connect never
        #     succeeded (``_ensure_connection_with_retry`` exhausted its
        #     ladder, or a non-transient connect error propagated). Nothing
        #     was ever sent, so a fresh re-dispatch on the next poll cannot
        #     double-run Friday. TRANSIENT ones (``_TRANSIENT_DISPATCH_ERRORS``
        #     -- connection refused/reset/timeout) re-arm; non-transient
        #     connect errors (auth backoff, ``GatewayConnectAuthError``) still
        #     mark failed, exactly as before.
        #
        #   * connected is True -> the connect SUCCEEDED and the failure came
        #     from ``register`` / ``mark_run_active`` / ``send_rpc``. A
        #     send_rpc failure is AMBIGUOUS: the socket write of chat.send may
        #     already have landed and OpenClaw may already have ACCEPTED the
        #     run when the ack merely timed out or the connection dropped
        #     WHILE WAITING (connection_pool.send_rpc:2588-2594). Re-arming
        #     would mint a NEW run_id / idempotencyKey on the next poll, which
        #     OpenClaw cannot dedup against a run it already took -> Friday's
        #     kickoff could execute more than once. So post-connect failures
        #     are DELIBERATELY TERMINAL: mark failed (a human or the next
        #     onboarding_generation can recover without risking a double
        #     kickoff), never re-arm.
        #
        # ``bootstrap_attempts`` on the onboarding dict is the PRE-claim
        # value; the claim this dispatch just won incremented it on the row,
        # so the attempt that just died is number pre+1.
        rearm_eligible = (not connected) and isinstance(e, _TRANSIENT_DISPATCH_ERRORS)
        attempt_number = (onboarding.get("bootstrap_attempts") or 0) + 1
        try:
            if rearm_eligible and attempt_number < MAX_BOOTSTRAP_ATTEMPTS:
                logger.info(
                    "bootstrap_dispatcher: transient connect-phase failure for %s "
                    "(attempt %d/%d) — re-arming for the next /status poll",
                    owner_id,
                    attempt_number,
                    MAX_BOOTSTRAP_ATTEMPTS,
                )
                await repo.rearm_bootstrap_dispatch(
                    row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id, reason=str(e)
                )
            else:
                await repo.mark_bootstrap_failed(
                    row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id, reason=str(e)
                )
        except Exception:  # noqa: BLE001 — best-effort failure recording
            logger.exception("bootstrap_dispatcher: failed to record dispatch failure for %s", owner_id)
