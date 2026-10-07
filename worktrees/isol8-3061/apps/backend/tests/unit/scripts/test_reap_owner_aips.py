"""scripts.reap_owner_aips: env fence, ManagedBy fence, dry run, per-owner isolation."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts import reap_owner_aips

_ARN = "arn:aws:bedrock:us-east-1:1:application-inference-profile/{}"
_PROFILES = {
    # name -> tags
    "lightbulb-dev-org_a-sonnet": {"ManagedBy": "lightbulb-backend", "owner_id": "org_a", "tier": "sonnet"},
    "lightbulb-dev-org_a-opus": {"ManagedBy": "lightbulb-backend", "owner_id": "org_a", "tier": "opus"},
    "isol8-dev-org_b-sonnet": {"ManagedBy": "lightbulb-backend", "owner_id": "org_b", "tier": "sonnet"},
    "lightbulb-dev-gone-haiku": {"ManagedBy": "lightbulb-backend"},  # orphan, no owner tag
    "lightbulb-prod-org_c-sonnet": {"ManagedBy": "lightbulb-backend", "owner_id": "org_c", "tier": "sonnet"},
    "lightbulb-dev-someone-else": {"ManagedBy": "a-human"},
    "unprefixed-thing": {"ManagedBy": "lightbulb-backend", "owner_id": "org_d"},
}


def _client(fail_arn: str | None = None) -> MagicMock:
    client = MagicMock()
    names = list(_PROFILES)
    client.list_inference_profiles.side_effect = [
        {
            "inferenceProfileSummaries": [
                {"inferenceProfileArn": _ARN.format(n), "inferenceProfileName": n} for n in names[:3]
            ],
            "nextToken": "t",
        },
        {
            "inferenceProfileSummaries": [
                {"inferenceProfileArn": _ARN.format(n), "inferenceProfileName": n} for n in names[3:]
            ]
        },
    ]
    client.list_tags_for_resource.side_effect = lambda resourceARN: {
        "tags": [{"key": k, "value": v} for k, v in _PROFILES[resourceARN.rsplit("/", 1)[1]].items()]
    }

    def _delete(inferenceProfileIdentifier):
        if inferenceProfileIdentifier == fail_arn:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteInferenceProfile")

    client.delete_inference_profile.side_effect = _delete
    return client


def test_find_env_aips_applies_the_env_and_managed_by_fences():
    found = reap_owner_aips.find_env_aips(_client(), "dev")
    assert {owner: sorted(p["name"] for p in ps) for owner, ps in found.items()} == {
        "org_a": ["lightbulb-dev-org_a-opus", "lightbulb-dev-org_a-sonnet"],
        "org_b": ["isol8-dev-org_b-sonnet"],
        "<untagged>": ["lightbulb-dev-gone-haiku"],
    }


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing(monkeypatch):
    client = _client()
    monkeypatch.setattr(reap_owner_aips, "_bedrock_client", lambda: client)
    monkeypatch.setattr("core.billing.bedrock_aip._bedrock_client", lambda: client)
    assert await reap_owner_aips.main(["--env", "dev"]) == 0
    client.delete_inference_profile.assert_not_called()


@pytest.mark.asyncio
async def test_apply_isolates_a_failing_owner(monkeypatch):
    client = _client(fail_arn=_ARN.format("lightbulb-dev-org_a-opus"))
    monkeypatch.setattr(reap_owner_aips, "_bedrock_client", lambda: client)
    monkeypatch.setattr("core.billing.bedrock_aip._bedrock_client", lambda: client)
    assert await reap_owner_aips.main(["--env", "dev", "--apply"]) == 1
    deleted = {
        c.kwargs["inferenceProfileIdentifier"].rsplit("/", 1)[1] for c in client.delete_inference_profile.call_args_list
    }
    # org_a's failure did not stop org_b or the orphan; prod and unmanaged were never touched.
    assert deleted == {
        "lightbulb-dev-org_a-sonnet",
        "lightbulb-dev-org_a-opus",
        "isol8-dev-org_b-sonnet",
        "lightbulb-dev-gone-haiku",
    }
