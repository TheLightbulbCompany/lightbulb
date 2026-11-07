"""Tests for the company deploy orchestration service.

These are mock-heavy unit tests: the real Paperclip admin client, gateway
pool, ECS manager, and EFS workspace are all faked, so the orchestration logic
is exercised with no AWS / container / Paperclip.

The deploy engine is FIRE-AND-FORGET: ``run_company_deploy`` is a background
task target that NEVER raises. There is no job row and no resume — progress and
the final roster live in Teams (the Paperclip roster is the source of truth).

The load-bearing invariants under test (from the task brief):

* every ``create_agent`` carries the SERVER-synthesized adapter
  (``adapter_type="openclaw_gateway"``) + ``budget_monthly_cents=1000`` — never
  a package-supplied adapter (a ``PlannedAgent`` carries no adapter at all);
* agents are created in topological order (managers before reports);
* the package CEO / division head reports to Friday; a child reports to its
  own manager's freshly-minted id;
* routines are created via ``create_agent_routines`` (the dormant path);
* on failure the agents this run created are rolled back INLINE — deleted +
  unwound — and never an ``is_seeded_agent``;
* an unreachable container creates ZERO agents;
* ``run_company_deploy`` never raises.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.services import company_deploy_service as svc
from core.services.company_agent_materializer import render_agent_identity_md
from core.services.company_package import DeployPlan, PlannedAgent, PlannedRoutine
from core.services.hermes_provisioning import HermesProfileError
from core.services.paperclip_agent_lifecycle import resolve_agent_adapter as real_resolve_agent_adapter

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "companies" / "gstack"

OWNER = "user_owner_1"
FRIDAY_ID = "agent_friday"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeHttp:
    """Stands in for the httpx.AsyncClient the admin session owns."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeAdmin:
    """Records create_agent calls; mints a stable id per call.

    ``list_agents`` returns whatever roster it was seeded with (default: a
    single Friday with ``role == "ceo"``).
    """

    def __init__(self, roster=None) -> None:
        self.roster = roster if roster is not None else [{"id": FRIDAY_ID, "role": "ceo", "name": "Friday"}]
        self.create_calls: list[dict] = []
        self.name_to_id: dict[str, str] = {}
        self._counter = 0

    async def list_agents(self, *, session_cookie, company_id):
        return list(self.roster)

    async def create_agent(
        self,
        *,
        session_cookie,
        company_id,
        name,
        role,
        adapter_type,
        adapter_config,
        title=None,
        capabilities=None,
        icon=None,
        reports_to=None,
        budget_monthly_cents=0,
        idempotency_key=None,
        idempotency_replay_only=False,
    ):
        self._counter += 1
        pid = f"pc_{self._counter}"
        self.name_to_id[name] = pid
        self.create_calls.append(
            {
                "name": name,
                "role": role,
                "adapter_type": adapter_type,
                "adapter_config": adapter_config,
                "title": title,
                "capabilities": capabilities,
                "reports_to": reports_to,
                "budget_monthly_cents": budget_monthly_cents,
                "idempotency_key": idempotency_key,
                "idempotency_replay_only": idempotency_replay_only,
            }
        )
        return {"id": pid, "name": name, "role": role}


class Harness:
    """Bundles the fakes + call recorders installed onto the service module."""

    def __init__(self) -> None:
        self.admin = FakeAdmin()
        self.http = FakeHttp()
        self.routine_calls: list[dict] = []
        self.seeded_ids: set[str] = set()
        self.deleted: list[str] = []
        self.container = dict(AGENTCORE_CONTAINER)
        # (listing_id, name) tuples recorded by the one-company-per-workspace
        # marker write on real success.
        self.deployed_markers: list[tuple[str, str]] = []


