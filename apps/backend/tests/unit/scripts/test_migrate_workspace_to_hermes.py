"""M6 lane R1 — scripts/migrate_workspace_to_hermes.py.

No live AWS/EFS/Paperclip anywhere here: EFS is a real ``tmp_path`` tree behind a
``Workspace`` the tests construct themselves (never the process-wide ``get_workspace()``
singleton — see ``_workspace_at`` below for why), and every AWS/Paperclip call is a
monkeypatched stand-in.
"""

from __future__ import annotations

import sys
from pathlib import Path
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
    """Records every ``put_object`` call, in order, as ``(bucket, key)`` -- and, when given an
    ``order_sink`` (fix round 2, item 4), also appends a compact ``"upload:<key>"`` marker there
    so a test can interleave upload order with other lifecycle calls recorded in that SAME sink
    (locking "session stop after the LAST override upload", not merely "session stop happened
    somewhere after prepare/provision")."""

    def __init__(self, sink: list[tuple[str, str]], order_sink: list[str] | None = None):
        self._sink = sink
        self._order_sink = order_sink

    def put_object(self, *, Bucket, Key, Body):
        self._sink.append((Bucket, Key))
        if self._order_sink is not None:
            self._order_sink.append(f"upload:{Key}")


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


@pytest.fixture(autouse=True)
def adapter_repoint_stub(monkeypatch):
    """Task-15 item 1 added a Paperclip adapter re-point to BOTH legs (the tail and rollback),
    and it talks to Paperclip over HTTP. Stub it everywhere by default so no pre-existing test
    grows a network dependency; the tests that actually exercise it re-patch it (or patch one
    level down, at ``repoint_agent_adapter``) themselves."""
    real = mod._repoint_paperclip_adapters
    monkeypatch.setattr(mod, "_repoint_paperclip_adapters", AsyncMock())
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    return real


@pytest.fixture(autouse=True)
def reproject_stub(monkeypatch):
    """The tail re-projects the owner's connected integrations, which reads the integrations
    table and Secrets Manager. Stub it everywhere by default — same reasoning (and same
    re-patching escape hatch) as ``adapter_repoint_stub`` above."""
    monkeypatch.setattr(mod, "reproject_connections", AsyncMock(return_value={}))


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
# Ruling M6-R47 — carry plan no longer copies MEMORY.md/memory/** verbatim
# ---------------------------------------------------------------------------


def test_collect_carry_files_drops_raw_memory_and_carries_built_memories(tmp_path):
    root = _fake_workspace(tmp_path)
    files = mod._collect_carry_files(root / f"workspace-{CEO_ID}")

    assert "MEMORY.md" not in files
    assert not any(k == "memory" or k.startswith("memory/") for k in files)

    assert files["AGENTS.md"] == root / f"workspace-{CEO_ID}" / "AGENTS.md"
    assert isinstance(files["AGENTS.md"], Path)
    assert any(k.startswith("skills/") for k in files)

    # build_memory_files' output (bytes, built in-process -- no single EFS source file) is
    # merged straight into the same carry-file mapping the upload loop reads.
    assert isinstance(files["memories/MEMORY.md"], bytes)
    memory_text = files["memories/MEMORY.md"].decode("utf-8")
    assert "remembered things" in memory_text
    assert "daily note" in memory_text


