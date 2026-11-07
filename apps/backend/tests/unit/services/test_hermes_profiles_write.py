import re

import pytest
import yaml

from core.services import hermes_profiles
from core.services.hermes_profiles import (
    SOUL_GENERATED_MARKER,
    ProfileFiles,
    is_generated_soul,
    upload_profile,
    upload_profile_files,
    write_agent_profile,
)


def test_write_agent_profile_returns_the_three_files():
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "name": "Ada", "role": "ceo"},
        provider_choice="byo_key",
        byo_provider="anthropic",
        integrations={"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"}},
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert isinstance(files, ProfileFiles)
    config = yaml.safe_load(files.config_yaml)
    assert config["model"]["default"] == "claude-opus-5"  # role ceo -> premium tier
    assert config["mcp_servers"]["stripe"]["auth"] == "oauth"
    assert "STRIPE_SECRET_KEY=sk_live_x" in files.dotenv
    # M6-R48: a GENERATED SOUL.md leads with the marker, then the heading.
    assert files.soul_md.startswith(SOUL_GENERATED_MARKER)
    assert "# Ada" in files.soul_md


def test_config_yaml_carries_no_secret_even_though_dotenv_does():
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "role": "engineering"},
        provider_choice="byo_key",
        byo_provider="anthropic",
        integrations={},
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert "sk_live_x" not in files.config_yaml
    assert "sk_live_x" in files.dotenv


def test_byo_provider_and_key_are_owner_level_kwargs_not_read_from_the_roster_row():
    """C2: the Paperclip roster row never carries a byo_provider — it's an OWNER-level choice
    threaded next to provider_choice everywhere else (ecs_manager.py:2760,2889,3039). Before this
    fix, write_agent_profile read ``agent.get("byo_provider")``, which is always None for a real
    roster row, so every BYO-OpenAI owner silently got a profile pinned to Anthropic with no key
    anywhere and every turn failed on the first call."""
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "role": "engineering"},  # roster row: no byo_provider key at all
        provider_choice="byo_key",
        integrations={},
        env_vars={},
        byo_provider="openai",
        byo_api_key="sk-oai-x",
    )
    config = yaml.safe_load(files.config_yaml)
    # supports_vision: #3344 -- Hermes's tool-result image table lists "openai", not "openai-api".
    assert config["model"] == {"default": "gpt-5.4", "provider": "openai-api", "supports_vision": True}
    assert "OPENAI_API_KEY=sk-oai-x" in files.dotenv
    assert "ANTHROPIC_API_KEY" not in files.dotenv


def test_soul_md_override_replaces_the_generated_identity():
    """M6 lane R1: the workspace migration job carries an agent's EFS ``SOUL.md`` verbatim
    into its Hermes profile instead of the generic ``build_soul_md`` identity — the only
    reason a caller outside this module ever wants a raw string in ``ProfileFiles.soul_md``."""
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "name": "Ada", "role": "ceo"},
        provider_choice="byo_key",
        byo_provider="anthropic",
        integrations={},
        env_vars={},
        soul_md_override="# Ada\n\nI am the CEO, carried over from ECS.\n",
    )
    assert files.soul_md == "# Ada\n\nI am the CEO, carried over from ECS.\n"


def test_no_soul_md_override_falls_back_to_build_soul_md():
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "name": "Ada", "role": "ceo"},
        provider_choice="byo_key",
        byo_provider="anthropic",
        integrations={},
        env_vars={},
    )
    assert is_generated_soul(files.soul_md)
    assert "# Ada" in files.soul_md
    assert "report to you" in files.soul_md


class _FakeS3Client:
    """Records every ``put_object`` call as ``(bucket, key, decoded body)``."""

    def __init__(self, sink):
        self._sink = sink

    def put_object(self, *, Bucket, Key, Body):
        self._sink.append((Bucket, Key, Body.decode() if isinstance(Body, bytes) else Body))