def _install(monkeypatch, h: Harness, plan=None):
    """Wire every external dependency of the service to a fake."""

    # Admin session — the SERVER-side bootstrap-admin cookie mechanism.
    async def fake_open_admin_session():
        return h.http, h.admin, "admin_cookie"

    monkeypatch.setattr(svc, "_open_admin_session", fake_open_admin_session)

    # Plan parse — either a hand-built plan or (when None) the real tar path.
    if plan is not None:

        async def fake_parse(_tar_bytes):
            return plan

        monkeypatch.setattr(svc, "_parse_plan_async", fake_parse)

    # Readiness: the deploy reads the row directly -- a row with a runtime ARN IS "reachable"
    # (see run_company_deploy). Bound late through the harness so a test can null it.
    async def fake_get_by_owner_id(owner_id):
        return h.container

    monkeypatch.setattr(svc.container_repo, "get_by_owner_id", fake_get_by_owner_id)

    # Company resolution.
    async def fake_resolve_company(*, owner_id, repo):
        return "company_1"

    monkeypatch.setattr(svc, "resolve_company_id_for_owner", fake_resolve_company)

    # Adapter selection + synthesis (server-side) -- faked at the ONE shared seam
    # (resolve_agent_adapter) every Paperclip agent-creation door calls through, rather than its
    # internal pieces. It takes only the owner now: its agent_label/gateway_user_id arguments
    # were accepted and unused, and there is one adapter to pick. The dedicated
    # adapter-selection tests below exercise the real resolver.
    async def fake_resolve_adapter(owner_id):
        return "hermes_gateway", {"url": "https://fake", "hookToken": "hook_tok"}

    monkeypatch.setattr(svc, "resolve_agent_adapter", fake_resolve_adapter)

    # Routines.
    async def fake_create_routines(*, admin, session_cookie, company_id, agent_id, routines):
        h.routine_calls.append({"agent_id": agent_id, "routines": routines})
        return [f"routine_{agent_id}"]

    monkeypatch.setattr(svc, "create_agent_routines", fake_create_routines)

    # Rollback deps.
    async def fake_is_seeded(*, owner_id, agent_id):
        return agent_id in h.seeded_ids

    async def fake_delete(*, owner_id, agent_id):
        h.deleted.append(agent_id)

    monkeypatch.setattr(svc, "is_seeded_agent", fake_is_seeded)
    monkeypatch.setattr(svc, "delete_paperclip_for_marketplace", fake_delete)

    # One-company marker — recorded, never hits DynamoDB in unit tests.
    async def fake_set_deployed(owner_id, *, listing_id, name):
        h.deployed_markers.append((listing_id, name))

    monkeypatch.setattr(svc.container_repo, "set_deployed_company", fake_set_deployed)


# ---------------------------------------------------------------------------
# Hand-built plans
# ---------------------------------------------------------------------------


def _agent(slug, *, name=None, role="member", reports_to_slug=None):
    return PlannedAgent(
        slug=slug,
        name=name or slug,
        role=role,
        title=None,
        body_md=f"# {slug}\n",
        reports_to_slug=reports_to_slug,
        skills=[],
        capabilities=None,
    )


def _simple_plan(routines=None):
    """ceo -> (cto) division head -> (worker) child."""
    return DeployPlan(
        slug="acme",
        ceo_slug="ceo",
        agents=[
            _agent("ceo", name="CEO", role="ceo", reports_to_slug=None),
            _agent("cto", name="CTO", reports_to_slug="ceo"),
            _agent("worker", name="Worker", reports_to_slug="cto"),
        ],
        routines=routines or [],
    )


# ---------------------------------------------------------------------------
# Happy path (real gstack fixture tarball → parse → deploy)
# ---------------------------------------------------------------------------


def _tar_fixture(pkg_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(pkg_dir), arcname=pkg_dir.name)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_gstack_full_deploy_topological(monkeypatch):
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=None)  # real parse path
    _resolve_calls, write_profile, upload_files, _inputs = _wire_agentcore_profile(monkeypatch)

    tar_bytes = _tar_fixture(FIXTURE)
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=tar_bytes)

    # All five agents created, CEO first, CTO second (topological).
    names = [c["name"] for c in h.admin.create_calls]
    assert names[0] == "CEO"
    assert names[1] == "CTO"
    assert set(names) == {"CEO", "CTO", "QA Engineer", "Release Engineer", "Staff Engineer"}

    # Every create is server-synthesized + budgeted; never a package adapter.
    for call in h.admin.create_calls:
        assert call["adapter_type"] == "hermes_gateway"
        assert call["budget_monthly_cents"] == 1000
        assert call["adapter_config"]["hookToken"] == "hook_tok"
        # Company deploy has no durable operation row yet. Replaying an agent
        # UUID here could let one background worker roll back another's live
        # install, so this path must stay outside create idempotency for now.
        assert call["idempotency_key"] is None
        assert call["idempotency_replay_only"] is False

    by_name = {c["name"]: c for c in h.admin.create_calls}
    # CEO (package root) and CTO (reports to package ceo) both graft onto Friday.
    assert by_name["CEO"]["reports_to"] == FRIDAY_ID
    assert by_name["CTO"]["reports_to"] == FRIDAY_ID
    # Reports of the CTO point at the CTO's freshly-minted id.
    cto_id = h.admin.name_to_id["CTO"]
    assert by_name["QA Engineer"]["reports_to"] == cto_id
    assert by_name["Release Engineer"]["reports_to"] == cto_id
    assert by_name["Staff Engineer"]["reports_to"] == cto_id

    # Every agent's Hermes profile written -- that IS the install.
    assert len(write_profile.calls) == 5
    assert len(upload_files.calls) == 5
    # No rollback on the happy path.
    assert h.deleted == []
    # Admin session closed.
    assert h.http.closed is True


