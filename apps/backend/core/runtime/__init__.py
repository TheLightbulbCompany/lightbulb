"""Runtime registry — the ONLY place that maps a runtime kind to an adapter."""

import logging
from functools import lru_cache

from core.runtime.base import Runtime

logger = logging.getLogger(__name__)

# Process-local owner_id -> runtime_kind cache. ``runtime_kind`` is set ONCE at
# provision and is immutable for the life of a container, so this needs no
# invalidation — a warm entry can never go stale. It stays a plain sync dict so
# ``get_runtime_for_owner`` reads it without ``await``, keeping every call site
# synchronous. ``prime_runtime_kind`` populates it wherever the container row is
# already in hand (RPC resolution, provisioning). A miss resolves to the safe
# "openclaw" default, so the sync path never depends on the cache being warm.
_RUNTIME_KIND_CACHE: dict[str, str] = {}


@lru_cache(maxsize=None)
def get_runtime(kind: str) -> Runtime:
    if kind == "openclaw":
        from core.runtime.openclaw.adapter import OpenClawRuntime

        return OpenClawRuntime()
    raise ValueError(f"unknown runtime kind: {kind!r}")


def get_runtime_for_owner(owner_id: str) -> Runtime:
    """Resolve the runtime for an owner — SYNC and INFALLIBLE.

    Reads the owner's ``runtime_kind`` from the process-local cache (primed by
    ``prime_runtime_kind`` off an already-fetched container row). A cache miss
    resolves to OpenClaw — correct for every owner today, since nothing writes
    a non-OpenClaw ``runtime_kind`` yet.

    Fail-safe by contract: ANY failure to resolve a concrete runtime (an
    unknown kind cached, an adapter not yet registered in this process, …)
    falls back to OpenClaw with a logged warning and NEVER raises. Callers
    invoke this synchronously and then await a method on the result; keeping it
    sync avoids forcing ``await`` onto every call site, and keeping it
    non-raising protects ``container_auth.revoke_provider_auth_in_container``'s
    never-raise contract.
    """
    kind = _RUNTIME_KIND_CACHE.get(owner_id, "openclaw")
    try:
        return get_runtime(kind)
    except Exception as exc:  # noqa: BLE001 — resolution must never raise.
        logger.warning(
            "runtime resolution fell back to openclaw for owner=%s kind=%r: %s",
            owner_id,
            kind,
            exc,
        )
        return get_runtime("openclaw")


async def prime_runtime_kind(owner_id: str, container_row: dict | None = None) -> str:
    """Populate the process-local runtime-kind cache for ``owner_id``.

    Call where the owner's container row is ALREADY fetched (pass it as
    ``container_row`` to skip a redundant DDB read); otherwise this reads the
    row itself. Because ``runtime_kind`` is immutable, priming once keeps the
    sync ``get_runtime_for_owner`` correct for the rest of the process's life
    with no invalidation.

    Fail-safe: any error (missing row, DDB failure) caches — and returns — the
    "openclaw" default and NEVER raises, so callers can prime unconditionally
    on the hot path. Returns the resolved kind (for logging/tests).
    """
    try:
        row = container_row
        if row is None:
            # Lazy import keeps this module import-light: the sync registry must
            # load without any repository/AWS plumbing (the subprocess tripwire
            # in test_registry enforces that core.containers never leaks in).
            from core.repositories import container_repo

            row = await container_repo.get_by_owner_id(owner_id)
        kind = (row or {}).get("runtime_kind") or "openclaw"
    except Exception as exc:  # noqa: BLE001 — priming must never raise.
        logger.warning(
            "prime_runtime_kind: resolve failed for owner=%s; caching openclaw: %s",
            owner_id,
            exc,
        )
        kind = "openclaw"
    _RUNTIME_KIND_CACHE[owner_id] = kind
    return kind
