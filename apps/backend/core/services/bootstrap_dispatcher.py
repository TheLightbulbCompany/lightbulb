"""Server-owned bootstrap dispatch -- INV-5, spec PR-6 (BOOTSTRAP).

Friday's first-run kickoff is dispatched by the BACKEND, exactly once per
``onboarding_generation``, with no browser tab required. The kickoff message
is the USER'S OWN BRIEF (``onboarding_brief.build_first_run_message`` woven
from the ``onboarding_brief_text`` persisted at ``/onboarding/brief`` submit),
so Friday's chat opens with the user's ask -- not a canned prompt. This
replaces the client-side latch it used to depend on (``useFirstRunKickoff``
+ Clerk ``firstRun.started``/``completed``) and the thread-emptiness
reconcile that latch needed to self-heal -- both since deleted. See
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

That observer is in-process memory, so it dies with the process that
dispatched (a rolling deploy's graceful shutdown, the frontend-gone grace
close) while the kickoff keeps running inside OpenClaw. Two things make
completion survive that. The kickoff's ``idempotencyKey`` -- which OpenClaw
echoes back as ``runId`` on every event of the run -- IS the persisted
``bootstrap_dispatch_id``, so the row alone identifies the run. And a poll
that finds the row ``sent`` with no observer registered in ITS process
re-attaches one (``_reattach_kickoff_observer``) on a live connection --
no claim, no chat.send. The pool's ``close()`` accordingly hands observers
off (drops them) instead of failing them; only a genuine gateway drop (dead
reader loop) still resolves a pending observer as failed. If the terminal
fires while no process is connected at all, the row stays ``sent`` --
truthful (the kickoff WAS delivered), invisible to the owner, legible on
the admin page.

THREE STAMPS, EACH WRITTEN AT THE MOMENT IT BECOMES TRUE. The claim used
to be the only stamp before the terminal one, which meant a row sat at
``dispatched`` across the whole ~3-minute connect ladder AND the entire
first agent run -- and if the backend process died anywhere in that window
(deploy, OOM, scale-in) the row stayed ``dispatched`` FOREVER: nothing read
``bootstrap_dispatched_at``, there was no lease TTL, and the admin lever
was fenced on ``failed``. The ladder is now:

  1. ``not_started -> dispatched`` + ``bootstrap_dispatched_at`` -- CLAIMED
     (``claim_bootstrap_dispatch``).
  2. ``bootstrap_send_attempted_at`` -- written IMMEDIATELY BEFORE the
     chat.send RPC (``mark_bootstrap_send_attempted``), i.e. write-ahead of
     an outcome nobody knows yet.
  3. ``dispatched -> sent`` + ``bootstrap_sent_at`` -- DELIVERED, written
     the instant the send RPC returns its ack (``mark_bootstrap_sent``).
     The terminal ``delivered``/``failed`` transitions sit on top of this
     one unchanged (both marks accept ``dispatched`` or ``sent``).

The stamps exist to make a dead claimant's row READABLE by whoever finds
it next, which is what turns "stuck forever" into "recovered on the next
poll" -- see the staleness rule below.

LAZY STALENESS EXPIRY -- no sweeper, no cron. ``maybe_dispatch_bootstrap``
is already called on every ``/container/status`` poll with the onboarding
dict in hand, so the check costs ZERO extra reads and needs no second
mechanism to keep alive. A ``dispatched`` row older than
``STALE_DISPATCH_AFTER_S`` splits by stamp 2:

* **No ``bootstrap_send_attempted_at`` -> PROVABLY UNSENT.** The claimant
  died before writing anything to the socket, so a fresh dispatch cannot
  double-run Friday. Re-armed to ``not_started``
  (``rearm_bootstrap_dispatch``) and re-claimed by THIS same call. Both
  recovery writes carry the fence for what they concluded from a stale READ,
  not just generation + ``bootstrap_dispatch_id``: the re-arm requires the
  write-ahead stamp to still be ABSENT, the failed-settle requires no ack
  (``only_if_unacked``). That is what makes the rule safe against a claimant
  that was merely SLOW rather than dead and woke up mid-window — without it,
  it could send while this poll re-armed and re-claimed underneath it.
* **``bootstrap_send_attempted_at`` set, never acked -> AMBIGUOUS.** The
  send may already have been accepted by OpenClaw. Exactly the case the
  post-connect classifier already refuses to re-arm, so it is NOT re-sent:
  the row is marked ``failed`` with that explanation, which makes it
  visible and hands it to the admin lever instead of leaving it stuck.
  A provably-unsent claim that is out of ``MAX_BOOTSTRAP_ATTEMPTS`` lands
  the same way (re-arming it would strand the row at ``not_started``, which
  no lever accepts).
* **``sent`` -> LEFT ALONE.** Delivery happened; only the completion
  bookkeeping was lost, and re-sending would double-run Friday.

``routers/container.py``'s pre-gate therefore admits ``dispatched`` rows
too (a stuck claim can only be found by a poll that actually calls in);
the dispatcher returns immediately for a fresh one. It admits ``failed``
for the same reason -- see the bounded-retry tier below.

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
  WHILE WAITING (``connection_pool.send_rpc``). Re-arming there would re-claim
  the row on the next poll, and every claim mints a NEW
  ``bootstrap_dispatch_id`` -- which IS the kickoff's ``idempotencyKey`` /
  runId -- so OpenClaw cannot dedup the second send against a run it already
  took -- so Friday's kickoff could execute more than once. Marking ``failed``
  instead keeps that ambiguous case recoverable by a human / the next
  ``onboarding_generation`` without
  ever risking a double kickoff. Without ANY re-arm, ``failed`` was terminal
  (``claim_bootstrap_dispatch`` admits only ``not_started``) and one boot
  race killed the onboarding forever; scoping re-arm to the provably-pre-send
  connect phase recovers the boot-window blip without reintroducing that risk.

* **Bounded retry OF a ``failed`` row** (``_retry_failed_bootstrap``). The two
  tiers above only catch a failure they can classify DURING the dispatch,
  which left a real hole: a transient the classifier types deterministic (a
  connect rejection carrying OpenClaw's own "gateway starting; retry shortly"
  arrives as a bare ``RuntimeError``) or one that only surfaces as the run's
  terminal (a rate-limited first run) lands in ``failed``, and ``failed`` was
  reachable ONLY by the admin lever. Both were observed on one dev account on
  2026-08-30, each behind a RUNNING, HEALTHY container, leaving the owner on
  the funnel spinner for 21 hours. A ``failed`` row is therefore re-armed
  automatically now -- under the SAME ``bootstrap_attempts`` budget, no sooner
  than ``FAILED_RETRY_BACKOFF_S`` after the failed attempt started, and NEVER
  for the ambiguous send-attempted-never-acked row. Past those bounds nothing
  changes: the row stays ``failed``, and ``reset_bootstrap_for_retry`` (which
  zeroes the counter) is still the operator's way out.

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
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from core.gateway.connection_pool import GatewayReconnectThrottled
from core.services.agent_roster import roster_entries
from core.observability.metrics import put_metric
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

# How long a ``dispatched`` claim may sit before a later poll may treat its
# claimant as DEAD (see the module docstring's staleness rule). Derived from
# the in-dispatch budget it has to clear: the connect ladder above is ~185s
# worst case, and the chat.send ack adds its own timeout on top, so a LIVE
# claimant reaches stamp 2 or 3 inside ~4 minutes. Ten minutes leaves that
# comfortably behind, so anything older is a claim no process is holding.
# Shared with the admin lever (``routers/admin.py``'s reset-bootstrap), which
# must refuse a claim this rule would still consider live.
STALE_DISPATCH_AFTER_S = 600

# How long a ``failed`` row must sit before the BOUNDED automatic retry may
# re-arm it (see ``_retry_failed_bootstrap``). Measured from
# ``bootstrap_dispatched_at``, i.e. from when the failed attempt STARTED, which
# is what actually needs bounding: /container/status is polled every ~3s by
# every loading client, so without a floor a failed row would re-dispatch on
# every poll and hammer the gateway and the model. Two minutes, not the ten of
# STALE_DISPATCH_AFTER_S: that constant answers a different question (is a
# claimant's process dead) and is sized off the in-dispatch connect ladder,
# while this one is paid by an owner watching a spinner. Combined with
# MAX_BOOTSTRAP_ATTEMPTS the whole automatic recovery window is a handful of
# minutes, after which the row is ``failed`` for the operator lever.
FAILED_RETRY_BACKOFF_S = 120

# Retry ladder for the ``sent`` stamp (~7.5s total). Short and bounded: it runs
# inline in the dispatch task, after delivery, so it must not hold the task open
# for minutes -- but it must outlast a DDB blip, because losing this write turns
# a KNOWN-delivered kickoff into a row that reads as an unacked send (terminal,
# and admin-retryable into a double delivery). See ``_settle_ack``.
_ACK_STAMP_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 2, 5)

# A re-attach that did NOT end with an observer registered (runtime not up,
# no default agent in the EFS config, connect refused, connection vanished
# between dial and register) is retried by the next poll -- but no sooner than
# this. ``sent`` has no staleness rule, so without a bound a row that can
# never re-attach would pay a container resolve + EFS read + gateway dial on
# every ~3s poll, forever. Process-local: a wedged row is throttled per
# backend task, which is the only place the cost is paid.
_REATTACH_BACKOFF_S = 60.0
_reattach_not_before: dict[str, float] = {}

# Recorded on ``bootstrap_last_error`` by the staleness rule. Written for a
# human (the admin surface renders it verbatim), so they say what happened and
# what the operator's next move is.
_STALE_UNSENT_ERROR = (
    "dispatch claim expired with no chat.send attempted — the claiming process died before "
    "sending; re-armed for the next poll"
)
_STALE_ATTEMPTED_ERROR = (
    "send attempted, ack never recorded — process died mid-send; use admin reset to retry, may double-deliver"
)
_STALE_UNSENT_CAPPED_ERROR = (
    "dispatch claim expired with no chat.send attempted, and the attempt budget is spent — use admin reset to retry"
)

# Failure classes the dispatcher treats as TRANSIENT: connection-level
# failures (``OSError`` covers ConnectionRefusedError/ConnectionResetError/
# the raw [Errno 111] connect refusal), timeouts (``asyncio.TimeoutError``
# IS ``TimeoutError`` on >=3.11), and ``GatewayReconnectThrottled`` -- the
# pool's own 5s reconnect floor, which clears on its own with no other
# change. Everything else -- including ``GatewayConnectAuthError`` and
# ``GatewayAuthBackoff`` (same credentials cannot succeed on retry, and each
# attempt counts toward the gateway's 10-in-60s lockout) and chat-level
# errors -- is DETERMINISTIC and marks the bootstrap failed for real.
#
# The throttle MUST be matched by its own type, never by ``RuntimeError``:
# the auth-backoff guard is also a RuntimeError and is the semantic
# opposite, so a broad catch would hammer an owner into the gateway lockout.
# Listing it here is what makes a 5-second cooldown survivable -- before it,
# a status poll connecting ~0.3s ahead of the dispatcher latched the row at
# ``failed`` permanently, silently costing that owner first contact. The
# first rung of ``_CONNECT_RETRY_DELAYS_S`` is 5s, exactly the floor, so one
# retry clears it.
#
# NB: being of a transient TYPE is NECESSARY but NOT SUFFICIENT for a
# cross-poll re-arm. The re-arm also requires that the failure occurred
# BEFORE a successful gateway connect (``connected is False`` in the
# classifier) -- a transient-typed error raised AFTER the connect (e.g. a
# ``send_rpc`` ack timeout) is NOT re-armed, because a chat.send may already
# have been accepted by OpenClaw and a fresh re-dispatch could double-run
# Friday. See ``maybe_dispatch_bootstrap``'s classifier for the full rule.
_TRANSIENT_DISPATCH_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    GatewayReconnectThrottled,
)


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


def _age_exceeds(stamp, seconds: float) -> bool:
    """Is *stamp* more than *seconds* in the past?

    Takes whatever the caller already holds -- an ISO string (the
    ``get_onboarding_state`` bridge dict) or a ``datetime`` (the
    ``PaperclipCompany`` dataclass the admin lever reads) -- so every "has
    enough time passed" question on this row is answered with the same
    parsing.

    An absent or unparseable stamp is NEVER old enough (fail closed): a row we
    cannot date is one we must leave alone rather than guess at, and both
    callers pay for a wrong "yes" in the same currency -- a double-dispatched
    kickoff.
    """
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp)
        except ValueError:
            return False
    if not isinstance(stamp, datetime):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp > timedelta(seconds=seconds)


def is_dispatch_stale(dispatched_at) -> bool:
    """Is a ``dispatched`` claim older than ``STALE_DISPATCH_AFTER_S``?

    Both sides of the "is this claim dead" question -- this module's lazy
    expiry and the admin lever (``routers/admin.py``'s reset-bootstrap) --
    answer it through here, with the SAME threshold.
    """
    return _age_exceeds(dispatched_at, STALE_DISPATCH_AFTER_S)


async def _recover_stale_dispatch(repo, onboarding: dict, *, owner_id: str) -> bool:
    """Lazy expiry for a ``dispatched`` row whose claimant never came back.

    Called from ``maybe_dispatch_bootstrap``'s own state guard -- the poll
    that finds the row is the one that recovers it (see the module docstring
    for why there is no sweeper). Returns True ONLY when the row was re-armed
    to ``not_started`` and this caller should go on to re-claim it.

    Both writes are conditional on the row still being ``dispatched`` under
    the SAME ``bootstrap_dispatch_id`` this dict was read at, so two
    concurrent pollers cannot both act. Each also carries the fence for the
    thing it just concluded from a STALE read: the re-arm requires
    ``bootstrap_send_attempted_at`` to still be absent, the failed-settle
    requires no ack (``only_if_unacked``). The time threshold says the
    claimant is *probably* dead; those two clauses are what make it safe when
    it was merely SLOW and woke up mid-window -- otherwise a live claimant
    could send while this poll re-armed and re-claimed underneath it, running
    Friday twice.
    """
    if not is_dispatch_stale(onboarding.get("bootstrap_dispatched_at")):
        return False

    row_user_id = onboarding.get("user_id")
    generation = onboarding.get("onboarding_generation")
    dispatch_id = onboarding.get("bootstrap_dispatch_id")
    if not (row_user_id and generation and dispatch_id):
        # No fencing token to write against (a legacy/degraded row). Leave it
        # for the admin lever rather than write unfenced.
        logger.warning("bootstrap_dispatcher: stale dispatch for %s carries no fencing token", owner_id)
        return False

    attempted = onboarding.get("bootstrap_send_attempted_at")
    attempts = onboarding.get("bootstrap_attempts") or 0
    if not attempted and attempts < MAX_BOOTSTRAP_ATTEMPTS:
        # PROVABLY UNSENT: the write-ahead stamp is missing, so nothing was
        # ever written to the socket. Safe to re-dispatch from scratch.
        logger.warning(
            "bootstrap_dispatcher: expired dispatch claim for %s (dispatch_id=%s, nothing sent) — re-arming",
            owner_id,
            dispatch_id,
        )
        await repo.rearm_bootstrap_dispatch(
            row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id, reason=_STALE_UNSENT_ERROR
        )
        return True

    # Two ways to land here, and both are recorded ``failed`` -- terminal but
    # VISIBLE and admin-recoverable, which is the whole improvement over a row
    # stuck at ``dispatched`` forever:
    #
    #   * AMBIGUOUS -- the send was attempted and never acked, so OpenClaw may
    #     already have taken the run. Re-dispatching would mint a fresh run_id
    #     / idempotencyKey it cannot dedup, the same reason the post-connect
    #     classifier refuses to re-arm.
    #   * OUT OF ATTEMPTS -- re-arming would drop the row at ``not_started``
    #     with nothing willing to claim it (the cap stands every later poll
    #     down) and no lever able to take it, since the admin reset accepts
    #     only ``failed`` or a stale ``dispatched``. Marking failed at the cap
    #     is exactly what the in-dispatch classifier does, and the lever's
    #     failed path zeroes the counter.
    logger.warning(
        "bootstrap_dispatcher: expired dispatch claim for %s (dispatch_id=%s, attempted=%s, attempts=%d) "
        "— marking failed, NOT re-sending",
        owner_id,
        dispatch_id,
        bool(attempted),
        attempts,
    )
    await repo.mark_bootstrap_failed(
        row_user_id,
        onboarding_generation=generation,
        dispatch_id=dispatch_id,
        reason=_STALE_ATTEMPTED_ERROR if attempted else _STALE_UNSENT_CAPPED_ERROR,
        # A claim we judged dead may be merely SLOW and land its ack in the
        # window between the read above and this write. Refuse to settle a row
        # that acked in the meantime — mislabelling a DELIVERED kickoff failed
        # is what would invite an admin reset into a double delivery.
        only_if_unacked=True,
    )
    return False


async def _retry_failed_bootstrap(repo, onboarding: dict, *, owner_id: str) -> bool:
    """BOUNDED automatic retry of a ``failed`` row. True == re-armed, and this
    caller should go on to re-claim it (same shape as
    ``_recover_stale_dispatch``, evaluated on the poll that already read the
    row -- there is still no sweeper).

    ``failed`` was terminal-until-an-operator-noticed, and that is a live
    product defect: a transient the classifier happens to type non-transient
    dead-ends the owner permanently. Observed on dev 2026-08-30, twice on one
    account, each behind a RUNNING, HEALTHY container -- a connect rejection
    carrying OpenClaw's own "gateway starting; retry shortly", then a
    rate-limited first run -- with the funnel spinner up for 21 hours.

    But ``failed`` was not excluded by accident, and this does not simply
    admit it. Three bounds, all of which must hold:

    * **Attempt budget.** ``MAX_BOOTSTRAP_ATTEMPTS``, the SAME counter every
      other dispatch path spends (the re-arm keeps it, each claim increments
      it), so this is one budget for all recovery, not a second one stacked on
      top. At the cap the row stays ``failed`` for the operator lever --
      unchanged from today, and the reason a per-poll admission would have
      hammered the gateway/model forever.
    * **Backoff.** ``FAILED_RETRY_BACKOFF_S`` since the failed attempt
      STARTED. /container/status is polled every ~3s by every loading client;
      this is what keeps a retry from riding that cadence.
    * **Never the AMBIGUOUS row.** A send attempted but never acked may
      already have been taken by OpenClaw, and every claim mints a fresh
      ``bootstrap_dispatch_id`` (== the kickoff's ``idempotencyKey``) it cannot
      dedup -- so re-dispatching could run Friday twice. The other post-send
      shape IS retried: an ACKED kickoff whose run was OBSERVED to terminate in
      a chat error is definitively over, so a fresh run cannot overlap it. The
      checks here are a cheap pre-filter; ``rearm_failed_bootstrap``'s
      condition expression is the actual fence, so two backend instances (a
      rolling deploy runs 2) cannot both re-arm.

    Ceiling, accepted: ``_record_late_ack`` can stamp ``bootstrap_sent_at`` on
    a row the staleness rule just settled ``failed``, so a claimant slow by
    >10 minutes whose send then lands reads here as "acked, run over" while its
    run may still be live. That needs a >10-minute-slow claimant AND a run
    still going ``FAILED_RETRY_BACKOFF_S`` after its ack; the cost is one
    duplicate kickoff, against a defect that today costs the owner everything.
    """
    attempts = onboarding.get("bootstrap_attempts") or 0
    if attempts >= MAX_BOOTSTRAP_ATTEMPTS:
        # Terminal for real. The operator lever (which zeroes the counter) is
        # the only way out, exactly as before this function existed.
        return False
    if not _age_exceeds(onboarding.get("bootstrap_dispatched_at"), FAILED_RETRY_BACKOFF_S):
        return False
    if onboarding.get("bootstrap_send_attempted_at") and not onboarding.get("bootstrap_sent_at"):
        return False

    row_user_id = onboarding.get("user_id")
    generation = onboarding.get("onboarding_generation")
    dispatch_id = onboarding.get("bootstrap_dispatch_id")
    if not (row_user_id and generation and dispatch_id):
        # No fencing token (a legacy/degraded row) -- leave it for the admin
        # lever rather than write unfenced, same as the staleness rule.
        return False

    if not await repo.rearm_failed_bootstrap(row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id):
        return False
    put_metric("bootstrap.failed_retry_rearmed")
    logger.warning(
        "bootstrap_dispatcher: retrying failed bootstrap for %s (dispatch_id=%s, attempt %d/%d, last error: %s)",
        owner_id,
        dispatch_id,
        attempts + 1,
        MAX_BOOTSTRAP_ATTEMPTS,
        onboarding.get("bootstrap_last_error"),
    )
    return True


async def _stamp_sent_with_retry(repo, row_user_id: str, *, owner_id: str, generation: str, dispatch_id: str) -> bool:
    """Advance the row to ``sent``, retrying transient write failures.

    Same ladder-then-final-attempt shape as ``_ensure_connection_with_retry``.
    Returns whether the stamp actually LANDED — which is not the same as "no
    exception". The two ways to miss are handled differently:

    * **The write failed** (throttle, timeout, transport) — retryable, so it
      rides the ladder.
    * **The write was REFUSED by its own guard** (``mark_bootstrap_sent``
      returns False: the row moved on under this claim — a stale-claim
      recovery already settled it ``failed``, an admin reset, a superseded
      generation). A CAS that failed on STATE cannot be fixed by trying it
      again, so this returns immediately — but it returns *False*, because the
      delivery is just as unrecorded as an outright failure and the caller's
      loud "unrecorded delivery" path is exactly what that row needs.
    """
    for attempt, delay_s in enumerate(_ACK_STAMP_RETRY_DELAYS_S, start=1):
        try:
            return bool(
                await repo.mark_bootstrap_sent(row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id)
            )
        except Exception as e:  # noqa: BLE001 — retryable until the ladder is spent
            logger.warning(
                "bootstrap_dispatcher: sent stamp failed for %s (attempt %d/%d, retrying in %.1fs): %s",
                owner_id,
                attempt,
                len(_ACK_STAMP_RETRY_DELAYS_S) + 1,
                delay_s,
                e,
            )
            await asyncio.sleep(delay_s)
    try:
        return bool(
            await repo.mark_bootstrap_sent(row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id)
        )
    except Exception:  # noqa: BLE001 — the caller decides what an unrecorded delivery means
        logger.exception("bootstrap_dispatcher: sent stamp exhausted its retries for %s", owner_id)
        return False


async def _settle_ack(repo, row_user_id: str, *, owner_id: str, generation: str, dispatch_id: str) -> None:
    """Ack-time bookkeeping. NEVER raises.

    Everything here runs AFTER OpenClaw acked the chat.send, i.e. after the
    kickoff is delivered — so a failure in it must never reach the dispatch
    failure classifier, which would mark a delivered kickoff ``failed`` (and,
    worse, hand it to a recovery path as if it were retryable).

    The ``sent`` stamp is RETRIED rather than dropped on the first error,
    because it is the only durable record that this kickoff was delivered: a
    row that loses it reads — to the staleness rule, to the admin lever, to a
    human — exactly like a send that was never acked, which is terminal and
    admin-retryable into a DOUBLE delivery. A DDB blip must not be able to
    cost that.

    If the stamp does not land — the ladder is exhausted, or the write is
    REFUSED because the row moved on under this claim (a stale-claim recovery
    settled it, an admin reset) — the delivery is UNRECORDED, and there is no
    second durable place to put it (the table that would hold it is the one
    that just failed or disowned us), so this ends in a loud ERROR plus the
    ``bootstrap.ack_unrecorded`` metric — the signal an operator needs BEFORE
    pressing reset. Two things still catch that case in practice: the
    run-completion observer resolves the row on ``chat.final``
    (``mark_bootstrap_delivered`` accepts a still-``dispatched`` row), and the
    staleness rule's own ``failed`` reason says in as many words that a retry
    may double-deliver.
    """
    if not await _stamp_sent_with_retry(
        repo, row_user_id, owner_id=owner_id, generation=generation, dispatch_id=dispatch_id
    ):
        put_metric("bootstrap.ack_unrecorded")
        logger.error(
            "bootstrap_dispatcher: UNRECORDED DELIVERY for %s dispatch_id=%s — the kickoff WAS acked by "
            "OpenClaw but bootstrap_state could not be advanced to 'sent' (write failed, or refused "
            "because the row moved on under this claim). The row therefore reads as an unacked send: "
            "staleness recovery will settle it 'failed', and an admin reset would DOUBLE-DELIVER "
            "Friday's kickoff. Do not reset this row.",
            owner_id,
            dispatch_id,
        )

    # DELIVERY, not completion, is what retires the pending-brief sentinel: the
    # kickoff carrying that brief has been accepted, so the item can never be
    # looked at again, and one that outlives this moment would silently block
    # the owner's next brief (its put-if-absent key) — e.g. after a reprovision.
    # It used to be swept from the run-completion observer, which never fired at
    # all if the run never terminated. Normally the drain has already deleted it
    # and this is a no-op. Guarded separately from the stamp above: delivery
    # happened either way, so one failing must not skip the other.
    try:
        await repo.delete_pending_brief(owner_id)
    except Exception:  # noqa: BLE001 — post-delivery bookkeeping must never fail the dispatch
        logger.exception("bootstrap_dispatcher: pending-brief sweep failed for %s", owner_id)


async def _resolve_default_agent_id(owner_id: str) -> str | None:
    """Friday's OpenClaw agent id, read straight off EFS's openclaw.json --
    no RPC round trip needed (and no live gateway connection required just
    to answer this).

    THREE identifiers, tried in order, because the one this used to rely on
    does not survive an 8.1 boot:

    1. ``default: True`` -- what ``_build_agents_list`` stamps on the
       Paperclip-role="ceo" entry at provision time. Correct on 7.2 and on a
       freshly-written config. **Gone after 8.1's doctor runs**: doctor
       consumes the marker to materialize per-surface ownership and then
       strips it (upstream ``legacy.default-agent-roles.ts``), so every entry
       reads ``default: None`` on a booted-8.1 container.
    2. ``params.paperclip.role == "ceo"`` -- the DURABLE identifier. It is
       what the marker was derived from in the first place, and nothing in
       8.1's migration touches it.
    3. ``talk.agentId`` -- one of the per-surface fields doctor materializes
       FROM the marker, so on a migrated config it points at exactly the
       agent the marker used to name. Last resort, and only if it matches a
       real roster entry.

    This is not defensive layering for its own sake: (1) alone silently
    returned None on every 8.1 container, and the caller treats None as
    "nothing to do" and returns BEFORE claiming -- no attempt, no error, no
    bootstrap_error row. A real customer sat with bootstrap_state=not_started
    and attempts=0 while every poll no-opped, because the kickoff was never
    reached rather than tried and failed. Silent no-ops are the failure mode
    this function must not have.

    Returns None only when the config genuinely names no CEO; a dispatcher
    must degrade, never raise.
    """
    from core.containers.config import read_openclaw_config_from_efs

    config = await read_openclaw_config_from_efs(owner_id)
    if not config:
        return None

    entries = [entry for entry in roster_entries(config) if isinstance(entry, dict)]

    def _entry_id(entry: dict) -> str | None:
        agent_id = entry.get("id")
        return agent_id if isinstance(agent_id, str) and agent_id else None

    for entry in entries:
        if entry.get("default") is True:
            if agent_id := _entry_id(entry):
                return agent_id

    for entry in entries:
        paperclip = (entry.get("params") or {}).get("paperclip") or {}
        if paperclip.get("role") == "ceo":
            if agent_id := _entry_id(entry):
                return agent_id

    talk_agent_id = (config.get("talk") or {}).get("agentId")
    if isinstance(talk_agent_id, str) and talk_agent_id:
        if any(_entry_id(entry) == talk_agent_id for entry in entries):
            return talk_agent_id

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
    tool call against ``/agent-mail/u/{owner_id}/send``. It only notices she
    hasn't and reminds her.

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


def _kickoff_observer(
    repo,
    *,
    row_user_id: str,
    owner_id: str,
    generation: str,
    dispatch_id: str,
    session_key: str,
    friday_agent_id: str,
    submitted_by: str,
):
    """The run-completion observer for one kickoff (``dispatch_id`` == runId).

    Built from the row's fields rather than closed over a dispatch's locals so
    the SAME observer can be registered by the process that dispatched and,
    after that process is gone, by whichever process re-attaches from the
    ``sent`` row (``_reattach_kickoff_observer``). The repo writes it makes
    are fenced on ``dispatch_id`` + live state, so two processes observing
    the same run cannot double-resolve it.
    """

    async def _on_terminal(state: str, payload: dict) -> None:
        try:
            if state == "final":
                # Both of these are COMPLETION-dependent and stay here: the
                # delivered/complete marks describe a run that ENDED, and the
                # nudge asks why it ended without a first email. The one thing
                # that moved to ack time is delete_pending_brief (see
                # ``_settle_ack``) -- it depends on DELIVERY, not completion.
                won = await repo.mark_bootstrap_delivered(
                    row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id
                )
                # Two processes can legitimately observe the same run (the one
                # that dispatched and one that re-attached during a rolling
                # deploy's overlap); both see the terminal, but only the CAS
                # winner owns the delivered-only follow-up, or Friday gets two
                # nudges for one run.
                if won:
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
        except Exception:  # noqa: BLE001 -- a completion-observer callback must never raise
            logger.exception(
                "bootstrap_dispatcher: completion observer failed for %s dispatch_id=%s",
                owner_id,
                dispatch_id,
            )

    return _on_terminal


async def _reattach_kickoff_observer(repo, onboarding: dict, *, owner_id: str) -> None:
    """Make sure THIS process observes a ``sent`` kickoff's terminal.

    The completion observer is in-process memory. The process that dispatched
    can be gone before Friday finishes -- a rolling deploy's graceful
    shutdown, the frontend-gone grace close -- while the kickoff keeps running
    inside OpenClaw (the pool's ``close()`` hands observers off rather than
    failing them for exactly this reason). The row is still truthfully
    ``sent``, and its ``bootstrap_dispatch_id`` IS the run's ``runId``, so any
    poll that finds it can register an observer keyed by that id on a live
    connection -- no claim, no chat.send, nothing that could run Friday twice.

    Cheap in steady state: when this process already holds the observer (the
    common case -- the dispatching process is usually the one polling) this
    is one dict lookup. Otherwise it resolves the runtime and opens/reuses the
    gateway connection -- the same dial the dispatch itself makes. Failures
    (runtime not up, connect refused) are left for the next poll; nothing here
    ever settles the row, which is still truthfully ``sent``.

    Ceiling, accepted: if the terminal fires while no process is connected at
    all (the shutdown-to-reconnect gap, an owner away with the grace close
    done), no observer sees it and the row stays ``sent`` -- invisible to the
    owner (the kickoff WAS delivered) and legible on the admin page.
    Reconciling that from ``chat.history`` is the upgrade path if it matters.
    """
    row_user_id = onboarding.get("user_id")
    generation = onboarding.get("onboarding_generation")
    dispatch_id = onboarding.get("bootstrap_dispatch_id")
    if not (row_user_id and generation and dispatch_id):
        return

    from core.containers import get_ecs_manager, get_gateway_pool

    pool = get_gateway_pool()
    if pool.has_run_completion_callback(owner_id, dispatch_id):
        return
    now = time.monotonic()
    if _reattach_not_before.get(owner_id, 0.0) > now:
        return

    registered = False
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        friday_agent_id = await _resolve_default_agent_id(owner_id) if container and ip else None
        if friday_agent_id:
            from routers.websocket_chat import build_chat_session_key

            submitted_by = onboarding.get("onboarding_submitted_by") or row_user_id
            session_key = build_chat_session_key(friday_agent_id, submitted_by, None)
            await pool.ensure_connection(owner_id, ip, container["gateway_token"])
            registered = pool.register_run_completion_callback(
                owner_id,
                dispatch_id,
                _kickoff_observer(
                    repo,
                    row_user_id=row_user_id,
                    owner_id=owner_id,
                    generation=generation,
                    dispatch_id=dispatch_id,
                    session_key=session_key,
                    friday_agent_id=friday_agent_id,
                    submitted_by=submitted_by,
                ),
            )
    except Exception as e:  # noqa: BLE001 -- best-effort; a later poll tries again
        logger.info("bootstrap_dispatcher: observer re-attach for %s deferred: %s", owner_id, e)

    if registered:
        _reattach_not_before.pop(owner_id, None)
        put_metric("bootstrap.observer_reattached")
        logger.info("bootstrap_dispatcher: re-attached kickoff observer for %s dispatch_id=%s", owner_id, dispatch_id)
    else:
        # Runtime not up, no default agent, dial failed, or the connection
        # vanished between dial and register -- the next poll after the backoff
        # retries. Metered so a row that NEVER re-attaches is visible.
        _reattach_not_before[owner_id] = now + _REATTACH_BACKOFF_S
        put_metric("bootstrap.observer_reattach_missed")


async def maybe_dispatch_bootstrap(owner_id: str, *, onboarding: dict | None = None) -> None:
    """Best-effort: dispatch Friday's first-run kickoff if it is this
    caller's turn to (INV-5) and the runtime is reachable right now -- and,
    on the way in, expire a ``dispatched`` claim whose process died (the lazy
    staleness rule; see the module docstring). Making a claim's turn come
    round again is the same code path as taking a turn, so recovery needs no
    sweeper of its own.

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
    bootstrap_state = onboarding.get("bootstrap_state") or "not_started"
    if bootstrap_state == "sent":
        # The chat.send was ACKED -- by some process, maybe not this one, maybe
        # one that no longer exists. Nothing to claim or send; the only
        # outstanding work is observing the run's terminal, and that observer
        # is in-process memory. Make sure THIS process holds one.
        await _reattach_kickoff_observer(repo, onboarding, owner_id=owner_id)
        return
    if bootstrap_state == "dispatched":
        # A peer holds the claim (possibly mid connect-retry) -- UNLESS its
        # process is gone. This is the lazy staleness expiry (module
        # docstring): off the dict this poll already read, so a fresh claim
        # costs one comparison and returns. A stale one is either re-armed
        # here (provably unsent) and re-claimed by the lines below, or
        # settled ``failed`` (ambiguous) and stood down from.
        if not await _recover_stale_dispatch(repo, onboarding, owner_id=owner_id):
            return
    elif bootstrap_state == "failed":
        # A DETERMINISTIC connect failure, an AMBIGUOUS post-connect failure,
        # an expired ambiguous claim, a run that terminated in a chat error, or
        # the attempts cap. It used to be terminal without exception, which
        # made every transient the classifier happens to type non-transient a
        # PERMANENT dead end for the owner. Now it is retried -- bounded by the
        # attempt budget and a backoff, and never for the ambiguous row (see
        # _retry_failed_bootstrap). Past those bounds this returns False and
        # the row stays failed for the admin lever, exactly as before.
        if not await _retry_failed_bootstrap(repo, onboarding, owner_id=owner_id):
            return
    elif bootstrap_state != "not_started":
        # delivered: done.
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
    # The kickoff's chat.send idempotencyKey -- which OpenClaw echoes back as
    # ``runId`` on every event of the run -- is the persisted dispatch id, NOT
    # a fresh uuid: the row alone then identifies the run, so a process that
    # did not dispatch (the one that survives a rolling deploy) can still
    # observe its terminal (``_reattach_kickoff_observer``). Re-arm safety is
    # unchanged: every claim mints a new dispatch id (module docstring).
    run_id = dispatch_id
    _on_terminal = _kickoff_observer(
        repo,
        row_user_id=row_user_id,
        owner_id=owner_id,
        generation=generation,
        dispatch_id=dispatch_id,
        session_key=session_key,
        friday_agent_id=friday_agent_id,
        submitted_by=submitted_by,
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
        # WRITE-AHEAD of the send (stamp 2): recorded while the outcome is
        # still unknown, because after a process death it is the ONLY evidence
        # of which side of the socket write we died on. A False return means
        # the row moved on under this claim (superseded generation, admin
        # reset, a stale-claim recovery) -- we no longer hold it, so we must
        # not send at all.
        if not await repo.mark_bootstrap_send_attempted(
            row_user_id, onboarding_generation=generation, dispatch_id=dispatch_id
        ):
            raise RuntimeError("bootstrap claim no longer held (row moved on) — refusing to send")
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
        # ACKED: OpenClaw accepted the run. Stamp 3 + the delivery-dependent
        # side effects, inside a helper that never raises -- past this point
        # nothing may re-classify a DELIVERED kickoff as a failed dispatch.
        await _settle_ack(repo, row_user_id, owner_id=owner_id, generation=generation, dispatch_id=dispatch_id)
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
