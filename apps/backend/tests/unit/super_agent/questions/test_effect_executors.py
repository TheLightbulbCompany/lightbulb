"""Landing executors (wave2-Q Task 6): done / refused (definitely not done) / unknown (maybe done, never retried)."""

from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import agent_mail_send, paperclip_github_token
from core.services import effect_executors as ex

OWNER = "org_1"
MAIL = {"agentId": "a-1", "to": "acme@example.com", "subject": "Q3 invoice", "body": "Attached."}


@pytest.mark.parametrize(
    ("result", "outcome"),
    [("sent", "mail:sent"), ("error", ex.ExecutorUnknown), ("rate_limited", ex.ExecutorRefused)],
)
async def test_mail_send(monkeypatch, result, outcome):
    send = AsyncMock(return_value=result)
    monkeypatch.setattr(agent_mail_send, "send", send)
    if isinstance(outcome, str):
        assert await ex.mail_send(OWNER, MAIL, "user_sam") == outcome
    else:
        with pytest.raises(outcome):
            await ex.mail_send(OWNER, MAIL, "user_sam")
    kw = send.await_args.kwargs
    assert (kw["agent_id"], kw["to"], kw["subject"], kw["body_text"], kw["purpose"]) == (
        "a-1",
        "acme@example.com",
        "Q3 invoice",
        "Attached.",
        "task",
    )


PR = {"repo": "acme/web", "number": 7, "sha": "abc123"}
PATH = "/repos/acme/web/pulls/7"


@pytest.fixture
def github(monkeypatch):
    """GitHub behind a MockTransport: `routes[(method, path)]` answers; every request is recorded."""
    monkeypatch.setattr(paperclip_github_token, "github_token", AsyncMock(return_value="ghp_x"))
    state = {"routes": {}, "seen": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["seen"].append((request.method, request.url.path, request.content))
        answer = state["routes"][(request.method, request.url.path)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ex, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return state


async def test_pr_merge_merges_the_reviewed_sha(github):
    github["routes"][("GET", PATH)] = httpx.Response(200, json={"merged": False})
    github["routes"][("PUT", f"{PATH}/merge")] = httpx.Response(200, json={"merged": True, "sha": "m1"})
    assert await ex.pr_merge(OWNER, PR, "user_sam") == "pr:acme/web#7:m1"
    assert github["seen"][-1] == ("PUT", f"{PATH}/merge", b'{"merge_method":"merge","sha":"abc123"}')


async def test_pr_merge_retry_after_lost_response_is_done_not_merged_twice(github):
    github["routes"][("GET", PATH)] = httpx.Response(200, json={"merged": True, "merge_commit_sha": "m1"})
    assert await ex.pr_merge(OWNER, PR, "user_sam") == "pr:acme/web#7:m1"
    assert [m for m, *_ in github["seen"]] == ["GET"]


async def test_pr_merge_newer_push_refused(github):
    github["routes"][("GET", PATH)] = httpx.Response(200, json={"merged": False})
    github["routes"][("PUT", f"{PATH}/merge")] = httpx.Response(409, json={"message": "Head branch was modified"})
    with pytest.raises(ex.ExecutorRefused) as err:
        await ex.pr_merge(OWNER, PR, "user_sam")
    assert err.value.code == "github_409"


async def test_pr_merge_transport_error_after_send_is_unknown(github):
    github["routes"][("GET", PATH)] = httpx.Response(200, json={"merged": False})
    github["routes"][("PUT", f"{PATH}/merge")] = httpx.ReadTimeout("slow")
    with pytest.raises(ex.ExecutorUnknown):
        await ex.pr_merge(OWNER, PR, "user_sam")


async def test_pr_merge_unreachable_before_send_is_refused(github):
    github["routes"][("GET", PATH)] = httpx.ConnectError("down")
    with pytest.raises(ex.ExecutorRefused) as err:
        await ex.pr_merge(OWNER, PR, "user_sam")
    assert err.value.code == "github_unreachable"


async def test_pr_merge_without_github(monkeypatch):
    monkeypatch.setattr(paperclip_github_token, "github_token", AsyncMock(return_value=None))
    with pytest.raises(ex.ExecutorRefused) as err:
        await ex.pr_merge(OWNER, PR, "user_sam")
    assert err.value.code == "github_not_connected"


def test_executor_kinds():
    assert set(ex.EXECUTORS) == {"mail.send", "pr.merge"}
