"""Agent-initiated owner email.

A per-user OpenClaw container calls this to email its OWN owner unprompted —
Friday's first contact today, seeded nightly routines later. There is no Clerk
JWT (the container has no user session), so like ``routers/hooks_proxy.py``
and ``routers/agent_mail_relay.py`` this router deliberately does NOT use
``Depends(get_current_user)``.

**Auth model.** The owner is NAMED in the path and the presented bearer must
timing-safe-match THAT owner's ``containers.hook_token``. Only that owner's own
container process holds that token, so a container can only ever send for its
own owner.

Unlike the reply relay there is no ``reply_token``, and there is deliberately
**no recipient parameter**: the recipient is the owner's own address, resolved
server-side from ``owner_id``. Nothing a caller supplies can redirect the
message, so there is no credential to mint, expire, or leak into a prompt.

**Limits.** A per-minute in-process bucket (burst smoothing, per ECS task) plus
the DURABLE per-owner daily cap enforced in
``container_repo.try_consume_daily_email_send`` — the latter is the real fence
against an agent stuck in a loop.
"""

import hmac
import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.repositories import container_repo
from core.services.agent_mail_owner_send import VALID_PURPOSES, send_to_owner

logger = logging.getLogger(__name__)
router = APIRouter()

# Burst smoothing ONLY. This bucket is per-process and several backend tasks
# run behind the ALB, so it is not a real quota — the durable daily cap is.
RATE_LIMIT_PER_MINUTE = 60

# Module-level so the bucket survives across requests inside one process.
# Independent from the relay's and hooks_proxy's buckets by design — different
# call surfaces, different callers.
_RATE_BUCKETS_PER_OWNER: dict[str, deque[float]] = defaultdict(deque)


class OwnerSendRequest(BaseModel):
    purpose: str
    subject: str
    body_text: str
    body_html: str | None = None


def _check_rate_limit(owner_id: str) -> bool:
    """In-process sliding-window token bucket. True = allowed."""
    now = time.monotonic()
    cutoff = now - 60.0
    q = _RATE_BUCKETS_PER_OWNER[owner_id]
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


@router.post("/send-to-owner/u/{owner_id}")
async def send_owner_email(owner_id: str, body: OwnerSendRequest, request: Request) -> dict:
    """Send an email from this container's agent to this container's owner.

    Returns ``{"status": ...}`` — "sent"/"already_sent"/"no_address"/
    "disabled"/"rate_limited"/"error" from ``send_to_owner`` — or raises
    400/401/429 on validation/auth failures.
    """
    # 1. Bearer extract
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    presented = auth[len("Bearer ") :]

    # 2. Timing-safe compare against the NAMED owner's hook_token. This is the
    # load-bearing check — see module docstring.
    container = await container_repo.get_by_owner_id(owner_id)
    expected = container.get("hook_token") if container else ""
    # Compare bytes, not str: hmac.compare_digest raises TypeError on `str`
    # arguments containing non-ASCII characters, and `presented` comes straight
    # from the raw Authorization header. An unhandled TypeError would surface
    # as a 500 instead of the intended 401.
    if not expected or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail={"status": "invalid_token"})

    # 3. Validate `purpose` to bound metric cardinality — NOT for safety.
    # Policy never reads it; once-only comes from first_contact_state.
    if body.purpose not in VALID_PURPOSES:
        raise HTTPException(status_code=400, detail={"status": "bad_purpose"})

    # 4. Rate limit (after auth so 401s don't fill the bucket)
    if not _check_rate_limit(owner_id):
        raise HTTPException(status_code=429, detail={"status": "rate_limited"})

    # 5. Send. `send_to_owner` never raises — it maps its own failures to a
    # result string.
    result = await send_to_owner(
        owner_id=owner_id,
        purpose=body.purpose,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
    )
    return {"status": result}
