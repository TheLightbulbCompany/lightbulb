"""The pasted-context merge into a Hermes profile's ``memories/USER.md``."""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core.config import settings
from core.services import context_import as ci
from core.services.hermes_profiles import USER_CHAR_LIMIT

BUCKET = "test-replicas"
PREFIX = "org_a/hermes/ceo_1/"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        with patch.object(settings, "OWNER_STATE_BUCKET", BUCKET):
            yield client


def _get(s3, rel):
    try:
        return s3.get_object(Bucket=BUCKET, Key=PREFIX + rel)["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        return None


def test_small_paste_lands_in_user_md_as_hermes_entries(s3):
    out = ci.merge_context("org_a", "ceo_1", "# Company\n- We sell bread\n- Based in Lyon\n")
    assert out == {"merged": len("Company: We sell bread") + len("Company: Based in Lyon"), "overflowed": False}
    assert _get(s3, "memories/USER.md") == "Company: We sell bread\n§\nCompany: Based in Lyon\n"
    assert _get(s3, "memories/user_profile_overflow.txt") is None
    assert _get(s3, ".revision")  # the bridge's re-pull signal
    assert ci.has_imported_context("org_a", "ceo_1") is True


def test_second_paste_augments_and_dedupes(s3):
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "memories/USER.md", Body=b"Prefers email\n")
    ci.merge_context("org_a", "ceo_1", "Prefers email\n\nSells bread")
    assert _get(s3, "memories/USER.md") == "Prefers email\n§\nSells bread\n"


def test_tables_and_code_are_kept_not_dropped(s3):
    ci.merge_context("org_a", "ceo_1", "| plan | price |\n|---|---|\n| pro | 50 |\n\n```\nSKU-1\n```")
    body = _get(s3, "memories/USER.md")
    assert "| pro | 50 |" in body and "SKU-1" in body and "|---|" not in body


def test_40kb_paste_spills_overflow_and_stays_under_budget(s3):
    paste = "\n\n".join(f"Fact number {i}: " + "x" * 60 for i in range(600))  # ~45 KB
    out = ci.merge_context("org_a", "ceo_1", paste)
    user_md = _get(s3, "memories/USER.md")
    overflow = _get(s3, "memories/user_profile_overflow.txt")
    assert out["overflowed"] is True
    assert len(user_md.rstrip("\n")) <= USER_CHAR_LIMIT
    assert ci.OVERFLOW_POINTER in user_md  # the agent is told where the rest is
    kept = [e for e in user_md.rstrip("\n").split("\n§\n") if e != ci.OVERFLOW_POINTER]
    assert len(kept) + len(overflow.splitlines()) == 600  # nothing silently dropped
    assert out["merged"] == sum(len(e) for e in kept)

    # A second overflowing paste appends to the spill file instead of replacing it.
    ci.merge_context("org_a", "ceo_1", "Another fact " + "y" * 2000)
    assert len(_get(s3, "memories/user_profile_overflow.txt").splitlines()) == len(overflow.splitlines()) + 1
    assert _get(s3, "memories/USER.md").count(ci.OVERFLOW_POINTER) == 1


def test_nothing_imported_before_a_paste(s3):
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "memories/USER.md", Body=b"learned by the agent\n")
    assert ci.has_imported_context("org_a", "ceo_1") is False


def test_whitespace_only_paste_has_no_entries(s3):
    with pytest.raises(ValueError):
        ci.merge_context("org_a", "ceo_1", "  \n\n ")
    assert _get(s3, "memories/USER.md") is None
