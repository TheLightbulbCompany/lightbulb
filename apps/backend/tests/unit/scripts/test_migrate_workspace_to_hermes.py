"""M6 lane R1 — scripts/migrate_workspace_to_hermes.py.

No live AWS/EFS/Paperclip anywhere here: EFS is a real ``tmp_path`` tree behind a
``Workspace`` the tests construct themselves (never the process-wide ``get_workspace()``
singleton — see ``_workspace_at`` below for why), and every AWS/Paperclip call is a
monkeypatched stand-in.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.containers.workspace import Workspace
from core.repositories.container_repo import ProvisionSuperseded
from core.services.hermes_provisioning import ProfileInputs
from scripts import migrate_workspace_to_hermes as mod

OWNER = "org_test1"
CEO_ID = "11111111-1111-1111-1111-111111111111"
ENG_ID = "22222222-2222-2222-2222-222222222222"
ROSTER = [
    {"id": CEO_ID, "role": "ceo", "name": "Ada"},
    {"id": ENG_ID, "role": "engineering", "name": "Bea"},
]

#: A canned ``resolve_profile_inputs`` result -- Ruling M6-R18 adopted that shared resolver in
#: _apply_efs_overrides, so any test exercising it needs a stand-in rather than the real
#: (network-touching) resolution chain.
_PROFILE_INPUTS = ProfileInputs(
    provider_choice="bedrock_claude",
    byo_provider=None,
    byo_api_key=None,
    aip_arns={"sonnet": "arn:son"},
    integrations={},
    env_vars={},
)


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
def efs_ready(tmp_path, monkeypatch):
    """Ruling M6-R20(a): the EFS-presence precondition needs ``get_workspace().user_path(owner)``
    to resolve to a real, EXISTING directory -- on a test runner the untouched default
    (``/mnt/efs/users``) never exists, so every ``migrate()`` call would otherwise fail this
    check first, before reaching whatever the test actually means to exercise. Most
    precondition-path tests don't care about the directory's CONTENT (see ``_fake_workspace``
    for tests that do)."""
    (tmp_path / OWNER).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    return tmp_path


@pytest.fixture
def settings_ok(monkeypatch):
    """Ruling M6-R20(b): the two settings ``provision_hermes_runtime`` hard-requires."""
    monkeypatch.setattr(mod.settings, "AGENTCORE_HERMES_IMAGE", "hermes:test")
    monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", "test-hermes-bucket")


@pytest.fixture
def precondition_ok(efs_ready, settings_ok, monkeypatch):
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
        "core.services.hermes_provisioning.resolve_profile_inputs",
        AsyncMock(return_value=_PROFILE_INPUTS),
    )
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pcp_live_x")
    )

    ecs = SimpleNamespace(stop_user_service=AsyncMock(side_effect=lambda *a, **kw: calls.append("stop")))
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)

    leased = AsyncMock(side_effect=lambda *a, **kw: calls.append("stamp"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", leased)

    session_stop = MagicMock(side_effect=lambda *a: calls.append("session_stop"))
    monkeypatch.setattr(mod, "_stop_runtime_session", session_stop)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK

    # Ordering (Ruling M6-R1/M6-R16/M6-R18): provision only after prepare, the session cold-stop
    # only after the last override upload (already done by the time "provision" -> the tail
    # starts), ECS stop only after the session stop, the hermes_migrated_at stamp only after that.
    assert calls == ["prepare", "provision", "session_stop", "stop", "stamp"]
    prepare.assert_awaited_once_with(OWNER, claim_id="claim-1", attempt_token="claim-1", migrate=True)
    provision.assert_awaited_once_with(OWNER, claim_id="claim-1", attempt_token="claim-1", migrate=True)
    session_stop.assert_called_once_with("arn:hermes-1", mod.runtime_transport.session_id_for(OWNER))
    # force=True (Ruling M6-R15): bypasses the AgentCore no-op guard -- the row already reads
    # runtime_host=agentcore by the time this call happens.
    ecs.stop_user_service.assert_awaited_once_with(OWNER, reason="hermes_migration", force=True)

    # Ruling M6-R16: the LAST fenced write stamps hermes_migrated_at and re-asserts
    # status="running" (correcting stop_user_service's own status="stopped" row write).
    leased.assert_awaited_once()
    stamped_fields = leased.await_args.args[2]
    assert stamped_fields["hermes_migrated_at"]
    # Ruling M6-R17: status is NOT re-asserted here -- stop_user_service(force=True) never
    # touches the row any more, so status is exactly what provision_hermes_runtime's own
    # terminal write already set ("running"), untouched since.
    assert "status" not in stamped_fields

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


async def test_session_stop_failure_does_not_abort_the_tail(tmp_path, precondition_ok, monkeypatch):
    """Fix round 2, item 1: StopRuntimeSession is best-effort, exactly like rollback's own use of
    it -- a fresh full-mode runtime usually has NO warm session yet, so an unknown-session raise
    here must not abort before the ECS stop and the stamp (which would leave the row flipped with
    ECS still billing, and --all retrying this owner forever)."""
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", "test-hermes-bucket")

    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(return_value="arn:hermes-1"))
    monkeypatch.setattr(mod, "_apply_efs_overrides", AsyncMock())
    monkeypatch.setattr(mod, "_stop_runtime_session", MagicMock(side_effect=RuntimeError("unknown session")))
    ecs = SimpleNamespace(stop_user_service=AsyncMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)
    stamp = AsyncMock()
    monkeypatch.setattr(mod, "_stamp_migrated", stamp)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK
    ecs.stop_user_service.assert_awaited_once_with(OWNER, reason="hermes_migration", force=True)
    stamp.assert_awaited_once_with(OWNER)


async def test_reference_org_is_refused_outright(monkeypatch):
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(side_effect=AssertionError("must not even read the row"))
    )
    assert await mod.migrate(mod.REFERENCE_ORG_ID, dry_run=False) == mod.EXIT_REFERENCE_ORG


@pytest.mark.parametrize(
    "row,company,roster",
    [
        (None, SimpleNamespace(status="active"), ROSTER),
        # Ruling M6-R16: a STAMPED row -- already migrated, full stop -- not merely "on agentcore"
        # (see test_an_unstamped_agentcore_row_runs_only_the_tail for that case, which is NOT a
        # precondition failure any more).
        (
            {"owner_id": OWNER, "runtime_host": "agentcore", "hermes_migrated_at": "2026-09-17T00:00:00Z"},
            SimpleNamespace(status="active"),
            ROSTER,
        ),
        ({"owner_id": OWNER, "runtime_host": "ecs"}, None, ROSTER),
        ({"owner_id": OWNER, "runtime_host": "ecs"}, SimpleNamespace(status="seeding"), ROSTER),
        ({"owner_id": OWNER, "runtime_host": "ecs"}, SimpleNamespace(status="active"), []),
    ],
)
async def test_precondition_failures_exit_2_and_claim_nothing(
    efs_ready, settings_ok, monkeypatch, row, company, roster
):
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=company))
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(
        mod.container_repo, "takeover_provision", AsyncMock(side_effect=AssertionError("must not claim"))
    )
    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_PRECONDITION


async def test_missing_efs_workspace_directory_exits_precondition(settings_ok, tmp_path, monkeypatch):
    """Ruling M6-R20(a): an operator laptop with no /mnt/efs mount must never "migrate" with
    efs_files=0 -- get_workspace().user_path(owner).is_dir() being False refuses outright."""
    # tmp_path itself exists, but the owner subdirectory is never created -- exactly what a
    # missing EFS mount looks like from this job's point of view.
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(side_effect=AssertionError("must not read the row"))
    )
    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_PRECONDITION


@pytest.mark.parametrize(
    "image,bucket",
    [
        ("", "test-hermes-bucket"),
        ("hermes:test", ""),
        ("", ""),
    ],
)
async def test_missing_required_settings_exits_precondition(efs_ready, monkeypatch, caplog, image, bucket):
    """Ruling M6-R20(b): AGENTCORE_HERMES_IMAGE and OWNER_STATE_BUCKET are checked ONCE before
    any row is read (before get_by_owner_id, per efs_ready/this test's own row guard) -- an
    unset value refuses instantly, by name, rather than --all sweeping the whole fleet into
    provision_hermes_runtime's own less friendly RuntimeError one owner at a time."""
    monkeypatch.setattr(mod.settings, "AGENTCORE_HERMES_IMAGE", image)
    monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", bucket)
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(side_effect=AssertionError("must not read the row"))
    )
    with caplog.at_level("ERROR"):
        assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_PRECONDITION
    if not image:
        assert "AGENTCORE_HERMES_IMAGE" in caplog.text
    if not bucket:
        assert "OWNER_STATE_BUCKET" in caplog.text


