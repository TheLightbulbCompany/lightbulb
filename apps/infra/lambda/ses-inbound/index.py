"""SES-inbound agent-email ingest Lambda — MIME parse, verdicts, attachments,
POST to the Isol8 backend.

This is the SES side of provisioned agent email's inbound leg. A receipt rule
on the domain in ``AGENT_EMAIL_DOMAIN`` runs two Lambda-action-adjacent steps:
  1. an S3 action stores the raw MIME at
     ``s3://{RAW_MAIL_BUCKET}/{RAW_MAIL_PREFIX}{messageId}``, then
  2. this Lambda action fires with the SES receipt event (verdicts +
     envelope metadata, but NOT the mail body — that is fetched from S3).

For each record this Lambda:
  - reads the raw MIME from S3 and parses it with the stdlib ``email``
    package (no third-party MIME parser — this asset ships with
    ``Code.fromAsset`` and no pip deps, so the Lambda runtime's bundled
    boto3 is the only dependency),
  - extracts SES's own SPF/DKIM/DMARC/spam/virus verdicts (SES's receipt
    pipeline has already run these scans by the time this Lambda sees the
    mail — they are authoritative, not best-effort),
  - stores each attachment to S3 and mints a 24h presigned GET URL,
  - and POSTs a clean JSON payload to the backend's ``/webhooks/ses-inbound``
    ingest endpoint, authenticated with a shared secret (``X-Isol8-SES-Secret``,
    fetched from Secrets Manager and cached across warm invocations).

The backend, not this Lambda, makes every content decision (recipient
resolution, sender allowlist, the free-tier cost cap, spam/virus/DMARC
drop, the auto-submitted loop guard) — see ``routers/ses_inbound.py``'s
module docstring. This Lambda's job is purely "parse safely and deliver a
clean payload"; it does not drop mail based on content.

FAILURE POSTURE. Three exhaustive outcomes for the backend POST:
  - a 2xx/4xx/5xx HTTP response was received (a real ``HTTPError`` for
    non-2xx) -> logged, NOT raised. The backend already answered; retrying
    the same payload would not change a deterministic 4xx, and a 5xx is the
    backend's problem to alarm on, not this Lambda's to retry into.
  - the POST could not be completed at all (``URLError``/timeout/socket
    error — no response came back) -> re-raised. This is "the whole thing
    failed to POST", and SES/Lambda's own retry policy re-invokes with the
    same S3 object, so a transient backend/network outage recovers on its
    own.
  - reading the raw MIME from S3 or parsing it failed -> also re-raised,
    for the same reason: these are recoverable-by-retry failures, not
    content decisions.
A per-record try/except logs any exception before it propagates so the
CloudWatch trail always shows which ``messageId`` failed. Re-raising is
safe against duplicate delivery on retry: the backend's ``/ses-inbound``
dedups on ``message_id`` before doing anything else (see
``webhook_dedup.record_event_or_skip`` in that router), so a redelivered
event that already succeeded downstream is a no-op there, not a double
delivery to the agent.

Per-attachment failures are handled separately and do NOT propagate: one
malformed/oversized attachment part must not sink an otherwise-good email
delivery. Attachments are capped at ``_MAX_ATTACHMENTS`` count and
``_MAX_ATTACHMENTS_TOTAL_BYTES`` combined size; anything beyond the cap is
simply skipped (logged), not treated as an error.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from email import message_from_bytes, policy

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Attachment caps — a mail bombed with dozens/huge attachments must not blow
# out this Lambda's memory or the backend's request body. Anything beyond
# either cap is skipped (logged), never treated as a hard failure.
_MAX_ATTACHMENTS = 10
_MAX_ATTACHMENTS_TOTAL_BYTES = 25 * 1024 * 1024

# Presigned attachment URLs are valid for 24h — long enough for an agent to
# fetch them well after the triggering chat turn, short enough to bound
# exposure of what is otherwise an unauthenticated-sender's content.
_PRESIGNED_URL_EXPIRY_SECONDS = 24 * 60 * 60

_BACKEND_POST_TIMEOUT_SECONDS = 10

# Cold-start caches: the shared secret and the boto3 clients are
# fetched/built once per Lambda container and reused across invocations.
_SECRET_CACHE = None
_S3 = None
_SECRETS_MANAGER = None


def _s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.client("s3")
    return _S3


def _secretsmanager():
    global _SECRETS_MANAGER
    if _SECRETS_MANAGER is None:
        _SECRETS_MANAGER = boto3.client("secretsmanager")
    return _SECRETS_MANAGER


def _get_secret():
    """Return the shared ``X-Isol8-SES-Secret`` value, fetched once at
    cold-start and cached for the life of the container."""
    global _SECRET_CACHE
    if _SECRET_CACHE is None:
        arn = os.environ["SES_INBOUND_SECRET_ARN"]
        _SECRET_CACHE = _secretsmanager().get_secret_value(SecretId=arn)["SecretString"]
    return _SECRET_CACHE


def _extract_verdicts(receipt):
    """Map SES's per-check verdict objects to a flat {name: status} dict.

    Every lookup is guarded with ``.get`` — a receipt missing a key (a
    verdict SES chose not to run, e.g. virus scanning disabled on some
    accounts) must default to an empty string rather than raise.
    """

    def _status(key):
        return (receipt.get(key) or {}).get("status", "")

    return {
        "spf": _status("spfVerdict"),
        "dkim": _status("dkimVerdict"),
        "dmarc": _status("dmarcVerdict"),
        "spam": _status("spamVerdict"),
        "virus": _status("virusVerdict"),
    }


def _process_attachments(msg, message_id):
    """Store every attachment to S3 and mint a presigned download URL.

    Each attachment is handled independently: a failure decoding, storing,
    or presigning ONE attachment is logged and skipped, never allowed to
    sink the rest of the message. Stops accepting new attachments once
    either cap is hit (count or cumulative bytes) and logs a single warning
    noting how many were dropped — the caller still gets whatever attachments
    did fit.
    """
    attachments = []
    total_bytes = 0
    skipped = 0

    for att in msg.iter_attachments():
        if len(attachments) >= _MAX_ATTACHMENTS:
            skipped += 1
            continue
        try:
            filename = att.get_filename() or "attachment"
            content_type = att.get_content_type()
            data = att.get_content()
            if isinstance(data, str):
                # text/* attachments decode to str under policy.default;
                # everything downstream (size, S3 Body) wants bytes.
                data = data.encode("utf-8")
            size = len(data)
            if total_bytes + size > _MAX_ATTACHMENTS_TOTAL_BYTES:
                skipped += 1
                continue

            key = f"inbound/{message_id}/{filename}"
            _s3().put_object(
                Bucket=os.environ["ATTACHMENTS_BUCKET"],
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            download_url = _s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": os.environ["ATTACHMENTS_BUCKET"], "Key": key},
                ExpiresIn=_PRESIGNED_URL_EXPIRY_SECONDS,
            )
            attachments.append(
                {
                    "filename": filename,
                    "content_type": content_type,
                    "size": size,
                    "download_url": download_url,
                }
            )
            total_bytes += size
        except Exception:  # noqa: BLE001 - one bad attachment must not sink the message
            logger.exception("ses-inbound: failed to process an attachment for message_id=%s", message_id)
            continue

    if skipped:
        logger.warning(
            "ses-inbound: skipped %d attachment(s) for message_id=%s (count/size cap reached)",
            skipped,
            message_id,
        )
    return attachments


def _build_payload(ses, raw_mime):
    """Parse the raw MIME + SES receipt metadata into the clean JSON payload
    the backend's ``SesInboundBody`` expects."""
    mail = ses["mail"]
    receipt = ses.get("receipt", {})
    message_id = mail["messageId"]

    msg = message_from_bytes(raw_mime, policy=policy.default)

    from_header = msg.get("From")
    from_ = str(from_header) if from_header is not None else ""
    subject_header = msg.get("Subject")
    subject = str(subject_header) if subject_header is not None else ""

    body = msg.get_body(preferencelist=("plain",))
    text = str(body.get_content()) if body is not None else ""

    headers = {str(k): str(v) for k, v in msg.items()}

    return {
        "message_id": message_id,
        # The envelope recipients from SES's own receipt event are the
        # authoritative recipient list — NOT the (spoofable, possibly
        # multi-value/BCC-blind) To header.
        "to": list(mail.get("destination") or []),
        "from": from_,
        "subject": subject,
        "text": text,
        "headers": headers,
        "verdicts": _extract_verdicts(receipt),
        "attachments": _process_attachments(msg, message_id),
    }


