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
    mail — they are authoritative, not best-effort), plus the two identities
    those flat verdicts do not carry, as FIRST-CLASS verdict fields:
    ``dkim_domains`` (the ``d=`` tags of the message's DKIM signatures, and
    only when SES itself reported ``dkimVerdict == PASS``) and ``spf_domain``
    (the domain of SES's recorded envelope sender). Neither is read out of a
    trustworthy-looking header: an attacker with a valid signature for their
    own domain can also paste a forged ``Authentication-Results`` into the
    message, so the backend's wake gate must never parse one,
  - stores each attachment to S3 and mints a 1h presigned GET URL,
  - and POSTs a clean JSON payload to the backend's ``/webhooks/ses-inbound``
    ingest endpoint, authenticated with a shared secret (``X-Lightbulb-SES-Secret``,
    fetched from Secrets Manager and cached across warm invocations).

The backend, not this Lambda, makes every content decision (recipient
resolution, sender allowlist, the free-tier cost cap, spam/virus/DMARC
drop, the auto-submitted loop guard) — see ``routers/ses_inbound.py``'s
module docstring. This Lambda's job is purely "parse safely and deliver a
clean payload"; it does not drop mail based on content.

FAILURE POSTURE. Four exhaustive outcomes for the backend POST:
  - a 2xx or 4xx HTTP response was received (a real ``HTTPError`` for the
    4xx case) -> logged, NOT raised. The backend already answered; retrying
    the same payload would not change a deterministic 4xx (bad secret,
    malformed payload), and a 2xx means the backend accepted the delivery.
  - a 5xx HTTP response was received (also a real ``HTTPError``) ->
    RE-RAISED. The backend uses 5xx to mean "transient failure, please
    retry" (as opposed to a 4xx, which is terminal), so this propagates out
    of the handler and SES/Lambda's own retry policy re-invokes.
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
delivery to the agent — this holds whether the retry was triggered by a
transport failure or by a backend 5xx.

Per-attachment failures are handled separately and do NOT propagate: one
malformed/oversized attachment part must not sink an otherwise-good email
delivery. Attachments are capped at ``_MAX_ATTACHMENTS`` count and
``_MAX_ATTACHMENTS_TOTAL_BYTES`` combined size; anything beyond the cap is
simply skipped (logged), not treated as an error.
"""

import json
import logging
import os
import re
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

# Presigned attachment URLs are valid for 1h. A 24h TTL is illusory: the
# Lambda execution role's temporary credentials that sign the URL typically
# expire well before 24h, so a long-dated URL just 403s early anyway. 1h is
# safely within any single Lambda invocation's credential window and is
# honest about how long the URL actually works.
_PRESIGNED_URL_EXPIRY_SECONDS = 60 * 60

_BACKEND_POST_TIMEOUT_SECONDS = 10

# Cold-start caches: the shared secret and the boto3 clients are
# fetched/built once per Lambda container and reused across invocations.
_SECRET_CACHE = {}
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


def _get_secret(arn=None):
    """Return the shared ``X-Lightbulb-SES-Secret`` value for ``arn``, fetched on
    first use and cached for the life of the container.

    Cached PER ARN: one Lambda serves both environments (see ``_route_for``),
    and each backend verifies against its own secret, so a single-slot cache
    would present prod's secret to dev and get a 401.
    """
    if arn is None:
        arn = os.environ["SES_INBOUND_SECRET_ARN"]
    if arn not in _SECRET_CACHE:
        _SECRET_CACHE[arn] = _secretsmanager().get_secret_value(SecretId=arn)["SecretString"]
    return _SECRET_CACHE[arn]


# A `DKIM-Signature` value is a `tag=value; …` list; the signing domain is its
# `d=` tag. Anchored on the start of a tag (string start or a `;`) so a stray
# "d=" inside another tag's value — the base64 in `b=`/`bh=` — cannot match.
_DKIM_D_TAG_RE = re.compile(r"(?:^|;)\s*d\s*=\s*([^;\s]+)", re.IGNORECASE)


def _dkim_signer_domains(msg, dkim_status):
    """The `d=` signing domains of this message's own DKIM signatures.

    Returned ONLY when SES reported ``dkimVerdict == PASS``: SES cryptograph-
    ically verified a signature on this message, and a signature's `d=` tag is
    itself covered by that signature, so it cannot be rewritten without
    breaking it. That is why this is read off `DKIM-Signature` and NOT off
    `Authentication-Results` — the latter is a plain header any sender can
    paste (see the module docstring).

    CONSERVATIVE, NOT PERFECT. SES's flat PASS says *at least one* signature
    verified, not which one, so every signature's domain is forwarded and the
    backend accepts alignment against any of them. An attacker who can get one
    genuinely valid signature past SES could therefore attach a second,
    unverified signature naming a domain they do not own. DMARC=PASS is the
    strong path; this fallback exists only for senders SES gives no DMARC
    verdict for, and it gates WAKING the agent, never storing the mail.
    """
    if str(dkim_status or "").strip().upper() != "PASS":
        return []
    domains = []
    for raw in msg.get_all("DKIM-Signature") or []:
        match = _DKIM_D_TAG_RE.search(str(raw))
        if not match:
            continue
        domain = match.group(1).strip().strip(".").lower()
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def _envelope_sender_domain(mail):
    """Domain of SES's recorded envelope sender — the identity SPF authenticated.

    ``mail["source"]`` is the SMTP MAIL FROM from SES's own receipt event. The
    MIME's `Return-Path` is deliberately NOT used: that is whatever the sender
    typed.
    """
    source = str(mail.get("source") or "").strip().strip("<>").strip().lower()
    return source.rsplit("@", 1)[-1].strip().strip(".") if source else ""


def _extract_verdicts(receipt, msg, mail):
    """Map SES's per-check verdict objects to a flat {name: status} dict, plus
    the two authenticated identities those flat verdicts do not carry.

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
        # First-class identities for the backend's wake gate — derived from
        # SES's receipt + the signed `DKIM-Signature`, never from a header
        # the sender controls. See the two helpers above.
        "dkim_domains": _dkim_signer_domains(msg, _status("dkimVerdict")),
        "spf_domain": _envelope_sender_domain(mail),
    }