def test_rollback_and_all_is_rejected_at_argument_parsing(monkeypatch, capsys):
    """Ruling M6-R20(d): --rollback --all is nonsensical (rollback takes one owner) and must be
    caught with its OWN message -- not conflated with the pre-existing "--rollback requires
    --owner-id" guard, which a bare --rollback --all would also trip (--owner-id is absent
    either way, since it is mutually exclusive with --all at the argparse level)."""
    monkeypatch.setattr(sys, "argv", ["migrate_workspace_to_hermes.py", "--rollback", "--all"])
    with pytest.raises(SystemExit):
        mod.main()
    # The usage banner always lists --all regardless of which check fired, so assert on the
    # specific combination message rather than mere substring presence of an option name.
    assert "cannot be combined with --all" in capsys.readouterr().err


async def test_a_natively_provisioned_hermes_row_is_refused_not_tailed(efs_ready, settings_ok, monkeypatch):
    """Ruling M6-R19: a row on agentcore with NO service_name never went through ECS at all --
    the fresh-provision path (settings.HERMES_RUNTIME) never touches the ECS manager. Running
    the tail on it would regenerate its live profiles over themselves for nothing, so it is
    refused (exit 2), same as any other precondition failure, and never claimed."""
    monkeypatch.setattr(
        mod.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": OWNER, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:native"}),
    )
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(
        mod.container_repo, "takeover_provision", AsyncMock(side_effect=AssertionError("must not claim"))
    )
    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_PRECONDITION


