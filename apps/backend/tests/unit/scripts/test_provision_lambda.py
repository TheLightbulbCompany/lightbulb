"""scripts/provision_lambda.py: the provision state machine's Lambda entry point.

The runner's exit ABI becomes typed errors the machine's Retry/Catch match on, and
the backend's secrets are loaded into the environment before core.config is read.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def lam(monkeypatch):
    monkeypatch.delenv("LAMBDA_SECRET_ENV_MAP", raising=False)
    from scripts import provision_lambda

    return provision_lambda


@pytest.mark.parametrize(
    ("code", "error"),
    [(11, "ProvisionTransient"), (10, "ProvisionFailed")],
)
def test_a_failed_run_raises_the_error_the_machine_matches(lam, monkeypatch, code, error):
    monkeypatch.setattr(lam.provision_runner, "run", AsyncMock(return_value=code))
    with pytest.raises(Exception) as exc:
        lam.handler({"owner_id": "org_a"}, None)
    # Step Functions sees the exception's class name as the error name.
    assert type(exc.value).__name__ == error
    assert "org_a" in str(exc.value)


def test_a_provisioned_run_returns_its_outcome(lam, monkeypatch):
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(lam.provision_runner, "run", run)
    # Extra keys (an execution started by a pre-deploy backend) are ignored.
    assert lam.handler({"owner_id": "org_a", "attempt": 0, "command": ["x"]}, None) == {"outcome": "provisioned"}
    run.assert_awaited_once_with("org_a")


def test_warm_invocations_share_one_event_loop(lam, monkeypatch):
    loops = []

    async def _run(owner_id):
        import asyncio

        loops.append(asyncio.get_running_loop())
        return 0

    monkeypatch.setattr(lam.provision_runner, "run", _run)
    lam.handler({"owner_id": "org_a"}, None)
    lam.handler({"owner_id": "org_b"}, None)
    assert loops[0] is loops[1]


def test_load_secrets_sets_every_mapped_env_var(lam):
    calls = []

    class _Sm:
        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            return {"SecretString": f"value-of-{SecretId}"}

    environ = {"ALREADY_SET": "keep"}
    lam.load_secrets({"CLERK_SECRET_KEY": "arn:clerk", "ALREADY_SET": "arn:other"}, environ, client=_Sm())
    assert environ == {"CLERK_SECRET_KEY": "value-of-arn:clerk", "ALREADY_SET": "keep"}
    assert calls == ["arn:clerk"]


def test_secrets_are_in_the_environment_before_settings_is_first_imported():
    """Cold start in a clean interpreter: importing the handler module loads the mapped
    secrets BEFORE core.config is imported, so Settings reads them like ECS-injected env."""
    code = """
import sys, boto3

class _Sm:
    def get_secret_value(self, SecretId):
        return {"SecretString": "from-" + SecretId}

boto3.client = lambda *a, **k: _Sm()
assert "core.config" not in sys.modules
import scripts.provision_lambda
from core.config import settings
assert settings.CLERK_SECRET_KEY == "from-arn:clerk", settings.CLERK_SECRET_KEY
"""
    env = {**os.environ, "LAMBDA_SECRET_ENV_MAP": json.dumps({"CLERK_SECRET_KEY": "arn:clerk"})}
    env.pop("CLERK_SECRET_KEY", None)
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
