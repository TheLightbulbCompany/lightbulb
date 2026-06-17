"""SP2 FIS harness fixtures. DEV ONLY — hard guard refuses any other env.

This harness drives REAL faults against the dev environment. The
``_dev_only_guard`` autouse fixture below SKIPS the entire suite unless the
caller has explicitly opted into a dev game-day. No AWS or network call is
made at import or collection time — the STS account check only runs AFTER the
two env gates pass.
"""

import os

# Import-safety defaults only — these mirror the parent chaos conftest so
# importing core.config (pulled in transitively by scenario modules) doesn't
# raise on a bare machine. We deliberately do NOT default ENVIRONMENT or
# CHAOS_FIS_CONFIRM: those are the real game-day gates and must be set by the
# caller, never silently satisfied here.
os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENCRYPTION_KEY", "wHc3hAOcLlFzWyu3Ph7xIyClIdVQTrIzFOZDtu_pIEY=")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
import pytest

DEV_ACCOUNT_ID = "877352799272"  # isol8 AWS account (CLAUDE.md). Dev + prod share it,
# so we ALSO require ENVIRONMENT=dev + an explicit opt-in flag below.


@pytest.fixture(scope="session", autouse=True)
def _dev_only_guard():
    """Refuse to run unless explicitly targeting dev. Three gates:
    ENVIRONMENT=dev, the caller-set CHAOS_FIS_CONFIRM=dev, and the dev account.

    The STS call is made ONLY after both env gates pass, so a bare run (no env
    set) skips without touching AWS or the network."""
    if os.environ.get("ENVIRONMENT") != "dev":
        pytest.skip("SP2 FIS harness runs only with ENVIRONMENT=dev")
    if os.environ.get("CHAOS_FIS_CONFIRM") != "dev":
        pytest.skip("set CHAOS_FIS_CONFIRM=dev to confirm a dev game-day run")
    ident = boto3.client("sts").get_caller_identity()
    assert ident["Account"] == DEV_ACCOUNT_ID, f"refusing: account {ident['Account']} != dev"


@pytest.fixture(scope="session")
def fis_client():
    return boto3.client("fis", region_name="us-east-1")


@pytest.fixture(scope="session")
def ecs_client():
    return boto3.client("ecs", region_name="us-east-1")