def test_collect_carry_files_refuses_reproducible_subtrees_inside_a_skill(tmp_path):
    """Fix round 1 (task-21 B1): the skills walk had no exclusion list at all, so a skill that
    vendors node_modules/ or ships a built dist/ was uploaded file-by-file into the profile
    prefix and re-downloaded on every cold start forever. The bridge's save_profile refuses the
    same names, but nothing the MIGRATION writes goes through it."""
    root = _fake_workspace(tmp_path)
    skill = root / f"workspace-{CEO_ID}" / "skills" / "example"
    for rel in (
        "node_modules/left-pad/index.js",
        "dist/bundle.js",
        "__pycache__/m.pyc",
        ".venv/bin/python",
        "coverage/lcov.info",
    ):
        path = skill / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("junk")
    (skill / "reference.md").write_text("# keep me")

    files = mod._collect_carry_files(root / f"workspace-{CEO_ID}")

    assert "skills/example/SKILL.md" in files
    assert "skills/example/reference.md" in files
    assert not [k for k in files if any(part in mod._SKIP_CARRY_DIR_NAMES for part in k.split("/"))]


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

    monkeypatch.setattr(mod, "_s3_client", lambda: _FakeS3Client(uploaded_keys, order_sink=calls))
    monkeypatch.setattr(
        "core.services.hermes_profiles._new_s3_client", lambda: _FakeS3Client(uploaded_keys, order_sink=calls)
    )

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

    # Ordering (Ruling M6-R1/M6-R16/M6-R18, fix round 2 item 4): provision only after prepare;
    # every override upload happens after provision and before the session cold-stop; the
    # session cold-stop happens strictly after the LAST override upload (not merely "after
    # prepare/provision somewhere") and before the ECS stop; the hermes_migrated_at stamp is
    # last of all. Uploads and lifecycle calls share ONE sink so this ordering can't be
    # satisfied by coincidence.
    assert calls[0] == "prepare"
    assert calls[1] == "provision"
    upload_indices = [i for i, c in enumerate(calls) if c.startswith("upload:")]
    assert upload_indices, "expected at least one profile upload"
    assert max(upload_indices) < calls.index("session_stop") < calls.index("stop") < calls.index("stamp")
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
    # Fix round 2, item 3: a successful stamp also clears any earlier hermes_migrate_error/
    # hermes_migrate_exit_code left by a previous pre-flip failure (Ruling M6-R22).
    assert stamped_fields["hermes_migrate_error"] is None
    assert stamped_fields["hermes_migrate_exit_code"] is None

    # Every agent got a profile prefix, the SOUL.md override, and the carried EFS content --
    # each agent's .revision write is the LAST key for that agent.
    ceo_keys = [k for b, k in uploaded_keys if k.startswith(f"{OWNER}/hermes/{CEO_ID}/")]
    eng_keys = [k for b, k in uploaded_keys if k.startswith(f"{OWNER}/hermes/{ENG_ID}/")]
    for keys in (ceo_keys, eng_keys):
        assert keys, "expected at least one uploaded key per agent"
        assert keys[-1].endswith(".revision")
        assert any(k.endswith("SOUL.md") for k in keys)
        assert any(k.endswith("AGENTS.md") for k in keys)
        # Ruling M6-R47: MEMORY.md + memory/*.md daily notes are merged into memories/MEMORY.md
        # (not a verbatim raw-dir copy) by the vendored Hermes translator -- see
        # test_hermes_memory_migration.py for the wrapper that drives it (the merge algorithm
        # itself is upstream code and is deliberately not re-tested in this repo).
        assert any(k.endswith("memories/MEMORY.md") for k in keys)
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


# ---------------------------------------------------------------------------
# M6 task-15 item 1 — the Paperclip adapter re-point (task-14 row 1)
# ---------------------------------------------------------------------------


def _tail_only(monkeypatch, tmp_path, *, repoint, order=None):
    """Everything in the tail except the step under test, stubbed. ``order`` (optional) is the
    ONE sink the lifecycle stubs record into, so an ordering assertion can't be satisfied by
    coincidence."""
    _fake_workspace(tmp_path)
    note = (lambda name: lambda *a, **kw: order.append(name)) if order is not None else (lambda name: None)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(return_value="arn:hermes-1"))
    monkeypatch.setattr(mod, "_apply_efs_overrides", AsyncMock(side_effect=note("overrides")))
    monkeypatch.setattr(mod, "_stop_runtime_session", MagicMock(side_effect=note("session_stop")))
    monkeypatch.setattr(mod, "_repoint_paperclip_adapters", repoint)
    ecs = SimpleNamespace(stop_user_service=AsyncMock(side_effect=note("stop")))
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)
    stamp = AsyncMock(side_effect=note("stamp"))
    monkeypatch.setattr(mod, "_stamp_migrated", stamp)
    return ecs, stamp


