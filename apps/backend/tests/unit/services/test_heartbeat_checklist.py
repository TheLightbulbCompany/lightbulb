"""The per-agent heartbeat checklist: its CEO default matches the bundled skill reference, and the
seed never overwrites an existing file."""

from pathlib import Path
from unittest.mock import patch

from core.config import settings
from core.services import hermes_profiles
from core.services.heartbeat_checklist import CEO_CHECKLIST, SPECIALIST_CHECKLIST, default_checklist

_REFERENCE = Path(__file__).resolve().parents[5] / "apps/infra/hermes/skills-src/paperclip/references/ceo-heartbeat.md"


def test_ceo_default_is_the_bundled_reference_after_the_build_swap():
    body = _REFERENCE.read_text().split("-->\n", 1)[1].lstrip("\n")
    assert CEO_CHECKLIST == body.replace("/app/skills/", "/opt/hermes-skills/")


def test_default_by_role():
    assert default_checklist("ceo") is CEO_CHECKLIST
    assert default_checklist("engineer") is SPECIALIST_CHECKLIST
    assert default_checklist(None) is SPECIALIST_CHECKLIST


def test_seed_creates_once_and_never_overwrites():
    import boto3
    from moto import mock_aws

    hermes_profiles._SHARED_S3_CLIENT = None
    try:
        with mock_aws(), patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-replicas")
            assert hermes_profiles.seed_profile_file("org_1", "a1", "HEARTBEAT.md", "default") is True
            s3.put_object(Bucket="test-replicas", Key="org_1/hermes/a1/HEARTBEAT.md", Body=b"edited")
            assert hermes_profiles.seed_profile_file("org_1", "a1", "HEARTBEAT.md", "default") is False
            assert s3.get_object(Bucket="test-replicas", Key="org_1/hermes/a1/HEARTBEAT.md")["Body"].read() == b"edited"
    finally:
        hermes_profiles._SHARED_S3_CLIENT = None