async def test_an_unstamped_agentcore_row_runs_only_the_tail(settings_ok, tmp_path, monkeypatch):
    """Ruling M6-R16: a row already on agentcore but with no hermes_migrated_at stamp is a prior
    attempt that crashed after READY but before its tail finished. Re-running must NOT call
    prepare_hermes_workspace or provision_hermes_runtime again -- only the EFS-override pass,
    the forced ECS stop, and the stamp."""
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(
        mod.container_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": OWNER,
                "runtime_host": "agentcore",
                "agentcore_runtime_arn": "arn:x",
                # Ruling M6-R19: tail mode requires ECS provenance -- service_name proves this
                # row went through the ECS-migration path, not a native fresh Hermes provision.
                "service_name": "openclaw-org_test1-abc123",
            }
        ),
    )
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    prepare = AsyncMock(side_effect=AssertionError("must not re-prepare"))
    provision = AsyncMock(side_effect=AssertionError("must not re-provision"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", prepare)
    monkeypatch.setattr(mod, "provision_hermes_runtime", provision)
    monkeypatch.setattr(
        mod.container_repo, "takeover_provision", AsyncMock(side_effect=AssertionError("must not claim the lease"))
    )

    monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value={"sonnet": "arn:son"}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_PROFILE_INPUTS)
    )
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pcp_live_x")
    )
    monkeypatch.setattr(mod, "_s3_client", lambda: _FakeS3Client([]))
    monkeypatch.setattr("core.services.hermes_profiles._new_s3_client", lambda: _FakeS3Client([]))
    ecs = SimpleNamespace(stop_user_service=AsyncMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)

    session_stop = MagicMock()
    monkeypatch.setattr(mod, "_stop_runtime_session", session_stop)

    stamp = AsyncMock()
    monkeypatch.setattr(mod, "_stamp_migrated", stamp)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK
    prepare.assert_not_called()
    provision.assert_not_called()
    # Ruling M6-R18: the tail-mode ARN comes straight off the row _check_preconditions already
    # read (no second lookup) -- "arn:x" here, not provision_hermes_runtime's return (unreachable
    # in this test, since provision is never called).
    session_stop.assert_called_once_with("arn:x", mod.runtime_transport.session_id_for(OWNER))
    ecs.stop_user_service.assert_awaited_once_with(OWNER, reason="hermes_migration", force=True)
    stamp.assert_awaited_once_with(OWNER)


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