async def test_migrate_repoints_every_roster_adapter_before_stopping_ecs(tmp_path, precondition_ok, monkeypatch):
    """Without this step a migrated workspace's agents keep an ``openclaw_gateway`` adapter
    dialling a dead operator socket: every routine, issue run and Teams Run/Retry dies while room
    chat keeps working.

    Position is the contract: AFTER the profile overrides + the cold-session stop (so the first
    Paperclip run the re-point makes possible cold-restores the right profile) and BEFORE the ECS
    stop (so a failure leaves the old gateway serving the old adapters)."""
    order: list[str] = []
    repoint = AsyncMock(side_effect=lambda *a, **kw: order.append("repoint"))
    _ecs, stamp = _tail_only(monkeypatch, tmp_path, repoint=repoint, order=order)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK

    repoint.assert_awaited_once_with(OWNER, ROSTER, runtime_host="agentcore")
    assert order == ["overrides", "session_stop", "repoint", "stop", "stamp"]
    stamp.assert_awaited_once()


async def test_a_failed_repoint_aborts_the_tail_and_never_stamps(tmp_path, precondition_ok, monkeypatch):
    """ "Fail loudly rather than flip the row": a workspace whose agents could not be re-pointed
    must not end up stamped ``hermes_migrated_at`` (which would make every later run skip it) nor
    have its ECS service stopped."""
    ecs, stamp = _tail_only(monkeypatch, tmp_path, repoint=AsyncMock(side_effect=RuntimeError("paperclip 500")))

    # Non-zero (the exact code is classify_provision_error's business, not this test's) and,
    # critically, nothing after the re-point ran.
    assert await mod.migrate(OWNER, dry_run=False) != mod.EXIT_OK

    ecs.stop_user_service.assert_not_awaited()
    stamp.assert_not_awaited()


async def test_repoint_passes_the_whole_roster_and_counts_only_real_patches(monkeypatch, adapter_repoint_stub):
    """``_repoint_paperclip_adapters`` is a thin loop over the shared per-agent helper — the one
    thing it owns is that EVERY roster agent is offered, with the explicit runtime_host (never a
    row read that could be mid-flip)."""
    seen = []

    async def _fake_repoint(owner_id, agent, *, runtime_host, gateway_token=None):
        seen.append((owner_id, agent["id"], runtime_host, gateway_token))
        return agent["id"] == CEO_ID  # the other one was already on the target adapter

    monkeypatch.setattr("core.services.paperclip_agent_lifecycle.repoint_agent_adapter", _fake_repoint)

    await adapter_repoint_stub(OWNER, ROSTER, runtime_host="agentcore")

    assert seen == [(OWNER, CEO_ID, "agentcore", None), (OWNER, ENG_ID, "agentcore", None)]


