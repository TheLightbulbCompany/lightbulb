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
from core.services import service_token as service_token_mod
from core.services.company_agent_materializer import render_agent_identity_md
from core.services.company_package import DeployPlan, PlannedAgent, PlannedRoutine
from core.services.hermes_provisioning import HermesProfileError
from core.services.paperclip_agent_lifecycle import resolve_agent_adapter as real_resolve_agent_adapter
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired as real_ensure_operator_device_paired,
)
from core.services.paperclip_openclaw_pairing import pair_with_openclaw as real_pair_with_openclaw

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


class FakeEcs:
    def __init__(self, container, ip) -> None:
        self._container = container
        self._ip = ip

    async def resolve_running_container(self, owner_id):
        return self._container, self._ip


class Harness:
    """Bundles the fakes + call recorders installed onto the service module."""

    def __init__(self) -> None:
        self.admin = FakeAdmin()
        self.http = FakeHttp()
        self.pair_calls: list[str] = []
        self.ensure_calls: list[str] = []
        self.materialize_calls: list[str] = []
        self.routine_calls: list[dict] = []
        self.seeded_ids: set[str] = set()
        self.deleted: list[str] = []
        self.unwound: list[str] = []
        self.unwind_causes: list[BaseException | None] = []
        # When set, FakeCatalog.unwind_partial_deploy raises instead of returning
        # — the unknown-unknown branch the rollback has to treat as a FAILED
        # unwind rather than a completed one.
        self.unwind_raises: BaseException | None = None
        self.container = {"gateway_token": "gw_token"}
        self.ip = "10.0.0.5"
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

    # Container reachability.
    monkeypatch.setattr(svc, "get_ecs_manager", lambda: FakeEcs(h.container, h.ip))
    monkeypatch.setattr(svc, "get_workspace", lambda: object())

    # Company resolution.
    async def fake_resolve_company(*, owner_id, repo):
        return "company_1"

    monkeypatch.setattr(svc, "resolve_company_id_for_owner", fake_resolve_company)

    # Adapter selection + synthesis (server-side) -- faked at the ONE shared seam
    # (resolve_agent_adapter) every Paperclip agent-creation door now calls through,
    # rather than its two internal pieces (adapter_type_for's container-row read +
    # synthesize_openclaw_adapter). Byte-identical to today's ECS/openclaw_gateway shape;
    # the dedicated adapter-selection tests below exercise the real resolver.
    async def fake_resolve_adapter(owner_id, *, agent_label, gateway_user_id):
        return "openclaw_gateway", {"url": "wss://fake", "authToken": "svc_tok", "sessionKey": gateway_user_id}

    monkeypatch.setattr(svc, "resolve_agent_adapter", fake_resolve_adapter)

    # Pairing + materialize.
    async def fake_pair(*, owner_id, paperclip_agent, extra_update_fields=None):
        h.pair_calls.append(paperclip_agent["id"])

    async def fake_ensure(*, owner_id, paperclip_agent, gateway_token):
        h.ensure_calls.append(paperclip_agent["id"])

    async def fake_materialize(*, owner_id, new_agent_id, planned, skill_assets, workspace, apply_aux):
        h.materialize_calls.append(new_agent_id)
        return {"id": new_agent_id}

    monkeypatch.setattr(svc, "pair_with_openclaw", fake_pair)
    monkeypatch.setattr(svc, "ensure_operator_device_paired", fake_ensure)
    monkeypatch.setattr(svc, "materialize_agent", fake_materialize)

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

    class FakeCatalog:
        async def unwind_partial_deploy(self, *, owner_id, new_agent_id, cause=None):
            h.unwound.append(new_agent_id)
            h.unwind_causes.append(cause)
            if h.unwind_raises is not None:
                raise h.unwind_raises
            # Mirrors the real return contract: False iff the unwind was SKIPPED
            # because the cause was a client-side timeout. The rollback keys the
            # Paperclip delete off this.
            return not isinstance(cause, TimeoutError)

    monkeypatch.setattr(svc, "is_seeded_agent", fake_is_seeded)
    monkeypatch.setattr(svc, "delete_paperclip_for_marketplace", fake_delete)
    monkeypatch.setattr(svc, "get_catalog_service", lambda: FakeCatalog())

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


