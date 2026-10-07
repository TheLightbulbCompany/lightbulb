"""The owner-scoped Secrets Manager sweep.

Closes the wipe's old KNOWN GAP: BYOK LLM keys, integration credentials and
per-owner OAuth client apps all survived a "wipe". An account whose Anthropic
key is still readable from Secrets Manager is not wiped.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.services import owner_secret_sweep


def _sm_with(names: list[str]) -> MagicMock:
    sm = MagicMock()
    sm.list_secrets.side_effect = lambda **kw: {
        "SecretList": [{"Name": n} for n in names if n.startswith(kw["Filters"][0]["Values"][0])]
    }
    return sm


def test_prefixes_cover_both_credential_roots():
    with patch.object(owner_secret_sweep.settings, "ENVIRONMENT", "dev"):
        prefixes = owner_secret_sweep.owner_secret_prefixes("user_1")
    # BYOK LLM keys and integrations/OAuth apps live under different roots;
    # missing either leaves real credential material behind.
    assert "lightbulb/dev/user-keys/user_1/" in prefixes
    assert "lightbulb/dev/integrations/user_1/" in prefixes


@pytest.mark.asyncio
async def test_deletes_every_owner_secret_forcefully():
    names = [
        "lightbulb/dev/user-keys/user_1/anthropic",
        "lightbulb/dev/integrations/user_1/slack",
        "lightbulb/dev/integrations/user_1/oauth-app/github",
    ]
    sm = _sm_with(names)
    with (
        patch.object(owner_secret_sweep.settings, "ENVIRONMENT", "dev"),
        patch.object(owner_secret_sweep.boto3, "client", return_value=sm),
    ):
        deleted = await owner_secret_sweep.delete_owner_secrets("user_1")

    assert sorted(deleted) == sorted(names)
    # Force-delete, not the 30-day soft delete: a recoverable secret keeps the
    # plaintext retrievable AND holds the name reserved, so the same owner
    # re-signing up collides with their own tombstone.
    for call in sm.delete_secret.call_args_list:
        assert call.kwargs["ForceDeleteWithoutRecovery"] is True


@pytest.mark.asyncio
async def test_a_longer_owner_id_is_never_swept_by_a_shorter_one():
    """u_1 must not take u_10's credentials.

    The owner id is a path segment, so a server-side prefix match alone would
    let a shorter id match a longer one. The client-side startswith on the
    trailing slash is the guard, and this is the test that it holds.
    """
    sm = MagicMock()
    sm.list_secrets.side_effect = lambda **kw: {
        "SecretList": [
            {"Name": "lightbulb/dev/user-keys/u_1/anthropic"},
            {"Name": "lightbulb/dev/user-keys/u_10/anthropic"},
        ]
    }
    with (
        patch.object(owner_secret_sweep.settings, "ENVIRONMENT", "dev"),
        patch.object(owner_secret_sweep.boto3, "client", return_value=sm),
    ):
        deleted = await owner_secret_sweep.delete_owner_secrets("u_1")

    assert deleted == ["lightbulb/dev/user-keys/u_1/anthropic"]


@pytest.mark.asyncio
async def test_one_failing_secret_does_not_strand_the_rest():
    names = ["lightbulb/dev/user-keys/user_1/a", "lightbulb/dev/user-keys/user_1/b"]
    sm = _sm_with(names)
    sm.delete_secret.side_effect = [RuntimeError("boom"), None]
    with (
        patch.object(owner_secret_sweep.settings, "ENVIRONMENT", "dev"),
        patch.object(owner_secret_sweep.boto3, "client", return_value=sm),
    ):
        deleted = await owner_secret_sweep.delete_owner_secrets("user_1")

    # The second still went; the report undercounts rather than lying.
    assert deleted == ["lightbulb/dev/user-keys/user_1/b"]


@pytest.mark.asyncio
async def test_listing_is_read_only():
    sm = _sm_with(["lightbulb/dev/user-keys/user_1/anthropic"])
    with (
        patch.object(owner_secret_sweep.settings, "ENVIRONMENT", "dev"),
        patch.object(owner_secret_sweep.boto3, "client", return_value=sm),
    ):
        names = await owner_secret_sweep.list_owner_secret_names("user_1")

    assert names == ["lightbulb/dev/user-keys/user_1/anthropic"]
    sm.delete_secret.assert_not_called()