async def test_repoint_on_the_ecs_leg_carries_the_container_gateway_token(monkeypatch, adapter_repoint_stub):
    """Rolling BACK synthesizes a fresh ``openclaw_gateway`` adapter whose ``authToken`` is the
    service JWT, which the live gateway rejects — the per-agent bind needs the container's own
    gateway token, so the ECS leg (and only it) reads the row for one."""
    seen = []

    async def _fake_repoint(owner_id, agent, *, runtime_host, gateway_token=None):
        seen.append(gateway_token)
        return True

    monkeypatch.setattr("core.services.paperclip_agent_lifecycle.repoint_agent_adapter", _fake_repoint)
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "gw-tok"}))

    await adapter_repoint_stub(OWNER, ROSTER, runtime_host="ecs")

    assert seen == ["gw-tok", "gw-tok"]


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
    """F5 (predeploy review, M6-R38): the AgentCore-side teardown itself moved to
    hermes_provisioning.teardown_hermes_runtime (its own dedicated tests cover the AWS call
    ordering) -- this test now proves rollback's OWN contract: teardown runs with
    delete_profiles=False and BEFORE the row flips back to ECS and the service restarts."""
    calls: list[str] = []
    row = {"owner_id": OWNER, "agentcore_runtime_arn": "arn:hermes-1", "bedrock_gate_token_hash": "hash-1"}
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value=row))

    async def _fake_teardown(owner_id, *, container_row, delete_profiles):
        assert owner_id == OWNER
        assert container_row == row
        assert delete_profiles is False
        calls.append("teardown")

    monkeypatch.setattr(mod, "teardown_hermes_runtime", _fake_teardown)

    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-9"))
    leased = AsyncMock(side_effect=lambda *a, **kw: calls.append("flip_row"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", leased)
    ecs = SimpleNamespace(start_user_service=AsyncMock(side_effect=lambda *a, **kw: calls.append("start_ecs")))
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)

    assert await mod.rollback(OWNER) == mod.EXIT_OK

    # Ruling M6-R15: the row flips back to "ecs" BEFORE start_user_service is called, so
    # start_user_service's own _hosted_on_agentcore guard (unforced here) can never bite --
    # the row it reads is already off AgentCore by the time that call happens.
    assert calls == ["teardown", "flip_row", "start_ecs"]
    # Task-15 item 1: the adapters migrate() moved onto `http` point at a bridge this rollback
    # just tore down, so they go back to openclaw_gateway — last, once the row reads "ecs" and
    # the service is coming up.
    mod._repoint_paperclip_adapters.assert_awaited_once_with(OWNER, ROSTER, runtime_host="ecs")
    leased.assert_awaited_once()
    _, kwargs_or_fields = leased.await_args.args[0], leased.await_args.args[2]
    assert kwargs_or_fields["runtime_host"] == "ecs"
    assert kwargs_or_fields["agentcore_runtime_arn"] is None
    # Ruling M6-R16: the stamp is cleared with the flip -- a rolled-back owner is not "already
    # migrated" any more.
    assert kwargs_or_fields["hermes_migrated_at"] is None
    ecs.start_user_service.assert_awaited_once_with(OWNER, force=True)


async def test_rollback_restores_the_surviving_ecs_gate_token_generation(monkeypatch):
    """Minor (predeploy review, M6-R39 housekeeping): prepare_hermes_workspace's migrate-time
    mint_gate_token moved the ECS task's own live bearer into bedrock_gate_superseded_hashes
    (appended last) when it minted the Hermes-only one. Rollback must point
    bedrock_gate_token_hash back at it -- not leave it on the hash teardown just revoked, with
    the real live bearer reachable only through the superseded-hash retention drain."""
    row = {
        "owner_id": OWNER,
        "agentcore_runtime_arn": "arn:hermes-1",
        "bedrock_gate_token_hash": "hash-hermes",
        "bedrock_gate_superseded_hashes": ["hash-ancient", "hash-ecs-live"],
    }
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(mod, "teardown_hermes_runtime", AsyncMock())
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-9"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", AsyncMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: SimpleNamespace(start_user_service=AsyncMock()))
    swap = AsyncMock(return_value=("written", None))
    monkeypatch.setattr(mod.container_repo, "swap_gate_token_hash", swap)

    assert await mod.rollback(OWNER) == mod.EXIT_OK

    swap.assert_awaited_once_with(
        OWNER, new_hash="hash-ecs-live", superseded=["hash-ancient"], expected_prior_hash="hash-hermes"
    )


async def test_rollback_gate_token_restore_is_a_noop_with_nothing_superseded(monkeypatch):
    """No prior mint ever ran for this workspace (e.g. rolled back right after prepare, before
    a runtime was ever created) -- nothing to restore, and rollback must not call the swap at
    all (there is no ECS generation to point at)."""
    row = {"owner_id": OWNER, "bedrock_gate_token_hash": "hash-hermes"}
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(mod, "teardown_hermes_runtime", AsyncMock())
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-9"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", AsyncMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: SimpleNamespace(start_user_service=AsyncMock()))
    swap = AsyncMock(side_effect=AssertionError("nothing to restore"))
    monkeypatch.setattr(mod.container_repo, "swap_gate_token_hash", swap)

    assert await mod.rollback(OWNER) == mod.EXIT_OK
    swap.assert_not_called()


async def test_rollback_gate_token_restore_is_best_effort_on_a_conflict(monkeypatch):
    """A concurrent gate-token mint racing a manual --rollback is a real but rare edge case --
    it must be logged, not fail the rollback (the row still flips back onto ECS regardless)."""
    row = {
        "owner_id": OWNER,
        "bedrock_gate_token_hash": "hash-hermes",
        "bedrock_gate_superseded_hashes": ["hash-ecs-live"],
    }
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(mod, "teardown_hermes_runtime", AsyncMock())
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-9"))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", AsyncMock())
    ecs = SimpleNamespace(start_user_service=AsyncMock())
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: ecs)
    monkeypatch.setattr(
        mod.container_repo,
        "swap_gate_token_hash",
        AsyncMock(return_value=("conflict", {"bedrock_gate_token_hash": "x"})),
    )

    assert await mod.rollback(OWNER) == mod.EXIT_OK  # must not raise
    ecs.start_user_service.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 3 — --all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("image,bucket", [("", "test-hermes-bucket"), ("hermes:test", ""), ("", "")])
