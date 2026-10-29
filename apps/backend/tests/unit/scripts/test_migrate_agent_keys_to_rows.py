"""migrate_agent_keys_to_rows against moto Secrets Manager + DynamoDB: dry run, copy + verify, delete."""

from datetime import datetime, timezone
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo, PaperclipWorkspace
from core.services.paperclip_api_key import _secret_name
from scripts import migrate_agent_keys_to_rows as mig


@pytest.fixture
def aws():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=settings.AWS_REGION)
        ddb.create_table(
            TableName="paperclip-companies",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        with patch("core.dynamodb._table_prefix", ""), patch("core.dynamodb._dynamodb_resource", ddb):
            yield sm, PaperclipRepo(table_name="paperclip-companies")


async def _seed(sm, repo):
    now = datetime.now(timezone.utc)
    await repo.put_workspace(
        PaperclipWorkspace(org_id="org_a", company_id="co", status="active", created_at=now, updated_at=now)
    )
    sm.create_secret(Name=_secret_name("org_a", "a1"), SecretString="key-a1")
    sm.create_secret(Name=_secret_name("org_a", "a2"), SecretString="key-a2")
    sm.create_secret(Name=_secret_name("org_gone", "g1"), SecretString="key-g1")  # no workspace row


def _scheduled(sm, name):
    return "DeletedDate" in sm.describe_secret(SecretId=name)


async def test_dry_run_counts_and_writes_nothing(aws):
    sm, repo = aws
    await _seed(sm, repo)

    counts = await mig.run(mode="dry-run")

    assert counts["secrets"] == 3 and counts["owners"] == 2
    assert counts["to_copy"] == 2 and counts["no_row"] == 1
    assert await repo.get_agent_key("org_a", "a1") is None


async def test_copy_verifies_and_never_deletes_then_delete_schedules_only_migrated(aws):
    sm, repo = aws
    await _seed(sm, repo)

    counts = await mig.run(mode="copy")
    assert counts["copied"] == 2 and counts["no_row"] == 1 and not counts["verify_failed"]
    assert await repo.get_agent_key("org_a", "a1") == "key-a1"
    assert not any(_scheduled(sm, _secret_name(*k)) for k in [("org_a", "a1"), ("org_a", "a2"), ("org_gone", "g1")])

    assert (await mig.run(mode="copy"))["verified"] == 2  # re-run is a no-op check

    # A row that holds a DIFFERENT key keeps its secret.
    repo._table().update_item(
        Key={"user_id": "org_a"},
        UpdateExpression="REMOVE agent_keys.a2",
    )
    await repo.put_agent_key("org_a", "a2", "newer-row-key")

    counts = await mig.run(mode="delete-migrated")
    assert counts["deletion_scheduled"] == 1 and counts["kept_not_migrated"] == 1 and counts["no_row"] == 1
    assert _scheduled(sm, _secret_name("org_a", "a1"))
    assert not _scheduled(sm, _secret_name("org_a", "a2"))
    assert not _scheduled(sm, _secret_name("org_gone", "g1"))


async def test_owner_id_scopes_the_run(aws):
    sm, repo = aws
    await _seed(sm, repo)

    counts = await mig.run(mode="dry-run", owner_id="org_gone")

    assert counts["secrets"] == 1 and counts["no_row"] == 1