def _process_attachments(msg, message_id):
    """Store every attachment to S3 and mint a presigned download URL.

    Each attachment is handled independently: a failure decoding, storing,
    or presigning ONE attachment is logged and skipped, never allowed to
    sink the rest of the message. Stops accepting new attachments once
    either cap is hit (count or cumulative bytes) and logs a single warning
    noting how many were dropped — the caller still gets whatever attachments
    did fit. The size cap is enforced from the part's still-encoded payload
    length BEFORE ``get_content()`` decodes it into memory, so a single
    oversized part can never be fully decoded just to find out it should be
    dropped.
    """
    attachments = []
    total_bytes = 0
    skipped = 0

    for i, att in enumerate(msg.iter_attachments()):
        if len(attachments) >= _MAX_ATTACHMENTS:
            skipped += 1
            continue
        try:
            filename = att.get_filename() or "attachment"
            content_type = att.get_content_type()

            # Estimate the decoded size BEFORE calling get_content() — which
            # fully base64-decodes the part into memory — so a crafted huge
            # attachment is never decoded just to discover it blows the cap.
            # Base64 expands the decoded bytes by ~4/3, so 3/4 of the
            # encoded (raw, undecoded) payload length is a conservative
            # upper bound on the decoded size.
            raw_len = len(att.get_payload(decode=False) or "")
            estimated_decoded_size = int(raw_len * 3 / 4)
            if total_bytes + estimated_decoded_size > _MAX_ATTACHMENTS_TOTAL_BYTES:
                skipped += 1
                continue

            data = att.get_content()
            if isinstance(data, str):
                # text/* attachments decode to str under policy.default;
                # everything downstream (size, S3 Body) wants bytes.
                data = data.encode("utf-8")
            size = len(data)

            # The index is folded into the key (not just the filename) so
            # two parts sharing a filename (or both defaulting to
            # "attachment") never collide on the same S3 object — the
            # manifest's `filename` field below keeps the original name.
            key = f"inbound/{message_id}/{i}-{filename}"
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

    # A calendar invite rides as a text/calendar sibling of text/plain; the
    # backend parses it. First one wins, capped so a huge part can't bloat the POST.
    calendar = next((p.get_content()[:65536] for p in msg.walk() if p.get_content_type() == "text/calendar"), None)

    # Headers are forwarded verbatim for the inbox/threading, and are NOT an
    # authentication source: the backend's wake gate reads the identity fields
    # in `verdicts` below instead (see `_extract_verdicts`).
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
        "verdicts": _extract_verdicts(receipt, msg, mail),
        "attachments": _process_attachments(msg, message_id),
        "calendar": calendar,
    }


