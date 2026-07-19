"""Tests for the company deploy orchestration service (Task 4).

These are mock-heavy unit tests: the real Paperclip admin client, gateway
pool, ECS manager, EFS workspace, and the deploy-job repo are all faked, so
the orchestration logic is exercised with no AWS / container / Paperclip.

The load-bearing invariants under test (from the task brief):

* every ``create_agent`` carries the SERVER-synthesized adapter
  (``adapter_type="openclaw_gateway"``) + ``budget_monthly_cents=1000`` — never
  a package-supplied adapter (a ``PlannedAgent`` carries no adapter at all);
* agents are created in topological order (managers before reports);
* the package CEO / division head reports to Friday; a child reports to its
  own manager's freshly-minted id;
* routines are created via ``create_agent_routines`` (the dormant path);
* resume skips an already-``materialized`` agent (no duplicate create);
* rollback deletes exactly the created set and never an ``is_seeded_agent``;
* an unreachable container fails the job with ZERO creates.
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
            }
        )
        return {"id": pid, "name": name, "role": role}


class FakeEcs:
    def __init__(self, container, ip) -> None:
        self._container = container
        self._ip = ip

    async def resolve_running_container(self, owner_id):
        return self._container, self._ip


class FakeRepo:
    """In-memory deploy-job store mirroring company_deploy_repo's surface."""

    def __init__(self, job) -> None:
        self.job = job
        self.status_calls: list[tuple[str, str | None]] = []
        self.record_calls: list[tuple[str, str | None, str]] = []
        self.routine_ids_calls: list[list[str]] = []
        # Ordered log of the terminal writes so a test can assert the routine
        # ids land BEFORE the completed status.
        self.op_log: list[str] = []

    async def get_job(self, job_id):
        return self.job

    async def record_agent(self, *, job_id, slug, paperclip_id, state, now_iso):
        self.record_calls.append((slug, paperclip_id, state))
        if self.job:
            for a in self.job.get("agents", []):
                if a.get("slug") == slug:
                    a["paperclip_id"] = paperclip_id
                    a["state"] = state
                    break

    async def record_routine_ids(self, *, job_id, routine_ids, now_iso):
        self.routine_ids_calls.append(list(routine_ids))
        self.op_log.append("record_routine_ids")
        if self.job:
            self.job["routine_ids"] = list(routine_ids)

    async def set_status(self, *, job_id, status, now_iso, error=None):
        self.status_calls.append((status, error))
        self.op_log.append(f"set_status:{status}")
        if self.job:
            self.job["status"] = status
            self.job["error"] = error


class Harness:
    """Bundles the fakes + call recorders installed onto the service module."""

    def __init__(self) -> None:
        self.admin = FakeAdmin()
        self.http = FakeHttp()
        self.repo = None
        self.pair_calls: list[str] = []
        self.ensure_calls: list[str] = []
        self.materialize_calls: list[str] = []
        self.routine_calls: list[dict] = []
        self.seeded_ids: set[str] = set()
        self.deleted: list[str] = []
        self.unwound: list[str] = []
        self.container = {"gateway_token": "gw_token"}
        self.ip = "10.0.0.5"


def _seed_job(agent_slugs, materialized=None, paperclip_ids=None):
    """Build a running job row with pending agents (some pre-materialized)."""
    materialized = materialized or set()
    paperclip_ids = paperclip_ids or {}
    return {
        "job_id": "cdpl_test",
        "owner_id": OWNER,
        "status": "running",
        "error": None,
        "routine_ids": [],
        "agents": [
            {
                "slug": s,
                "paperclip_id": paperclip_ids.get(s),
                "state": "materialized" if s in materialized else "pending",
            }
            for s in agent_slugs
        ],
    }