# ---------------------------------------------------------------------------
# Per-agent pipeline + adapter/budget invariants (hand-built plan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_create_carries_server_adapter_and_budget(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls  # sanity
    for call in h.admin.create_calls:
        assert call["adapter_type"] == "hermes_gateway"
        assert call["budget_monthly_cents"] == 1000
        assert call["adapter_config"]["hookToken"] == "hook_tok"


@pytest.mark.asyncio
async def test_the_real_resolver_selects_the_native_hermes_adapter(monkeypatch):
    """There is one adapter: ``resolve_agent_adapter`` binds every company agent to the native
    Hermes gateway facade. The ECS-default branch it used to pick for a row-less owner (the
    device-paired ``openclaw_gateway``) went with that transport."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)
    monkeypatch.setattr(svc, "resolve_agent_adapter", real_resolve_agent_adapter)
    h.container = {**AGENTCORE_CONTAINER, "hook_token": "hook_abc"}
    monkeypatch.setattr(svc.settings, "PUBLIC_API_BASE", "https://api-dev.thelightbulb.company")

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls
    for call in h.admin.create_calls:
        assert call["adapter_type"] == "hermes_gateway"
        assert call["adapter_config"]["apiKey"] == "hook_abc"
        assert call["adapter_config"]["apiBaseUrl"] == (
            f"https://api-dev.thelightbulb.company/api/v1/internal/paperclip/hermes/{OWNER}"
        )


@pytest.mark.asyncio
async def test_child_reports_to_freshly_minted_manager(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    by_name = {c["name"]: c for c in h.admin.create_calls}
    assert by_name["CEO"]["reports_to"] == FRIDAY_ID
    assert by_name["CTO"]["reports_to"] == FRIDAY_ID  # reports to package ceo -> Friday
    assert by_name["Worker"]["reports_to"] == h.admin.name_to_id["CTO"]


# ---------------------------------------------------------------------------
# Routines (dormant path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routines_created_via_create_agent_routines(monkeypatch):
    plan = _simple_plan(
        routines=[
            PlannedRoutine(agent_slug="cto", title="Nightly", description="d", cron="0 8 * * *"),
            PlannedRoutine(agent_slug="worker", title="Hourly", description="d2", cron="0 * * * *"),
            # cron-less routine is dropped (create_agent_routines requires a cron).
            PlannedRoutine(agent_slug="worker", title="NoCron", description="d3", cron=None),
        ]
    )
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    by_agent = {c["agent_id"]: c["routines"] for c in h.routine_calls}
    cto_id = h.admin.name_to_id["CTO"]
    worker_id = h.admin.name_to_id["Worker"]
    assert by_agent[cto_id] == [{"title": "Nightly", "description": "d", "cron": "0 8 * * *"}]
    assert by_agent[worker_id] == [{"title": "Hourly", "description": "d2", "cron": "0 * * * *"}]


@pytest.mark.asyncio
async def test_no_cron_routines_creates_none(monkeypatch):
    """A plan with no cron'd routines makes no create_agent_routines calls and
    still completes without raising."""
    plan = _simple_plan(routines=[])
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.routine_calls == []
    assert len(h.admin.create_calls) == 3


@pytest.mark.asyncio
async def test_routine_failure_does_not_roll_back_materialized_company(monkeypatch):
    """A create_agent_routines failure AFTER every agent materialized leaves the
    deployed company intact — routines are created paused and are re-addable from
    Teams, so a routine hiccup must NOT tear down healthy agents. run never
    raises."""
    plan = _simple_plan(routines=[PlannedRoutine(agent_slug="cto", title="Nightly", description="d", cron="0 8 * * *")])
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _resolve_calls, write_profile, upload_files, _inputs = _wire_agentcore_profile(monkeypatch)

    async def boom(*, admin, session_cookie, company_id, agent_id, routines):
        raise RuntimeError("routine creation exploded")

    monkeypatch.setattr(svc, "create_agent_routines", boom)

    # Must NOT raise.
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # All three agents were created + their Hermes profiles written...
    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO", "Worker"]
    assert len(write_profile.calls) == 3
    # ...and NONE were rolled back despite the routine failure.
    assert h.deleted == []
    # Admin session still closed.
    assert h.http.closed is True


# ---------------------------------------------------------------------------
# Failure path — inline rollback of exactly this run's created agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_rolls_back_created_set_and_skips_seeded(monkeypatch):
    """A Hermes profile-write failure mid-run tears down exactly the agents
    created so far via a straight Paperclip delete (AgentCore rollback has no
    EFS/openclaw.json to unwind), guarding is_seeded_agent so a seed is never
    deleted."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _resolve_calls, write_profile, upload_files, _inputs = _wire_agentcore_profile(monkeypatch)

    async def boom(owner_id, agent_row, *, inputs=None, soul_md_override=None, publish=True):
        if agent_row["name"] == "CTO":
            raise RuntimeError("profile write exploded")
        write_profile.calls.append({"owner_id": owner_id, "agent_row": agent_row, "inputs": inputs})

    monkeypatch.setattr(svc, "write_profile_for_agent", boom)
    # Pretend the CEO's freshly-minted id is a seeded agent (never deletable).
    h.seeded_ids = {"pc_1"}

    # Must NOT raise.
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # Two agents were created (CEO pc_1, CTO pc_2) before the crash; the Worker
    # was never reached.
    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO"]
    # pc_2 (CTO) is not seeded -> deleted. pc_1 (CEO) is seeded -> skipped.
    assert h.deleted == ["pc_2"]
    # AgentCore rollback deletes directly -- never the ECS/EFS unwind path.
    assert "pc_1" not in h.deleted
    # Admin session still closed on the failure path.
    assert h.http.closed is True