async def test_a_deterministic_prepare_failure_releases_running_not_error(precondition_ok, monkeypatch, tmp_path):
    """Ruling M6-R22: a prepare/provision failure happens BEFORE the row flips to agentcore --
    the ECS container is still up and serving. The row must come back status="running" (never
    "error"), with the failure recorded on hermes_migrate_error/hermes_migrate_exit_code, and
    _release_hermes_failure_to_error (which sets status="error") must never be called for this."""
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock(side_effect=ValueError("bad state")))
    leased = AsyncMock()
    monkeypatch.setattr(mod.container_repo, "update_container_leased", leased)
    released_to_error = AsyncMock(side_effect=AssertionError("must not release to status=error pre-flip"))
    monkeypatch.setattr("scripts.provision_runner._release_hermes_failure_to_error", released_to_error)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OTHER
    released_to_error.assert_not_called()
    leased.assert_awaited_once()
    fields = leased.await_args.args[2]
    assert fields["status"] == "running"
    assert "bad state" in fields["hermes_migrate_error"]
    assert fields["hermes_migrate_exit_code"] == mod.EXIT_OTHER


async def test_a_transient_provision_failure_releases_running_and_retries_without_double_create(
    precondition_ok, monkeypatch, tmp_path
):
    """Ruling M6-R22, both halves: (1) the pre-flip release restores status="running"; (2) a
    retried migrate() call for the same owner does not re-run anything already released -- it
    just re-claims and calls provision_hermes_runtime exactly once more (no double runtime)."""
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    leased = AsyncMock()
    monkeypatch.setattr(mod.container_repo, "update_container_leased", leased)

    attempts: list[str] = []

    async def flaky_then_ok(owner_id, *, claim_id, attempt_token, migrate):
        attempts.append(owner_id)
        if len(attempts) == 1:
            raise RuntimeError("throttled")
        return "arn:hermes-1"

    monkeypatch.setattr(mod, "provision_hermes_runtime", flaky_then_ok)

    # Attempt 1: transient failure, pre-flip.
    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_TRANSIENT
    fields = leased.await_args.args[2]
    assert fields["status"] == "running"
    assert fields["hermes_migrate_exit_code"] == mod.EXIT_TRANSIENT

    # Attempt 2 (retry): provision_hermes_runtime succeeds -- called exactly once more (twice
    # total across both attempts), never twice within the same attempt.
    monkeypatch.setattr(mod, "_apply_efs_overrides", AsyncMock())
    monkeypatch.setattr(mod, "_stamp_migrated", AsyncMock())
    monkeypatch.setattr(mod, "_stop_runtime_session", MagicMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: SimpleNamespace(stop_user_service=AsyncMock()))
    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK
    assert len(attempts) == 2


async def test_pre_flip_release_tolerates_a_concurrent_takeover(precondition_ok, monkeypatch, tmp_path):
    """A successor (a deliberate reprovision, an admin action) can take the row's lease between
    this run's own claim and its failure -- the release attempt then raises ProvisionSuperseded,
    which must be swallowed (the successor owns the row now), not propagated."""
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(
        mod.container_repo, "update_container_leased", AsyncMock(side_effect=ProvisionSuperseded("taken"))
    )

    # Must not raise.
    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_TRANSIENT


async def test_post_flip_failure_leaves_the_row_untouched_for_tail_reentry(precondition_ok, monkeypatch, tmp_path):
    """Ruling M6-R22: a failure AFTER the flip (inside the tail) must not release/flip the row at
    all -- status is exactly whatever provision_hermes_runtime's terminal write left it as, and
    the NEXT migrate() call for this owner re-enters via _MODE_TAIL (Ruling M6-R16)."""
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(return_value="arn:hermes-1"))
    leased = AsyncMock(side_effect=AssertionError("must not touch the row on a post-flip failure"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", leased)
    monkeypatch.setattr(mod, "_apply_efs_overrides", AsyncMock(side_effect=RuntimeError("efs upload failed")))

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_TRANSIENT
    leased.assert_not_called()


# ---------------------------------------------------------------------------
# Ruling M6-R12 — ChatGPT OAuth owners
# ---------------------------------------------------------------------------


async def test_chatgpt_oauth_owner_migrates_without_staging_and_removes_the_stale_grant(
    tmp_path, precondition_ok, monkeypatch
):
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None)))
    monkeypatch.setattr(mod, "_s3_client", lambda: _FakeS3Client([]))
    monkeypatch.setattr("core.services.hermes_profiles._new_s3_client", lambda: _FakeS3Client([]))

    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", AsyncMock())
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(return_value="arn:hermes-1"))
    monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value={"sonnet": "arn:son"}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_PROFILE_INPUTS)
    )
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pcp_live_x")
    )
    monkeypatch.setattr(mod, "_stop_runtime_session", MagicMock())
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
    # Ruling M6-R16: the stamp is cleared with the flip -- a rolled-back owner is not "already
    # migrated" any more.
    assert kwargs_or_fields["hermes_migrated_at"] is None
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


