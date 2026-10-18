import json

import httpx
import pytest

from core.config import settings
from core.services import photon

PRJ = "/v1/projects/pho_prj_1"


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(settings, "PHOTON_API_KEY", "pho_sk_x")
    monkeypatch.setattr(settings, "PHOTON_PROJECT_ID", "pho_prj_1")
    monkeypatch.setattr(settings, "PHOTON_WEBHOOK_SECRET", "whsec_x")
    yield
    monkeypatch.setattr(photon, "_transport", None)
    photon._reset_client()


def _mock(monkeypatch, handler):
    calls = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(photon, "_transport", httpx.MockTransport(wrapped))
    photon._reset_client()
    return calls


async def test_calls_share_one_client(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={}))
    await photon.get_operation("op1")
    first = photon._client
    await photon.get_operation("op2")
    assert first is not None and photon._client is first


def test_enabled(monkeypatch):
    assert photon.enabled()
    monkeypatch.setattr(settings, "PHOTON_WEBHOOK_SECRET", "")
    assert not photon.enabled()


async def test_send_text_body_and_headers(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(202, json={"id": "msg_1"}))
    out = await photon.send_text(sender_id="u1", space_id="sp1", text="hi", idempotency_key="k1")
    assert out["id"] == "msg_1"
    (req,) = calls
    assert req.method == "POST"
    assert req.url.path == f"{PRJ}/messages"
    assert req.headers["authorization"] == "Bearer pho_sk_x"
    assert req.headers["idempotency-key"] == "k1"
    assert json.loads(req.content) == {
        "senderId": "u1",
        "spaceId": "sp1",
        "content": {"type": "text", "format": "plain", "text": "hi"},
    }


async def test_send_text_has_no_dry_run_short_circuit(monkeypatch):
    """A bench injects ``_transport``; the production client never fakes a send."""
    calls = _mock(monkeypatch, lambda r: httpx.Response(202, json={"id": "msg_2"}))
    out = await photon.send_text(sender_id="u1", space_id="dry:abc", text="hi", idempotency_key="k")
    assert out == {"id": "msg_2"} and len(calls) == 1


async def test_problem_code_mapping(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(402, json={"code": "payment_required", "status": 402}))
    with pytest.raises(photon.PhotonError) as ei:
        await photon.send_text(sender_id="u", space_id="s", text="t", idempotency_key="k")
    assert (ei.value.status, ei.value.code) == (402, "payment_required")


async def test_non_string_problem_code_is_a_string(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(409, json={"code": 4091}))
    with pytest.raises(photon.PhotonError) as ei:
        await photon.get_operation("op1")
    assert ei.value.code == "4091"


async def test_unparseable_error_is_http_status(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(photon.PhotonError) as ei:
        await photon.get_operation("op1")
    assert ei.value.code == "http_502"


async def test_404_is_not_found(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, text="gone"))
    with pytest.raises(photon.PhotonError) as ei:
        await photon.get_operation("op1")
    assert (ei.value.status, ei.value.code) == (404, "not_found")
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"code": "space_missing"}))
    with pytest.raises(photon.PhotonError) as ei:
        await photon.get_operation("op1")
    assert (ei.value.status, ei.value.code) == (404, "space_missing")


async def test_typing_posts_and_swallows(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(202, json={}))
    await photon.send_typing(sender_id="u1", space_id="sp1", typing=False)
    (req,) = calls
    assert req.url.path == f"{PRJ}/spaces/sp1/typing"
    assert json.loads(req.content) == {"senderId": "u1", "typing": False}
    _mock(monkeypatch, lambda r: httpx.Response(500, json={}))
    await photon.send_typing(sender_id="u1", space_id="sp1")


async def test_mark_read_idempotency_and_swallows(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(202, json={}))
    await photon.mark_read("m1")
    (req,) = calls
    assert req.url.path == f"{PRJ}/messages/m1/read"
    assert req.headers["idempotency-key"]

    def boom(r):
        raise httpx.ConnectError("x")

    _mock(monkeypatch, boom)
    await photon.mark_read("m1")


async def test_request_dedicated_line(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(202, json={"operationId": "op1", "state": "pending"}))
    out = await photon.request_dedicated_line(idempotency_key="idem")
    assert out["operationId"] == "op1"
    (req,) = calls
    assert req.url.path == f"{PRJ}/platforms/imessage/dedicated"
    assert req.headers["idempotency-key"] == "idem"