# ---------------------------------------------------------------------------
# Container unreachable -> zero creates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_container_unreachable_creates_nothing(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = None  # no row at all
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert h.deleted == []


@pytest.mark.asyncio
async def test_missing_gateway_token_creates_nothing(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = {"gateway_token": ""}  # row exists but carries no runtime ARN
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert h.deleted == []


# ---------------------------------------------------------------------------
# M6-R25: transport-aware readiness gate (row #9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agentcore_row_without_runtime_arn_creates_nothing(monkeypatch):
    """An AgentCore row that hasn't minted its runtime ARN yet is not
    reachable -- no gateway_token fallback applies to it."""
    plan = _simple_plan()
    h = Harness()
    h.container = {"runtime_host": "agentcore"}  # no agentcore_runtime_arn yet
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert h.deleted == []


# ---------------------------------------------------------------------------
# M6-R33: the AgentCore per-agent loop (lifts M6-R26's temporary refusal)
# ---------------------------------------------------------------------------

AGENTCORE_CONTAINER = {
    "runtime_host": "agentcore",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/abc",
    "bedrock_aip_arns": {"sonnet": "arn:aws:bedrock:us-east-1:1:foundation-model/sonnet"},
}


class _RecordingWriteProfile:
    """Stands in for hermes_provisioning.write_profile_for_agent -- records every call
    instead of touching S3/Paperclip."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, owner_id, agent_row, *, inputs=None, soul_md_override=None, publish=True):
        assert publish is False  # the deploy publishes sessions.json once, after the loop
        self.calls.append({"owner_id": owner_id, "agent_row": agent_row, "inputs": inputs})


class _RecordingUploadFiles:
    """Stands in for hermes_profiles.upload_profile_files -- records every call instead of
    touching S3."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, owner_id, agent_id, files):
        self.calls.append((owner_id, agent_id, files))


def _wire_agentcore_profile(monkeypatch):
    """Fake the Hermes profile-writing boundary: resolve_profile_inputs /
    write_profile_for_agent / upload_profile_files. Nothing else is stubbed, so a deploy that
    somehow reached a runtime would attempt a live call and fail loudly in this sandbox rather
    than have a fake silently record it. (The device-pairing functions this used to restore to
    their real implementations for that purpose are deleted -- there is nothing to pair.)"""
    resolve_calls: list[str] = []
    inputs_sentinel = object()

    async def fake_resolve_profile_inputs(owner_id):
        resolve_calls.append(owner_id)
        return inputs_sentinel

    write_profile = _RecordingWriteProfile()
    upload_files = _RecordingUploadFiles()
    monkeypatch.setattr(svc, "resolve_profile_inputs", fake_resolve_profile_inputs)
    monkeypatch.setattr(svc, "write_profile_for_agent", write_profile)
    monkeypatch.setattr(svc, "upload_profile_files", upload_files)
    return resolve_calls, write_profile, upload_files, inputs_sentinel


@pytest.mark.asyncio
async def test_a_company_deploy_runs_the_per_agent_profile_loop(monkeypatch):
    """M6-R33: N Paperclip creates, N write_profile_for_agent calls sharing the ONE resolved
    owner-level inputs, and each agent's own content uploaded under ITS profile."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    resolve_calls, write_profile, upload_files, inputs_sentinel = _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO", "Worker"]

    # Owner-level inputs resolved exactly ONCE for the whole roster.
    assert resolve_calls == [OWNER]
    assert len(write_profile.calls) == 3
    assert {id(c["inputs"]) for c in write_profile.calls} == {id(inputs_sentinel)}

    # Each agent's own content (today: just AGENTS.md) lands under ITS profile.
    by_agent_id = {agent_id: files for _, agent_id, files in upload_files.calls}
    assert set(by_agent_id) == {h.admin.name_to_id[n] for n in ("CEO", "CTO", "Worker")}
    for planned in plan.agents:
        agent_id = h.admin.name_to_id[planned.name]
        assert by_agent_id[agent_id] == {"AGENTS.md": render_agent_identity_md(planned).encode()}

    assert h.deleted == []


@pytest.mark.asyncio
async def test_agentcore_skipped_aux_mutations_logged_and_counted(monkeypatch, caplog):
    """apply_deploy_aux_mutations has no Hermes equivalent yet -- each skipped kind is logged
    at INFO per agent, and the deploy summary rolls the per-kind counts up once at the end."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    with caplog.at_level("INFO"):
        await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert caplog.text.count("skipped aux mutation kind=agent_entry") == 3
    assert caplog.text.count("skipped aux mutation kind=plugins_patch") == 3
    assert "deploy summary" in caplog.text
    assert "'agent_entry': 3" in caplog.text
    assert "'plugins_patch': 3" in caplog.text


@pytest.mark.asyncio
async def test_agentcore_mid_loop_failure_rolls_back_by_paperclip_delete_only(monkeypatch, caplog):
    """A HermesProfileError partway through the AgentCore loop rolls back exactly the agents
    created so far via a straight Paperclip delete -- never catalog.unwind_partial_deploy
    (there is no EFS/openclaw.json/.template sidecar state on this transport to unwind). S3
    profile objects are left in place; a re-run overwrites them. The deploy reports the
    failure (logged), never raises to its fire-and-forget caller."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _resolve_calls, write_profile, _upload_files, _inputs = _wire_agentcore_profile(monkeypatch)

    async def boom(owner_id, agent_row, *, inputs=None, soul_md_override=None, publish=True):
        if agent_row["name"] == "CTO":
            raise HermesProfileError("paperclip API key mint/read failed")
        write_profile.calls.append({"owner_id": owner_id, "agent_row": agent_row, "inputs": inputs})

    monkeypatch.setattr(svc, "write_profile_for_agent", boom)

    # Must NOT raise.
    with caplog.at_level("ERROR"):
        await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # The deploy reports the failure...
    assert f"company_deploy: owner={OWNER} failed" in caplog.text
    # CEO + CTO created before the crash; the Worker was never reached.
    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO"]
    ceo_id = h.admin.name_to_id["CEO"]
    cto_id = h.admin.name_to_id["CTO"]
    assert sorted(h.deleted) == sorted([ceo_id, cto_id])
    # Never through the ECS-only container/EFS unwind path.


# ---------------------------------------------------------------------------
# _find_friday_id — the true apex, not just "first ceo"
# ---------------------------------------------------------------------------


def test_find_friday_id_picks_rootless_ceo_despite_order():
    """After a prior deploy the company has 2+ role=ceo agents; the package
    CEO is a division head with a non-empty reportsTo. Even with the package
    CEO listed FIRST, Friday (the rootless ceo) must win — list order must
    not fool the selection."""
    roster = [
        {"id": "pc_pkg_ceo", "role": "ceo", "reportsTo": FRIDAY_ID},  # prior deploy's CEO, first
        {"id": FRIDAY_ID, "role": "ceo", "reportsTo": None},  # the true apex
    ]
    assert svc._find_friday_id(roster) == FRIDAY_ID


def test_find_friday_id_accepts_snake_case_and_missing_field():
    # snake_case reports_to is honored defensively.
    snake = [
        {"id": "pc_pkg_ceo", "role": "ceo", "reports_to": FRIDAY_ID},
        {"id": FRIDAY_ID, "role": "ceo", "reports_to": ""},
    ]
    assert svc._find_friday_id(snake) == FRIDAY_ID
    # A ceo with no reports-to field at all is treated as rootless.
    assert svc._find_friday_id([{"id": FRIDAY_ID, "role": "ceo"}]) == FRIDAY_ID


def test_find_friday_id_falls_back_to_first_ceo_if_none_rootless():
    # Impossible in practice (Friday is always rootless) but never return None
    # when a ceo exists.
    roster = [
        {"id": "pc_a", "role": "ceo", "reportsTo": "x"},
        {"id": "pc_b", "role": "ceo", "reportsTo": "y"},
    ]
    assert svc._find_friday_id(roster) == "pc_a"
    assert svc._find_friday_id([{"id": "a1", "role": "member"}]) is None


@pytest.mark.asyncio
async def test_deploy_grafts_onto_rootless_friday_not_prior_package_ceo(monkeypatch):
    """End-to-end: with a prior package CEO listed first, the new deploy's
    division head still reports to the rootless Friday, not the prior CEO."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    h.admin = FakeAdmin(
        roster=[
            {"id": "pc_prior_ceo", "role": "ceo", "reportsTo": FRIDAY_ID},  # earlier deploy, first
            {"id": FRIDAY_ID, "role": "ceo", "reportsTo": None},
        ]
    )
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    by_name = {c["name"]: c for c in h.admin.create_calls}
    assert by_name["CEO"]["reports_to"] == FRIDAY_ID
    assert by_name["CTO"]["reports_to"] == FRIDAY_ID
    assert by_name["CTO"]["reports_to"] != "pc_prior_ceo"


