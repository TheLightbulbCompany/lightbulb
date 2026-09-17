import yaml

from core.services import hermes_profiles
from core.services.hermes_profiles import ProfileFiles, upload_profile, write_agent_profile

AIPS = {"sonnet": "arn:son", "opus": "arn:opu", "haiku": "arn:hai"}


def test_write_agent_profile_returns_the_four_files():
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "name": "Ada", "role": "ceo"},
        provider_choice="bedrock_claude",
        aip_arns=AIPS,
        paperclip_api_key="pcp_live_x",
        integrations=[{"key": "stripe", "url": "https://mcp.stripe.com", "auth": "oauth"}],
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert isinstance(files, ProfileFiles)
    config = yaml.safe_load(files.config_yaml)
    assert config["model"]["default"] == "arn:opu"  # role ceo -> premium tier
    assert config["mcp_servers"]["stripe"]["auth"] == "oauth"
    assert "STRIPE_SECRET_KEY=sk_live_x" in files.dotenv
    assert "PAPERCLIP_API_KEY=pcp_live_x" in files.dotenv
    assert files.soul_md.startswith("# Ada")
    assert files.managed_overlay == hermes_profiles.MANAGED_OVERLAY_YAML


def test_config_yaml_carries_no_secret_even_though_dotenv_does():
    files = write_agent_profile(
        "org_a",
        {"id": "a1", "role": "engineering"},
        provider_choice="bedrock_claude",
        aip_arns=AIPS,
        paperclip_api_key="pcp_live_x",
        integrations=[],
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert "sk_live_x" not in files.config_yaml
    assert "sk_live_x" in files.dotenv
    assert "pcp_live_x" not in files.config_yaml


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
        aip_arns=None,
        paperclip_api_key="pcp_live_x",
        integrations=[],
        env_vars={},
        byo_provider="openai",
        byo_api_key="sk-oai-x",
    )
    config = yaml.safe_load(files.config_yaml)
    assert config["model"] == {"default": "gpt-5.4", "provider": "openai-api"}
    assert "OPENAI_API_KEY=sk-oai-x" in files.dotenv
    assert "ANTHROPIC_API_KEY" not in files.dotenv


def test_upload_writes_owner_first_under_hermes(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((bucket, key)))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")
    upload_profile("org_a", "a1", ProfileFiles("model: {}\n", "A=1", "# Ada\n", "updates: {}\n"))
    keys = [key for _, key in puts]
    assert keys[:4] == [
        "org_a/hermes/a1/config.yaml",
        "org_a/hermes/a1/.env",
        "org_a/hermes/a1/SOUL.md",
        "org_a/hermes/a1/managed/config.yaml",
    ]
    assert keys[4] == "org_a/hermes/a1/.revision"  # written LAST — the bridge polls it
    assert {bucket for bucket, _ in puts} == {"lightbulb-dev-openclaw-replicas"}


def test_upload_is_a_no_op_without_a_bucket(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda *a: puts.append(a))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "")
    upload_profile("org_a", "a1", ProfileFiles("x", "y", "z", "w"))
    assert puts == []
