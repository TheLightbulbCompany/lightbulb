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


def test_skills_update_is_refused_rather_than_writing_a_config_nothing_uploads(home, fake_hermes):
    code, obj = call("skills.update", {"skillKey": "pdf", "enabled": False})
    assert code == 200 and obj["ok"] is False
    assert obj["error"]["code"] == "UNSUPPORTED"
    assert "config.patch" in obj["error"]["message"]


def test_skill_proposals_are_empty_because_write_approval_is_pinned_off(home):
    payload = call("skills.proposals.list")[1]["payload"]
    assert payload == {"proposals": [], "reason": "write_approval_disabled"}


@pytest.mark.parametrize("method", ["skills.install", "skills.search"])
def test_hub_install_and_search_are_cut(home, fake_hermes, method):
    """Ruling M6-R56. Hermes's hub machinery resolves its HUB_DIR from the live home, so its
    index cache (tens of MB) lands in the profile and syncs to S3; the GitHub adapter wants a
    secret scope a settings RPC should not install; and a cold fetch+scan+upload cannot fit the
    90 s ingress cap. The Skills panel is a READ on this runtime."""
    code, obj = call(method, {"query": "pdf", "source": "official/pdf"})
    assert code == 200 and obj["ok"] is False
    assert obj["error"]["code"] == "UNAVAILABLE"


def test_status_returns_the_STORED_disabled_list_not_just_what_rendered(home, fake_hermes):
    """Review I3. A disabled name whose skill is not on THIS profile's disk renders in no row.
    A client rebuilding the list from rows would drop it, and because a deep merge replaces a
    list wholesale, dropping it silently re-enables that skill on every profile."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"skills": {"disabled": ["shopify", "not-on-this-disk"]}})
    payload = call("skills.status")[1]["payload"]
    assert payload["disabledNames"] == ["not-on-this-disk", "shopify"]
    # ...and the name with no row is genuinely absent from the rows, which is the whole point.
    assert "not-on-this-disk" not in {s["name"] for s in payload["skills"]}


def test_a_disabled_list_stored_as_a_json_string_is_unpacked(home, fake_hermes):
    """Review I6.1: `hermes config set` writes a list as a quoted literal. Read as one name it
    would show every skill enabled AND let the next toggle wipe the real list."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"skills": {"disabled": '["pdf", "shopify"]'}})
    payload = call("skills.status")[1]["payload"]
    assert payload["disabledNames"] == ["pdf", "shopify"]
    assert {s["name"] for s in payload["skills"] if s["disabled"]} == {"pdf", "shopify"}


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


def test_auth_status_reports_bedrock_through_the_gate_bearer(home, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-token")
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"provider": "bedrock"}})
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "bedrock", "status": "ok", "via": "bedrock-gate"}]


def test_auth_status_reports_chatgpt_oauth_from_the_root_auth_json(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"provider": "openai-codex"}})
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "openai-codex", "status": "missing", "via": "chatgpt-oauth"}]
    (home / "auth.json").write_text("{}")
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "openai-codex", "status": "ok", "via": "chatgpt-oauth"}]


def test_auth_status_reads_ONE_profile_because_the_provider_is_owner_level(home, monkeypatch):
    """Review I2. `hermes_profiles.model_block` derives `model.provider` from owner-level
    inputs only, so a second profile cannot hold a second answer — and a per-agent
    `ensure_profile` is a full prefix LIST + recency pull on a cold microVM, inside the 90 s
    ingress cap."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-token")
    for name in ("ceo", "growth"):
        (home / "profiles" / name).mkdir(parents=True)
        sync.write_profile_config(name, {"model": {"provider": "bedrock"}})
    pulled = []
    monkeypatch.setattr(sync, "ensure_profile", lambda agent_id: pulled.append(agent_id))
    providers = call("models.authStatus")[1]["payload"]["providers"]
    assert providers == [{"provider": "bedrock", "status": "ok", "via": "bedrock-gate"}]
    assert pulled == ["ceo"]  # the first roster profile, and only it


def test_auth_status_falls_back_to_the_runtime_bearer_with_no_profile(home, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-token")
    assert call("models.authStatus")[1]["payload"]["providers"] == [
        {"provider": "bedrock", "status": "ok", "via": "bedrock-gate"}]