async def test_all_flag_migrates_every_non_reference_row_and_includes_unstamped_agentcore(monkeypatch):
    """Ruling M6-R16: an unstamped, TAIL-ELIGIBLE agentcore row (a crashed prior attempt's tail)
    is INCLUDED -- migrate() on it just runs the tail. A STAMPED row and a natively provisioned
    Hermes row (Ruling M6-R19 -- agentcore with no service_name/ARN) are both skipped."""
    rows = [
        {"owner_id": "a", "runtime_host": "ecs"},
        # unstamped but tail-eligible (service_name + ARN) -- tail-only, INCLUDED
        {"owner_id": "b", "runtime_host": "agentcore", "service_name": "svc-b", "agentcore_runtime_arn": "arn:b"},
        {"owner_id": "c", "runtime_host": "agentcore", "hermes_migrated_at": "2026-09-17T00:00:00Z"},  # skipped
        {"owner_id": "d", "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:d"},  # native -- skipped
        {"owner_id": mod.REFERENCE_ORG_ID, "runtime_host": "ecs"},  # reference org -- skipped
    ]
    migrated: list[str] = []

    async def fake_migrate(owner_id, *, dry_run, _company=None):
        migrated.append(owner_id)
        return mod.EXIT_OK

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", fake_migrate)
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_OK
    assert migrated == ["a", "b"]


async def test_all_flag_skips_pass_admission_rows(monkeypatch):
    rows = [{"owner_id": f"{mod.PASS_KEY_PREFIX}nightly-sweep"}, {"owner_id": "a", "runtime_host": "ecs"}]
    migrated: list[str] = []

    async def fake_migrate(owner_id, *, dry_run, _company=None):
        migrated.append(owner_id)
        return mod.EXIT_OK

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", fake_migrate)
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    await mod.migrate_all(dry_run=False)
    assert migrated == ["a"]


async def test_all_flag_skips_rows_with_no_active_paperclip_company(monkeypatch):
    rows = [{"owner_id": "no-company", "runtime_host": "ecs"}, {"owner_id": "a", "runtime_host": "ecs"}]
    migrated: list[str] = []

    async def fake_migrate(owner_id, *, dry_run, _company=None):
        migrated.append(owner_id)
        return mod.EXIT_OK

    async def fake_company(owner_id):
        return SimpleNamespace(status="active") if owner_id == "a" else None

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", fake_company)
    monkeypatch.setattr(mod, "migrate", fake_migrate)
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    await mod.migrate_all(dry_run=False)
    assert migrated == ["a"]


async def test_all_flag_retries_a_transient_exit_exactly_once(monkeypatch):
    rows = [{"owner_id": "a", "runtime_host": "ecs"}]
    attempts: list[str] = []

    async def flaky_then_ok(owner_id, *, dry_run, _company=None):
        attempts.append(owner_id)
        return mod.EXIT_TRANSIENT if len(attempts) == 1 else mod.EXIT_OK

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", flaky_then_ok)
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_OK
    assert attempts == ["a", "a"]


async def test_all_flag_reports_a_persistent_transient_failure(monkeypatch):
    rows = [{"owner_id": "a", "runtime_host": "ecs"}]
    attempts: list[str] = []

    async def always_transient(owner_id, *, dry_run, _company=None):
        attempts.append(owner_id)
        return mod.EXIT_TRANSIENT

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", always_transient)
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_TRANSIENT
    assert attempts == ["a", "a"]  # retried exactly once, not looped forever