# ---------------------------------------------------------------------------
# Preconditions that create nothing (and never raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_friday_creates_nothing(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.admin = FakeAdmin(roster=[{"id": "a1", "role": "member"}])  # no ceo
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert h.deleted == []


@pytest.mark.asyncio
async def test_no_paperclip_company_creates_nothing(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    async def none_company(*, owner_id, repo):
        return None

    monkeypatch.setattr(svc, "resolve_company_id_for_owner", none_company)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert h.deleted == []


@pytest.mark.asyncio
async def test_admin_http_closed_on_success(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.http.closed is True


# ---------------------------------------------------------------------------
# Owner-wide deploy claim (buyer launch): extended per agent, released on settle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_extended_per_agent_and_released_on_success(monkeypatch):
    """The run owns the router's claim: the lease is extended once per planned
    agent (a company's wall clock is N x a single deploy's) and released with
    no cause when the run succeeds."""
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=_simple_plan())
    _wire_agentcore_profile(monkeypatch)
    extends: list[str] = []
    releases: list[tuple[str, BaseException | None]] = []

    async def fake_extend(owner_id, *, token):
        extends.append(token)

    async def fake_release(owner_id, *, listing_id, token, cause=None):
        releases.append((token, cause))

    monkeypatch.setattr(svc.container_repo, "extend_owner_deploy_claim", fake_extend)
    monkeypatch.setattr(svc.container_repo, "release_owner_deploy_claim", fake_release)

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1"}, tar_bytes=b"", claim_token="tok")

    assert extends == ["tok", "tok", "tok"]
    assert releases == [("tok", None)]


@pytest.mark.asyncio
async def test_claim_released_with_the_failure_as_cause(monkeypatch):
    """A failed run releases the claim with the failure as ``cause`` — so a
    profile-write failure HOLDS the claim (release no-ops on it), exactly like
    the single-agent path."""
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=_simple_plan())
    _wire_agentcore_profile(monkeypatch)
    boom = RuntimeError("profile write failed")

    async def fail_write(owner_id, agent_row, *, inputs=None, soul_md_override=None, publish=True):
        raise boom

    monkeypatch.setattr(svc, "write_profile_for_agent", fail_write)

    async def fake_extend(owner_id, *, token):
        return None

    releases: list[tuple[str, BaseException | None]] = []

    async def fake_release(owner_id, *, listing_id, token, cause=None):
        releases.append((token, cause))

    monkeypatch.setattr(svc.container_repo, "extend_owner_deploy_claim", fake_extend)
    monkeypatch.setattr(svc.container_repo, "release_owner_deploy_claim", fake_release)

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1"}, tar_bytes=b"", claim_token="tok")

    assert releases == [("tok", boom)]