async def test_all_flag_preflight_settings_refuses_before_any_scan_or_company_read(monkeypatch, image, bucket):
    """Fix round 2, item 2: the settings preflight runs ONCE at the top of migrate_all, before
    scan_all and before any Paperclip read -- a misconfigured environment must exit 2 instantly,
    not scan the whole table, read every owner's company, and exit 0 having touched no one."""
    monkeypatch.setattr(mod.settings, "AGENTCORE_HERMES_IMAGE", image)
    monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", bucket)
    scan_all = AsyncMock(side_effect=AssertionError("must not scan"))
    load_company = AsyncMock(side_effect=AssertionError("must not read a company"))
    monkeypatch.setattr(mod.container_repo, "scan_all", scan_all)
    monkeypatch.setattr(mod, "_load_company", load_company)

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_PRECONDITION
    scan_all.assert_not_called()
    load_company.assert_not_called()


async def test_all_flag_efs_root_missing_refuses_before_any_scan_or_company_read(settings_ok, tmp_path, monkeypatch):
    """Fix round 2, item 2: same placement fix, for the EFS-root presence half."""
    # tmp_path exists, but get_workspace() is left pointing somewhere that does not (an
    # operator-laptop stand-in), same shape as test_missing_efs_workspace_directory_exits_precondition.
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path / "does-not-exist"))
    scan_all = AsyncMock(side_effect=AssertionError("must not scan"))
    load_company = AsyncMock(side_effect=AssertionError("must not read a company"))
    monkeypatch.setattr(mod.container_repo, "scan_all", scan_all)
    monkeypatch.setattr(mod, "_load_company", load_company)

    assert await mod.migrate_all(dry_run=False) == mod.EXIT_PRECONDITION
    scan_all.assert_not_called()
    load_company.assert_not_called()


async def test_all_flag_migrates_every_non_reference_row_and_includes_unstamped_agentcore(
    efs_ready, settings_ok, monkeypatch
):
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


async def test_all_flag_skips_pass_admission_rows(efs_ready, settings_ok, monkeypatch):
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


async def test_all_flag_skips_rows_with_no_active_paperclip_company(efs_ready, settings_ok, monkeypatch):
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


async def test_all_flag_retries_a_transient_exit_exactly_once(efs_ready, settings_ok, monkeypatch):
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


async def test_all_flag_reports_a_persistent_transient_failure(efs_ready, settings_ok, monkeypatch):
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


async def test_all_flag_summary_includes_reconnect_chatgpt(efs_ready, settings_ok, monkeypatch, capsys):
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
async def test_all_flag_exit_code_prefers_1_then_11_never_max(
    efs_ready, settings_ok, monkeypatch, codes, expected_attr
):
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


async def test_all_flag_passes_the_loaded_company_into_migrate(efs_ready, settings_ok, monkeypatch):
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


async def test_stamp_migrated_clears_an_earlier_error(monkeypatch):
    """Fix round 2, item 3: a row carrying an earlier hermes_migrate_error/hermes_migrate_exit_code
    (from a previous pre-flip failure, Ruling M6-R22) is stamped clean on success -- rollback
    already uses None the same way to clear a field."""
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    leased = AsyncMock()
    monkeypatch.setattr(mod.container_repo, "update_container_leased", leased)

    await mod._stamp_migrated(OWNER)

    fields = leased.await_args.args[2]
    assert fields["hermes_migrated_at"]
    assert fields["hermes_migrate_error"] is None
    assert fields["hermes_migrate_exit_code"] is None


# --- re-pointing an ALREADY-migrated owner's adapters (task 18) -----------------------------


