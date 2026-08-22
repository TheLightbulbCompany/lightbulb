"""SES-inbound agent-email ingest endpoint — provisioned agent email (inbound leg).

This is the sole inbound leg of provisioned agent email — it replaced the
Resend-webhook path (formerly ``routers/agent_mail.py``, deleted once this
endpoint took over). An AWS Lambda (built separately) parses the raw SES
inbound email (SNS notification -> raw MIME on S3) into a clean JSON payload
and POSTs it here. SES's own receipt pipeline already ran
SPF/DKIM/DMARC/spam/virus scanning by the time this endpoint sees the mail —
those verdicts are the AUTHORITATIVE sender-authentication signal here (see
the auth-verdict gate below), unlike the Resend predecessor's best-effort
`Authentication-Results` header parse.

Auth is a shared secret (``X-Isol8-SES-Secret``), timing-safe compared
against ``SES_INBOUND_SECRET`` — there is no per-request signature the way
Svix gave the old Resend webhook, because the Lambda is trusted
infrastructure we control, not a third party. An empty secret disables the
endpoint entirely (fail-closed).

**Outcome -> HTTP status is deliberate, not uniformly 200.** Every
DETERMINISTIC drop (disabled, unknown recipient, over cap, spam/virus, failed
authentication, auto-submitted, duplicate) is terminal — retrying would
produce the identical drop again — so those stay 200, as does a successful
``delivered`` and a ``stored_not_woken`` (the message is saved to the inbox
either way; only WAKING the agent is gated on the sender allowlist — see
``sender_allowed`` below). ``container_not_running`` also stays 200: it now
only means the owner is OVER CAP (the cap gate below already normally
intercepts that case before ``deliver_to_agent`` is even called; this is
belt-and-suspenders for the same predicate) — waking a capped owner is
exactly what the cap forbids, and hammering retries at it would not change
that.

TRANSIENT failures return 503 so SES/the Lambda's own (bounded) retry policy
gets a chance to succeed later: a ``deliver_to_agent`` outcome of ``"error"``
(e.g. a delivery error talking to a running container) or ``"woken"`` (an
under-cap owner's scaled-to-zero container was just started — the 503 is
what makes the Lambda re-raise so SES's own async retry, ~1 minute later,
lands after the Fargate cold start finishes and delivery actually happens),
and the top-level catch-all for an unexpected exception (e.g. a DynamoDB
blip). Dedup is keyed on ``message_id`` and recorded BEFORE delivery is
attempted, so a retry that later succeeds cannot double-deliver — the second
attempt short-circuits at the ``duplicate`` check. EVERY 503 return
(``woken``, ``error``, the catch-all) ROLLS THAT DEDUP ROW BACK first
(``_rollback_dedup``): a dedup record must never outlive a request we are
explicitly asking SES to retry, or the retry short-circuits as a
``duplicate``, the mail is dropped for good, and the Fargate cold start the
wake just paid for is wasted. Terminal drops keep their row — re-running one
would produce the identical drop.

A bad/missing shared secret is 401 — not retried; there is no fixing a
forged credential by resending the same request.

Drop reasons are returned in the JSON body's ``status`` field purely for
observability (log/metric correlation); the Lambda does not parse it.
"""

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.agent_email_repo import get_by_address, normalize
from core.repositories.agent_mail_inbox_repo import put_message, reply_key
from core.services.agent_email import sender_allowed
from core.services.agent_mail_autoreply import send_cap_autoreply
from core.services.agent_mail_thread import thread_session_key
from core.services.agent_mail_delivery import deliver_to_agent
from core.services.agent_mail_inbound import (
    build_attachment_manifest,
    build_delivered_message,
    is_auto_submitted,
    parse_from_address,
)
from core.services.agent_mail_reply import reply_subject
from core.services.trial_cap import trial_block_reason
from core.services.webhook_dedup import WebhookDedupResult, delete_event, record_event_or_skip

logger = logging.getLogger(__name__)

router = APIRouter()

# The outcomes worth counting individually. Everything else — every
# deterministic drop, plus the transient `error` — collapses to `dropped` so
# the dimension stays bounded.
_METRIC_RESULTS = ("delivered", "woken", "capped_notified", "stored_not_woken")


def _final(status: str, *, code: int = 200):
    """Emit the inbound-outcome metric and build the response."""
    try:
        put_metric(
            "agent_email.inbound",
            dimensions={"result": status if status in _METRIC_RESULTS else "dropped"},
        )
    except Exception:  # noqa: BLE001 — telemetry must never break ingestion
        logger.debug("agent_email.inbound metric emission failed", exc_info=True)
    if code == 200:
        return {"status": status}
    return JSONResponse(status_code=code, content={"status": status})