def _plan_of(n: int):
    """A flat ``n``-agent company: a CEO plus ``n-1`` direct reports.

    Ids mint as ``pc_1``..``pc_n`` in creation order (see ``FakeAdmin``).
    """
    return DeployPlan(
        slug="acme",
        ceo_slug="ceo",
        agents=[_agent("ceo", name="CEO", role="ceo", reports_to_slug=None)]
        + [_agent(f"a{i}", name=f"A{i}", reports_to_slug="ceo") for i in range(2, n + 1)],
        routines=[],
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
    _install(monkeypatch, h, plan=None)  # real parse path

    tar_bytes = _tar_fixture(FIXTURE)
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=tar_bytes)

    # All five agents created, CEO first, CTO second (topological).
    names = [c["name"] for c in h.admin.create_calls]
    assert names[0] == "CEO"
    assert names[1] == "CTO"
    assert set(names) == {"CEO", "CTO", "QA Engineer", "Release Engineer", "Staff Engineer"}

    # Every create is server-synthesized + budgeted; never a package adapter.
    for call in h.admin.create_calls:
        assert call["adapter_type"] == "openclaw_gateway"
        assert call["budget_monthly_cents"] == 1000
        assert call["adapter_config"]["authToken"] == "svc_tok"
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

    # Every agent paired + operator-device-trusted + materialized.
    assert len(h.pair_calls) == 5
    assert len(h.ensure_calls) == 5
    assert len(h.materialize_calls) == 5
    # No rollback on the happy path.
    assert h.deleted == []
    # Admin session closed.
    assert h.http.closed is True


# ---------------------------------------------------------------------------
# Per-agent pipeline + adapter/budget invariants (hand-built plan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_agent_created_paired_and_materialized(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    ceo_id = h.admin.name_to_id["CEO"]
    cto_id = h.admin.name_to_id["CTO"]
    worker_id = h.admin.name_to_id["Worker"]
    assert h.pair_calls == [ceo_id, cto_id, worker_id]
    assert h.ensure_calls == [ceo_id, cto_id, worker_id]
    assert h.materialize_calls == [ceo_id, cto_id, worker_id]
    assert h.deleted == []


@pytest.mark.asyncio
async def test_every_create_carries_server_adapter_and_budget(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls  # sanity
    for call in h.admin.create_calls:
        assert call["adapter_type"] == "openclaw_gateway"
        assert call["budget_monthly_cents"] == 1000
        assert call["adapter_config"]["authToken"] == "svc_tok"


@pytest.mark.asyncio
async def test_ecs_owner_adapter_selection_is_byte_identical_via_the_real_resolver(monkeypatch):
    """Same real ``resolve_agent_adapter`` the AgentCore test below exercises, but with no
    container row -- the pre-migration ECS default, byte-identical to what the harness's own
    fake (above) already asserts."""
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)
    monkeypatch.setattr(svc, "resolve_agent_adapter", real_resolve_agent_adapter)
    monkeypatch.setattr(svc.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(service_token_mod, "mint", lambda owner_id: "svc_tok")

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls
    for call in h.admin.create_calls:
        assert call["adapter_type"] == "openclaw_gateway"
        assert call["adapter_config"]["authToken"] == "svc_tok"


@pytest.mark.asyncio
async def test_agentcore_owner_gets_the_http_adapter_via_the_real_resolver(monkeypatch):
    """The one shared resolver (paperclip_agent_lifecycle.resolve_agent_adapter) picks the
    adapter type from the owner's ACTUAL container row -- an AgentCore-hosted owner's
    company-deploy agents get the fork's http adapter, not a device-paired openclaw_gateway
    one that has no gateway to pair with."""
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)
    monkeypatch.setattr(svc, "resolve_agent_adapter", real_resolve_agent_adapter)
    monkeypatch.setattr(
        svc.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"runtime_host": "agentcore", "hook_token": "hook_abc"}),
    )
    monkeypatch.setattr(svc.settings, "PUBLIC_API_BASE", "https://api-dev.flickmeon.ai")

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls
    for call in h.admin.create_calls:
        assert call["adapter_type"] == "http"
        assert call["adapter_config"]["headers"]["Authorization"] == "Bearer hook_abc"
        assert call["adapter_config"]["url"] == f"https://api-dev.flickmeon.ai/api/v1/internal/paperclip/runs/{OWNER}"