async def test_upload_writes_owner_first_under_hermes(monkeypatch):
    """I5 (fix round 1): no per-profile ``managed/config.yaml`` object any more — the baked
    ``/etc/hermes/config.yaml`` is the sole enforcement point (``get_managed_dir()`` resolves
    ``$HERMES_MANAGED_DIR`` or ``/etc/hermes`` only and never reads S3), so uploading a per-profile
    copy was a harmless write nobody ever reads. Backend-owned files are exactly the four Task 13
    (this lane's continuation) needs to exclude from the bridge's own ``save_profile``:
    config.yaml, .env, SOUL.md, .revision."""
    puts = []
    monkeypatch.setattr(hermes_profiles, "_new_s3_client", lambda: _FakeS3Client(puts))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")
    await upload_profile("org_a", "a1", ProfileFiles("model: {}\n", "A=1", "# Ada\n"))
    keys = [key for _, key, _ in puts]
    assert keys[:3] == [
        "org_a/hermes/a1/config.yaml",
        "org_a/hermes/a1/.env",
        "org_a/hermes/a1/SOUL.md",
    ]
    assert keys[3] == "org_a/hermes/a1/.revision"  # written LAST — the bridge polls it
    assert len(keys) == 4
    assert {bucket for bucket, _, _ in puts} == {"lightbulb-dev-openclaw-replicas"}


async def test_upload_builds_exactly_one_s3_client_for_the_whole_profile(monkeypatch):
    """I6 (fix round 1): the old implementation built a fresh boto3 client per file — 30 client
    constructions and 30 blocking round trips for a six-agent roster. One client, reused for
    every ``put_object`` in the profile."""
    puts = []
    client_builds = []
    monkeypatch.setattr(hermes_profiles, "_new_s3_client", lambda: (client_builds.append(1), _FakeS3Client(puts))[1])
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")
    await upload_profile("org_a", "a1", ProfileFiles("model: {}\n", "A=1", "# Ada\n"))
    assert len(client_builds) == 1
    assert len(puts) == 4


async def test_upload_revision_is_a_uuid4_hex_written_after_every_other_file(monkeypatch):
    """C4: the backend and the container's own ``sync.save_profile`` are two uncoordinated writers
    on the same S3 objects. ``.revision`` is the ONLY signal the bridge (Task 13, this lane's
    continuation) can poll to notice a backend-authored hot patch, so it must be unconditionally
    last and must change on every write (a uuid4 hex, not a timestamp that a clock skew or two
    writes within the same millisecond could collide on)."""
    puts = []
    monkeypatch.setattr(hermes_profiles, "_new_s3_client", lambda: _FakeS3Client(puts))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")
    await upload_profile("org_a", "a1", ProfileFiles("model: {}\n", "A=1", "# Ada\n"))
    last_bucket, last_key, last_body = puts[-1]
    assert last_key == "org_a/hermes/a1/.revision"
    assert re.fullmatch(r"[0-9a-f]{32}", last_body), last_body


async def test_upload_is_a_no_op_without_a_bucket(monkeypatch):
    def _fail():
        raise AssertionError("no S3 client should be built without a bucket")

    monkeypatch.setattr(hermes_profiles, "_new_s3_client", _fail)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    await upload_profile("org_a", "a1", ProfileFiles("x", "y", "z"))


async def test_new_s3_client_is_built_off_the_event_loop_via_run_in_thread(monkeypatch):
    """Minor (re-review): building a boto3 client does file I/O (credentials/config resolution)
    on first use — calling _new_s3_client() directly on the event loop reopens exactly the
    blocking-I/O risk I6 fixed for put_object; the constructor itself must go through
    run_in_thread too, not just the puts."""
    seen_funcs = []
    real_run_in_thread = hermes_profiles.run_in_thread

    async def spy_run_in_thread(func, *args, **kwargs):
        seen_funcs.append(func)
        return await real_run_in_thread(func, *args, **kwargs)

    monkeypatch.setattr(hermes_profiles, "run_in_thread", spy_run_in_thread)
    monkeypatch.setattr(hermes_profiles, "_new_s3_client", lambda: _FakeS3Client([]))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    await upload_profile("org_a", "a1", ProfileFiles("x", "y", "z"))
    assert hermes_profiles._new_s3_client in seen_funcs


# ---------------------------------------------------------------------------
# upload_profile_files — the non-tarball sibling of deploy_tarball_to_profile
# (M6-R33: company deploy's per-agent AGENTS.md upload).
# ---------------------------------------------------------------------------


