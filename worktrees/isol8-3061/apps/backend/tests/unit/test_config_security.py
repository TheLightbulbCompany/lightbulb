"""The deployed-environment security assertions on Settings.

The WS fence has an INSECURE default (impersonation enforcement off), and the
debug routes + Clerk webhook guard hinge on ENVIRONMENT reading exactly "prod".
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
    # MESSAGING_TABLE is CDK-wired like the WS secret; the fence for it is
    # covered by its own test below, so every other case supplies one.
    base = {"CLERK_ISSUER": _ISSUER, "MESSAGING_TABLE": "lightbulb-test-messaging"}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def in_fargate(monkeypatch):
    """Simulate running inside the backend ECS Fargate task."""
    monkeypatch.setenv(_FARGATE, "http://169.254.170.2/v4/abc")


# ---- Non-backend contexts (local dev, CI, operational CLIs) never assert ----


@pytest.mark.parametrize("env", ["", "local", "test"])
def test_non_deployed_env_skips_security_checks(env):
    _settings(ENVIRONMENT=env, WS_ENFORCE_INTERNAL_SECRET=False, WS_INTERNAL_SECRET="", CLERK_AUTHORIZED_PARTIES="")


def test_out_of_task_cli_with_prod_env_and_no_ws_is_ok():
    # scripts.reap_owner_aips / roll_hermes_runtimes set
    # ENVIRONMENT=prod to target prod but run outside Fargate and carry no WS
    # secret — they must import cleanly, or the ops tool is bricked.
    _settings(ENVIRONMENT="prod", WS_ENFORCE_INTERNAL_SECRET=False, WS_INTERNAL_SECRET="", CLERK_AUTHORIZED_PARTIES="")


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


def test_fargate_requires_ws_enforcement(in_fargate):
    with pytest.raises(ValueError, match="WS internal-caller"):
        _settings(ENVIRONMENT="prod", WS_ENFORCE_INTERNAL_SECRET=False, WS_INTERNAL_SECRET="")


def test_fargate_requires_ws_secret_even_when_enforced(in_fargate):
    with pytest.raises(ValueError, match="WS internal-caller"):
        _settings(ENVIRONMENT="prod", WS_ENFORCE_INTERNAL_SECRET=True, WS_INTERNAL_SECRET="")


# ---- SEC-8: azp (token-origin) verification rollout knobs ----


def test_fargate_requires_messaging_table(in_fargate):
    # The canonical room table is wired by the same stack as the WS secret; a
    # missing value is a dropped variable, and every room command would 500.
    with pytest.raises(ValueError, match="MESSAGING_TABLE"):
        _settings(ENVIRONMENT="prod", WS_ENFORCE_INTERNAL_SECRET=True, WS_INTERNAL_SECRET="s", MESSAGING_TABLE="")


def test_fargate_azp_audit_mode_warns_not_raises(in_fargate, caplog):
    # Audit mode (the rollout default) must boot and warn — a hard failure
    # before the allowlist is validated in the wild would crash every deploy.
    with caplog.at_level(logging.WARNING):
        _settings(ENVIRONMENT="prod", WS_ENFORCE_INTERNAL_SECRET=True, WS_INTERNAL_SECRET="s", CLERK_AZP_MODE="audit")
    assert any("AUDIT mode" in r.message for r in caplog.records)


def test_fargate_enforce_without_parties_rejected(in_fargate):
    # Enforce mode with an empty allowlist verifies nothing while looking
    # secure — refuse to boot.
    with pytest.raises(ValueError, match="CLERK_AUTHORIZED_PARTIES"):
        _settings(
            ENVIRONMENT="prod",
            WS_ENFORCE_INTERNAL_SECRET=True,
            WS_INTERNAL_SECRET="s",
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
