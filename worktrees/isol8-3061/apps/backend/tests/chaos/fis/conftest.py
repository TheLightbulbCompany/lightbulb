"""SP2 FIS harness fixtures. DEV ONLY — hard guard refuses any other env.

This harness drives REAL faults against the dev environment. The
``_dev_only_guard`` autouse fixture below SKIPS the entire suite unless the
caller has explicitly opted into a dev game-day. No AWS or network call is
made at import or collection time — the STS account check only runs AFTER the
two env gates pass.

Guard model: two env gates (ENVIRONMENT=dev + CHAOS_FIS_CONFIRM=dev) plus an
account sanity check. The account check is NOT a dev/prod discriminator —
dev and prod share one AWS account (877352799272), so confirming the account
proves nothing about which environment you are pointed at. The real
prod-safety boundary is that the FIS experiment templates only exist in the
dev CDK stage (instantiated solely for dev in isol8-stage.ts); there is
nothing in prod for this harness to start.
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

DEV_ACCOUNT_ID = "877352799272"  # isol8 AWS account (CLAUDE.md). Dev + prod share
# this ONE account, so the account check below is a sanity check only — NOT a
# dev/prod discriminator. The env gates + the dev-only FIS stack are what keep
# this off prod.


@pytest.fixture(scope="session", autouse=True)
def _dev_only_guard():
    """Refuse to run unless explicitly targeting dev.

    Two env gates plus an account sanity check:
      1. ENVIRONMENT=dev
      2. CHAOS_FIS_CONFIRM=dev (explicit caller opt-in)
      3. AWS account == isol8 account — a sanity check, NOT a dev/prod
         discriminator: dev and prod share one account (877352799272), so
         this confirms only that credentials point at isol8 at all. The real
         prod-safety boundary is that the FIS experiment templates only exist
         in the dev CDK stage — there is nothing in prod for this harness to
         start.

    The STS call is made ONLY after both env gates pass, so a bare run (no env
    set) skips without touching AWS or the network."""
    if os.environ.get("ENVIRONMENT") != "dev":
        pytest.skip("SP2 FIS harness runs only with ENVIRONMENT=dev")
    if os.environ.get("CHAOS_FIS_CONFIRM") != "dev":
        pytest.skip("set CHAOS_FIS_CONFIRM=dev to confirm a dev game-day run")
    ident = boto3.client("sts").get_caller_identity()
    assert ident["Account"] == DEV_ACCOUNT_ID, f"refusing: account {ident['Account']} != isol8"


def assert_dev_cluster(cluster_arn: str) -> None:
    """Assert the resolved target cluster is the dev cluster before starting.

    Phase B scenario start-paths should call this on the cluster ARN they
    resolve so an experiment is never started against a non-dev cluster (dev
    and prod share an AWS account, so the account check alone cannot catch
    this). Raises AssertionError unless the ARN names the dev cluster.
    """
    assert "isol8-dev-" in cluster_arn, f"refusing: cluster {cluster_arn} is not dev"


@pytest.fixture(scope="session")
def fis_client():
    return boto3.client("fis", region_name="us-east-1")


@pytest.fixture(scope="session")
def ecs_client():
    return boto3.client("ecs", region_name="us-east-1")
