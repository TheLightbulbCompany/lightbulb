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

Auth is a shared secret (``X-Lightbulb-SES-Secret``), timing-safe compared
against ``SES_INBOUND_SECRET`` — there is no per-request signature the way
Svix gave the old Resend webhook, because the Lambda is trusted
infrastructure we control, not a third party. An empty secret disables the
endpoint entirely (fail-closed).

**Outcome -> HTTP status is deliberate, not uniformly 200.** Every
DETERMINISTIC drop (disabled, unknown recipient, over cap, spam/virus, failed
authentication, auto-submitted, duplicate) is terminal — retrying would
produce the identical drop again — so those stay 200, as does a successful
``delivered`` and a ``stored_not_woken`` (the message is saved to the inbox
either way; only WAKING the agent is gated, on the sender being allowlisted OR
the address being open (``send_to_anyone``, the default), AND on that sender
being authenticated — see ``sender_allowed`` and ``authenticated_sender``
below; an open-mode stranger past its daily wake limit is the same, reported as
``stored_not_woken_rate_limited``). ``container_not_running`` also stays 200: it now
only means the owner is OVER CAP (the cap gate below already normally
intercepts that case before ``deliver_to_agent`` is even called; this is
belt-and-suspenders for the same predicate) — waking a capped owner is
exactly what the cap forbids, and hammering retries at it would not change
that.

TRANSIENT failures return 503 so SES/the Lambda's own (bounded) retry policy
gets a chance to succeed later: a ``deliver_to_agent`` outcome of ``"error"``
(a delivery error talking to the runtime; the invoke itself holds through a
cold microVM start, so a parked workspace is not a transient failure),
and the top-level catch-all for an unexpected exception (e.g. a DynamoDB
blip). Dedup is keyed on ``message_id`` and recorded BEFORE delivery is
attempted, so a retry that later succeeds cannot double-deliver — the second
attempt short-circuits at the ``duplicate`` check. EVERY 503 return
(``error``, the catch-all) ROLLS THAT DEDUP ROW BACK first
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
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import company_slug_repo
from core.repositories.agent_email_repo import (
    get_by_address,
    is_team_address,
    normalize,
    owner_for_host,
    resolve,
    update_settings,
)
from core.repositories.agent_mail_inbox_repo import find_by_message_id, put_message, reply_key
from core.repositories.container_repo import try_claim_daily_system_notice, try_consume_stranger_wake
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import agent_email, clerk_admin, meeting_invites
from core.services.agent_email import ensure_team_address, sender_allowed
from core.services.agent_mail_autoreply import send_cap_autoreply
from core.services.agent_mail_thread import thread_session_key
from core.services.agent_mail_delivery import deliver_to_agent
from core.services.agent_mail_owner_send import send_to_owner
from core.services.agent_mail_send import _owner_email
from core.services.agent_mail_inbound import (
    STRANGER_WAKES_PER_OWNER_PER_DAY,
    STRANGER_WAKES_PER_SENDER_PER_DAY,
    authenticated_sender,
    build_attachment_manifest,
    build_delivered_message,
    is_auto_submitted,
    parse_from_address,
    stranger_key,
)
from core.services.agent_mail_reply import reply_subject
from core.services.trial_cap import trial_block_reason
from core.services.user_provision import ceo_agent_id
from core.services.webhook_dedup import WebhookDedupResult, delete_event, record_event_or_skip

logger = logging.getLogger(__name__)

router = APIRouter()

# The outcomes worth counting individually. Everything else — every
# deterministic drop, plus the transient `error` — collapses to `dropped` so
# the dimension stays bounded.
_METRIC_RESULTS = ("delivered", "capped_notified", "stored_not_woken", "stored_not_woken_rate_limited")


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


async def _thread_writer(address: str, headers: dict[str, str]) -> str | None:
    """The agent whose sent mail this answers: the writer of ``References[0]``
    (the thread root), else of ``In-Reply-To``. Only rows at this workspace's
    address count, so a message id seen elsewhere cannot steer delivery."""
    for name in ("References", "In-Reply-To"):
        ids = next((v for k, v in headers.items() if k.lower() == name.lower()), "").split()
        item = await find_by_message_id(address, ids[0]) if ids else None
        if item and item.get("direction") == "out":
            return item.get("agent_id")
    return None