@pytest.mark.asyncio
async def test_repoint_adapters_only_runs_for_an_already_migrated_owner(monkeypatch):
    """The gate `migrate()` needs is exactly the one that locks this out: `_check_preconditions`
    exits 2 for any row already carrying `hermes_migrated_at`, and every owner that needs the
    native-adapter re-point is BY DEFINITION already migrated. So this door skips it."""
    repoint = AsyncMock()
    monkeypatch.setattr(mod, "_repoint_paperclip_adapters", repoint)
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(
        mod.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "runtime_host": "agentcore", "hermes_migrated_at": "2026-09-17"}),
    )
    precondition = AsyncMock()
    monkeypatch.setattr(mod, "_check_preconditions", precondition)

    assert await mod.repoint_adapters_only("org_1", dry_run=False) == 0

    precondition.assert_not_awaited()
    repoint.assert_awaited_once_with("org_1", ROSTER, runtime_host="agentcore")


@pytest.mark.asyncio
async def test_repoint_adapters_only_refuses_an_owner_not_on_agentcore(monkeypatch):
    """Re-pointing an ECS owner onto the native adapter would aim its runs at a runtime it does
    not have. The row decides, not the flag."""
    repoint = AsyncMock()
    monkeypatch.setattr(mod, "_repoint_paperclip_adapters", repoint)
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(
        mod.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_1", "runtime_host": "ecs"})
    )

    assert await mod.repoint_adapters_only("org_1", dry_run=False) == 2
    repoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_repoint_adapters_only_dry_run_writes_nothing(monkeypatch, capsys):
    repoint = AsyncMock()
    monkeypatch.setattr(mod, "_repoint_paperclip_adapters", repoint)
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(
        mod.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "runtime_host": "agentcore"}),
    )

    assert await mod.repoint_adapters_only("org_1", dry_run=True) == 0

    repoint.assert_not_awaited()
    assert "Ada" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Connected integrations — re-projected into the new profiles (credentials.reproject)
# ---------------------------------------------------------------------------


async def test_reproject_runs_after_the_efs_overrides_and_before_the_session_stop(
    tmp_path, precondition_ok, monkeypatch, capsys
):
    """Neither Hermes projection rail runs on the migration path, so the tail re-projects. It
    must land AFTER the last override upload and BEFORE the session stop: the cold restore that
    stop forces is what puts the projected files in front of the next turn."""
    _fake_workspace(tmp_path)
    monkeypatch.setattr(mod, "get_workspace", lambda: _workspace_at(tmp_path))
    monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", "test-hermes-bucket")

    calls: list[str] = []
    uploaded: list[tuple[str, str]] = []
    monkeypatch.setattr(mod.container_repo, "takeover_provision", AsyncMock(return_value="claim-1"))
    monkeypatch.setattr(mod, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(mod, "provision_hermes_runtime", AsyncMock(return_value="arn:hermes-1"))
    monkeypatch.setattr(mod, "_s3_client", lambda: _FakeS3Client(uploaded, order_sink=calls))
    monkeypatch.setattr(
        "core.services.hermes_profiles._new_s3_client", lambda: _FakeS3Client(uploaded, order_sink=calls)
    )
    monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value={"sonnet": "arn:son"}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_PROFILE_INPUTS)
    )
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pcp_live_x")
    )
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: SimpleNamespace(stop_user_service=AsyncMock()))
    monkeypatch.setattr(mod.container_repo, "update_container_leased", AsyncMock())
    monkeypatch.setattr(mod, "_stop_runtime_session", MagicMock(side_effect=lambda *a: calls.append("session_stop")))

    async def _reproject(owner_id):
        calls.append("reproject")
        return {"github": "projected", "google": "needs_reconnect"}

    monkeypatch.setattr(mod, "reproject_connections", _reproject)

    assert await mod.migrate(OWNER, dry_run=False) == mod.EXIT_OK

    upload_indices = [i for i, c in enumerate(calls) if c.startswith("upload:")]
    assert max(upload_indices) < calls.index("reproject") < calls.index("session_stop")
    # Printed, not merely logged: `needs_reconnect` is the operator's whole to-do list.
    out = capsys.readouterr().out
    assert f"{OWNER} reproject github=projected" in out
    assert f"{OWNER} reproject google=needs_reconnect" in out
