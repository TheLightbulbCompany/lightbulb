"""Readiness and the free-trial clock commit together, fenced by the provision lease."""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core.repositories import billing_repo, container_repo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,end,claim",
    [
        ("trial_local", None, "claim"),
        ("trial_local", 99, "claim"),
        ("active", None, "claim"),
        ("trial_local", None, "stale"),
    ],
)
async def test_ready_commit_cannot_leave_an_unarmed_trial(status, end, claim):
    with mock_aws():
        db = boto3.resource("dynamodb", region_name="us-east-1")
        tables = [
            db.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            for name in ("containers", "billing")
        ]
        containers, billing = tables
        containers.put_item(Item={"owner_id": "org_test", "status": "provisioning", "provision_claim_id": "claim"})
        account = {"owner_id": "org_test", "subscription_status": status}
        if end is not None:
            account["trial_end"] = end
        billing.put_item(Item=account)
        with (
            patch.object(container_repo, "_get_table", return_value=containers),
            patch.object(billing_repo, "_get_table", return_value=billing),
            patch("core.repositories.container_repo.time.time", return_value=1000),
        ):
            if claim == "stale":
                with pytest.raises(container_repo.ProvisionSuperseded):
                    await container_repo.update_container_leased(
                        "org_test", claim, {"status": "running"}, release_lease=True, arm_trial=True
                    )
            else:
                await container_repo.update_container_leased(
                    "org_test", claim, {"status": "running"}, release_lease=True, arm_trial=True
                )
        runtime = containers.get_item(Key={"owner_id": "org_test"})["Item"]
        after = billing.get_item(Key={"owner_id": "org_test"})["Item"]
        assert runtime["status"] == ("provisioning" if claim == "stale" else "running")
        if claim == "stale" or status == "active":
            assert after == account
        else:
            assert after["trial_end"] == (99 if end is not None else 2_593_000)
            assert "provision_claim_id" not in runtime