async def _rollback_dedup(message_id: str) -> None:
    """Drop the dedup row recorded at the top of this request.

    The dedup record must not outlive a request we are asking SES to retry:
    the retry arrives with the SAME message_id, would short-circuit at the
    dedup gate as a ``duplicate``, and the mail would be dropped permanently.
    Best-effort — a failed rollback must not change the 503 we return.
    """
    if not message_id:
        return
    try:
        await delete_event(message_id)
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning("ses_inbound: dedup rollback failed for %s", message_id, exc_info=True)


class SesInboundBody(BaseModel):
    """Clean JSON payload the SES-ingest Lambda POSTs after parsing raw SES mail.

    ``from`` is aliased to ``from_`` since `from` is a Python keyword.
    ``message_id`` is SES's stable message id — the dedup key across SES
    retries; it can arrive empty (dedup is then skipped, not treated as an
    error — see the dedup step below).
    """

    model_config = ConfigDict(populate_by_name=True)

    message_id: str = ""
    to: list[str] = Field(default_factory=list)
    from_: str = Field(default="", alias="from")
    subject: str = ""
    text: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    verdicts: dict[str, str] = Field(default_factory=dict)
    attachments: list[dict] = Field(default_factory=list)


@router.post(
    "/ses-inbound",
    summary="Handle SES inbound email ingest",
    description="Receives a Lambda-parsed JSON payload for inbound SES email addressed to provisioned agent-email addresses.",
    operation_id="handle_ses_inbound",
    include_in_schema=False,
)
async def handle_ses_inbound(body: SesInboundBody, request: Request):
    """Handle a POST from the SES-ingest Lambda. No Clerk JWT — shared-secret header only."""
    try:
        # Feature gate: agent email is off unless BOTH a real shared secret
        # and a mint domain are configured. Half-configured (one set, one
        # not) is treated as disabled rather than partially processing.
        if not settings.SES_INBOUND_SECRET or not settings.AGENT_EMAIL_DOMAIN.strip():
            return {"status": "disabled"}

        # Auth BEFORE any parsing/dedup work — an unauthenticated caller must
        # not be able to churn the dedup table or trigger downstream reads.
        presented = request.headers.get("X-Isol8-SES-Secret", "")
        if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), settings.SES_INBOUND_SECRET.encode("utf-8")
        ):
            return JSONResponse(status_code=401, content={"status": "bad_secret"})

        # Idempotency dedup — keyed on SES's message_id, stable across SES's
        # own at-least-once retry policy. Recorded up front (unlike the
        # Resend webhook, which defers dedup until right before delivery):
        # every gate below this point is a deterministic function of the
        # payload SES already fully scanned, so there is no transient-failure
        # window here that would need to stay retryable. A missing/empty
        # message_id skips dedup rather than erroring — better to risk a
        # double-delivery than to silently drop mail because the Lambda
        # didn't have a stable id for this particular message.
        if body.message_id:
            dedup = await record_event_or_skip(body.message_id, source="ses_inbound")
            if dedup is WebhookDedupResult.ALREADY_SEEN:
                return _final("duplicate")

        # Resolve recipient: first provisioned address in `to` that we know
        # about wins. A message can be addressed to multiple recipients;
        # only one of them needs to be one of ours.
        row = None
        for addr in body.to:
            try:
                candidate = normalize(addr)
            except Exception:  # noqa: BLE001 — a malformed recipient entry must not abort scanning the rest of `to`.
                continue
            row = await get_by_address(candidate)
            if row is not None:
                break
        if row is None:
            return _final("dropped_unknown_recipient")

        sender = parse_from_address(body.from_)

        # Anti-abuse gate: SES's own receipt pipeline already scanned this
        # mail for spam/viruses before the Lambda ever saw it, so an explicit
        # FAIL here is authoritative — always dropped.
        verdicts = body.verdicts or {}
        if (verdicts.get("virus") or "").upper() == "FAIL" or (verdicts.get("spam") or "").upper() == "FAIL":
            return _final("dropped_spam_or_virus")

        # Anti-spoof gate: DMARC is authoritative when SES reports a verdict
        # for it — an explicit FAIL always drops, and (unlike the Resend
        # path's best-effort header parse) a PASS is trusted outright with no
        # further fallback. Only when SES surfaces NO dmarc verdict at all do
        # we fall back to requiring SPF and DKIM to both explicitly FAIL
        # before dropping. A missing/GRAY verdict on any of these is never a
        # hard drop — the sender allowlist below is the primary control on
        # whether the message WAKES the agent (it is always stored either way).
        dmarc = verdicts.get("dmarc")
        if dmarc is not None:
            if dmarc.upper() == "FAIL":
                return _final("dropped_failed_authentication")
        else:
            spf = (verdicts.get("spf") or "").upper()
            dkim = (verdicts.get("dkim") or "").upper()
            if spf == "FAIL" and dkim == "FAIL":
                return _final("dropped_failed_authentication")

        # Loop guard: never deliver autoresponder/bulk/list traffic — an
        # agent replying to one of these could re-trigger itself forever.
        if is_auto_submitted(body.headers or {}):
            return _final("dropped_auto_submitted")

        # Store first — every gated-clean message lands in the inbox whether or
        # not it wakes the agent. Waking (and the cap autoreply) is reserved for
        # the owner + the owner's allowlist.
        stored = await put_message(
            address=row["address"],
            owner_id=row["owner_id"],
            agent_id=row["agent_id"],
            message_id=body.message_id,
            from_addr=sender,
            subject=body.subject or "",
            text=body.text or "",
            headers=body.headers or {},
            attachments=body.attachments or [],
        )
        if not sender_allowed(row, sender):
            return _final("stored_not_woken")
        key = reply_key(row["address"], stored["received_key"])

        # Free-tier cost cap — same predicate the chat/container-start
        # gates use, so an over-cap owner can't route around it via email.
        #
        # ANSWERED, not silently dropped — because only an allowlisted sender
        # (`sender_allowed` above) reaches this point at all: every other
        # sender already exited with `stored_not_woken`. That allowlist check
        # is the whole safety argument for replying here in a way we do NOT
        # for the drops above (replying to those would confirm the address
        # exists and turn this into a reflector).
        #
        # The reply is server-templated with no agent run — that is the whole
        # reason we can answer at all when the cap says we cannot think.
        #
        # POSITION IS LOAD-BEARING: this is the first branch that originates
        # real outbound mail, so it must sit AFTER every gate above that
        # decides whether this inbound is trustworthy (spam/virus, DMARC/
        # SPF+DKIM anti-spoof, auto-submitted loop guard, sender allowlist) —
        # `From` is a bare header parse with no DKIM/SPF binding, so the
        # sender allowlist alone does not prove the message wasn't spoofed or
        # a mail-loop auto-reply. Sending here before those gates run would
        # turn this endpoint into a reflector or a loop. Do not move this
        # block back above them.
        cap_reason = await trial_block_reason(row["owner_id"])
        if cap_reason is not None:
            notified = await send_cap_autoreply(
                owner_id=row["owner_id"],
                reason=cap_reason,
                from_addr=row["address"],
                to=sender,
                in_reply_to=body.headers.get("Message-ID", "") or body.headers.get("Message-Id", ""),
                subject=reply_subject(body.subject or ""),
            )
            return _final("capped_notified" if notified else "dropped_over_cap")

        # Attachment manifest — best-effort. A manifest-formatting bug must
        # not drop a real delivery: a working text delivery beats a dropped one.
        try:
            manifest = build_attachment_manifest(body.attachments)
        except Exception:  # noqa: BLE001 — see comment above.
            manifest = ""

        message = build_delivered_message(
            from_addr=sender,
            subject=body.subject,
            body_text=body.text or "",
            reply_key=key,
            attachments_manifest=manifest,
        )

        result = await deliver_to_agent(
            owner_id=row["owner_id"],
            agent_id=row["agent_id"],
            message=message,
            idempotency_key=body.message_id,
            # Same mail thread -> same OpenClaw session, so a reply is a
            # continuation rather than a cold start. None (no usable
            # message-id) keeps the old per-message throwaway session.
            session_key=thread_session_key(body.headers),
        )
        if result == "woken":
            # deliver_to_agent just started an asleep, under-cap owner's
            # container -- signal transient so the ingest Lambda re-raises and
            # its async SES invocation is retried once the container is up.
            # See the module docstring: delivery completes on that retry, not
            # in this request -- which is exactly why the dedup row has to go
            # first, or that retry never gets past the duplicate check and the
            # cold start we just paid for is wasted.
            await _rollback_dedup(body.message_id)
            return _final(result, code=503)
        if result == "error":
            # Transient delivery failure (e.g. a running container's RPC
            # errored) — 503 so SES/the Lambda retries. `container_not_running`
            # is intentionally excluded: that now means OVER CAP, which a
            # retry cannot fix.
            await _rollback_dedup(body.message_id)
            return _final(result, code=503)
        return _final(result)
    except Exception:  # noqa: BLE001 — see module docstring.
        logger.exception("ses_inbound handler failed unexpectedly")
        # Same reason as the two 503s above: this one asks SES to retry too.
        await _rollback_dedup(body.message_id)
        return _final("error", code=503)
