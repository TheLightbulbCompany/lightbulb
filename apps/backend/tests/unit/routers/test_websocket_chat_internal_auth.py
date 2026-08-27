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


def test_disabled_by_default_passes_without_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", False)
    # Even a bogus/absent header passes when enforcement is off — no behavior change.
    assert require_ws_internal_caller(x_lightbulb_ws_internal=None, x_isol8_ws_internal=None) is None
    assert require_ws_internal_caller(x_lightbulb_ws_internal="anything", x_isol8_ws_internal=None) is None


def test_enforced_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    with pytest.raises(HTTPException) as exc:
        require_ws_internal_caller(x_lightbulb_ws_internal=None, x_isol8_ws_internal=None)
    assert exc.value.status_code == 403


def test_enforced_rejects_wrong_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    with pytest.raises(HTTPException) as exc:
        require_ws_internal_caller(x_lightbulb_ws_internal="not-the-secret", x_isol8_ws_internal=None)
    assert exc.value.status_code == 403


def test_enforced_allows_correct_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    assert require_ws_internal_caller(x_lightbulb_ws_internal="top-secret-value", x_isol8_ws_internal=None) is None


def test_enforced_but_unconfigured_fails_closed(monkeypatch):
    """Enforcement requested but no secret set → fail closed (503), never allow."""
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        require_ws_internal_caller(x_lightbulb_ws_internal="anything", x_isol8_ws_internal=None)
    assert exc.value.status_code == 503


def test_router_applies_the_guard_to_every_route():
    """The dependency is attached at the router level, so it covers connect,
    disconnect, and message uniformly (no per-endpoint opt-in to forget)."""
    dep_callables = [getattr(d, "dependency", None) for d in router.dependencies if isinstance(d, DependsParam)]
    assert require_ws_internal_caller in dep_callables


# ── Legacy header acceptance ────────────────────────────────────────────────
# The WS Lambdas flip to x-lightbulb-ws-internal the instant their deploy lands,
# but ECS rolls at maxHealthyPercent 200 -- old tasks keep serving for minutes.
# For that window the two sides disagree about the header NAME, so a task must
# accept either or it 403s every chat message.
#
# Delete this block together with the x_isol8_ws_internal param, once prod has
# rolled onto the new name.


def test_enforced_allows_the_legacy_header(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    assert require_ws_internal_caller(x_lightbulb_ws_internal=None, x_isol8_ws_internal="top-secret-value") is None


def test_legacy_header_still_rejects_a_wrong_secret(monkeypatch):
    """Accepting a second header NAME must not weaken the secret check itself."""
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    with pytest.raises(HTTPException) as exc:
        require_ws_internal_caller(x_lightbulb_ws_internal=None, x_isol8_ws_internal="not-the-secret")
    assert exc.value.status_code == 403


def test_new_header_wins_when_both_are_present(monkeypatch):
    """A request carrying both names is authenticated on the new one."""
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", True)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "top-secret-value")
    assert require_ws_internal_caller(x_lightbulb_ws_internal="top-secret-value", x_isol8_ws_internal="stale") is None
