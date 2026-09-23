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
    monkeypatch.delenv("LAMBDA_ENV_PARAMETER", raising=False)
    from scripts import provision_lambda, provision_runner

    # Warm: the first invocation's env load + import has already happened.
    monkeypatch.setattr(provision_lambda, "provision_runner", provision_runner)
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


class _Ssm:
    def __init__(self, env):
        self.env = env

    def get_parameter(self, Name):
        return {"Parameter": {"Value": json.dumps(self.env)}}


class _Sm:
    def __init__(self):
        self.calls = []

    def get_secret_value(self, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": f"value-of-{SecretId}"}


def test_load_environment_sets_the_plain_env_then_every_mapped_secret(lam):
    environ = {
        "LAMBDA_ENV_PARAMETER": "/lightbulb/dev/provisioner-env",
        "LAMBDA_SECRET_ENV_MAP": json.dumps({"CLERK_SECRET_KEY": "arn:clerk", "ALREADY_SET": "arn:other"}),
        "ALREADY_SET": "keep",
        "HOME": "/tmp",
    }
    sm = _Sm()
    lam.load_environment(environ, ssm=_Ssm({"OWNER_STATE_BUCKET": "b", "HOME": "/root"}), secretsmanager=sm)
    assert environ["OWNER_STATE_BUCKET"] == "b"
    assert environ["HOME"] == "/tmp"  # the function's own env wins
    assert environ["CLERK_SECRET_KEY"] == "value-of-arn:clerk"
    assert environ["ALREADY_SET"] == "keep"
    assert sm.calls == ["arn:clerk"]


def test_secrets_are_in_the_environment_before_settings_is_first_imported():
    """Cold start in a clean interpreter: importing the handler module does NOTHING heavy
    (Lambda's 10 s init cap), and the first invocation loads the mapped secrets BEFORE
    core.config is imported, so Settings reads them like ECS-injected env."""
    code = """
import sys, boto3

class _Sm:
    def get_secret_value(self, SecretId):
        return {"SecretString": "from-" + SecretId}

class _Ssm:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": '{"OWNER_STATE_BUCKET": "from-ssm"}'}}

boto3.client = lambda service, *a, **k: _Ssm() if service == "ssm" else _Sm()
assert "core.config" not in sys.modules
import scripts.provision_lambda as lam
assert "core.config" not in sys.modules  # init stays light
lam.load_environment()  # what the first invoke runs before importing the runner
from core.config import settings
assert settings.CLERK_SECRET_KEY == "from-arn:clerk", settings.CLERK_SECRET_KEY
assert settings.OWNER_STATE_BUCKET == "from-ssm", settings.OWNER_STATE_BUCKET
"""
    env = {
        **os.environ,
        "LAMBDA_ENV_PARAMETER": "/lightbulb/dev/provisioner-env",
        "LAMBDA_SECRET_ENV_MAP": json.dumps({"CLERK_SECRET_KEY": "arn:clerk"}),
    }
    env.pop("CLERK_SECRET_KEY", None)
    env.pop("OWNER_STATE_BUCKET", None)
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