def _post_to_backend(payload):
    """POST the payload to BACKEND_INGEST_URL with the shared-secret header.

    Returns the HTTP status on any response the backend actually sent
    (including non-2xx — the caller does not raise on those; see the
    module docstring's FAILURE POSTURE). Lets a transport-level failure
    (no response received at all) propagate to the caller unchanged.
    """
    req = urllib.request.Request(
        os.environ["BACKEND_INGEST_URL"],
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Isol8-SES-Secret": _get_secret(),
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=_BACKEND_POST_TIMEOUT_SECONDS)
        status = getattr(resp, "status", None) or resp.getcode()
        logger.info("ses-inbound: backend responded %s for message_id=%s", status, payload["message_id"])
        return status
    except urllib.error.HTTPError as e:
        # The backend answered (just not happily) — never raise on this.
        logger.warning(
            "ses-inbound: backend returned non-2xx status %s for message_id=%s", e.code, payload["message_id"]
        )
        return e.code


def _process_record(ses):
    """Fetch + parse one SES record's raw mail and deliver it to the backend.

    Returns the backend's HTTP status (or None if the POST itself failed to
    complete — that exception propagates to the caller, see module docstring).
    """
    mail = ses["mail"]
    message_id = mail["messageId"]

    bucket = os.environ["RAW_MAIL_BUCKET"]
    prefix = os.environ.get("RAW_MAIL_PREFIX", "")
    raw_mime = _s3().get_object(Bucket=bucket, Key=prefix + message_id)["Body"].read()

    payload = _build_payload(ses, raw_mime)
    return _post_to_backend(payload)


def handler(event, context):
    """Entry point: an SES receipt-rule Lambda-action event.

    Each record is processed independently and any exception is logged with
    its ``messageId`` before propagating — S3/parse failures and backend
    transport failures are all "retry this" conditions (see module
    docstring), so they are deliberately NOT swallowed here. A non-2xx
    backend response is not an exception at all (``_post_to_backend``
    returns its status code), so the common case never raises.
    """
    processed = []
    for record in event.get("Records", []):
        ses = record["ses"]
        message_id = ses["mail"]["messageId"]
        try:
            status = _process_record(ses)
        except Exception:
            logger.exception("ses-inbound: failed to process record message_id=%s", message_id)
            raise
        processed.append({"message_id": message_id, "backend_status": status})

    return {"processed": len(processed), "records": processed}
