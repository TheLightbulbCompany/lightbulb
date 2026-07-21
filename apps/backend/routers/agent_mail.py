"""Resend inbound-email webhook router — provisioned agent email (inbound leg).

Receives ``email.received`` events from Resend for addresses provisioned
under ``AGENT_EMAIL_DOMAIN`` and relays them into the owning agent's running
OpenClaw container via ``agent_mail_delivery.deliver_to_agent``.

Verification uses the same svix HMAC-SHA256 scheme as the Clerk webhook
(``routers/webhooks.py``), factored out into ``core.crypto.svix`` so both
callers share one implementation. Unlike the Clerk router, an unset secret
does NOT skip verification — it disables the whole endpoint (see the
feature-gate check below), because there is no "local dev is fine without
this" case for third-party inbound mail.

**Every branch returns 200** except two deliberately non-2xx cases Resend
(via Svix) is expected to retry on: a genuinely bad signature (401 — not
retried; there is no fixing a forged/corrupt payload by resending it) and a
failed fetch against Resend's receiving API (503 — a transient blip that
retrying can plausibly fix). Every other drop reason (unknown recipient,
unverified sender, over cap, auto-submitted, failed authentication) is
terminal — retrying would just reach the same drop again — so those stay
200. The entire handler body is wrapped in a top-level try/except: an
unexpected exception must never surface as an unhandled 5xx, because that
would put Resend into a retry storm against a permanently-broken event. The
tradeoff mirrors the Clerk webhook's posture (see its module docstring) — we
own recovery from here on, not the provider's retry logic.

Drop reasons are returned in the JSON body's ``status`` field purely for
observability (log/metric correlation); Resend does not parse it.
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.config import settings
from core.crypto.svix import verify_svix_signature
from core.repositories.agent_email_repo import get_by_address, normalize
from core.services.agent_mail_delivery import deliver_to_agent
from core.services.agent_mail_inbound import (
    build_attachment_manifest,
    build_delivered_message,
    fetch_received_email,
    is_auto_submitted,
    list_received_attachments,
    parse_from_address,
    sender_authentication_verdict,
)
from core.services.agent_mail_token import encode_reply_token
from core.services.trial_cap import trial_block_reason
from core.services.webhook_dedup import WebhookDedupResult, record_event_or_skip

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/resend-inbound",
    summary="Handle Resend inbound email webhook",
    description="Receives email.received events for provisioned agent-email addresses.",
    operation_id="handle_resend_inbound_webhook",
    include_in_schema=False,
)
async def handle_resend_inbound(request: Request):
    """Handle a Resend inbound-email webhook delivery. No Clerk JWT auth — svix signature only."""
    try:
        # Feature gate: agent email is off unless BOTH a real webhook secret
        # and a mint domain are configured. Half-configured (one set, one
        # not) is treated as disabled rather than partially processing.
        if not settings.RESEND_WEBHOOK_SECRET.startswith("whsec_") or not settings.AGENT_EMAIL_DOMAIN.strip():
            return {"status": "disabled"}

        body = await request.body()
        if not verify_svix_signature(
            secret=settings.RESEND_WEBHOOK_SECRET,
            body=body,
            headers=dict(request.headers),
        ):
            return JSONResponse(status_code=401, content={"status": "bad_signature"})

        payload = json.loads(body)
        if payload.get("type") != "email.received":
            return {"status": "ignored"}

        data = payload["data"]
        email_id = data["email_id"]
        to_list = data.get("to") or []
        if isinstance(to_list, str):
            # A single recipient can arrive as a bare string rather than a
            # one-element list; iterating it directly below would scan it
            # character-by-character instead of as one address.
            to_list = [to_list]
        raw_from = data.get("from", "")
        subject = data.get("subject", "")
        message_id = data.get("message_id", "")

        # Resolve recipient: first provisioned address in `to` that we know
        # about wins. A message can be addressed to multiple recipients;
        # only one of them needs to be one of ours.
        row = None
        for addr in to_list:
            try:
                candidate = normalize(addr)
            except Exception:  # noqa: BLE001 — a malformed recipient entry must not abort scanning the rest of `to`.
                continue
            row = await get_by_address(candidate)
            if row is not None:
                break
        if row is None:
            return {"status": "dropped_unknown_recipient"}

        # Sender allowlist — skipped entirely for public addresses (no
        # allowlist to enforce), enforced for private ones.
        sender = parse_from_address(raw_from)
        if not row.get("public") and sender not in (row.get("verified_senders") or []):
            return {"status": "dropped_unverified_sender"}

        # Free-tier cost cap — same predicate the chat/container-start
        # gates use, so an over-cap owner can't route around it via email.
        if await trial_block_reason(row["owner_id"]) is not None:
            return {"status": "dropped_over_cap"}

        # Resend's webhook envelope is thin; the full body/headers require a
        # second fetch against Resend's receiving API.
        fetched = await fetch_received_email(email_id)
        if fetched is None:
            # Transient (Resend API blip, network error) — nothing has been
            # deduped yet (dedup is recorded later, right before delivery),
            # so a non-2xx here makes Resend/Svix retry the delivery instead
            # of silently dropping it forever.
            return JSONResponse(status_code=503, content={"status": "fetch_failed_retryable"})

        # Loop guard: never deliver autoresponder/bulk/list traffic — an
        # agent replying to one of these could re-trigger itself forever.
        if is_auto_submitted(fetched.get("headers") or {}):
            return {"status": "dropped_auto_submitted"}

        # Anti-spoof gate: the From header alone is forgeable (that's what
        # `verified_senders` above just matched against), so read the mail's
        # SPF/DKIM/DMARC verdict per the fetched Authentication-Results header.
        # An explicit FAIL is always dropped. Resend surfaces no verdict field
        # at all, so an UNKNOWN verdict (the common case) is delivered — the
        # sender allowlist above is the primary control, this header is only
        # best-effort defense-in-depth on top of it.
        verdict = sender_authentication_verdict(fetched.get("headers") or {})
        if verdict == "fail":
            return {"status": "dropped_failed_authentication"}

        # v1: plain-text body only. No HTML-stripping fallback — an empty
        # text part just delivers an empty body rather than attempting to
        # derive text from HTML.
        body_text = fetched.get("text") or ""

        token = encode_reply_token(
            {
                "owner_id": row["owner_id"],
                "agent_id": row["agent_id"],
                "reply_to": sender,
                "in_reply_to": message_id,
                "subject": subject,
            }
        )

        # Attachment manifest — best-effort. `list_received_attachments`
        # already never raises, but this guard means a manifest-formatting
        # bug can't drop a real delivery: a working text delivery beats a
        # dropped one.
        try:
            attachments = await list_received_attachments(email_id)
            manifest = build_attachment_manifest(attachments)
        except Exception:  # noqa: BLE001 — see comment above.
            manifest = ""

        message = build_delivered_message(
            from_addr=sender,
            subject=subject,
            body_text=body_text,
            reply_token=token,
            attachments_manifest=manifest,
        )

        # Idempotency dedupe — Resend, like svix-based providers generally, is
        # at-least-once delivery. Keyed on svix-id, which is unique per
        # genuine event and identical across redeliveries of the same one.
        # Recorded here, immediately before delivery, rather than up front
        # right after signature verification: every gate above this point
        # (recipient/sender/cap checks, the Resend fetch) can fail
        # transiently and must stay retryable — only a message that is
        # actually about to be delivered gets marked as seen, so a genuine
        # at-least-once redelivery is still caught right before it would
        # double-deliver.
        svix_id = request.headers.get("svix-id")
        if svix_id:
            dedup = await record_event_or_skip(svix_id, source="resend_inbound")
            if dedup is WebhookDedupResult.ALREADY_SEEN:
                return {"status": "duplicate"}

        result = await deliver_to_agent(
            owner_id=row["owner_id"],
            agent_id=row["agent_id"],
            message=message,
            idempotency_key=email_id,
        )
        return {"status": result}
    except Exception:  # noqa: BLE001 — must never 5xx; see module docstring.
        logger.exception("agent_mail resend-inbound handler failed unexpectedly")
        return {"status": "error"}
