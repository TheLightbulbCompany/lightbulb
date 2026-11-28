"""Spark's autocomplete, two routes under /api/v1/os, answered whole (API Gateway HTTP API cannot stream):

- `POST /assist/complete`: the context of a typing pause (or of Tab Tab) in, one suggestion out.
- `POST /assist/reach`: what the sentence being written is about to need, as up to 3 search phrases the Mac runs
  against its own memory and sends back with the next suggestion as `reaches_for`.

Shapes are in `core.services.autocomplete`; the Mac-side contract is the Mac repo's
`research/2026-11-28-autocomplete-route-contract.md`.

Order: the environment switch (404 when off, before anything else, so the routes look absent), the Clerk session,
the method (405), the body (413 over the size, 415 for an encoding other than gzip, 400 for a bad body), the provider
key (503), the voice rule for who may use Spark (402), the daily ceiling (429, one counter for both routes), then the
model (502 when it fails).

Never logged or stored: any text of a request or a reply. One log line per answered request carries counts, sizes,
timings and the model id."""

import json
import logging
import time
import zlib
from typing import TypeVar

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError

from core.auth import AuthContext, get_human_session
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services import autocomplete as ac
from core.services import os_billing
from routers import os as os_router

logger = logging.getLogger(__name__)
router = APIRouter()
M = TypeVar("M", bound=BaseModel)

# The largest body the 9.12 caps allow is about 450,000 chars, up to 6 bytes each as JSON escapes. Over this (sent,
# or after gunzip) is 413.
BODY_MAX_BYTES = 4_000_000


def _switch() -> None:
    if not settings.SPARK_AUTOCOMPLETE_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")  # Starlette's own 404 for an unknown path


async def _body(request: Request, model: type[M]) -> tuple[M, int]:
    """The validated request and its size on the wire. Read as a stream, so the cap holds whatever the header says."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > BODY_MAX_BYTES:
        raise HTTPException(status_code=413, detail="too_large")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="too_large")
    size = len(raw)
    encoding = request.headers.get("content-encoding", "identity").strip().lower()
    if encoding == "gzip":
        unzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = unzip.decompress(raw, BODY_MAX_BYTES + 1)
        except zlib.error:
            raise HTTPException(status_code=400, detail="invalid_request") from None
        if len(raw) > BODY_MAX_BYTES or unzip.unconsumed_tail:
            raise HTTPException(status_code=413, detail="too_large")
        if not unzip.eof:
            raise HTTPException(status_code=400, detail="invalid_request")
    elif encoding != "identity":
        raise HTTPException(status_code=415, detail="unsupported_encoding")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
        json.dumps(body, ensure_ascii=False).encode()  # a lone surrogate (\ud800) would fail later, at the provider
        return model.model_validate(body), size
    except (ValueError, RecursionError, ValidationError):  # UnicodeEncodeError is a ValueError
        # never echo or log the error: pydantic's message quotes the input
        raise HTTPException(status_code=400, detail="invalid_request") from None


async def _within_daily_max(user_id: str, now: int, chars: int) -> bool:
    """Counts this request and its prompt's characters on the person's users row (`os_ac_day`, `os_ac_n`, `os_ac_c`;
    deleting the account removes them, as it removes every `os_*`). False once today's requests reached
    SPARK_AUTOCOMPLETE_DAILY_MAX or this prompt would pass SPARK_AUTOCOMPLETE_DAILY_CHARS. Shared by every task."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    table = get_table("users")
    cap_n, cap_c = settings.SPARK_AUTOCOMPLETE_DAILY_MAX, settings.SPARK_AUTOCOMPLETE_DAILY_CHARS
    if cap_n < 1 or chars > cap_c:
        return False
    values = {":d": day, ":one": 1, ":c": chars}
    for _ in range(2):  # a request that saw yesterday's day while another reset it reads again
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": user_id},
                UpdateExpression="ADD os_ac_n :one, os_ac_c :c",
                ConditionExpression="os_ac_day = :d AND os_ac_n < :max AND (attribute_not_exists(os_ac_c) OR os_ac_c <= :room)",
                ExpressionAttributeValues=values | {":max": cap_n, ":room": cap_c - chars},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": user_id},
                UpdateExpression="SET os_ac_day = :d, os_ac_n = :one, os_ac_c = :c",
                # `<`, not `<>`: a request still on yesterday's date never resets today's count (ISO dates sort)
                ConditionExpression="attribute_not_exists(os_ac_day) OR os_ac_day < :d",
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    return False


# Every method, so that off, each is the same 404 (a CORS preflight OPTIONS is answered by the middleware first).
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]