async def test_upload_profile_files_writes_named_files_then_revision_last(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_new_s3_client", lambda: _FakeS3Client(puts))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")

    await upload_profile_files("org_a", "a1", {"AGENTS.md": b"# Ada\nBody.\n"})

    keys = [key for _, key, _ in puts]
    assert keys[0] == "org_a/hermes/a1/AGENTS.md"
    assert keys[-1] == "org_a/hermes/a1/.revision"
    assert len(keys) == 2
    bodies = {key: body for _, key, body in puts}
    assert bodies["org_a/hermes/a1/AGENTS.md"] == "# Ada\nBody.\n"
    assert re.fullmatch(r"[0-9a-f]{32}", bodies["org_a/hermes/a1/.revision"])


async def test_upload_profile_files_writes_every_named_file(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_new_s3_client", lambda: _FakeS3Client(puts))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")

    await upload_profile_files("org_a", "a1", {"AGENTS.md": b"x", "skills/foo.md": b"y"})

    keys = [key for _, key, _ in puts]
    assert "org_a/hermes/a1/AGENTS.md" in keys
    assert "org_a/hermes/a1/skills/foo.md" in keys
    assert keys[-1] == "org_a/hermes/a1/.revision"
    assert len(keys) == 3


async def test_upload_profile_files_is_a_no_op_without_a_bucket(monkeypatch):
    def _fail():
        raise AssertionError("no S3 client should be built without a bucket")

    monkeypatch.setattr(hermes_profiles, "_new_s3_client", _fail)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    await upload_profile_files("org_a", "a1", {"AGENTS.md": b"x"})


@pytest.mark.parametrize("name", ["config.yaml", ".env", "SOUL.md", ".revision"])
async def test_upload_profile_files_refuses_writer_owned_names(monkeypatch, name):
    """A content upload must never overwrite what write_agent_profile just wrote (Q9d review)."""
    puts = []
    monkeypatch.setattr(hermes_profiles, "_new_s3_client", lambda: _FakeS3Client(puts))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")

    with pytest.raises(ValueError, match=name.replace(".", r"\.")):
        await upload_profile_files("org_a", "a1", {"AGENTS.md": b"x", name: b"clobber"})

    assert puts == []


def test_front_desk_profile_has_no_workspace_connectors():
    """End-state Task 4 (audit H): Front desk faces callers, so its Hermes profile gets none of the owner's
    connectors or their secrets, and none of the toolsets that reach the machine (shell, files, browser) or the
    owner's rooms and sessions; Office, the owner's specialist, keeps them all."""
    from core.services.hermes_provisioning import ProfileInputs

    connected = {
        "google_workspace": {"url": "https://mcp.google.example", "headers": {"Authorization": "Bearer g-tok"}},
        "stripe": {"url": "https://mcp.stripe.com", "headers": {"Authorization": "Bearer sk-tok"}},
    }
    inputs = ProfileInputs("byo_key", "anthropic", "sk-ant", connected, {}, contact_agent_id="fd-1")

    def build(agent_id):
        files = write_agent_profile(
            "org_a", {"id": agent_id, "role": "general"}, provider_choice="byo_key", byo_provider="anthropic",
            integrations=connected, env_vars={}, contact_only=inputs.contact_only(agent_id),
        )  # fmt: skip
        return files, yaml.safe_load(files.config_yaml)

    front_desk, fd_config = build("fd-1")
    office, office_config = build("office-1")
    assert set(office_config["mcp_servers"]) == {"google_workspace", "stripe"}
    assert "g-tok" in office.dotenv and "sk-tok" in office.dotenv
    assert fd_config["mcp_servers"] == {}
    assert "g-tok" not in front_desk.dotenv and "sk-tok" not in front_desk.dotenv
    off = set(fd_config["agent"]["disabled_toolsets"])
    assert {"browser", "terminal", "file", "code_execution", "delegation", "lightbulb"} <= off
    assert not off & {"memory", "skills"}  # Knowledge stays
    assert "browser" not in office_config["agent"]["disabled_toolsets"]
    assert hermes_profiles.CONTACT_SAFE_CONNECTORS == frozenset()  # Task 8 adds its one scoped browser here
