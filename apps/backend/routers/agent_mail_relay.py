"""Authenticated outbound relay for provisioned agent-email replies.

A per-user OpenClaw container calls this endpoint to send a threaded email
reply on behalf of one of its agents. There is no Clerk JWT — the container
has no user session — so this is, like ``routers/hooks_proxy.py``, a router
that deliberately does NOT use ``Depends(get_current_user)``.

**Auth model.** The request carries two pieces of proof:

1. ``reply_token`` — an opaque, Fernet-encrypted context (owner_id, agent_id,
   reply_to, in_reply_to, subject) minted by the inbound-mail handler
   (``agent_mail_token.encode_reply_token``) when the original email was
   delivered to the agent. It is unforgeable, but decoding it only tells us
   *who the caller claims to be replying as* — it is not, by itself, proof
   the caller is entitled to act as that owner.
2. ``Authorization: Bearer {hook_token}`` — the SAME per-owner token used by
   ``hooks_proxy.py``, compared timing-safely against ``containers.hook_token``
   for the owner NAMED INSIDE THE TOKEN (not a token the caller can supply).

The load-bearing check is step 3 below: the caller must hold the hook_token
of the owner named in the reply_token. Only that owner's own container process
holds that hook_token, so a container can only ever send replies for its own
owner — it cannot use a reply_token skimmed from another owner's traffic to
impersonate them, because it would still fail the hook_token compare.

**Rate limit.** In-process sliding-window bucket, copied from
``hooks_proxy._check_rate_limit`` with its own independent buckets (not
shared with the webhook-ingress proxy).
"""

import hmac
import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.repositories import container_repo
from core.services.agent_mail_reply import send_reply
from core.services.agent_mail_token import decode_reply_token

logger = logging.getLogger(__name__)
router = APIRouter()

RATE_LIMIT_PER_MINUTE = 60

# Module-level so the bucket survives across requests inside one process.
# Independent from hooks_proxy's own bucket by design — these are different
# call surfaces with different callers.
_RATE_BUCKETS_PER_OWNER: dict[str, deque[float]] = defaultdict(deque)


class ReplyRequest(BaseModel):
    reply_token: str
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


@router.post("/reply")
async def reply(body: ReplyRequest, request: Request) -> dict:
    """Send a threaded email reply on behalf of the calling container's owner.

    Bearer-token authenticated against ``containers.hook_token`` (timing-safe
    compare) for the owner named inside the (unforgeable) ``reply_token``.
    Returns ``{"status": ...}`` — "sent"/"no_address"/"disabled"/"error" from
    ``send_reply`` on success, or raises 400/401/429 on auth/validation
    failures.
    """
    # 1. Bearer extract
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    presented = auth[len("Bearer ") :]

    # 2. Decode the reply context. An unforgeable token that won't decode is
    # a bad request, not an auth failure — 400, not 401.
    ctx = decode_reply_token(body.reply_token)
    if ctx is None:
        raise HTTPException(status_code=400, detail={"status": "bad_token"})

    # 3. Timing-safe compare against the NAMED owner's hook_token. This is
    # the load-bearing check — see module docstring.
    container = await container_repo.get_by_owner_id(ctx["owner_id"])
    expected = container.get("hook_token") if container else ""
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail={"status": "invalid_token"})

    # 4. Rate limit (after auth so 401s don't fill the bucket)
    if not _check_rate_limit(ctx["owner_id"]):
        raise HTTPException(status_code=429, detail={"status": "rate_limited"})

    # 5. Send. `send_reply` never raises — it maps its own failures to a
    # result string ("no_address"/"disabled"/"error").
    result = await send_reply(ctx=ctx, body_text=body.body_text, body_html=body.body_html)
    return {"status": result}
