"""M6 task 10-E: the Hermes-native skills / sessions / health / models.authStatus RPCs.

The real loaders (`tools.skills_tool`, `hermes_cli.skills_hub`, `hermes_state_registry`) only
exist inside the Hermes image, so the tests that need them install a stand-in in `sys.modules`
— the same seam the rest of this suite uses for `run_agent`/`hermes_constants`. What is asserted
is the PROJECTION (what the panel receives) and the refusals, never Hermes's own behaviour.
"""
import sys
import types

import pytest

from bridge import server, sync

AUTH = {"authorization": "Bearer gw-test"}
RPC = "/plugins/lightbulb-channel/rpc"


def call(method, params=None):
    return server.handle_plugin(RPC, "POST", AUTH, {"method": method, "params": params or {}})


@pytest.fixture(autouse=True)
def _clear_restore_state():
    """`restore_profile` is once-per-process and `_roster` caches its S3 listing; both outlive
    a test's tmp_path HOME."""
    sync._restored.clear()
    server._roster_cache = (0.0, ())
    yield
    sync._restored.clear()
    server._roster_cache = (0.0, ())


def _parse_config_string_list(value):
    """Verbatim port of `agent.skill_utils.parse_config_string_list` at v2026.9.14 — the real
    module only exists inside the image, and the bridge calls this one by name. Kept
    byte-faithful so the assertions below mean what they say; re-check it on a Hermes bump."""
    import ast

    if isinstance(value, str):
        if value.strip().startswith("["):
            try:
                parsed = ast.literal_eval(value.strip())
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        return [value]
    return [str(item) for item in value] if isinstance(value, (list, tuple, set, frozenset)) else []


@pytest.fixture
def fake_hermes(monkeypatch):
    """Minimal stand-ins for the Hermes modules the panel RPCs import."""
    skill_utils = types.ModuleType("agent.skill_utils")
    skill_utils.ESSENTIAL_SKILLS = frozenset({"hermes-agent"})
    skill_utils.parse_config_string_list = _parse_config_string_list
    skills_tool = types.ModuleType("tools.skills_tool")
    skills_tool._find_all_skills = lambda **_kw: [
        {"name": "hermes-agent", "description": "core", "category": None},
        {"name": "pdf", "description": "Read PDFs", "category": "productivity"},
        {"name": "shopify", "description": "", "category": None},
    ]
    agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
    tools_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    for name, module in (("agent", agent_pkg), ("tools", tools_pkg),
                         ("agent.skill_utils", skill_utils), ("tools.skills_tool", skills_tool)):
        monkeypatch.setitem(sys.modules, name, module)
    return skills_tool


# ---- health ------------------------------------------------------------------------------------
def test_health_carries_uptime_and_an_agent_count(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    payload = call("health")[1]["payload"]
    assert payload["ready"] is True and payload["runtime"] == "hermes"
    assert isinstance(payload["uptimeS"], float)
    assert payload["agents"] == 1
    # Ruling M6-R54: no session count. It would have been a sqlite read per profile on the
    # readiness path, for a panel that is hidden on this runtime.
    assert "sessions" not in payload


# ---- sessions ----------------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["sessions.list", "sessions.patch", "sessions.delete"])
def test_sessions_stay_stubbed_because_the_backend_guard_is_a_privacy_rule(home, method):
    """Ruling M6-R54. Every session this platform creates on Hermes is room- or event-scoped
    (`agent:<id>:lightbulb:channel:<roomId>`), and the backend's guard admits only a PERSONAL
    key (`agent:<id>:<userId>`). Serving these would mean widening a privacy rule, so the
    surface stays stubbed and the panel is hidden instead."""
    code, obj = call(method, {})
    assert code == 200 and obj["ok"] is False
    assert obj["error"]["code"] == "UNAVAILABLE"


# ---- roster ------------------------------------------------------------------------------------
def test_roster_is_the_local_profiles_when_there_is_no_bucket(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    (home / "profiles" / "growth").mkdir(parents=True)
    assert server._roster() == ["ceo", "growth"]


# ---- skills ------------------------------------------------------------------------------------
def test_skills_status_projects_hermes_skills_into_the_panel_shape(home, fake_hermes):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"skills": {"disabled": ["shopify"]}})
    payload = call("skills.status")[1]["payload"]
    by_name = {s["name"]: s for s in payload["skills"]}
    assert set(by_name) == {"hermes-agent", "pdf", "shopify"}
    assert by_name["pdf"]["description"] == "Read PDFs"
    assert by_name["pdf"]["source"] == "productivity"
    assert by_name["pdf"]["skillKey"] == "pdf"
    assert by_name["shopify"]["disabled"] is True
    assert by_name["pdf"]["disabled"] is False
    # Essential skills cannot be turned off, which the panel renders as "Always on".
    assert by_name["hermes-agent"]["always"] is True and by_name["pdf"]["always"] is False
    # Nothing is ever "missing"/"unsupported": Hermes has no per-skill dependency manifest.
    assert all(not s["missing"]["bins"] and s["install"] == [] for s in payload["skills"])