async def _gate(request: Request, auth: AuthContext, model: type[M]) -> tuple[M, int, int]:
    """Everything both routes check before any model call: the method, the body, the key, the voice rule. Returns
    the request, its size on the wire and the clock."""
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    req, size = await _body(request, model)
    if not ac.provider.configured():
        raise HTTPException(status_code=503, detail="autocomplete_unavailable")
    now = os_router._now()
    row = await os_router._account(auth, now)  # the voice session's rule: a trial not started yet counts as running
    if os_billing.status(row, now) == "ended":
        os_billing.trial_ended_event(row, now)
        raise HTTPException(status_code=402, detail="trial_ended")
    # Using autocomplete starts the trial clock, as a first voice conversation does; otherwise an account that never
    # talks would be "not started", and so in trial, forever. A no-op once started.
    await os_router._start_trial(auth.user_id, row, now)
    return req, size, now


async def _ask(user_id: str, now: int, developer: str, user: str, kind: ac.Kind) -> ac.Completion:
    """The daily ceiling (one counter for both routes), then the one model call."""
    if not await _within_daily_max(user_id, now, len(developer) + len(user)):
        logger.info(
            "autocomplete refused: daily max %d requests / %d chars",
            settings.SPARK_AUTOCOMPLETE_DAILY_MAX,
            settings.SPARK_AUTOCOMPLETE_DAILY_CHARS,
        )
        raise HTTPException(status_code=429, detail="daily_limit")
    try:
        return await ac.provider.complete(developer, user, kind.max_tokens, ac.cache_key(user_id), kind.timeout_s)
    except ac.ProviderError as e:
        logger.warning("autocomplete upstream failed: %s model=%s", e, settings.SPARK_AUTOCOMPLETE_MODEL)
        raise HTTPException(status_code=502, detail="upstream_failed") from None


def _log(route: str, comp: ac.Completion, size: int, prompt_chars: int, started: float, asked: float, **counts) -> None:
    """The one log line per answered request: counts, sizes, timings and the model id, never text."""
    done = time.monotonic()
    logger.info(
        "autocomplete %s %s body_bytes=%d prompt_chars=%d in_tokens=%d cached_tokens=%d out_tokens=%d logprobs=%s "
        "model_ms=%d total_ms=%d model=%s",
        route,
        " ".join(f"{k}={v}" for k, v in counts.items()),
        size,
        prompt_chars,
        comp.input_tokens,
        comp.cached_tokens,
        comp.output_tokens,
        comp.token_logprobs is not None,
        (done - asked) * 1000,
        (done - started) * 1000,
        settings.SPARK_AUTOCOMPLETE_MODEL,
    )


@router.api_route("/assist/complete", methods=_METHODS, dependencies=[Depends(_switch)], include_in_schema=False)
async def complete(request: Request, auth: AuthContext = Depends(get_human_session)) -> dict:
    """One suggestion for the pause the Mac describes. `_switch` runs before the session check."""
    started = time.monotonic()
    req, size, now = await _gate(request, auth, ac.CompleteRequest)
    kind = ac.kind_of(req)
    if kind.name == "cont" and not (req.before or "").strip():
        return ac.quiet("no_draft", None)  # nothing to finish: no model call, not counted
    developer, user = ac.messages(req, kind)
    asked = time.monotonic()
    comp = await _ask(auth.user_id, now, developer, user, kind)
    answer = ac.reply(req, kind, comp)
    _log(
        "complete",
        comp,
        size,
        len(developer) + len(user),
        started,
        asked,
        kind=kind.name,
        versions=len(answer.get("option", [])),
        shown=answer["show"],
        reason=answer.get("reason"),
    )
    return answer


@router.api_route("/assist/reach", methods=_METHODS, dependencies=[Depends(_switch)], include_in_schema=False)
async def reach(request: Request, auth: AuthContext = Depends(get_human_session)) -> dict:
    """Up to 3 search phrases for what the sentence being written is about to need; the Mac runs them against its
    own memory and sends them back as `reaches_for`. An empty list when nothing is needed."""
    started = time.monotonic()
    req, size, now = await _gate(request, auth, ac.ReachRequest)
    if not (req.before or "").strip():
        return {"queries": []}  # no sentence yet: no model call, not counted
    developer, user = ac.reach_messages(req)
    asked = time.monotonic()
    comp = await _ask(auth.user_id, now, developer, user, ac.REACH)
    queries = ac.reach_queries(comp.text)
    _log("reach", comp, size, len(developer) + len(user), started, asked, queries=len(queries))
    return {"queries": queries}