@pytest.mark.asyncio
async def test_no_claim_token_touches_no_claim(monkeypatch):
    """claim_token=None (direct service callers, older dispatch shapes) never
    calls the claim machinery."""
    h = Harness()
    _install(monkeypatch, h, plan=_simple_plan())
    touched: list[str] = []

    async def fake_extend(owner_id, *, token):
        touched.append("extend")

    async def fake_release(owner_id, *, listing_id, token, cause=None):
        touched.append("release")

    monkeypatch.setattr(svc.container_repo, "extend_owner_deploy_claim", fake_extend)
    monkeypatch.setattr(svc.container_repo, "release_owner_deploy_claim", fake_release)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert touched == []


# ---------------------------------------------------------------------------
# One-company-per-workspace marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_writes_the_deployed_company_marker(monkeypatch):
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    _install(monkeypatch, h, plan=_simple_plan())
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1", "name": "Acme"}, tar_bytes=b"")

    assert h.deployed_markers == [("l1", "Acme")]


@pytest.mark.asyncio
async def test_failure_does_not_burn_the_company_slot(monkeypatch):
    """A rolled-back run must NOT write the marker — the owner retries."""
    h = Harness()
    _install(monkeypatch, h, plan=_simple_plan())

    # The profile write is the step that can fail per agent now (it was the pairing RPC).
    async def fail_write_profile(owner_id, agent, *, inputs, publish=True):
        raise RuntimeError("profile write failed")

    monkeypatch.setattr(svc, "resolve_profile_inputs", AsyncMock(return_value=object()))
    monkeypatch.setattr(svc, "write_profile_for_agent", fail_write_profile)

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1", "name": "Acme"}, tar_bytes=b"")

    assert h.deployed_markers == []