def test_skills_status_is_empty_when_the_workspace_has_no_profile(home, fake_hermes):
    assert call("skills.status")[1]["payload"]["skills"] == []


@pytest.mark.parametrize("method", ["skills.install", "skills.search"])
def test_hub_install_and_search_are_cut(home, fake_hermes, method):
    """Ruling M6-R56. Hermes's hub machinery resolves its HUB_DIR from the live home, so its
    index cache (tens of MB) lands in the profile and syncs to S3; the GitHub adapter wants a
    secret scope a settings RPC should not install; and a cold fetch+scan+upload cannot fit the
    90 s ingress cap. The Skills panel is a READ on this runtime."""
    code, obj = call(method, {"query": "pdf", "source": "official/pdf"})
    assert code == 200 and obj["ok"] is False
    assert obj["error"]["code"] == "UNAVAILABLE"


def test_a_row_is_flagged_disabled_from_the_profiles_stored_list_not_from_what_rendered(
    home, fake_hermes
):
    """The per-row `disabled` flag is a DISPLAY fact and stays here. A stored name whose skill
    is not on this profile's disk simply renders in no row, which is correct -- and is exactly
    why the written-back LIST cannot be rebuilt from rows, and is served by the backend."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"skills": {"disabled": ["shopify", "not-on-this-disk"]}})
    payload = call("skills.status")[1]["payload"]
    assert {s["name"] for s in payload["skills"] if s["disabled"]} == {"shopify"}
    assert "not-on-this-disk" not in {s["name"] for s in payload["skills"]}


def test_the_bridge_never_answers_disabled_names(home, fake_hermes):
    """M6-R57, second half: the list the panel writes BACK is the backend's
    (`runtime_rpc.agentcore_disabled_skill_names`), because only the backend has every
    profile's config.yaml -- the bridge has it in `sync.EXCLUDE_GLOBS`, so a container copy
    never reaches S3, and one profile's list is not the workspace's.

    A key here would be WORSE than none: the backend overwrites it, so a bridge answer that
    drifted would be invisible until the day the overwrite was removed."""
    for name, disabled in (("ceo", ["shopify"]), ("growth", ["pdf"])):
        (home / "profiles" / name).mkdir(parents=True)
        sync.write_profile_config(name, {"skills": {"disabled": disabled}})
    assert "disabledNames" not in call("skills.status")[1]["payload"]


def test_a_disabled_list_stored_as_a_json_string_is_unpacked_for_the_row_flags(home, fake_hermes):
    """Review I6.1: `hermes config set` writes a list as a quoted literal. Read as one name it
    would render every skill enabled."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"skills": {"disabled": '["pdf", "shopify"]'}})
    payload = call("skills.status")[1]["payload"]
    assert {s["name"] for s in payload["skills"] if s["disabled"]} == {"pdf", "shopify"}


class _FakeS3:
    """Enough of the S3 client for the roster LIST."""

    def __init__(self, agent_ids, *, list_error=None):
        self.agent_ids, self.list_error = agent_ids, list_error

    def get_paginator(self, _name):
        if self.list_error:
            raise self.list_error
        prefixes = [{"Prefix": f"{sync.S3_PREFIX}/{a}/"} for a in sorted(self.agent_ids)]
        return types.SimpleNamespace(paginate=lambda **_kw: [{"CommonPrefixes": prefixes}])


class _ClientError(Exception):
    """botocore's shape, as `sync._is_absent` reads it: `.response["Error"]["Code"]`."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture
def s3(monkeypatch):
    """Point the bridge at a fake bucket. Returns a factory for the client."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket")
    monkeypatch.setattr(sync, "S3_PREFIX", "org_1/hermes")

    def _install(agent_ids, **kwargs):
        client = _FakeS3(agent_ids, **kwargs)
        monkeypatch.setattr(sync, "_s3", lambda: client)
        return client

    return _install


