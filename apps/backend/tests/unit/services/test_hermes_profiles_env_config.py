"""M6-R31 fix round 1, Important 1 — hermes_profiles.patch_profile_env_and_config.

The combined .env + config.yaml write for an openclaw_paths-projected provider (e.g.
parallel's PARALLEL_API_KEY + web.backend). Replaces two separate calls
(patch_profile_env then patch_profile_config) that could land the .env half with no
.revision bump if the config half then raised -- a silently orphaned partial write.
"""

import pytest

from core.services import hermes_profiles


def test_writes_env_then_config_then_revision_once(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-owner-state")
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a, **kw: {"model": {"default": "arn:son"}})
    monkeypatch.setattr(hermes_profiles, "_require_text", lambda bucket, key, what: "PAPERCLIP_API_KEY=pcp_x\n")

    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((key, body)))

    hermes_profiles.patch_profile_env_and_config(
        "org_a", "ceo", {"PARALLEL_API_KEY": "par-live"}, {"web": {"backend": "parallel"}}
    )

    keys = [k for k, _ in puts]
    assert keys == [
        "org_a/hermes/ceo/.env",
        "org_a/hermes/ceo/config.yaml",
        "org_a/hermes/ceo/.revision",
    ]
    assert "PARALLEL_API_KEY=par-live" in puts[0][1]
    assert "web" in puts[1][1] and "parallel" in puts[1][1]
    # The pre-existing config key survives the merge.
    assert "arn:son" in puts[1][1]


def test_a_missing_config_yaml_raises_before_any_write():
    """C4 parity with patch_profile_config: a patch onto a never-provisioned profile is a
    defect upstream, not something to merge onto {} -- and critically here, it must raise
    BEFORE either put_object, so a not-yet-provisioned profile can never end up with a
    changed .env and no revision bump to surface it."""

    def _must_not_put(*a, **k):
        raise AssertionError("must not write anything before both reads succeed")

    import unittest.mock as mock

    with (
        mock.patch.object(hermes_profiles, "_s3_get", return_value=None),
        mock.patch.object(hermes_profiles, "_s3_put", side_effect=_must_not_put),
        mock.patch.object(hermes_profiles.settings, "OWNER_STATE_BUCKET", "bucket"),
    ):
        with pytest.raises(FileNotFoundError):
            hermes_profiles.patch_profile_env_and_config(
                "org_a", "ceo", {"PARALLEL_API_KEY": "par-live"}, {"web": {"backend": "parallel"}}
            )


def test_a_missing_dotenv_raises_before_any_write(monkeypatch):
    """Same guard on the OTHER read: config.yaml exists but .env doesn't yet."""
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a, **kw: {"model": {}})

    def _fail_env(bucket, key, what):
        raise FileNotFoundError(f"no {what} at {key}")

    monkeypatch.setattr(hermes_profiles, "_require_text", _fail_env)

    def _must_not_put(*a, **k):
        raise AssertionError("must not write anything before both reads succeed")

    monkeypatch.setattr(hermes_profiles, "_s3_put", _must_not_put)

    with pytest.raises(FileNotFoundError):
        hermes_profiles.patch_profile_env_and_config(
            "org_a", "ceo", {"PARALLEL_API_KEY": "par-live"}, {"web": {"backend": "parallel"}}
        )


def test_is_a_no_op_without_a_bucket(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")

    def _fail(*a, **k):
        raise AssertionError("must not touch S3 without a configured bucket")

    monkeypatch.setattr(hermes_profiles, "read_profile_config", _fail)
    monkeypatch.setattr(hermes_profiles, "_require_text", _fail)
    monkeypatch.setattr(hermes_profiles, "_s3_put", _fail)

    hermes_profiles.patch_profile_env_and_config("org_a", "ceo", {"K": "v"}, {"web": {}})  # must not raise


def test_empty_value_clears_the_key_instead_of_leaving_it_stale(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a, **kw: {})
    monkeypatch.setattr(hermes_profiles, "_require_text", lambda bucket, key, what: "PARALLEL_API_KEY=stale\nOTHER=1\n")
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((key, body)))

    hermes_profiles.patch_profile_env_and_config("org_a", "ceo", {"PARALLEL_API_KEY": ""}, {"web": {"backend": ""}})

    env_body = puts[0][1]
    assert "PARALLEL_API_KEY" not in env_body
    assert "OTHER=1" in env_body
