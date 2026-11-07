"""The per-agent heartbeat checklist: the default by role, and the seed never overwrites an existing file."""

from unittest.mock import patch

from core.config import settings
from core.services import hermes_profiles
from core.services.heartbeat_checklist import CEO_CHECKLIST, SPECIALIST_CHECKLIST, default_checklist


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


async def test_upload_profile_seeds_the_checklist_only_when_absent():
    import boto3
    from moto import mock_aws

    files = hermes_profiles.ProfileFiles(config_yaml="c", dotenv="e", soul_md="s", heartbeat_md="default")
    hermes_profiles._SHARED_S3_CLIENT = None
    with mock_aws(), patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-replicas")
        key = "org_1/hermes/a1/HEARTBEAT.md"
        await hermes_profiles.upload_profile("org_1", "a1", files)
        assert s3.get_object(Bucket="test-replicas", Key=key)["Body"].read() == b"default"
        s3.put_object(Bucket="test-replicas", Key=key, Body=b"edited")
        await hermes_profiles.upload_profile("org_1", "a1", files)
        assert s3.get_object(Bucket="test-replicas", Key=key)["Body"].read() == b"edited"
