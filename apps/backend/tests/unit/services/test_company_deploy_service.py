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

import pytest

from core.services import company_deploy_service as svc
from core.services.company_package import DeployPlan, PlannedAgent, PlannedRoutine

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
    monkeypatch.setattr(svc, "get_gateway_pool", lambda: object())
    monkeypatch.setattr(svc, "get_workspace", lambda: object())

    # Company resolution.
    async def fake_resolve_company(*, owner_id, repo):
        return "company_1"

    monkeypatch.setattr(svc, "resolve_company_id_for_owner", fake_resolve_company)

    # Token + adapter synthesis (server-side).
    monkeypatch.setattr(svc.service_token, "mint", lambda owner_id: "svc_tok")
    monkeypatch.setattr(
        svc,
        "synthesize_openclaw_adapter",
        lambda *, gateway_url, service_token, user_id: {
            "url": gateway_url,
            "authToken": service_token,
            "sessionKey": user_id,
        },
    )

    # Pairing + materialize.
    async def fake_pair(*, owner_id, paperclip_agent, gateway_pool, ip, token):
        h.pair_calls.append(paperclip_agent["id"])

    async def fake_ensure(*, owner_id, paperclip_agent, gateway_token, ip):
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

    async def boom(*, owner_id, paperclip_agent, gateway_pool, ip, token):
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

    async def boom(*, owner_id, paperclip_agent, gateway_pool, ip, token):
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

    async def boom(*, owner_id, paperclip_agent, gateway_pool, ip, token):
        if paperclip_agent["id"] == "pc_10":
            raise TimeoutError()
        return await real_pair(
            owner_id=owner_id, paperclip_agent=paperclip_agent, gateway_pool=gateway_pool, ip=ip, token=token
        )

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

    async def boom(*, owner_id, paperclip_agent, gateway_pool, ip, token):
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
