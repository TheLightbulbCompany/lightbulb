"""M6 lane R1 — scripts/migrate_workspace_to_hermes.py.

No live AWS/EFS/Paperclip anywhere here: EFS is a real ``tmp_path`` tree behind a
``Workspace`` the tests construct themselves (never the process-wide ``get_workspace()``
singleton — see ``_workspace_at`` below for why), and every AWS/Paperclip call is a
monkeypatched stand-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.containers.workspace import Workspace
from core.repositories.container_repo import ProvisionSuperseded
from scripts import migrate_workspace_to_hermes as mod

OWNER = "org_test1"
CEO_ID = "11111111-1111-1111-1111-111111111111"
ENG_ID = "22222222-2222-2222-2222-222222222222"
ROSTER = [
    {"id": CEO_ID, "role": "ceo", "name": "Ada"},
    {"id": ENG_ID, "role": "engineering", "name": "Bea"},
]


def _fake_workspace(tmp_path):
    """Builds ``<tmp_path>/<OWNER>/workspace-{agentId}/...`` for both roster agents, matching
    the EFS layout ``core.containers.workspace.Workspace`` reads. Only the CEO gets a SOUL.md
    override, so the plan/upload assertions can distinguish "carried" from "generated"."""
    root = tmp_path / OWNER
    for agent_id, soul in [(CEO_ID, "# Ada\n\nI am the CEO, carried over from ECS.\n"), (ENG_ID, None)]:
        ws = root / f"workspace-{agent_id}"
        (ws / "memory").mkdir(parents=True)
        (ws / "skills" / "example").mkdir(parents=True)
        if soul is not None:
            (ws / "SOUL.md").write_text(soul)
        (ws / "AGENTS.md").write_text("playbook")
        (ws / "MEMORY.md").write_text("remembered things")
        (ws / "memory" / "2026-09-15.md").write_text("daily note")
        (ws / "skills" / "example" / "SKILL.md").write_text("# example skill")
        # NEVER carried: OpenClaw runtime state siblings, at the user root, not under
        # workspace-{id}/ (see agent_export.py's own split) -- present here only to prove
        # _collect_carry_files never reaches for them.
    (root / "agents" / CEO_ID / "sessions").mkdir(parents=True)
    (root / "agents" / CEO_ID / "sessions" / "transcript.db").write_text("not carried")
    return root


def _workspace_at(tmp_path) -> Workspace:
    """A ``Workspace`` pointed at ``tmp_path`` directly. NOT
    ``patch.object(mod.settings, "EFS_MOUNT_PATH", ...)`` -- ``core.containers.get_workspace()``
    is a process-wide singleton cached on first call in the whole pytest run, so a settings
    patch made after some earlier test already created it has no effect. Monkeypatching
    ``mod.get_workspace`` itself sidesteps the singleton entirely."""
    return Workspace(mount_path=str(tmp_path))


class _FakeS3Client:
    """Records every ``put_object`` call, in order, as ``(bucket, key)``."""

    def __init__(self, sink: list[tuple[str, str]]):
        self._sink = sink

    def put_object(self, *, Bucket, Key, Body):
        self._sink.append((Bucket, Key))


@pytest.fixture
def precondition_ok(monkeypatch):
    """Wires every precondition check to pass for OWNER with ROSTER, without touching AWS."""
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER, "runtime_host": "ecs"})
    )
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))


# ---------------------------------------------------------------------------
# Task 1 — dry run
# ---------------------------------------------------------------------------


async def test_dry_run_lists_agents_and_writes_nothing(tmp_path, capsys, precondition_ok, monkeypatch):
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(
        mod.container_repo, "takeover_provision", AsyncMock(side_effect=AssertionError("dry-run must not claim"))
    )

    assert await mod.migrate(OWNER, dry_run=True) == mod.EXIT_OK

    out = capsys.readouterr().out
    assert CEO_ID in out and ENG_ID in out
    assert "DRY RUN" in out


async def test_dry_run_reports_reconnect_chatgpt_for_an_oauth_owner(tmp_path, capsys, precondition_ok, monkeypatch):
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None)))

    assert await mod.migrate(OWNER, dry_run=True) == mod.EXIT_OK

    assert "reconnect_chatgpt" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Task 1 — the real migration
# ---------------------------------------------------------------------------


async def test_migrate_uploads_every_agent_provisions_then_stops_ecs_after_ready(
    tmp_path, precondition_ok, monkeypatch
):
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", "test-hermes-bucket")

    calls: list[str] = []
    uploaded_keys: list[tuple[str, str]] = []

    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    prepare = AsyncMock(side_effect=lambda *a, **kw: calls.append("prepare"))
    provision = AsyncMock(side_effect=lambda *a, **kw: calls.append("provision") or "arn:hermes-1")
    monkeypatch.setattr(mod, "prepare_hermes_workspace", prepare)
    monkeypatch.setattr(mod, "provision_hermes_runtime", provision)

    monkeypatch.setattr(mod, "_s3_client", lambda: _FakeS3Client(uploaded_keys))
    monkeypatch.setattr("core.services.hermes_profiles._new_s3_client", lambda: _FakeS3Client(uploaded_keys))

    monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value={"sonnet": "arn:son"}))
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers", AsyncMock(return_value={})
    )
    monkeypatch.setattr("core.services.provisioning_landing.build_provisioned_env", AsyncMock(return_value={}))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pcp_live_x")
    )

    ecs = SimpleNamespace(stop_user_service=AsyncMock(side_effect=lambda *a, **kw: calls.append("stop")))
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK

    # Ordering (Ruling M6-R1): provision only after prepare, stop only after provision.
    assert calls == ["prepare", "provision", "stop"]
    prepare.assert_awaited_once_with(OWNER, claim_id="claim-1", attempt_token="claim-1", migrate=True)
    provision.assert_awaited_once_with(OWNER, claim_id="claim-1", attempt_token="claim-1", migrate=True)
    # force=True (Ruling M6-R15): bypasses the AgentCore no-op guard -- the row already reads
    # runtime_host=agentcore by the time this call happens.
    ecs.stop_user_service.assert_awaited_once_with(OWNER, reason="hermes_migration", force=True)

    # Every agent got a profile prefix, the SOUL.md override, and the carried EFS content --
    # each agent's .revision write is the LAST key for that agent.
    ceo_keys = [k for b, k in uploaded_keys if k.startswith(f"{OWNER}/hermes/{CEO_ID}/")]
    eng_keys = [k for b, k in uploaded_keys if k.startswith(f"{OWNER}/hermes/{ENG_ID}/")]
    for keys in (ceo_keys, eng_keys):
        assert keys, "expected at least one uploaded key per agent"
        assert keys[-1].endswith(".revision")
        assert any(k.endswith("SOUL.md") for k in keys)
        assert any(k.endswith("AGENTS.md") for k in keys)
        assert any(k.endswith("MEMORY.md") for k in keys)
        assert any("memory/" in k for k in keys)
        assert any("skills/" in k for k in keys)
    # NEVER carried: OpenClaw runtime state (sessions/transcripts/agent auth).
    assert not any("sessions" in k or "transcript" in k for _, k in uploaded_keys)


async def test_reference_org_is_refused_outright(monkeypatch):
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(side_effect=AssertionError("must not even read the row"))
    )
    assert await mod.migrate(mod.REFERENCE_ORG_ID, dry_run=False) == mod.EXIT_REFERENCE_ORG


@pytest.mark.parametrize(
    "row,company,roster",
    [
        (None, SimpleNamespace(status="active"), ROSTER),
        ({"owner_id": OWNER, "runtime_host": "agentcore"}, SimpleNamespace(status="active"), ROSTER),
        ({"owner_id": OWNER, "runtime_host": "ecs"}, None, ROSTER),
        ({"owner_id": OWNER, "runtime_host": "ecs"}, SimpleNamespace(status="seeding"), ROSTER),
        ({"owner_id": OWNER, "runtime_host": "ecs"}, SimpleNamespace(status="active"), []),
    ],
)
async def test_precondition_failures_exit_2_and_claim_nothing(monkeypatch, row, company, roster):
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=company))
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(
        mod.container_repo, "takeover_provision", AsyncMock(side_effect=AssertionError("must not claim"))
    )
    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_PRECONDITION


async def test_a_lost_claim_exits_10_and_writes_nothing(precondition_ok, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(side_effect=ProvisionSuperseded("taken")))
    prepare = AsyncMock(side_effect=AssertionError("must not reach prepare"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", prepare)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_LOST_CLAIM
    prepare.assert_not_called()


async def test_a_lost_claim_during_prepare_also_exits_10(precondition_ok, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock(side_effect=ProvisionSuperseded("taken")))
    provision = AsyncMock(side_effect=AssertionError("must not reach provision"))
    monkeypatch.setattr(mod, "provision_hermes_runtime", provision)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_LOST_CLAIM
    provision.assert_not_called()


async def test_a_deterministic_prepare_failure_releases_to_error_and_exits_1(precondition_ok, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock(side_effect=ValueError("bad state")))
    released = AsyncMock(return_value=10)  # scripts.provision_runner.EXIT_DETERMINISTIC
    monkeypatch.setattr(mod, "_release_hermes_failure_to_error", released)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OTHER
    released.assert_awaited_once()


async def test_a_transient_provision_failure_releases_to_error_and_exits_11(precondition_ok, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(side_effect=RuntimeError("throttled")))
    released = AsyncMock(return_value=11)  # scripts.provision_runner.EXIT_TRANSIENT
    monkeypatch.setattr(mod, "_release_hermes_failure_to_error", released)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_TRANSIENT


# ---------------------------------------------------------------------------
# Ruling M6-R12 — ChatGPT OAuth owners
# ---------------------------------------------------------------------------


async def test_chatgpt_oauth_owner_migrates_without_staging_and_removes_the_stale_grant(
    tmp_path, precondition_ok, monkeypatch
):
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None)))
    monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", "")  # no S3 traffic needed for this test

    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(return_value="arn:hermes-1"))
    monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value={"sonnet": "arn:son"}))
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers", AsyncMock(return_value={})
    )
    monkeypatch.setattr("core.services.provisioning_landing.build_provisioned_env", AsyncMock(return_value={}))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pcp_live_x")
    )
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: SimpleNamespace(stop_user_service=AsyncMock()))

    resolve_token_owner = AsyncMock(return_value="user_the_connector")
    revoke_user_oauth = AsyncMock()
    monkeypatch.setattr("core.services.oauth_service.resolve_token_owner", resolve_token_owner)
    monkeypatch.setattr("core.services.oauth_service.revoke_user_oauth", revoke_user_oauth)

    # The module must never even import stage_codex_auth -- it has no such name at all.
    assert not hasattr(mod, "stage_codex_auth")

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK
    resolve_token_owner.assert_awaited_once_with(owner_id=OWNER)
    revoke_user_oauth.assert_awaited_once_with(user_id="user_the_connector")


# ---------------------------------------------------------------------------
# Ruling M6-R3 — rollback
# ---------------------------------------------------------------------------


async def test_rollback_order_and_row_flip(monkeypatch):
    calls: list[str] = []
    row = {"owner_id": OWNER, "agentcore_runtime_arn": "arn:hermes-1", "bedrock_gate_token_hash": "hash-1"}
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(mod, "_stop_runtime_session", lambda *a: calls.append("stop_session"))
    monkeypatch.setattr(mod, "_delete_runtime", lambda *a: calls.append("delete_runtime"))
    monkeypatch.setattr(mod, "_delete_boot_secret", lambda *a: calls.append("delete_secret"))
    delete_hashes = AsyncMock(side_effect=lambda hashes: calls.append("revoke_token"))
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", delete_hashes)

    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-9"))
    leased = AsyncMock(side_effect=lambda *a, **kw: calls.append("flip_row"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", leased)
    ecs = SimpleNamespace(start_user_service=AsyncMock(side_effect=lambda *a, **kw: calls.append("start_ecs")))
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)

    assert await mod.rollback(OWNER) == mod.EXIT_OK

    # Ruling M6-R15: the row flips back to "ecs" BEFORE start_user_service is called, so
    # start_user_service's own _hosted_on_agentcore guard (unforced here) can never bite --
    # the row it reads is already off AgentCore by the time that call happens.
    assert calls == ["stop_session", "delete_runtime", "revoke_token", "delete_secret", "flip_row", "start_ecs"]
    delete_hashes.assert_awaited_once_with(["hash-1"])
    leased.assert_awaited_once()
    _, kwargs_or_fields = leased.await_args.args[0], leased.await_args.args[2]
    assert kwargs_or_fields["runtime_host"] == "ecs"
    assert kwargs_or_fields["agentcore_runtime_arn"] is None
    ecs.start_user_service.assert_awaited_once_with(OWNER, force=True)


async def test_rollback_tolerates_a_missing_runtime_and_secret(monkeypatch):
    """No ARN on the row (e.g. rollback right after prepare, before create) -- nothing to stop
    or delete on the AgentCore side, but the row still flips back and ECS still restarts."""
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER}))
    stop_session = AsyncMock(side_effect=AssertionError("nothing to stop"))
    monkeypatch.setattr(mod, "_stop_runtime_session", stop_session)
    monkeypatch.setattr(mod, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-9"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", AsyncMock())
    ecs = SimpleNamespace(start_user_service=AsyncMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)

    assert await mod.rollback(OWNER) == mod.EXIT_OK
    stop_session.assert_not_called()
    ecs.start_user_service.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 3 — --all
# ---------------------------------------------------------------------------


async def test_all_flag_migrates_every_non_agentcore_non_reference_row(monkeypatch):
    rows = [
        {"owner_id": "a", "runtime_host": "ecs"},
        {"owner_id": "b", "runtime_host": "agentcore"},  # already migrated -- skipped
        {"owner_id": mod.REFERENCE_ORG_ID, "runtime_host": "ecs"},  # reference org -- skipped
    ]
    migrated: list[str] = []

    async def fake_migrate(owner_id, *, dry_run):
        migrated.append(owner_id)
        return mod.EXIT_OK

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", fake_migrate)

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_OK
    assert migrated == ["a"]


async def test_all_flag_skips_pass_admission_rows(monkeypatch):
    rows = [{"owner_id": f"{mod.PASS_KEY_PREFIX}nightly-sweep"}, {"owner_id": "a", "runtime_host": "ecs"}]
    migrated: list[str] = []

    async def fake_migrate(owner_id, *, dry_run):
        migrated.append(owner_id)
        return mod.EXIT_OK

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", fake_migrate)

    await mod.migrate_all(dry_run=False)
    assert migrated == ["a"]


async def test_all_flag_skips_rows_with_no_active_paperclip_company(monkeypatch):
    rows = [{"owner_id": "no-company", "runtime_host": "ecs"}, {"owner_id": "a", "runtime_host": "ecs"}]
    migrated: list[str] = []

    async def fake_migrate(owner_id, *, dry_run):
        migrated.append(owner_id)
        return mod.EXIT_OK

    async def fake_company(owner_id):
        return SimpleNamespace(status="active") if owner_id == "a" else None

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", fake_company)
    monkeypatch.setattr(mod, "migrate", fake_migrate)

    await mod.migrate_all(dry_run=False)
    assert migrated == ["a"]


async def test_all_flag_retries_a_transient_exit_exactly_once(monkeypatch):
    rows = [{"owner_id": "a", "runtime_host": "ecs"}]
    attempts: list[str] = []

    async def flaky_then_ok(owner_id, *, dry_run):
        attempts.append(owner_id)
        return mod.EXIT_TRANSIENT if len(attempts) == 1 else mod.EXIT_OK

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", flaky_then_ok)

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_OK
    assert attempts == ["a", "a"]


async def test_all_flag_reports_a_persistent_transient_failure(monkeypatch):
    rows = [{"owner_id": "a", "runtime_host": "ecs"}]
    attempts: list[str] = []

    async def always_transient(owner_id, *, dry_run):
        attempts.append(owner_id)
        return mod.EXIT_TRANSIENT

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", always_transient)

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_TRANSIENT
    assert attempts == ["a", "a"]  # retried exactly once, not looped forever