async def _ceo(owner_id: str) -> str | None:
    """The starter CEO, else a legacy roster's first seeded agent — the same resolution every
    other CEO door uses. Reading only ``starter_agent_ids`` dropped every first-contact mail to
    the founder workspace (no starter roster) as ``dropped_unknown_recipient`` (prod 2026-10-07)."""
    row = await WorkspaceRepo().resolve_owner_row(owner_id)
    return ceo_agent_id(row) if row else None


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
    # `dict[str, Any]`, not `dict[str, str]`: `verdicts["dkim_domains"]` is a
    # LIST of DKIM signing domains (see the Lambda's `_extract_verdicts`), and
    # a str-valued dict would reject the whole payload. Everything reading a
    # flat verdict off this dict must therefore `str()` it first.
    verdicts: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict] = Field(default_factory=list)
    # The first `text/calendar` part, raw, when the mail carries one (a calendar invite).
    calendar: str | None = None


async def _shared_team_mailbox(body: SesInboundBody) -> tuple[dict | None, str | None, str | None]:
    """Resolve the shared address only from authenticated, verified current membership.

    A personal account or several businesses is not an implicit workspace selection.
    Failures from Clerk/DynamoDB remain retryable through the ingest's existing 503 path.
    """
    verdicts = body.verdicts or {}
    if any(str(verdicts.get(k) or "").upper() == "FAIL" for k in ("spam", "virus")):
        return None, "dropped_spam_or_virus", None
    sender = parse_from_address(body.from_)
    if str(verdicts.get("dmarc") or "").upper() == "FAIL" or not authenticated_sender(verdicts, {}, sender):
        return None, "dropped_failed_authentication", None
    user = await clerk_admin.find_user_by_email(sender, strict=True)
    if not user or not any(
        str(e.get("email_address") or "").lower() == sender
        and (e.get("verification") or {}).get("status") == "verified"
        for e in user.get("email_addresses", [])
    ):
        return None, "dropped_unregistered_sender", None
    memberships = await clerk_admin.list_user_organizations(user["id"], limit=100, strict=True)
    owners = {m["id"] for m in memberships}
    if len(owners) > 1:
        return None, "dropped_ambiguous_workspace", None
    if not owners:
        return None, "dropped_unregistered_sender", None
    owner = next(iter(owners))  # exactly one, never first among several
    address, reason = await ensure_team_address(owner, sender)
    if not address:
        raise RuntimeError(f"shared team address not ready: {reason}")
    row = await get_by_address(address)
    if not row or row.get("owner_id") != owner:
        raise RuntimeError("shared team address owner mismatch")
    return row, None, user["id"]


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
        presented = request.headers.get("X-Lightbulb-SES-Secret", "")
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

        # Explicit workspace addresses keep their routing. With only the shared
        # agents@<domain> recipient, authenticated membership must select exactly
        # one workspace instead of following any founder's public alias. Which AGENT reads the mail is
        # decided below, after the gates: the thread's writer, else the CEO.
        row = None
        verified_sender_user_id = None
        shared = f"agents@{settings.AGENT_EMAIL_DOMAIN.strip().lower()}"
        has_shared = False
        for addr in body.to:
            try:
                candidate = normalize(addr)
            except Exception:  # noqa: BLE001 — a malformed recipient entry must not abort scanning the rest of `to`.
                continue
            if candidate == shared:
                has_shared = True
                continue
            team = await is_team_address(candidate)
            if team is None:
                continue
            row = await get_by_address(candidate)
            if row is None:
                address, reason = await ensure_team_address(team[0], await _owner_email(team[0]))
                if reason in ("mint_failed", "slug_allocation_failed"):
                    # Transient: 503 + dedup rollback below, so SES redelivers this first mail.
                    raise RuntimeError(f"team address not ready: {reason}")
                row = await get_by_address(address) if address == candidate else None
            if row is not None:
                break
        if row is None and has_shared:
            row, rejection, verified_sender_user_id = await _shared_team_mailbox(body)
            if rejection:
                logger.info("agent_email shared routing: %s", rejection)
                return _final(rejection)
        if row is None:
            return _final("dropped_unknown_recipient")

        sender = parse_from_address(body.from_)

        # Anti-abuse gate: SES's own receipt pipeline already scanned this
        # mail for spam/viruses before the Lambda ever saw it, so an explicit
        # FAIL here is authoritative — always dropped.
        verdicts = body.verdicts or {}
        if str(verdicts.get("virus") or "").upper() == "FAIL" or str(verdicts.get("spam") or "").upper() == "FAIL":
            return _final("dropped_spam_or_virus")

        # Anti-spoof gate: DMARC is authoritative when SES reports a verdict
        # for it — an explicit FAIL always drops, and (unlike the Resend
        # path's best-effort header parse) a PASS is trusted outright with no
        # further fallback. Anything else is NO dmarc verdict — SES says GRAY
        # for a domain without a DMARC record, and the Lambda always sends the
        # key, "" when SES ran no check — and there we fall back to requiring
        # SPF and DKIM to both explicitly FAIL before dropping. A missing/GRAY
        # SPF or DKIM is never a hard drop — the WAKE gate below
        # (`sender_allowed` AND `authenticated_sender`) is what decides whether
        # an unproven sender reaches the agent; the message is always stored
        # either way.
        dmarc = str(verdicts.get("dmarc") or "").upper()
        if dmarc == "FAIL":
            return _final("dropped_failed_authentication")
        if dmarc != "PASS":
            spf = str(verdicts.get("spf") or "").upper()
            dkim = str(verdicts.get("dkim") or "").upper()
            if spf == "FAIL" and dkim == "FAIL":
                return _final("dropped_failed_authentication")

        # A member's invite to the team address is a meeting, not mail. After
        # the spam/spoof gates, before the loop guard: an invite is not a reply.
        if body.calendar:
            if verified_sender_user_id is not None:
                outcome = await meeting_invites.handle_team_mail(
                    row["owner_id"], body, row, verified_sender_user_id=verified_sender_user_id
                )
            else:
                outcome = await meeting_invites.handle_team_mail(row["owner_id"], body, row)
            if outcome is not None:
                return _final(outcome)

        # Loop guard: never deliver autoresponder/bulk/list traffic — an
        # agent replying to one of these could re-trigger itself forever.
        if is_auto_submitted(body.headers or {}):
            return _final("dropped_auto_submitted")

        # A reply to an agent's mail goes back to that agent — resolved BEFORE
        # delivery, since the thread's session key carries no agent id and the
        # bridge qualifies it per agent. Everything else is the CEO's.
        agent_id = await _thread_writer(row["address"], body.headers or {}) or await _ceo(row["owner_id"])
        if not agent_id:
            return _final("dropped_unknown_recipient")

        # Store first — every gated-clean message lands in the inbox whether or
        # not it wakes the agent. Waking is open to any authenticated sender
        # unless the owner turned ``send_to_anyone`` off, in which case it is
        # reserved for the owner + the owner's allowlist. The cap autoreply is
        # reserved for the allowlist either way (see the cap block below).
        #
        # Idempotent on SES's message_id: the 503 paths below roll back the
        # dedup row so SES retries, and that retry must reuse the row the
        # first attempt stored rather than add a second one.
        stored = await find_by_message_id(row["address"], body.message_id)
        if not stored:
            stored = await put_message(
                address=row["address"],
                owner_id=row["owner_id"],
                agent_id=agent_id,
                message_id=body.message_id,
                from_addr=sender,
                subject=body.subject or "",
                text=body.text or "",
                headers=body.headers or {},
                attachments=body.attachments or [],
            )
            await agent_email.mail_changed(row["owner_id"])  # a replay already nudged the panel
        # Waking takes BOTH: the sender is permitted — listed by the owner, or
        # the address is open (`send_to_anyone`, the default since the
        # 2026-09-24 owner ruling) — AND the `From` is actually authenticated.
        # `sender_allowed` alone is not enough — `From` is a bare header parse,
        # so an attacker whose own domain passes SPF can forge an allowlisted
        # `From` and wake somebody else's agent. See `authenticated_sender`,
        # which stays mandatory in open mode too. The message is still STORED
        # either way; only the wake tightens.
        allowlisted = sender_allowed(row, sender)
        if not (
            (allowlisted or row.get("send_to_anyone")) and authenticated_sender(verdicts, body.headers or {}, sender)
        ):
            return _final("stored_not_woken")
        key = reply_key(row["address"], stored["received_key"])

        # Free-tier cost cap — same predicate the chat/container-start
        # gates use, so an over-cap owner can't route around it via email.
        #
        # ANSWERED only for an EXPLICITLY allowlisted sender (`allowlisted`
        # above — the owner or someone the owner listed). An open-mode
        # stranger who reaches this point is dropped WITHOUT a reply
        # (`dropped_over_cap`): replying would confirm the address exists and
        # turn this into a reflector, exactly what we refuse for the drops
        # above. That allowlist check is the whole safety argument for
        # replying here.
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
            if not allowlisted:
                return _final("dropped_over_cap")
            notified = await send_cap_autoreply(
                owner_id=row["owner_id"],
                agent_id=agent_id,
                reason=cap_reason,
                from_addr=row["address"],
                to=sender,
                in_reply_to=body.headers.get("Message-ID", "") or body.headers.get("Message-Id", ""),
                subject=reply_subject(body.subject or ""),
            )
            return _final("capped_notified" if notified else "dropped_over_cap")

        # Open-mode strangers are bounded per sender and per owner per day: a wake spends the
        # owner's inference budget, so an unbounded stranger could drain a trial's daily allowance
        # or a paid owner's credits. Over the limit the mail stays stored but does not wake the
        # agent, and nobody is answered. The owner and allowlisted senders are never limited.
        # Placed after the cap block so mail that is refused anyway does not burn the counters; a
        # 503 retry of the same message counts again, which only makes the limit slightly stricter.
        if not allowlisted and not await try_consume_stranger_wake(
            row["owner_id"],
            sender_key=stranger_key(sender),
            day=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            owner_cap=STRANGER_WAKES_PER_OWNER_PER_DAY,
            sender_cap=STRANGER_WAKES_PER_SENDER_PER_DAY,
        ):
            return _final("stored_not_woken_rate_limited")

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
            trusted=allowlisted,
        )

        result = await deliver_to_agent(
            owner_id=row["owner_id"],
            agent_id=agent_id,
            message=message,
            idempotency_key=body.message_id,
            # Same mail thread -> same OpenClaw session, so a reply is a
            # continuation rather than a cold start. None (no usable
            # message-id) keeps the old per-message throwaway session.
            session_key=thread_session_key(body.headers),
            # Super-agent workspaces: how the Router's reply gets back into this thread (lane A, A9).
            source="mail",
            reply_ref={
                "address": sender,
                "messageId": body.headers.get("Message-ID", "") or body.headers.get("Message-Id", "") or None,
                "references": body.headers.get("References") or None,
                "subject": body.subject or "",
            },
        )
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