def test_the_roster_unions_the_s3_prefix_with_the_local_profile_dirs(home, s3):
    """A settings panel can land on a microVM that has never run a turn, where `profiles/` is
    empty -- answering "no agents" from that would be an empty answer that reads as fact."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    s3(["growth", "finance"])
    server._roster_cache = (0.0, ())
    assert server._roster() == ["ceo", "finance", "growth"]


def test_a_failed_roster_listing_still_serves_the_local_view(home, s3):
    """Nothing served from the roster is written back any more (the one read that was,
    `skills.disabled`, moved to the backend), so a short answer degrades a display and cannot
    corrupt a write. That is why there is no "is this AUTHORITATIVE" flag."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    s3(["growth"], list_error=_ClientError("SlowDown"))
    server._roster_cache = (0.0, ())
    assert server._roster() == ["ceo"]


# ---- models.authStatus -------------------------------------------------------------------------
def test_auth_status_reports_the_profiles_own_provider_and_its_credential(home, monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    profile = home / "profiles" / "ceo"
    profile.mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"provider": "anthropic"}})
    (profile / ".env").write_text("OTHER=1\nANTHROPIC_API_KEY=sk-not-a-real-key\n")
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "anthropic", "status": "ok", "via": "byo-key"}]


def test_auth_status_reports_missing_when_the_byo_key_is_absent(home, monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    profile = home / "profiles" / "ceo"
    profile.mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"provider": "openai-api"}})
    (profile / ".env").write_text("OPENAI_API_KEY=\n")
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "openai-api", "status": "missing", "via": "byo-key"}]


def test_auth_status_reports_managed_custom_through_the_gate_bearer(home, monkeypatch):
    # The PROFILE decides, not the boot-time provider choice (a warm process outlives a switch).
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-token")
    monkeypatch.setenv("LIGHTBULB_PROVIDER_CHOICE", "bedrock_trial")
    monkeypatch.setenv("LIGHTBULB_MANAGED_GATE_URL", "https://gate.example/v1")
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"provider": "custom", "base_url": "https://gate.example/v1"}})
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "custom", "status": "ok", "via": "lightbulb-gate"}]


def test_auth_status_does_not_call_a_non_gate_custom_route_the_lightbulb_gate(home, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-token")
    monkeypatch.setenv("LIGHTBULB_PROVIDER_CHOICE", "openrouter_managed")
    monkeypatch.setenv("LIGHTBULB_MANAGED_GATE_URL", "https://gate.example/v1")
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"provider": "custom", "base_url": "https://elsewhere.example/v1"}})
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "custom", "status": "missing", "via": "unknown"}]


def test_auth_status_reports_chatgpt_oauth_from_the_root_auth_json(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"provider": "openai-codex"}})
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "openai-codex", "status": "missing", "via": "chatgpt-oauth"}]
    (home / "auth.json").write_text("{}")
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "openai-codex", "status": "missing", "via": "chatgpt-oauth"}]
    (home / "auth.json").write_text('{"providers":{"openai-codex":{"tokens":{"refresh_token":"rt"}}}}')
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "openai-codex", "status": "ok", "via": "chatgpt-oauth"}]


def test_auth_status_reads_ONE_profile_because_the_provider_is_owner_level(home, monkeypatch):
    """Review I2. `hermes_profiles.model_block` derives `model.provider` from owner-level
    inputs only, so a second profile cannot hold a second answer — and a per-agent
    `ensure_profile` is a full prefix LIST + recency pull on a cold microVM, inside the 90 s
    ingress cap."""
    for name in ("ceo", "growth"):
        (home / "profiles" / name).mkdir(parents=True)
        sync.write_profile_config(name, {"model": {"provider": "anthropic"}})
    pulled = []
    monkeypatch.setattr(sync, "ensure_profile", lambda agent_id: pulled.append(agent_id))
    providers = call("models.authStatus")[1]["payload"]["providers"]
    assert providers == [{"provider": "anthropic", "status": "missing", "via": "byo-key"}]
    assert pulled == ["ceo"]  # the first roster profile, and only it


def test_auth_status_reports_no_provider_with_no_profile(home, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-token")
    assert call("models.authStatus")[1]["payload"]["providers"] == []
