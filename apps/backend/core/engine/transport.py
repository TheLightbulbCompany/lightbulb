"""The engine's HTTP wire, both directions (`settings.ENGINE_TRANSPORT == "http"`): products to the engine service
(`core.engine.client` -> `routers/engine_api.py`) and the engine back to the products (`core.engine.http_hooks` ->
`routers/engine_callbacks.py`).

Each side proves itself with `Authorization: Bearer <ENGINE_SERVICE_TOKEN>`, compared in constant time; an unset
token refuses every call. A route answers with the value as JSON, or with the refusal the in-process call raises,
marked by a header so the caller raises that same exception again: an `EngineError` (status, `{"detail": ...}`), an
`HTTPException` raised inside the call (a product tool's refusal), an `ApiProblem` (its own body), or a lost
conditional write (botocore's `TransactionCanceledException`, which a caller's read-merge-write loop retries).
"""

from __future__ import annotations

import hmac
from typing import Any

import httpx
from botocore.exceptions import ClientError
from fastapi import Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from core.config import settings
from core.dynamodb import is_cancelled
from core.engine.contract import EngineError

ENGINE_ERROR = "X-Engine-Error"  # an EngineError: the value is its code
RELAYED = "X-Engine-Relayed"  # an HTTPException raised inside the call
PROBLEM = "X-Engine-Problem"  # an ApiProblem
CANCELLED = "X-Engine-Cancelled"  # a conditional write lost to a writer since the caller's read
_MARKS = (ENGINE_ERROR, RELAYED, PROBLEM, CANCELLED)

#: One attempt's limit for a product's call to the engine: under the huddle worker's own 10 s voice-route timeout
#: (apps/huddle-agent `ROUTE_TIMEOUT_S`), so a slow engine fails the route rather than the worker giving up first.
ENGINE_TIMEOUT_S = 8.0
#: One attempt's limit for the engine's call to a product (a tool, a context answer, an event): sized for a product
#: tool that reaches an outside service, under every Temporal activity's own timeout.
HOOK_TIMEOUT_S = 30.0
_RETRY_STATUS = frozenset({502, 503, 504})  # a gateway's or a dead task's answer, never the call's own refusal

_clients: dict[str, httpx.AsyncClient] = {}


def require_token(authorization: str = Header("")) -> None:
    """The route dependency both sides use."""
    want = settings.ENGINE_SERVICE_TOKEN
    if not want or not hmac.compare_digest(authorization.encode(), f"Bearer {want}".encode()):
        raise HTTPException(status_code=401, detail="invalid_engine_token")


def _plain(value: Any) -> Any:
    """Contract models as their camelCase dicts, Python values kept (a row's Decimal stays a number, not a string)."""
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def dump(value: Any) -> Any:
    """`value` as JSON-ready data: a Decimal becomes an int or float, a set a list."""
    return jsonable_encoder(_plain(value))


async def reply(call, status: int = 200) -> Response:
    """A route's answer for the awaitable `call`: its value, or its refusal marked for `result`."""
    from core.services.messaging import ApiProblem

    try:
        value = await call
    except EngineError as e:
        return JSONResponse({"detail": dump(e.detail)}, e.status, headers={ENGINE_ERROR: e.code})
    except HTTPException as e:
        return JSONResponse({"detail": dump(e.detail)}, e.status_code, headers={RELAYED: "1"})
    except ApiProblem as e:
        return JSONResponse(e.body(), e.status_code, headers={PROBLEM: "1"})
    except ClientError as e:
        if not is_cancelled(e):
            raise
        return JSONResponse({"detail": {"code": "cancelled"}}, 409, headers={CANCELLED: "1"})
    if status == 204:
        return Response(status_code=204)
    return JSONResponse(dump(value), status)


def result(r: httpx.Response) -> Any:
    """The value a route sent, or the exception the in-process call raises."""
    if code := r.headers.get(ENGINE_ERROR):
        raise EngineError(code, r.status_code, r.json()["detail"])
    if r.headers.get(RELAYED):
        raise HTTPException(status_code=r.status_code, detail=r.json()["detail"])
    if r.headers.get(PROBLEM):
        from core.services.messaging import ApiError, ApiProblem

        err = ApiError.model_validate(r.json())
        raise ApiProblem(
            err.code,
            err.message,
            retryable=err.retryable,
            reconcile_from_seq=err.reconcile_from_seq,
            retry_after=err.retry_after,
            details=err.details,
        )
    if r.headers.get(CANCELLED):
        error = {"Code": "TransactionCanceledException", "Message": "lost to a writer since the read"}
        raise ClientError({"Error": error}, "TransactWriteItems")
    r.raise_for_status()
    return r.json() if r.content else None


async def send(
    base: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: Any = None,
    timeout: float = ENGINE_TIMEOUT_S,
    retry_status: bool = True,
) -> Any:
    """One call to the other side at `base`, retried once with the same body on a connect error (nothing was sent)
    and, with `retry_status`, on an unmarked 502/503/504 (the call is idempotent by its key, so a retry replays)."""
    if not base:
        raise RuntimeError("the engine's HTTP transport has no URL for the other side")
    client = _clients.get(base)
    if client is None:
        client = _clients[base] = httpx.AsyncClient(base_url=base)
    headers = {"Authorization": f"Bearer {settings.ENGINE_SERVICE_TOKEN}"}
    params = {k: v for k, v in (params or {}).items() if v is not None}
    for attempt in (1, 2):
        try:
            r = await client.request(method, path, params=params, json=json, headers=headers, timeout=timeout)
        except httpx.ConnectError:
            if attempt == 2:
                raise
            continue
        if attempt == 1 and retry_status and r.status_code in _RETRY_STATUS and not any(m in r.headers for m in _MARKS):
            continue
        return result(r)
    raise AssertionError("unreachable")