_PAUSED_SUBJECT = "Outbound email paused"
_PAUSED_BODY = (
    "A recipient marked an email from one of your agents as spam, so outbound email "
    "from your agents is paused. Your agents can still email you.\n\n"
    "Review who your agents may email and re-enable sending in Control → Agents → Team email."
)


async def _slug_held_since(slug: str, sent_at: Any) -> bool:
    """True when the slug row was reserved at or before ``sent_at`` (the complained mail's SES
    timestamp). A missing or unparseable time on either side proves nothing: False."""
    try:
        reserved = datetime.fromisoformat((await company_slug_repo.get_by_slug(slug) or {})["created_at"])
        sent = datetime.fromisoformat(sent_at)
    except (KeyError, TypeError, ValueError):
        return False
    return sent.replace(tzinfo=sent.tzinfo or timezone.utc) >= reserved.replace(tzinfo=reserved.tzinfo or timezone.utc)


@router.post("/ses-events", include_in_schema=False)
async def handle_ses_events(request: Request):
    """SES complaint events (configuration-set → EventBridge → API destination)."""
    if not settings.SES_INBOUND_SECRET or not settings.AGENT_EMAIL_DOMAIN.strip():
        return {"status": "disabled"}
    presented = request.headers.get("X-Lightbulb-SES-Secret", "")
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), settings.SES_INBOUND_SECRET.encode("utf-8")):
        return JSONResponse(status_code=401, content={"status": "bad_secret"})
    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"status": "bad_json"})
    if not isinstance(event, dict):
        # Valid JSON, but not an object — e.g. a bare list/string/number. Same
        # drop as a decode failure: there is no `detail` to read off of it.
        return JSONResponse(status_code=400, content={"status": "bad_json"})
    detail = event.get("detail")
    if not isinstance(detail, dict):
        # No object to read eventType/mail off of — not a complaint we can act on.
        return {"status": "ignored"}
    if (detail.get("eventType") or "").lower() != "complaint":
        return {"status": "ignored"}
    mail = detail.get("mail")
    source = mail.get("source") if isinstance(mail, dict) else ""
    if not isinstance(source, str):
        # A non-string `source` is a PERMANENTLY invalid event; a 500 would make
        # EventBridge retry it until it expires.
        return {"status": "ignored"}
    # A From with a display name ("Friday · Acme agents" <agents@…>) reaches us as-is.
    candidate = normalize(parseaddr(source)[1]) if source else ""
    # ``resolve``: mail sent as a workspace's public address names its alias row.
    row = await resolve(candidate) if candidate else None
    # A retired per-agent address has no row any more; its host still names the workspace.
    resolved = (row["owner_id"], None) if row else (await owner_for_host(candidate) if candidate else None)
    if resolved and not row and not await _slug_held_since(resolved[1], mail.get("timestamp")):
        # A wipe frees a slug for another workspace: a complaint about mail sent before this
        # owner reserved it is the previous owner's, and must pause nothing.
        resolved = None
    if not resolved:
        return {"status": "unknown_source"}
    owner_id = resolved[0]
    try:
        # The workspace's one mailbox: pausing it pauses every agent — also when
        # the complaint names a retired per-agent address. A failed write raises
        # into the 503 below, so EventBridge retries.
        team, _reason = await ensure_team_address(owner_id, await _owner_email(owner_id))
        address = team or (row or {}).get("address")
        if not address:
            raise RuntimeError("no mailbox row to pause")  # 503: EventBridge retries
        await update_settings(address, outbound_disabled="complaint")
        # ONE notice per owner per UTC day. SES fans a complaint out per
        # recipient address, and `send_to_owner(system_notice=True)` bypasses
        # the daily send cap by design — this durable claim is the only bound
        # on it. Losing the claim suppresses the email only; the addresses are
        # disabled either way.
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        notified = await try_claim_daily_system_notice(owner_id, day=day, kind="complaint")
        put_metric(
            "agent_email.complaint",
            dimensions={"result": "disabled" if notified else "disabled_notice_suppressed"},
        )
        if notified:
            try:
                await send_to_owner(
                    owner_id=owner_id,
                    purpose="routine",
                    subject=_PAUSED_SUBJECT,
                    body_text=_PAUSED_BODY,
                    # Platform notice, not Friday saying hello — must not consume
                    # the owner's once-only first-contact slot.
                    system_notice=True,
                )
            except Exception:  # noqa: BLE001 — notification is best-effort
                logger.warning("ses_events: owner notify failed for %s", owner_id, exc_info=True)
        return {"status": "disabled", "owner_id": owner_id, "notified": notified}
    except Exception:  # noqa: BLE001 — mirrors handle_ses_inbound's transient-vs-terminal split
        logger.exception("ses_events handler failed for owner %s", owner_id)
        return JSONResponse(status_code=503, content={"status": "error"})
