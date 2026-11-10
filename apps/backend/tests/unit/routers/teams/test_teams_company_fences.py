"""Every Teams BFF route that takes an entity id is fenced to the caller's workspace.

A route with an id path param must depend on one of
the fence dependencies below, or be listed in ``ALLOWLIST`` with a reason.
The allowlist must match exactly, so a fixed route has to leave it too.
"""

import re

from routers.teams import agents, router

FENCES = {agents._workspace_agent}

ALLOWLIST: dict = {}


def _calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _calls(d)


def _walk(routes, prefix=""):
    # FastAPI >= 0.142 keeps include_router() as a nested _IncludedRouter instead of flattening.
    for r in routes:
        if hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes, prefix + r.include_context.prefix)
        else:
            yield prefix, r


def _unfenced():
    out = set()
    for prefix, r in _walk(router.routes):
        path = prefix + r.path
        if not re.search(r"\{\w+(?::\w+)?\}", path):
            continue
        if FENCES & set(_calls(r.dependant)):
            continue
        out |= {(m, path) for m in r.methods}
    return out


def test_every_id_route_is_fenced_or_allowlisted():
    unfenced = _unfenced()
    assert unfenced - ALLOWLIST.keys() == set(), "unfenced id routes: add a fence dependency"
    assert ALLOWLIST.keys() - unfenced == set(), "stale allowlist entries: the route is fenced now, delete them"
