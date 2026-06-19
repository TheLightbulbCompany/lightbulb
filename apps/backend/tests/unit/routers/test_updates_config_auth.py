"""Authorization wiring for the container-config patch routes in routers/updates.py.

Regression for the personal-context auth-bypass: both ``PATCH /container/config``
(fleet) and ``PATCH /container/config/{owner_id}`` (per-owner, owner from the
path) used ``Depends(get_current_user)`` plus an ``if auth.is_org_context:
require_org_admin(auth)`` branch — which passes through unconditionally for
personal (non-org) callers. That left every authenticated personal user able to
deep-merge config into another tenant's (or every tenant's) openclaw.json.

The fix gates both routes with ``require_platform_admin`` (the @isol8.co gate)
and removes the org-admin branch. These tests assert that wiring directly off the
FastAPI route definitions so the bypass can't silently return.
"""

from fastapi.dependencies.utils import get_flat_dependant

from core.auth import get_current_user, require_platform_admin
from routers.updates import router


def _patch_config_routes():
    routes = {}
    for r in router.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", set()) or set()
        if path in ("/config/{owner_id}", "/config") and "PATCH" in methods:
            routes[path] = r
    return routes


def test_both_patch_config_routes_exist():
    routes = _patch_config_routes()
    assert set(routes) == {"/config/{owner_id}", "/config"}


def test_config_routes_are_guarded_by_platform_admin():
    """Each PATCH /config* route must depend (directly) on require_platform_admin."""
    routes = _patch_config_routes()
    assert routes, "expected the PATCH /config routes to exist"
    for path, route in routes.items():
        direct_calls = [d.call for d in route.dependant.dependencies]
        assert require_platform_admin in direct_calls, f"{path} is not guarded by require_platform_admin"


def test_config_routes_do_not_use_get_current_user_directly():
    """The old idiom injected get_current_user directly so the handler could run
    the (bypassed) is_org_context branch. After the fix, get_current_user only
    appears nested *under* require_platform_admin, never as a direct route dep.
    """
    routes = _patch_config_routes()
    for path, route in routes.items():
        direct_calls = [d.call for d in route.dependant.dependencies]
        assert get_current_user not in direct_calls, f"{path} still injects get_current_user directly (bypass idiom)"
        # get_current_user must still be reachable transitively (platform-admin
        # depends on it) — confirm the auth chain is intact, just gated.
        flat_calls = [d.call for d in get_flat_dependant(route.dependant).dependencies]
        assert get_current_user in flat_calls
        assert require_platform_admin in flat_calls
