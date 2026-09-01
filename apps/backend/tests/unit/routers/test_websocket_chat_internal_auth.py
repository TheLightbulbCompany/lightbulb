"""Caller-attestation guard on the /api/v1/ws/* router (require_ws_internal_caller).

These endpoints derive identity from request headers (x-user-id etc.) with no
get_current_user, because they're meant to be invoked only by the trusted WS
Lambdas after the API Gateway WS authorizer validates the Clerk JWT. But the
public HTTP API Gateway $default route proxies every path to the same ALB with
no authorizer, so without this guard anyone can POST /api/v1/ws/connect with a
forged x-user-id. The Lambdas attach WS_INTERNAL_SECRET as a header the public
route can't supply.

The guard is off until WS_ENFORCE_INTERNAL_SECRET is true (zero-downtime rollout:
ship the secret to both sides, then flip enforcement).
"""

import pytest
from fastapi import HTTPException
from fastapi.params import Depends as DependsParam

from core.config import settings
from routers.websocket_chat import require_ws_internal_caller, router


def _call(lightbulb=None, isol8=None):
    """Invoke the guard the way FastAPI does — BOTH header params always bound.

    Calling it with an argument omitted would leave that parameter as its
    ``Header(None)`` default object (truthy), which is not a state any request
    can produce. Always pass both.
    """
    return require_ws_internal_caller(x_lightbulb_ws_internal=lightbulb, x_isol8_ws_internal=isol8)


def test_disabled_by_default_passes_without_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", False)
    # Even a bogus/absent header passes when enforcement is off — no behavior change.
    assert _call() is None
    assert _call(lightbulb="anything") is None


def test_enforced_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    with pytest.raises(HTTPException) as exc:
        _call()
    assert exc.value.status_code == 403


def test_enforced_rejects_wrong_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    with pytest.raises(HTTPException) as exc:
        _call(lightbulb="not-the-secret")
    assert exc.value.status_code == 403
    # …and the retired spelling gets no free pass either.
    with pytest.raises(HTTPException) as exc:
        _call(isol8="not-the-secret")
    assert exc.value.status_code == 403


def test_enforced_allows_correct_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    assert _call(lightbulb="top-secret-value") is None


def test_enforced_allows_retired_isol8_header_during_rollout(monkeypatch):
    """TRANSITIONAL: the old spelling still authenticates for one release.

    The Lambdas (ApiStack) and the backend (ServiceStack) are separate CFN
    stacks in one pipeline run and the backend rolls at maxHealthyPercent 200,
    so a new backend WILL see old-name traffic from not-yet-updated Lambdas for
    minutes. Rejecting it would 403 every WS connect/message in that window.

    DELETE THIS TEST with the x_isol8_ws_internal parameter, once this release
    is deployed to dev AND promoted to prod.
    """
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    assert _call(isol8="top-secret-value") is None


def test_enforced_but_unconfigured_fails_closed(monkeypatch):
    """Enforcement requested but no secret set → fail closed (503), never allow."""
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        _call(lightbulb="anything")
    assert exc.value.status_code == 503


def test_router_applies_the_guard_to_every_route():
    """The dependency is attached at the router level, so it covers connect,
    disconnect, and message uniformly (no per-endpoint opt-in to forget)."""
    dep_callables = [getattr(d, "dependency", None) for d in router.dependencies if isinstance(d, DependsParam)]
    assert require_ws_internal_caller in dep_callables
