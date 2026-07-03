"""Runtime registry — the ONLY place that maps a runtime kind to an adapter."""

from functools import lru_cache

from core.runtime.base import Runtime


@lru_cache(maxsize=None)
def get_runtime(kind: str) -> Runtime:
    if kind == "openclaw":
        from core.runtime.openclaw.adapter import OpenClawRuntime

        return OpenClawRuntime()
    raise ValueError(f"unknown runtime kind: {kind!r}")


def get_runtime_for_owner(owner_id: str) -> Runtime:
    """Resolve the runtime for an owner.

    Every owner runs OpenClaw today. When a second runtime ships, this reads
    ``runtime_kind`` off the owner's container/billing row — keeping that
    lookup here means no caller ever branches on a brand.
    """
    return get_runtime("openclaw")