def _install(monkeypatch, h: Harness, plan=None, tar_bytes=b""):
    """Wire every external dependency of the service to a fake."""
    if h.repo is None:
        h.repo = FakeRepo(_seed_job([p.slug for p in plan.agents]) if plan else None)

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
        async def unwind_partial_deploy(self, *, owner_id, new_agent_id):
            h.unwound.append(new_agent_id)

    monkeypatch.setattr(svc, "is_seeded_agent", fake_is_seeded)
    monkeypatch.setattr(svc, "delete_paperclip_for_marketplace", fake_delete)
    monkeypatch.setattr(svc, "get_catalog_service", lambda: FakeCatalog())

    # Repo.
    monkeypatch.setattr(svc.company_deploy_repo, "get_job", h.repo.get_job)
    monkeypatch.setattr(svc.company_deploy_repo, "record_agent", h.repo.record_agent)
    monkeypatch.setattr(svc.company_deploy_repo, "record_routine_ids", h.repo.record_routine_ids)
    monkeypatch.setattr(svc.company_deploy_repo, "set_status", h.repo.set_status)


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
    h.repo = FakeRepo(_seed_job(["ceo", "cto", "qa-engineer", "release-engineer", "staff-engineer"]))
    _install(monkeypatch, h, plan=None, tar_bytes=b"")  # real parse path

    tar_bytes = _tar_fixture(FIXTURE)
    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=tar_bytes)

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

    by_name = {c["name"]: c for c in h.admin.create_calls}
    # CEO (package root) and CTO (reports to package ceo) both graft onto Friday.
    assert by_name["CEO"]["reports_to"] == FRIDAY_ID
    assert by_name["CTO"]["reports_to"] == FRIDAY_ID
    # Reports of the CTO point at the CTO's freshly-minted id.
    cto_id = h.admin.name_to_id["CTO"]
    assert by_name["QA Engineer"]["reports_to"] == cto_id
    assert by_name["Release Engineer"]["reports_to"] == cto_id
    assert by_name["Staff Engineer"]["reports_to"] == cto_id

    assert ("completed", None) in h.repo.status_calls
    assert len(h.pair_calls) == 5
    assert len(h.ensure_calls) == 5
    assert len(h.materialize_calls) == 5


# ---------------------------------------------------------------------------
# Per-agent progression + adapter/budget invariants (hand-built plan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_progression_created_paired_materialized(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    # Each agent advances created -> paired -> materialized, in order.
    ceo_id = h.admin.name_to_id["CEO"]
    assert ("ceo", ceo_id, "created") in h.repo.record_calls
    assert ("ceo", ceo_id, "paired") in h.repo.record_calls
    assert ("ceo", ceo_id, "materialized") in h.repo.record_calls
    # 3 agents x 3 states.
    assert len(h.repo.record_calls) == 9
    assert h.repo.job["status"] == "completed"