# ---------------------------------------------------------------------------
# Fix round 1 minors
# ---------------------------------------------------------------------------


async def test_rollback_dry_run_performs_no_rollback(monkeypatch, capsys):
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(side_effect=AssertionError("dry-run must not read the row"))
    )
    monkeypatch.setattr(
        mod.container_repo, "takeover_provision", AsyncMock(side_effect=AssertionError("dry-run must not claim"))
    )
    ecs = SimpleNamespace(start_user_service=AsyncMock(side_effect=AssertionError("dry-run must not start ECS")))
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)

    assert await mod.rollback(OWNER, dry_run=True) == mod.EXIT_OK
    assert "DRY RUN" in capsys.readouterr().out


async def test_all_flag_summary_includes_reconnect_chatgpt(monkeypatch, capsys):
    rows = [{"owner_id": "a", "runtime_host": "ecs"}]
    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", AsyncMock(return_value=mod.EXIT_OK))
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None)))

    await mod.migrate_all(dry_run=False)
    assert "reconnect_chatgpt" in capsys.readouterr().out


@pytest.mark.parametrize(
    "codes,expected_attr",
    [
        (["EXIT_OK", "EXIT_OTHER", "EXIT_TRANSIENT"], "EXIT_OTHER"),  # 1 wins over 11
        (["EXIT_OK", "EXIT_TRANSIENT"], "EXIT_TRANSIENT"),
        (["EXIT_OK", "EXIT_PRECONDITION", "EXIT_LOST_CLAIM"], "EXIT_OK"),  # neither 1 nor 11 -> 0, never max()
    ],
)
async def test_all_flag_exit_code_prefers_1_then_11_never_max(monkeypatch, codes, expected_attr):
    rows = [{"owner_id": f"o{i}", "runtime_host": "ecs"} for i in range(len(codes))]
    code_by_owner = {f"o{i}": getattr(mod, name) for i, name in enumerate(codes)}

    async def fake_migrate(owner_id, *, dry_run, _company=None):
        return code_by_owner[owner_id]

    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=SimpleNamespace(status="active")))
    monkeypatch.setattr(mod, "migrate", fake_migrate)
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    assert await mod.migrate_all(dry_run=False) == getattr(mod, expected_attr)


async def test_check_preconditions_uses_the_passed_in_company_without_a_second_read(
    efs_ready, settings_ok, monkeypatch
):
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER, "runtime_host": "ecs"})
    )
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    load_company = AsyncMock(side_effect=AssertionError("must not read company -- one was passed in"))
    monkeypatch.setattr(mod, "_load_company", load_company)

    exit_code, _mode, _roster, _row = await mod._check_preconditions(OWNER, company=SimpleNamespace(status="active"))
    assert exit_code is None
    load_company.assert_not_called()


async def test_all_flag_passes_the_loaded_company_into_migrate(monkeypatch):
    rows = [{"owner_id": "a", "runtime_host": "ecs"}]
    company = SimpleNamespace(status="active")
    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    monkeypatch.setattr(mod, "_load_company", AsyncMock(return_value=company))
    migrate = AsyncMock(return_value=mod.EXIT_OK)
    monkeypatch.setattr(mod, "migrate", migrate)
    monkeypatch.setattr(mod, "resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None)))

    await mod.migrate_all(dry_run=False)
    migrate.assert_awaited_once_with("a", dry_run=False, _company=company)


async def test_a_provision_superseded_from_the_stamp_maps_to_lost_claim(precondition_ok, monkeypatch, tmp_path):
    """A concurrent takeover racing _stamp_migrated's own fenced write is a genuine lost claim
    (exit 10), not a generic transient failure (11) -- classify_provision_error has no special
    case for ProvisionSuperseded, so it must be caught explicitly."""
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(return_value="arn:hermes-1"))
    monkeypatch.setattr(mod, "_apply_efs_overrides", AsyncMock())
    monkeypatch.setattr(mod, "_stop_runtime_session", MagicMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: SimpleNamespace(stop_user_service=AsyncMock()))
    monkeypatch.setattr(mod, "_stamp_migrated", AsyncMock(side_effect=ProvisionSuperseded("taken")))

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_LOST_CLAIM