@pytest.mark.asyncio
async def test_child_reports_to_freshly_minted_manager(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

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
    _install(monkeypatch, h, plan=plan)

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
    _install(monkeypatch, h, plan=plan)

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
    _install(monkeypatch, h, plan=plan)

    async def boom(*, admin, session_cookie, company_id, agent_id, routines):
        raise RuntimeError("routine creation exploded")

    monkeypatch.setattr(svc, "create_agent_routines", boom)

    # Must NOT raise.
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # All three agents were created + materialized...
    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO", "Worker"]
    assert len(h.materialize_calls) == 3
    # ...and NONE were rolled back despite the routine failure.
    assert h.deleted == []
    assert h.unwound == []
    # Admin session still closed.
    assert h.http.closed is True


# ---------------------------------------------------------------------------
# Failure path — inline rollback of exactly this run's created agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_rolls_back_created_set_and_skips_seeded(monkeypatch):
    """A materialize failure mid-run tears down exactly the agents created so
    far (delete + unwind), guarding is_seeded_agent so a seed is never
    deleted."""
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    async def boom(*, owner_id, new_agent_id, planned, skill_assets, workspace, apply_aux):
        if planned.name == "CTO":
            raise RuntimeError("materialize exploded")
        h.materialize_calls.append(new_agent_id)
        return {"id": new_agent_id}

    monkeypatch.setattr(svc, "materialize_agent", boom)
    # Pretend the CEO's freshly-minted id is a seeded agent (never deletable).
    h.seeded_ids = {"pc_1"}

    # Must NOT raise.
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # Two agents were created (CEO pc_1, CTO pc_2) before the crash; the Worker
    # was never reached.
    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO"]
    # pc_2 (CTO) is not seeded -> deleted + unwound. pc_1 (CEO) is seeded -> skipped.
    assert h.deleted == ["pc_2"]
    assert h.unwound == ["pc_2"]
    assert "pc_1" not in h.deleted
    # Admin session still closed on the failure path.
    assert h.http.closed is True


@pytest.mark.asyncio
async def test_run_company_deploy_never_raises_and_rolls_back(monkeypatch):
    """A pairing failure on the first agent is swallowed (never escapes the
    background target) and the one created agent is rolled back."""
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    async def boom(*, owner_id, paperclip_agent, extra_update_fields=None):
        raise RuntimeError("pair exploded")

    monkeypatch.setattr(svc, "pair_with_openclaw", boom)

    # Must NOT raise.
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # Only the CEO was created before pairing blew up; it is rolled back.
    assert [c["name"] for c in h.admin.create_calls] == ["CEO"]
    assert h.deleted == ["pc_1"]
    assert h.unwound == ["pc_1"]
    assert len(h.unwind_causes) == 1
    assert isinstance(h.unwind_causes[0], RuntimeError)


@pytest.mark.asyncio
async def test_run_company_deploy_timeout_keeps_both_sides_of_the_install(monkeypatch):
    """pair_with_openclaw can raise TimeoutError after exhausting its
    bounded retry — a client-side 'no answer yet', not a failure. The
    rollback threads that exception through to unwind_partial_deploy as
    ``cause`` so it can skip the EFS/openclaw.json edit, and because the
    container-side install is therefore still standing the Paperclip row is
    KEPT with it. Deleting only the Paperclip row is what left an agent in the
    container that nobody could see or remove from Teams."""
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    async def boom(*, owner_id, paperclip_agent, extra_update_fields=None):
        raise TimeoutError()

    monkeypatch.setattr(svc, "pair_with_openclaw", boom)

    # Must NOT raise.
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert [c["name"] for c in h.admin.create_calls] == ["CEO"]
    # unwind_partial_deploy was still called (it makes the skip decision
    # itself); the TimeoutError was threaded through as its cause.
    assert h.unwound == ["pc_1"]
    assert len(h.unwind_causes) == 1
    assert isinstance(h.unwind_causes[0], TimeoutError)
    # ...and it reported "skipped", so the Paperclip row stays put.
    assert h.deleted == []


@pytest.mark.asyncio
async def test_timeout_on_the_last_agent_still_unwinds_the_earlier_ones(monkeypatch):
    """A 10-agent deploy whose LAST agent times out must unwind the other nine.

    ``created`` is appended to right after ``create_agent`` and before that
    agent is paired, so only ``created[-1]`` can still be mid-write; every
    earlier agent finished its RPCs. Applying the one TimeoutError to the whole
    list skipped the unwind for all ten — stranding nine EFS workspaces and
    openclaw.json entries forever while their Paperclip rows were deleted
    anyway, i.e. nine unreachable ghosts.
    """
    plan = _plan_of(10)
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    real_pair = svc.pair_with_openclaw

    async def boom(*, owner_id, paperclip_agent, extra_update_fields=None):
        if paperclip_agent["id"] == "pc_10":
            raise TimeoutError()
        return await real_pair(owner_id=owner_id, paperclip_agent=paperclip_agent)

    monkeypatch.setattr(svc, "pair_with_openclaw", boom)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.unwound == [f"pc_{i}" for i in range(1, 11)]
    # Only the agent the timeout landed on carries the cause.
    assert [c is None for c in h.unwind_causes[:9]] == [True] * 9
    assert isinstance(h.unwind_causes[9], TimeoutError)
    # The nine completed agents are fully rolled back; the timed-out one is
    # kept on BOTH sides so it stays visible + deletable in Teams.
    assert h.deleted == [f"pc_{i}" for i in range(1, 10)]


@pytest.mark.asyncio
async def test_rollback_keeps_the_row_when_the_unwind_itself_raises(monkeypatch):
    """``unwind_partial_deploy`` is documented never to raise, so a raise is an
    unknown-unknown: the container may still hold the workspace, the agents.list
    entry and the sidecar. Deleting the Paperclip row on top of that produces an
    agent Teams cannot show or remove. A retained row is recoverable; a ghost
    is not."""
    plan = _simple_plan()
    h = Harness()
    h.unwind_raises = RuntimeError("efs stalled")
    _install(monkeypatch, h, plan=plan)

    async def boom(*, owner_id, paperclip_agent, extra_update_fields=None):
        raise RuntimeError("pair blew up")

    monkeypatch.setattr(svc, "pair_with_openclaw", boom)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # The unwind was attempted...
    assert h.unwound != []
    # ...but because it raised, the Paperclip row is KEPT, not deleted.
    assert h.deleted == []


# ---------------------------------------------------------------------------
# Container unreachable -> zero creates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_container_unreachable_creates_nothing(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = None  # ECS reports no running container
    h.ip = None
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert h.deleted == []


@pytest.mark.asyncio
async def test_missing_gateway_token_creates_nothing(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = {"gateway_token": ""}  # row exists but no token
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
    h.ip = "agentcore"
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

    async def __call__(self, owner_id, agent_row, *, container_row, inputs=None, soul_md_override=None):
        self.calls.append(
            {"owner_id": owner_id, "agent_row": agent_row, "container_row": container_row, "inputs": inputs}
        )


class _RecordingUploadFiles:
    """Stands in for hermes_profiles.upload_profile_files -- records every call instead of
    touching S3."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, owner_id, agent_id, files):
        self.calls.append((owner_id, agent_id, files))


def _wire_agentcore_profile(monkeypatch):
    """Fake the Hermes profile-writing boundary (resolve_profile_inputs /
    write_profile_for_agent / upload_profile_files) and restore pair_with_openclaw /
    ensure_operator_device_paired to their REAL implementations -- container_rpc.call is
    left genuinely unmocked, so if the AgentCore branch still reached pairing it would
    attempt a live HTTP call and fail loudly in this sandbox instead of a fake silently
    recording it."""
    resolve_calls: list[str] = []
    inputs_sentinel = object()

    async def fake_resolve_profile_inputs(owner_id, *, container_row):
        resolve_calls.append(owner_id)
        return inputs_sentinel

    write_profile = _RecordingWriteProfile()
    upload_files = _RecordingUploadFiles()
    monkeypatch.setattr(svc, "resolve_profile_inputs", fake_resolve_profile_inputs)
    monkeypatch.setattr(svc, "write_profile_for_agent", write_profile)
    monkeypatch.setattr(svc, "upload_profile_files", upload_files)
    monkeypatch.setattr(svc, "pair_with_openclaw", real_pair_with_openclaw)
    monkeypatch.setattr(svc, "ensure_operator_device_paired", real_ensure_operator_device_paired)
    return resolve_calls, write_profile, upload_files, inputs_sentinel


@pytest.mark.asyncio
async def test_agentcore_row_runs_the_per_agent_loop_with_no_pairing_or_efs(monkeypatch):
    """M6-R33: an AgentCore row with a runtime ARN now runs the REAL per-agent loop instead
    of M6-R26's refusal -- N Paperclip creates, N write_profile_for_agent calls sharing the
    ONE resolved inputs, this agent's content uploaded via upload_profile_files, and neither
    pairing nor EFS materialization is reached."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    h.ip = "agentcore"
    _install(monkeypatch, h, plan=plan)
    resolve_calls, write_profile, upload_files, inputs_sentinel = _wire_agentcore_profile(monkeypatch)

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO", "Worker"]

    # Owner-level inputs resolved exactly ONCE for the whole roster.
    assert resolve_calls == [OWNER]
    assert len(write_profile.calls) == 3
    assert {id(c["inputs"]) for c in write_profile.calls} == {id(inputs_sentinel)}
    for call in write_profile.calls:
        assert call["container_row"] is h.container

    # Each agent's own content (today: just AGENTS.md) lands under ITS profile.
    by_agent_id = {agent_id: files for _, agent_id, files in upload_files.calls}
    assert set(by_agent_id) == {h.admin.name_to_id[n] for n in ("CEO", "CTO", "Worker")}
    for planned in plan.agents:
        agent_id = h.admin.name_to_id[planned.name]
        assert by_agent_id[agent_id] == {"AGENTS.md": render_agent_identity_md(planned).encode()}

    # No pairing, no EFS materialize.
    assert h.materialize_calls == []
    assert h.deleted == []
    assert h.unwound == []


@pytest.mark.asyncio
async def test_agentcore_skipped_aux_mutations_logged_and_counted(monkeypatch, caplog):
    """apply_deploy_aux_mutations has no Hermes equivalent yet -- each skipped kind is logged
    at INFO per agent, and the deploy summary rolls the per-kind counts up once at the end."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    h.ip = "agentcore"
    _install(monkeypatch, h, plan=plan)
    _wire_agentcore_profile(monkeypatch)

    with caplog.at_level("INFO"):
        await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert caplog.text.count("skipped aux mutation kind=agent_entry") == 3
    assert caplog.text.count("skipped aux mutation kind=plugins_patch") == 3
    assert "agentcore deploy summary" in caplog.text
    assert "'agent_entry': 3" in caplog.text
    assert "'plugins_patch': 3" in caplog.text


@pytest.mark.asyncio
async def test_agentcore_mid_loop_failure_rolls_back_by_paperclip_delete_only(monkeypatch):
    """A HermesProfileError partway through the AgentCore loop rolls back exactly the agents
    created so far via a straight Paperclip delete -- never catalog.unwind_partial_deploy
    (there is no EFS/openclaw.json/.template sidecar state on this transport to unwind). S3
    profile objects are left in place; a re-run overwrites them."""
    plan = _simple_plan()
    h = Harness()
    h.container = dict(AGENTCORE_CONTAINER)
    h.ip = "agentcore"
    _install(monkeypatch, h, plan=plan)
    _resolve_calls, write_profile, _upload_files, _inputs = _wire_agentcore_profile(monkeypatch)

    async def boom(owner_id, agent_row, *, container_row, inputs=None, soul_md_override=None):
        if agent_row["name"] == "CTO":
            raise HermesProfileError("paperclip API key mint/read failed")
        write_profile.calls.append(
            {"owner_id": owner_id, "agent_row": agent_row, "container_row": container_row, "inputs": inputs}
        )

    monkeypatch.setattr(svc, "write_profile_for_agent", boom)

    # Must NOT raise.
    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    # CEO + CTO created before the crash; the Worker was never reached.
    assert [c["name"] for c in h.admin.create_calls] == ["CEO", "CTO"]
    ceo_id = h.admin.name_to_id["CEO"]
    cto_id = h.admin.name_to_id["CTO"]
    assert sorted(h.deleted) == sorted([ceo_id, cto_id])
    # Never through the ECS-only container/EFS unwind path.
    assert h.unwound == []


@pytest.mark.asyncio
async def test_agentcore_ecs_row_stays_on_the_unchanged_pairing_path(monkeypatch):
    """Sanity check that the branch really is transport-gated: an ECS row (the default
    Harness container) still pairs + materializes, never touching the AgentCore helpers."""
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)
    _resolve_calls, write_profile, upload_files, _inputs = _wire_agentcore_profile(monkeypatch)
    # Undo the pairing/ensure restoration for THIS test -- the harness's own fakes should be
    # the ones reached on an ECS row, not the real network-bound implementations.
    fake_pair = AsyncMock()
    monkeypatch.setattr(svc, "pair_with_openclaw", fake_pair)
    monkeypatch.setattr(svc, "ensure_operator_device_paired", AsyncMock())

    await svc.run_company_deploy(owner_id=OWNER, listing={}, tar_bytes=b"")

    assert len(h.materialize_calls) == 3
    assert fake_pair.await_count == 3
    assert write_profile.calls == []
    assert upload_files.calls == []


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
    h.admin = FakeAdmin(
        roster=[
            {"id": "pc_prior_ceo", "role": "ceo", "reportsTo": FRIDAY_ID},  # earlier deploy, first
            {"id": FRIDAY_ID, "role": "ceo", "reportsTo": None},
        ]
    )
    _install(monkeypatch, h, plan=plan)

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
    _install(monkeypatch, h, plan=plan)

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
    _install(monkeypatch, h, plan=_simple_plan())
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
    pairing TimeoutError HOLDS the claim (release no-ops on it), exactly like
    the single-agent path."""
    h = Harness()
    _install(monkeypatch, h, plan=_simple_plan())
    boom = RuntimeError("pair failed")

    async def fail_pair(*, owner_id, paperclip_agent, extra_update_fields=None):
        raise boom

    monkeypatch.setattr(svc, "pair_with_openclaw", fail_pair)

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
    _install(monkeypatch, h, plan=_simple_plan())

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1", "name": "Acme"}, tar_bytes=b"")

    assert h.deployed_markers == [("l1", "Acme")]


@pytest.mark.asyncio
async def test_failure_does_not_burn_the_company_slot(monkeypatch):
    """A rolled-back run must NOT write the marker — the owner retries."""
    h = Harness()
    _install(monkeypatch, h, plan=_simple_plan())

    async def fail_pair(*, owner_id, paperclip_agent, extra_update_fields=None):
        raise RuntimeError("pair failed")

    monkeypatch.setattr(svc, "pair_with_openclaw", fail_pair)

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1", "name": "Acme"}, tar_bytes=b"")

    assert h.deployed_markers == []


@pytest.mark.asyncio
async def test_creating_nothing_does_not_burn_the_company_slot(monkeypatch):
    """The creating-nothing early returns (here: container unreachable) exit
    without an exception but created nothing — no marker."""
    h = Harness()
    h.ip = None
    _install(monkeypatch, h, plan=_simple_plan())

    await svc.run_company_deploy(owner_id=OWNER, listing={"listing_id": "l1", "name": "Acme"}, tar_bytes=b"")

    assert h.deployed_markers == []
