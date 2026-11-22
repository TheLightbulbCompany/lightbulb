"""The engine's operations over HTTP (engine-service plan Task 8): every operation answers the same through
`core.engine.client` -> `routers/engine_api.py` (`engine_main.app`) as through `core.engine.api` in process, the
same refusals included, and a retry after a lost response replays."""

from unittest.mock import AsyncMock

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.config import settings
from core.engine import api, client, transport
from core.engine.contract import (
    ActIn,
    AnnotateIn,
    AnswerIn,
    EngineError,
    EventIn,
    LineIn,
    OpenIn,
    Principal as Who,
    StartIn,
)
from core.repositories import effects_repo, messaging_repo, tasks_repo
from core.services import container_rpc, task_conversation
from core.services.messaging import ApiProblem

from .test_api import (  # noqa: F401 -- the moto tables and the rooms the in-process tests use
    ANSWER_ERRORS,
    CALL,
    DM,
    FRONT,
    OWNER,
    Q,
    TASK,
    WS,
    _act_world,
    _agent_line,
    _answer_world,
    _member_line,
    _room,
    _rows,
    dm,
    front,
    roster,
    tables,
)

BASE = "http://engine.test"
TOKEN = "engine-test-token"


class _FirstFails(httpx.AsyncBaseTransport):
    """The first request fails one way: `"refused"` never reaches the engine (connection refused); a status (502,
    503, 504) is the gateway's answer after the engine handled it."""

    def __init__(self, inner: httpx.AsyncBaseTransport, how) -> None:
        self.inner, self.how, self.calls = inner, how, 0

    async def handle_async_request(self, request):
        self.calls += 1
        if self.calls == 1 and self.how == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        response = await self.inner.handle_async_request(request)
        if self.calls == 1:
            await response.aread()
            return httpx.Response(self.how)
        return response