async def test_get_operation_unwraps_the_operation(monkeypatch):
    # The GET answers GetOperationResponse {operation, resource?}; create/cancel/delete answer the bare Operation.
    wrapped = {
        "operation": {"operationId": "op1", "state": "succeeded", "resourceId": "res1"},
        "resource": {"resourceId": "res1"},
    }
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json=wrapped))
    out = await photon.get_operation("op1")
    assert out["state"] == "succeeded" and out["resourceId"] == "res1"
    assert calls[0].method == "GET" and calls[0].url.path == f"{PRJ}/platforms/operations/op1"


async def test_get_operation_takes_the_resource_id_from_the_wrapper_when_the_operation_lacks_it(monkeypatch):
    wrapped = {"operation": {"operationId": "op1", "state": "succeeded"}, "resource": {"resourceId": "res9"}}
    _mock(monkeypatch, lambda r: httpx.Response(200, json=wrapped))
    out = await photon.get_operation("op1")
    assert out["resourceId"] == "res9" and out["state"] == "succeeded"


async def test_cancel_swallows_409_only(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(409, json={"code": "finished"}))
    await photon.cancel_operation("op1")
    assert calls[0].url.path == f"{PRJ}/platforms/operations/op1/cancel"
    _mock(monkeypatch, lambda r: httpx.Response(500, json={}))
    with pytest.raises(photon.PhotonError):
        await photon.cancel_operation("op1")


async def test_find_line_user_pages_and_matches(monkeypatch):
    def handler(r: httpx.Request):
        assert r.url.params["platform"] == "imessage"
        if "pageToken" not in r.url.params:
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "a", "handle": "+1", "details": {"resourceId": "r0"}}],
                    "nextPageToken": "t.2",
                },
            )
        assert r.url.params["pageToken"] == "t.2"
        return httpx.Response(200, json={"items": [{"id": "b", "handle": "+2", "details": {"resourceId": "r1"}}]})

    calls = _mock(monkeypatch, handler)
    assert await photon.find_line_user("r1") == ({"id": "b", "handle": "+2"}, None)
    assert len(calls) == 2 and calls[0].url.path == f"{PRJ}/users"
    assert await photon.find_line_user("nope") == (None, None)


async def test_find_line_user_stops_after_the_page_cap_and_resumes_from_its_token(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json={"items": [], "nextPageToken": "t.n"}))
    assert await photon.find_line_user("r1") == (None, "t.n")
    assert len(calls) == photon.FIND_USER_MAX_PAGES
    assert await photon.find_line_user("r1", page_token="t.n") == (None, "t.n")
    assert calls[photon.FIND_USER_MAX_PAGES].url.params["pageToken"] == "t.n"  # the next look starts there


@pytest.mark.parametrize("status", [200, 202, 404])
async def test_release_line_ok(monkeypatch, status):
    calls = _mock(monkeypatch, lambda r: httpx.Response(status, json={}))
    assert await photon.release_line("res1") is None
    assert calls[0].method == "DELETE" and calls[0].url.path == f"{PRJ}/platforms/imessage/dedicated/res1"


async def test_release_line_202_is_an_operation_looked_at_once(monkeypatch):
    """A 202 release counts only once its operation succeeded; an unfinished one comes back for the caller."""
    op = {"operationId": "op9", "state": "running"}

    def handler(r: httpx.Request):
        if r.method == "DELETE":
            return httpx.Response(202, json={"operationId": "op9", "state": "pending"})
        return httpx.Response(200, json={"operation": op})

    calls = _mock(monkeypatch, handler)
    assert (await photon.release_line("res1"))["state"] == "running"
    assert [(c.method, c.url.path) for c in calls] == [
        ("DELETE", f"{PRJ}/platforms/imessage/dedicated/res1"),
        ("GET", f"{PRJ}/platforms/operations/op9"),
    ]
    op["state"] = "succeeded"
    assert await photon.release_line("res1") is None


async def test_release_line_other_error_raises(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(429, json={"code": "rate"}))
    with pytest.raises(photon.PhotonError):
        await photon.release_line("res1")