@pytest.mark.asyncio
async def test_child_reports_to_freshly_minted_manager(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

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

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    by_agent = {c["agent_id"]: c["routines"] for c in h.routine_calls}
    cto_id = h.admin.name_to_id["CTO"]
    worker_id = h.admin.name_to_id["Worker"]
    assert by_agent[cto_id] == [{"title": "Nightly", "description": "d", "cron": "0 8 * * *"}]
    assert by_agent[worker_id] == [{"title": "Hourly", "description": "d2", "cron": "0 * * * *"}]


@pytest.mark.asyncio
async def test_created_routine_ids_persisted_onto_job_before_completed(monkeypatch):
    """The ids returned by create_agent_routines are accumulated and written to
    the job row via record_routine_ids — BEFORE the terminal completed status —
    so the frontend RoutinesEnableCard sees a non-empty routine_ids."""
    plan = _simple_plan(
        routines=[
            PlannedRoutine(agent_slug="cto", title="Nightly", description="d", cron="0 8 * * *"),
            PlannedRoutine(agent_slug="worker", title="Hourly", description="d2", cron="0 * * * *"),
        ]
    )
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    # fake create_agent_routines returns [f"routine_{agent_id}"] per assignee,
    # grouped cto-then-worker (plan.routines order).
    cto_id = h.admin.name_to_id["CTO"]
    worker_id = h.admin.name_to_id["Worker"]
    expected = [f"routine_{cto_id}", f"routine_{worker_id}"]

    # Persisted exactly once, with the accumulated ids, and landed on the row.
    assert h.repo.routine_ids_calls == [expected]
    assert h.repo.job["routine_ids"] == expected

    # Ordering: routine ids are recorded strictly before status flips to completed.
    assert h.repo.op_log.index("record_routine_ids") < h.repo.op_log.index("set_status:completed")


@pytest.mark.asyncio
async def test_no_routines_persists_empty_list(monkeypatch):
    """A plan with no cron'd routines still records an empty routine_ids list
    (idempotent with the seeded []), and still completes."""
    plan = _simple_plan(routines=[])
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.repo.routine_ids_calls == [[]]
    assert h.repo.job["routine_ids"] == []
    assert h.repo.job["status"] == "completed"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_skips_materialized_ceo(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    # CEO already materialized in a prior run.
    h.repo = FakeRepo(
        _seed_job(
            ["ceo", "cto", "worker"],
            materialized={"ceo"},
            paperclip_ids={"ceo": "pc_ceo_prior"},
        )
    )
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    created_names = [c["name"] for c in h.admin.create_calls]
    assert "CEO" not in created_names  # skipped on resume
    assert created_names == ["CTO", "Worker"]
    # Its prior id is not re-materialized.
    assert "pc_ceo_prior" not in h.materialize_calls
    assert h.repo.job["status"] == "completed"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_deletes_created_set_and_skips_seeded(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.repo = FakeRepo(
        _seed_job(
            ["ceo", "cto", "worker"],
            paperclip_ids={"ceo": "pc_ceo", "cto": "pc_cto", "worker": None},
        )
    )
    # Pretend the CEO id is a seeded agent (must never be deleted).
    h.seeded_ids = {"pc_ceo"}
    _install(monkeypatch, h, plan=plan)

    await svc.rollback_company_deploy(job_id="cdpl_test")

    # pc_cto has a paperclip_id and is not seeded -> deleted + unwound.
    assert h.deleted == ["pc_cto"]
    assert h.unwound == ["pc_cto"]
    # pc_ceo is seeded (skipped); worker has no id (skipped).
    assert "pc_ceo" not in h.deleted
    assert ("rolled_back", None) in h.repo.status_calls


# ---------------------------------------------------------------------------
# Container unreachable -> failed, no creates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_container_unreachable_fails_with_no_creates(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = None  # ECS reports no running container
    h.ip = None
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    statuses = [s for s, _ in h.repo.status_calls]
    assert statuses == ["failed"]


@pytest.mark.asyncio
async def test_missing_gateway_token_fails_with_no_creates(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.container = {"gateway_token": ""}  # row exists but no token
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert [s for s, _ in h.repo.status_calls] == ["failed"]


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

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    by_name = {c["name"]: c for c in h.admin.create_calls}
    assert by_name["CEO"]["reports_to"] == FRIDAY_ID
    assert by_name["CTO"]["reports_to"] == FRIDAY_ID
    assert by_name["CTO"]["reports_to"] != "pc_prior_ceo"


# ---------------------------------------------------------------------------
# Friday not found -> failed, no creates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_friday_fails_with_no_creates(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    h.admin = FakeAdmin(roster=[{"id": "a1", "role": "member"}])  # no ceo
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert [s for s, _ in h.repo.status_calls] == ["failed"]


@pytest.mark.asyncio
async def test_no_paperclip_company_fails(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    async def none_company(*, owner_id, repo):
        return None

    monkeypatch.setattr(svc, "resolve_company_id_for_owner", none_company)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.admin.create_calls == []
    assert [s for s, _ in h.repo.status_calls] == ["failed"]


# ---------------------------------------------------------------------------
# Never raises: an exception mid-loop records failed and leaves created agents.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_raises_records_failed_on_exception(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    async def boom(*, owner_id, new_agent_id, planned, skill_assets, workspace, apply_aux):
        raise RuntimeError("materialize exploded")

    monkeypatch.setattr(svc, "materialize_agent", boom)

    # Must NOT raise.
    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    # Failed status carries the error message.
    failed = [(s, e) for s, e in h.repo.status_calls if s == "failed"]
    assert failed
    assert "materialize exploded" in failed[-1][1]
    # The first agent's create was recorded before the crash (left for resume).
    assert any(state == "created" for _, _, state in h.repo.record_calls)


@pytest.mark.asyncio
async def test_admin_http_closed_on_success(monkeypatch):
    plan = _simple_plan()
    h = Harness()
    _install(monkeypatch, h, plan=plan)

    await svc.run_company_deploy(job_id="cdpl_test", owner_id=OWNER, listing={}, tar_bytes=b"")

    assert h.http.closed is True