@pytest.fixture
async def wire(monkeypatch):
    """`core.engine.client` pointed at `engine_main.app` in this process, with the service bearer."""
    import engine_main
    from core.voice import engine_hooks

    engine_hooks.register()  # importing engine_main registered the HTTP hooks; this test process keeps the products'
    monkeypatch.setattr(settings, "ENGINE_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "ENGINE_URL", BASE)
    asgi = httpx.ASGITransport(app=engine_main.app)
    http = httpx.AsyncClient(transport=asgi, base_url=BASE)
    monkeypatch.setitem(transport._clients, BASE, http)
    yield http
    await http.aclose()


@pytest.fixture
def world(dm, front, roster, wire, monkeypatch):  # noqa: F811
    monkeypatch.setattr(task_conversation, "front_agent_id", AsyncMock(return_value=FRONT))
    monkeypatch.setattr(container_rpc, "call", AsyncMock(return_value={}))
    return dm


def _start(trigger_id="voice:c:r1"):
    return StartIn(
        tenant_id=WS, principal=Who(kind="member", id=OWNER), conversation_id=DM, trigger_id=trigger_id,
        source="voice", title="Find a slot", brief="Caller asks.",
    )  # fmt: skip


async def _seed_line(e):
    return (await _agent_line(DM, "Hello.", "IDEMP#AGENT#seed"))["messageId"]


def _answer_ready(monkeypatch):
    _answer_world(monkeypatch, {})
    asked = {**Q, "kind": "approval", "title": "Send it?", "createdAt": "t0", "version": 2}
    tree = {
        "task": {
            "taskId": TASK,
            "status": "idle",
            "version": 1,
            "identifier": "LB-1",
            "title": "Send",
            "updatedAt": "t1",
        },
        "attempts": [],
        "questions": [asked],
    }
    monkeypatch.setattr(tasks_repo, "get_task_tree", AsyncMock(return_value=tree))
    monkeypatch.setattr(effects_repo, "effects_of_task", AsyncMock(return_value=[]))


def _act_ready(monkeypatch):
    _act_world(monkeypatch)
    monkeypatch.setattr(api, "_mark_action", AsyncMock())
    monkeypatch.setattr(api, "_summary", AsyncMock(return_value={"id": TASK, "version": 3}))


ME = Who(kind="member", id=OWNER)

#: (name, prepare(monkeypatch), op(engine module)): each op runs in process first, then over HTTP on the same world.
OPS = [
    ("open_conversation", None, lambda e: e.open_conversation(OpenIn(
        tenant_id=WS, conversation_id=CALL, client_key="call:1", kind="call", creator=ME, seat_front_agent=True,
        annotations={"call": {"status": "live", "n": 1}}))),
    ("annotate", None, lambda e: _then(_room(CALL, "call"), e.annotate(
        AnnotateIn(tenant_id=WS, conversation_id=CALL, set={"call": {"status": "done"}})))),
    ("append_line_member", None, lambda e: e.append_line(_member_line("voice-turn:k1", "Hello there"))),
    ("append_line_notice", None, lambda e: e.append_line(LineIn(
        tenant_id=WS, conversation_id=DM, principal=Who(kind="notice", id="lightbulb"), text="Moved.",
        idempotency_key="notice:1"))),
    ("start_task", None, lambda e: e.start_task(_start())),
    ("submit_event", None, lambda e: e.submit_event(EventIn(
        tenant_id=WS, trigger_id="webhook:e1", source="webhook", admission="candidate", principal=ME, text="Done.",
        conversation_id=DM, product_ref={"meetingFollowups": {"uid": "u1"}}))),
    ("read_lines_first", None, lambda e: _then(_seed_line(e), e.read_lines(WS, DM, None))),
    ("read_lines", None, lambda e: _then(_seed_line(e), e.read_lines(WS, DM, 0))),
    ("read_history", None, lambda e: _then(_seed_line(e), e.read_history(WS, DM, 10))),
    ("get_lines", None, lambda e: _lines(e)),
    ("get_conversation", None, lambda e: e.get_conversation(WS, DM, with_members=True)),
    ("get_conversation_member", None, lambda e: e.get_conversation(WS, DM, member=OWNER)),
    ("get_conversation_missing", None, lambda e: e.get_conversation(WS, CALL)),
    ("find_conversation", None, lambda e: e.find_conversation(DM, include_deleted=True)),
    ("list_conversations", None, lambda e: e.list_conversations(WS, ["dm", "call"], 5)),
    ("get_roster", None, lambda e: e.get_roster(WS)),
    ("get_front_agent", None, lambda e: e.get_front_agent(WS, DM)),
    ("seat_roster", None, lambda e: e.seat_roster(WS, DM)),
    ("get_task", None, lambda e: _then(api.start_task(_start()), e.get_task(WS, api.start_task_id("voice:c:r1")))),
    ("get_task_tree", None, lambda e: e.get_task(WS, TASK, tree=True)),
    ("get_trigger", None, lambda e: _then(api.start_task(_start()), e.get_trigger(WS, "voice:c:r1"))),
    ("list_conversation_tasks", None, lambda e: e.list_conversation_tasks(WS, DM)),
    ("answer", _answer_ready, lambda e: e.answer(AnswerIn(
        tenant_id=WS, task_id=TASK, qid="q-1", principal=ME, option_id="approve", client_answer_id="c1"))),
    ("act", _act_ready, lambda e: e.act(ActIn(tenant_id=WS, task_id=TASK, action="stop", principal=ME, version=3))),
    ("warm", None, lambda e: _warm(e)),
]  # fmt: skip


async def _then(first, second):
    await first
    return await second


async def _lines(e):
    return await e.get_lines(WS, DM, [await _seed_line(e), None, "no-such-message"])


async def _warm(e):
    e.warm(WS, [FRONT], DM)
    await _drain()


async def _drain():
    import asyncio

    while pending := [*api._background, *client._background]:
        await asyncio.gather(*pending)


@pytest.mark.parametrize("name,prepare,op", OPS, ids=[o[0] for o in OPS])
async def test_http_result_equals_in_process(world, monkeypatch, name, prepare, op):
    if prepare:
        prepare(monkeypatch)
    here = await op(api)
    there = await op(client)
    assert type(there) is type(here)
    if name in ("start_task", "submit_event"):  # the same id again replays: the HTTP call is the second
        assert (here.created, there.created) == (True, False)
        here, there = here.model_copy(update={"created": False}), there
    assert transport.dump(there) == transport.dump(here)


async def test_warm_reaches_the_runtime(world):
    await _warm(client)
    assert container_rpc.call.await_args_list[0].args == (WS, "health")
    assert container_rpc.call.await_args_list[1].args[:2] == (WS, "profile.warm")


async def test_retry_replays_same_message(world, wire, monkeypatch):
    """The connection was refused (nothing reached the engine): the client's one retry, same body, writes the line
    once, and the conversation holds one message and one admission."""
    failing = _FirstFails(wire._transport, "refused")
    monkeypatch.setattr(wire, "_transport", failing)
    out = await client.append_line(_member_line("voice-turn:retry", "Book Tuesday"))
    assert failing.calls == 2
    [row] = await messaging_repo.list_messages_before(DM, 10**19, 10)
    assert row["messageId"] == out.message_id
    assert len(_rows(world, "WORK#ADMIT#")) == 1


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_a_write_that_may_have_run_is_never_sent_again(world, wire, monkeypatch, status):
    """A gateway error after the engine got a write: it may have run, so the client raises instead of replaying (a
    replayed claim or unversioned action could answer differently)."""
    failing = _FirstFails(wire._transport, status)
    monkeypatch.setattr(wire, "_transport", failing)
    with pytest.raises(httpx.HTTPStatusError) as failed:
        await client.append_line(_member_line("voice-turn:once", "Book Tuesday"))
    assert (failing.calls, failed.value.response.status_code) == (1, status)
    assert len(await messaging_repo.list_messages_before(DM, 10**19, 10)) == 1  # the engine wrote it, once


@pytest.mark.parametrize("status,calls", [(502, 2), (503, 2), (504, 1)])
async def test_a_read_retries_only_a_gateway_that_never_ran_it(world, wire, monkeypatch, status, calls):
    failing = _FirstFails(wire._transport, status)
    monkeypatch.setattr(wire, "_transport", failing)
    if status == 504:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_conversation(WS, DM)
    else:
        assert (await client.get_conversation(WS, DM)).conversation_id == DM
    assert failing.calls == calls


async def test_the_retry_gets_only_the_time_left(monkeypatch):
    """Every attempt together stays within the budget: the second attempt is given what the first left."""
    import asyncio

    seen: list[float] = []

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(request.extensions["timeout"]["read"])
            await asyncio.sleep(0.2)
            raise httpx.ConnectError("refused", request=request)

    monkeypatch.setitem(transport._clients, BASE, httpx.AsyncClient(transport=Slow(), base_url=BASE))
    with pytest.raises(httpx.ConnectError):
        await transport.send(BASE, "GET", "/x", attempt_s=0.25, budget_s=0.3)
    assert seen[0] == 0.25 and 0 < seen[1] <= 0.1 + 0.02


def test_budgets_sit_under_their_callers_timeouts():
    """A live call's engine calls, all attempts, under the voice worker's 10 s route timeout; a hook call, all
    attempts, well under the shortest activity that calls one (30 s)."""
    from core.temporal import workflows

    assert transport.ENGINE_ATTEMPT_S <= transport.ENGINE_BUDGET_S < 10
    assert transport.HOOK_ATTEMPT_S <= transport.HOOK_BUDGET_S <= 12
    assert transport.HOOK_BUDGET_S < workflows._SHORT["start_to_close_timeout"].total_seconds()


def test_the_client_has_every_operation():
    """`core.engine.client` matches every public function of `core.engine.api`, signature for signature."""
    import inspect

    ops = {n for n, f in vars(api).items() if inspect.isfunction(f) and f.__module__ == api.__name__}
    ops = {n for n in ops if not n.startswith("_") and n != "start_task_id"}
    for name in sorted(ops):
        assert hasattr(client, name), name
        assert inspect.signature(getattr(client, name)) == inspect.signature(getattr(api, name)), name


async def test_delete_tenant_over_http(world, wire, monkeypatch):
    """The terminal wipe answers the same over HTTP, and is never sent a second time, not even after a refused
    connection (the wipe's own caller runs it again)."""
    from core.repositories import automations_repo
    from core.temporal import client as temporal_client
    from tests.unit.services.test_wipe_engine_state import _FakeTemporal

    monkeypatch.setattr(automations_repo, "list_schedules", AsyncMock(return_value=[]))
    temporal_client.set_client(_FakeTemporal(running=set(), schedules=set()))
    try:
        assert transport.dump(await client.delete_tenant("org_gone")) == transport.dump(
            await api.delete_tenant("org_gone")
        )
        failing = _FirstFails(wire._transport, "refused")
        monkeypatch.setattr(wire, "_transport", failing)
        with pytest.raises(httpx.ConnectError):
            await client.delete_tenant("org_gone")
        assert failing.calls == 1
    finally:
        temporal_client.set_client(None)


async def test_a_claim_replayed_after_it_landed_is_still_the_callers(world):
    """`annotate` with `if_absent`: the same value again is the caller's own claim (True), on both transports;
    another value is not."""
    await _room(CALL, "channel", extra={"meeting": {"uid": "u1"}})
    claim = {"meeting": {"ticketNonce": "n1", "botClaimAt": 5}}
    for e in (api, client):
        req = AnnotateIn(tenant_id=WS, conversation_id=CALL, set=claim, if_absent="ticketNonce")
        assert await e.annotate(req) is True
        other = req.model_copy(update={"set": {"meeting": {"ticketNonce": "n2", "botClaimAt": 6}}})
        assert await e.annotate(other) is False
    assert (await messaging_repo.get_room(CALL))["meeting"]["ticketNonce"] == "n1"


async def test_bad_token_401(world, monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_SERVICE_TOKEN", "")  # unset: every call refused, the empty bearer too
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await client.get_conversation(WS, DM)
    assert refused.value.response.status_code == 401
    r = await _raw_get(monkeypatch, "Bearer wrong")
    assert r.status_code == 401


async def _raw_get(monkeypatch, bearer: str):
    monkeypatch.setattr(settings, "ENGINE_SERVICE_TOKEN", TOKEN)
    http = transport._clients[BASE]
    return await http.get(f"/engine/v1/conversations/{DM}", params={"tenantId": WS}, headers={"Authorization": bearer})


@pytest.mark.parametrize("code,status,case,body", ANSWER_ERRORS, ids=[c[0] for c in ANSWER_ERRORS])
async def test_engine_error_status_identical(world, monkeypatch, code, status, case, body):
    """Each refusal raises the same EngineError (code, status, body) over HTTP as in process, never retried."""
    option = _answer_world(monkeypatch, case)
    req = AnswerIn(tenant_id=WS, task_id=TASK, qid="q-1", principal=ME, option_id=option, client_answer_id="c1")
    refusals = []
    for e in (api, client):
        with pytest.raises(EngineError) as err:
            await e.answer(req)
        refusals.append((err.value.code, err.value.status, err.value.detail))
    assert refusals == [(code, status, body)] * 2


async def test_conversation_refusals_identical(world):
    foreign = AnnotateIn(tenant_id="org_other", conversation_id=DM, set={"call": {}})
    engine_field = OpenIn(tenant_id=WS, conversation_id=CALL, client_key="c", kind="call", creator=ME,
                          annotations={"frontAgentId": "x"})  # fmt: skip
    for call in (lambda e: e.annotate(foreign), lambda e: e.open_conversation(engine_field)):
        refusals = []
        for e in (api, client):
            with pytest.raises(EngineError) as err:
                await call(e)
            refusals.append((err.value.code, err.value.status, err.value.detail))
        assert refusals[0] == refusals[1]


async def test_a_lost_revision_race_raises_the_store_error(world):
    """`annotate` over a writer since the caller's read raises botocore's cancellation, which the products' merge
    loops retry on, as in process."""
    await _room(CALL, "call", extra={"call": {"status": "live"}})
    stale = (await messaging_repo.get_room(CALL))["revision"] - 1
    with pytest.raises(ClientError) as lost:
        await client.annotate(
            AnnotateIn(tenant_id=WS, conversation_id=CALL, set={"call": {"x": 1}}, expected_revision=stale)
        )
    assert messaging_repo.is_cancelled(lost.value)


async def test_an_app_refusal_is_the_same_problem(world):
    """A refusal by the app's own door (a person not in the room) is the same ApiProblem, status and body."""
    stranger = _member_line("voice-turn:x", "Hi").model_copy(update={"principal": Who(kind="member", id="user_x")})
    problems = []
    for e in (api, client):
        with pytest.raises(ApiProblem) as err:
            await e.append_line(stranger)
        problems.append((err.value.status_code, err.value.body()))
    assert problems[0] == problems[1]


async def test_a_product_refusal_inside_an_op_is_relayed(world, monkeypatch):
    """An HTTPException raised inside an operation (a product hook's refusal) reaches the caller as itself."""
    monkeypatch.setattr(api, "read_lines", AsyncMock(side_effect=HTTPException(404, "call_not_found")))
    with pytest.raises(HTTPException) as err:
        await client.read_lines(WS, DM, 0)
    assert (err.value.status_code, err.value.detail) == (404, "call_not_found")


async def test_a_path_that_names_another_id_is_refused(world, wire):
    body = transport.dump(_member_line("voice-turn:p", "Hi"))
    r = await wire.post(
        f"/engine/v1/conversations/{CALL}/lines", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert (r.status_code, r.json()) == (400, {"detail": {"code": "path_mismatch"}})
    assert await messaging_repo.list_messages_before(DM, 10**19, 10) == []


def test_engine_follows_the_setting(monkeypatch):
    import core.engine as package

    for transport_name, module in (("http", client), ("inprocess", api)):
        monkeypatch.setattr(settings, "ENGINE_TRANSPORT", transport_name)
        monkeypatch.delitem(vars(package), "engine", raising=False)
        assert package.engine is module
    monkeypatch.delitem(vars(package), "engine", raising=False)
