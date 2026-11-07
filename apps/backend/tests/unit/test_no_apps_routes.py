"""Apps, the Stripe Projects rail, the marketplace and the Mac app's sign-in and session-start routes are deleted (Phase 1 task 10):
nothing may route them again."""

import pytest
from starlette.routing import Match, Mount

from main import app

GONE = [
    "/api/v1/apps",
    "/api/v1/apps/build",
    "/api/v1/apps/build/up",
    "/api/v1/apps/app_x",
    "/api/v1/marketplace/listings",
    "/api/v1/marketplace/seller/me",
    "/api/v1/marketplace/import/presign",
    "/api/v1/marketplace/payouts/onboard",
    "/api/v1/admin/marketplace/listings",
    "/api/v1/company/provisioning/resources",
    "/api/v1/desktop-sessions/targets",
    "/api/v1/auth/desktop/sign-in-token",
]


@pytest.mark.parametrize("path", GONE)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_route_is_gone(path, method):
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": []}
    # Routers are included lazily; only the root Mount matches every path, so skip it.
    routed = [r for r in app.routes if not isinstance(r, Mount) and r.matches(scope)[0] is not Match.NONE]
    assert not routed, f"{method} {path} is still routed by {[getattr(r, 'path', r) for r in routed]}"


def test_no_schema_path_names_apps_or_marketplace():
    app.openapi_schema = None
    paths = app.openapi()["paths"]
    assert not [
        p for p in paths if p.startswith(("/api/v1/apps", "/api/v1/company/provisioning")) or "/marketplace" in p
    ]
