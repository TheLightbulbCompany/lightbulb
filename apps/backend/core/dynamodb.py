"""DynamoDB client singleton and helpers."""

import asyncio
import functools
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, TypeVar

import threading

import boto3
from boto3.dynamodb.transform import TransformationInjector

from core.config import settings

# boto3 builds a call's Key/Attr conditions with ONE ConditionExpressionBuilder per client, reset and refilled per call,
# so two threads querying through one shared resource at once can swap placeholders: "Invalid KeyConditionExpression:
# KeyConditionExpressions must only contain one condition per key" (dev walk 16: GET /messaging/rooms 500 while its
# per-room mention counts ran side by side; boto3 issue #1604). The build is pure string work, so one lock costs nothing.
# Patched on the class at import, before any resource registers the handler (resources are built lazily).
_inject_condition_expressions = TransformationInjector.inject_condition_expressions
_inject_lock = threading.Lock()


def _locked_inject(self, params, model, **kwargs):
    with _inject_lock:
        return _inject_condition_expressions(self, params, model, **kwargs)


TransformationInjector.inject_condition_expressions = _locked_inject

_table_prefix: str = getattr(settings, "DYNAMODB_TABLE_PREFIX", "")
_dynamodb_resource = None


def _get_resource():
    global _dynamodb_resource
    if _dynamodb_resource is None:
        kwargs = {}
        endpoint = getattr(settings, "DYNAMODB_ENDPOINT_URL", None)
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        _dynamodb_resource = boto3.resource(
            "dynamodb",
            region_name=getattr(settings, "AWS_REGION", "us-east-1"),
            **kwargs,
        )
    return _dynamodb_resource


def table_name(short_name: str) -> str:
    return f"{_table_prefix}{short_name}"


def get_table(short_name: str):
    return _get_resource().Table(table_name(short_name))


T = TypeVar("T")


#: A caller that wants to know what its calls cost sets a list here; each call made under it
#: appends (seconds waiting for a pool thread, seconds running). The pool is the process's
#: default one (6 threads on 2 vCPU), so a long wait means something else filled it.
call_stats: ContextVar[list[tuple[float, float]] | None] = ContextVar("dynamodb_call_stats", default=None)


async def run_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    call = functools.partial(func, **kwargs) if kwargs else func
    stats = call_stats.get()
    if stats is None:
        return await asyncio.to_thread(call, *args)
    queued = time.monotonic()

    def timed() -> T:
        started = time.monotonic()
        try:
            return call(*args)
        finally:
            stats.append((started - queued, time.monotonic() - started))

    return await asyncio.to_thread(timed)


def plain(value: Any) -> Any:
    """Convert DynamoDB ``Decimal`` values back to ordinary JSON numbers."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def ddb_safe(value: Any) -> Any:
    """Floats → ``Decimal`` so JSON from an API can be written to DynamoDB
    (boto3 refuses ``float``). The inverse of ``plain``."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [ddb_safe(v) for v in value]
    return value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
