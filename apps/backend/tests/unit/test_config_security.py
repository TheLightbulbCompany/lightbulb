"""The deployed-environment security assertions on Settings.

Each fence below has an INSECURE default (WS impersonation enforcement off,
Clerk ``aud`` skipped when CLERK_AUDIENCE is None) or is gated on ENVIRONMENT
reading exactly ``"prod"`` (debug routes + Clerk webhook guard). A dropped CDK
variable would open them with no other signal, so Settings refuses to construct.
"""

from __future__ import annotations

import logging

import pytest

from core.config import Settings

# A minimal non-placeholder CLERK_ISSUER so the sibling validator doesn't fire.
_ISSUER = "https://real.clerk.accounts.dev"


def _settings(**overrides):
    base = {"CLERK_ISSUER": _ISSUER}
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize("env", ["", "local", "test"])
def test_non_deployed_env_skips_security_checks(env):
    # Local dev / CI need not set the deployment fences.
    _settings(ENVIRONMENT=env, WS_ENFORCE_INTERNAL_SECRET=False, WS_INTERNAL_SECRET="", CLERK_AUDIENCE=None)


def test_deployed_requires_ws_enforcement():
    with pytest.raises(ValueError, match="WS internal-caller"):
        _settings(
            ENVIRONMENT="prod",
            WS_ENFORCE_INTERNAL_SECRET=False,
            WS_INTERNAL_SECRET="",
            CLERK_AUDIENCE="aud",
        )


def test_deployed_requires_ws_secret_even_when_enforced():
    with pytest.raises(ValueError, match="WS internal-caller"):
        _settings(
            ENVIRONMENT="prod",
            WS_ENFORCE_INTERNAL_SECRET=True,
            WS_INTERNAL_SECRET="",
            CLERK_AUDIENCE="aud",
        )


def test_deployed_missing_clerk_audience_warns_not_raises(caplog):
    # CLERK_AUDIENCE is not wired in the CDK yet; a hard failure here would crash
    # every backend deploy on boot. It must warn, not raise.
    with caplog.at_level(logging.WARNING):
        _settings(
            ENVIRONMENT="prod",
            WS_ENFORCE_INTERNAL_SECRET=True,
            WS_INTERNAL_SECRET="s",
            CLERK_AUDIENCE=None,
        )
    assert any("CLERK_AUDIENCE is unset" in r.message for r in caplog.records)


def test_deployed_fully_configured_ok():
    _settings(
        ENVIRONMENT="staging",
        WS_ENFORCE_INTERNAL_SECRET=True,
        WS_INTERNAL_SECRET="s",
        CLERK_AUDIENCE="aud",
    )


def test_unknown_environment_rejected():
    with pytest.raises(ValueError, match="not one of"):
        _settings(ENVIRONMENT="prd")


def test_fargate_without_deployed_env_rejected(monkeypatch):
    # The metadata URI is injected by the Fargate agent — present here means we
    # ARE deployed, so a non-deployed ENVIRONMENT is a dropped CDK var.
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    with pytest.raises(ValueError, match="Fargate"):
        _settings(ENVIRONMENT="")


def test_fargate_with_deployed_env_ok(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    _settings(
        ENVIRONMENT="prod",
        WS_ENFORCE_INTERNAL_SECRET=True,
        WS_INTERNAL_SECRET="s",
        CLERK_AUDIENCE="aud",
    )
