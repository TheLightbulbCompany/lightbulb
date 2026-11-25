"""The deployed-environment security assertions on Settings.

The Clerk webhook guard hinges on ENVIRONMENT reading exactly "prod".
A dropped CDK var would open these with no other signal, so Settings refuses to
construct — but ONLY for the actual backend task (detected via the Fargate
metadata URI), so out-of-task operational CLIs that set ENVIRONMENT=prod still
import cleanly.
"""

from __future__ import annotations

import logging

import pytest

from core.config import Settings

# A minimal non-placeholder CLERK_ISSUER so the sibling validator doesn't fire.
_ISSUER = "https://real.clerk.accounts.dev"
_FARGATE = "ECS_CONTAINER_METADATA_URI_V4"


def _settings(**overrides):
    base = {"CLERK_ISSUER": _ISSUER}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def in_fargate(monkeypatch):
    """Simulate running inside the backend ECS Fargate task."""
    monkeypatch.setenv(_FARGATE, "http://169.254.170.2/v4/abc")


# ---- Non-backend contexts (local dev, CI, operational CLIs) never assert ----


@pytest.mark.parametrize("env", ["", "local", "test"])
def test_non_deployed_env_skips_security_checks(env):
    _settings(ENVIRONMENT=env, CLERK_AUTHORIZED_PARTIES="")


def test_out_of_task_cli_with_prod_env_is_ok():
    # A CLI that sets ENVIRONMENT=prod outside Fargate must import cleanly.
    _settings(ENVIRONMENT="prod", CLERK_AUTHORIZED_PARTIES="")


# ---- ENVIRONMENT value hygiene (applies everywhere) ----


def test_unknown_environment_rejected():
    with pytest.raises(ValueError, match="not one of"):
        _settings(ENVIRONMENT="prd")


@pytest.mark.parametrize("raw", ["PROD", " prod ", "Prod"])
def test_non_canonical_environment_rejected(raw):
    # Downstream guards compare == "prod" exactly, so a non-canonical value would
    # boot while exposing debug routes.
    with pytest.raises(ValueError, match="lowercase"):
        _settings(ENVIRONMENT=raw)


# ---- The deployed backend task (Fargate) must have the fences wired ----


def test_fargate_without_deployed_env_rejected(in_fargate):
    with pytest.raises(ValueError, match="Fargate"):
        _settings(ENVIRONMENT="")


# ---- SEC-8: azp (token-origin) verification rollout knobs ----


def test_fargate_azp_audit_mode_warns_not_raises(in_fargate, caplog):
    # Audit mode (the rollout default) must boot and warn — a hard failure
    # before the allowlist is validated in the wild would crash every deploy.
    with caplog.at_level(logging.WARNING):
        _settings(ENVIRONMENT="prod", CLERK_AZP_MODE="audit")
    assert any("AUDIT mode" in r.message for r in caplog.records)


def test_fargate_enforce_without_parties_rejected(in_fargate):
    # Enforce mode with an empty allowlist verifies nothing while looking
    # secure — refuse to boot.
    with pytest.raises(ValueError, match="CLERK_AUTHORIZED_PARTIES"):
        _settings(
            ENVIRONMENT="prod",
            CLERK_AZP_MODE="enforce",
            CLERK_AUTHORIZED_PARTIES="",
        )


def test_bad_azp_mode_rejected():
    with pytest.raises(ValueError, match="CLERK_AZP_MODE"):
        _settings(CLERK_AZP_MODE="on")


def test_fargate_fully_configured_ok(in_fargate):
    _settings(
        ENVIRONMENT="staging",
        WS_ENFORCE_INTERNAL_SECRET=True,
        WS_INTERNAL_SECRET="s",
        CLERK_AZP_MODE="enforce",
        CLERK_AUTHORIZED_PARTIES="https://dev.isol8.co",
    )