def _route_for(recipients):
    """Pick ``(backend_url, secret_arn)`` for this message's recipients.

    ONE receipt rule serves both environments. SES matches a recipient
    condition against the domain **and its subdomains** (see the SES
    "email receiving concepts" guide), so a rule for ``isol8.co`` already
    catches ``friday@acme.dev.isol8.co`` as well as
    ``friday@acme.isol8.co``. A second rule set is impossible anyway —
    ``SetActiveReceiptRuleSet`` is account+region scoped and prod owns it —
    so this function is what separates the environments.

    Matching is on a dotted suffix (``.dev.isol8.co``), never a bare
    ``endswith`` of the suffix itself: ``acme.notdev.isol8.co`` must not be
    mistaken for a dev address.

    ``DEV_MAIL_SUFFIX`` is a COMMA-SEPARATED list, because a mail-domain move
    leaves two live dev suffixes at once (``dev.thelightbulb.company`` and the
    ``dev.isol8.co`` addresses already minted into DynamoDB). A single value
    would hand every legacy dev address to the PROD backend, which has never
    heard of it. One value is still just a list of one.

    INERT until dev is configured: with no ``DEV_*`` environment, every
    message routes to prod exactly as before.

    Mixed-domain envelopes (a dev and a prod recipient on one message) route
    by the FIRST recipient. That combination cannot arise from either
    product flow — an owner-directed address is single-recipient — so it is
    only reachable by a hand-crafted envelope, and picking deterministically
    beats splitting one message across two backends.
    """
    prod = (os.environ["BACKEND_INGEST_URL"], os.environ["SES_INBOUND_SECRET_ARN"])
    dev_suffixes = [
        s.strip().lower()
        for s in (os.environ.get("DEV_MAIL_SUFFIX") or "").split(",")
        if s.strip()
    ]
    dev_url = (os.environ.get("DEV_BACKEND_INGEST_URL") or "").strip()
    dev_arn = (os.environ.get("DEV_SES_INBOUND_SECRET_ARN") or "").strip()
    if not (dev_suffixes and dev_url and dev_arn):
        return prod

    for addr in recipients or []:
        domain = str(addr).rsplit("@", 1)[-1].strip().lower()
        if any(domain == s or domain.endswith("." + s) for s in dev_suffixes):
            return (dev_url, dev_arn)
        break  # first recipient decides — see docstring
    return prod


def _post_to_backend(payload):
    """POST the payload to the environment's ingest URL with its shared secret.

    Returns the HTTP status for a 2xx or 4xx response — those are terminal;
    the caller does not raise on them (see the module docstring's FAILURE
    POSTURE). A 5xx response means the backend is signalling a transient
    failure, so it is RE-RAISED (as the ``HTTPError`` it already is) rather
    than returned, and propagates out to the caller so SES retries. A
    transport-level failure (no response received at all) also propagates
    unchanged.
    """
    backend_url, secret_arn = _route_for(payload.get("to"))
    req = urllib.request.Request(
        backend_url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Lightbulb-SES-Secret": _get_secret(secret_arn),
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=_BACKEND_POST_TIMEOUT_SECONDS)
        status = getattr(resp, "status", None) or resp.getcode()
        logger.info("ses-inbound: backend responded %s for message_id=%s", status, payload["message_id"])
        return status
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600:
            # The backend is telling us this failure is transient — let it
            # propagate so SES/Lambda's retry policy re-invokes. The
            # backend dedups on message_id, so a retry that eventually
            # succeeds does not double-deliver.
            logger.warning(
                "ses-inbound: backend returned 5xx status %s for message_id=%s; re-raising for retry",
                e.code,
                payload["message_id"],
            )
            raise
        # A 4xx is terminal — the backend answered (just not happily), and
        # retrying a deterministic 4xx (bad secret, malformed payload)
        # would not change the outcome, only retry-storm the Lambda.
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
    its ``messageId`` before propagating — S3/parse failures, backend
    transport failures, and backend 5xx responses are all "retry this"
    conditions (see module docstring), so they are deliberately NOT
    swallowed here. A 2xx/4xx backend response is not an exception at all
    (``_post_to_backend`` returns its status code), so the common case
    never raises.
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