@pytest.mark.asyncio
async def test_creating_nothing_does_not_burn_the_company_slot(monkeypatch):
    """The creating-nothing early returns (here: no provisioned runtime) exit without an
    exception but created nothing — no marker."""
    h = Harness()
    h.container = None
    _install(monkeypatch, h, plan=_simple_plan())

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1", "name": "Acme"}, tar_bytes=b"")

    assert h.deployed_markers == []


@pytest.mark.asyncio
async def test_company_deploy_creates_every_agent_with_can_create_agents_false(monkeypatch):
    """Paperclip 916 defaults a new agent's canCreateAgents to TRUE. An imported company's
    agents -- its role=ceo included -- reach Paperclip with an explicit false through the
    real admin-client funnel."""
    import json

    import httpx

    from core.services.paperclip_admin_client import PaperclipAdminClient

    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[{"id": FRIDAY_ID, "role": "ceo", "name": "Friday"}])
        body = json.loads(req.read().decode())
        bodies.append(body)
        return httpx.Response(201, json={"id": f"pc_{len(bodies)}", "name": body["name"]})

    h = Harness()
    h.admin = PaperclipAdminClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://pc.test")
    )
    _install(monkeypatch, h, plan=_simple_plan())
    _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert len(bodies) == 3
    assert all(b["permissions"] == {"canCreateAgents": False} for b in bodies)


@pytest.fixture(autouse=True)
def _no_registry_mirror(monkeypatch):
    """These pin the Paperclip calls; the registry mirror (always on since the switch retired) has its own suite."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services.paperclip_admin_client import PaperclipAdminClient as _Client

    monkeypatch.setattr(_Client, "_mirror", _AsyncMock())
