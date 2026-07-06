"""Hermes run.completed usage metering (H2.5) — the billing trap.

Hermes' ``run.completed.usage`` reports a CACHE-INCLUSIVE ``input_tokens``
(the full prompt incl. cached tokens). Metering that as uncached input
over-bills cache-heavy sessions. The H1.2 fork patch adds
``uncached_input_tokens`` (= ``input_tokens - cache_read``) alongside
``cache_read_tokens`` / ``cache_write_tokens``; ``send_chat`` must meter the
NON-cache-inclusive figure as input, never the raw ``input_tokens``.

These tests drive ``send_chat`` over the same ``httpx.MockTransport`` fake
Hermes server the transport tests use, and capture the ``record_usage`` call by
patching ``core.services.usage_service.record_usage`` (resolved lazily inside
the adapter, so patching the module attribute intercepts it).
"""

import json

import httpx

from core.runtime.base import ChatResult
from core.runtime.hermes.adapter import (
    HermesRuntime,
    _member_from_session_key,
    _resolve_uncached_input,
)

_IP = "10.0.0.9"
_KEY = "api-key-abc"
_RUN_ID = "run_1"


def _sse(frames: list[tuple[str, dict]]) -> bytes:
    return "".join(f"data: {json.dumps({'event': name, **payload})}\n\n" for name, payload in frames).encode()


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _install(monkeypatch, handler) -> None:
    async def fake_resolve(self, owner_id):
        return _IP, _KEY

    monkeypatch.setattr(HermesRuntime, "_resolve", fake_resolve)
    monkeypatch.setattr(
        "core.runtime.hermes.adapter._client_factory",
        lambda: _mock_client(handler),
    )


def _install_usage_recorder(monkeypatch) -> list[dict]:
    """Capture every record_usage call's kwargs. Patches the usage_service
    attribute the adapter resolves lazily at call time."""
    recorded: list[dict] = []

    async def fake_record_usage(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr("core.services.usage_service.record_usage", fake_record_usage)
    return recorded


def _handler_with(sse_body: bytes):
    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    return handler


async def _collect(rt: HermesRuntime, session_key: str = "agent:agent_x:u1") -> tuple[list[dict], ChatResult | None]:
    events: list[dict] = []

    async def on_event(ev):
        events.append(ev)

    result = await rt.send_chat("org_1", "agent_x", "hi there", session_key, on_event)
    return events, result


# ---------------------------------------------------------------------------
# _resolve_uncached_input — the billing-trap resolver, unit-tested in isolation
# ---------------------------------------------------------------------------


def test_resolve_uncached_input_prefers_patched_field():
    # Patched payload: uncached_input_tokens present → meter THAT, not the
    # cache-inclusive input_tokens.
    usage = {
        "input_tokens": 1000,
        "uncached_input_tokens": 200,
        "cache_read_tokens": 800,
        "cache_write_tokens": 50,
    }
    assert _resolve_uncached_input(usage) == (200, 800, 50, True)


def test_resolve_uncached_input_computes_when_field_absent():
    # uncached_input_tokens missing but input_tokens + cache_read present →
    # compute input_tokens - cache_read; flagged unpatched.
    usage = {"input_tokens": 1000, "cache_read_tokens": 800}
    assert _resolve_uncached_input(usage) == (200, 800, 0, False)


def test_resolve_uncached_input_computed_never_negative():
    usage = {"input_tokens": 100, "cache_read_tokens": 500}
    input_tokens, cache_read, cache_write, patched = _resolve_uncached_input(usage)
    assert input_tokens == 0  # max(0, 100 - 500)
    assert (cache_read, cache_write, patched) == (500, 0, False)


def test_resolve_uncached_input_falls_back_to_raw_when_no_cache_fields():
    # Only input_tokens, no cache fields → best-effort raw input; flagged unpatched.
    usage = {"input_tokens": 300}
    assert _resolve_uncached_input(usage) == (300, 0, 0, False)


# ---------------------------------------------------------------------------
# _member_from_session_key — best-effort member attribution
# ---------------------------------------------------------------------------


def test_member_from_session_key_extracts_clerk_user():
    assert _member_from_session_key("agent:agent_x:user_abc", "org_1") == "user_abc"


def test_member_from_session_key_falls_back_to_owner():
    # Personal webchat ("main") and non-user segments attribute to the owner.
    assert _member_from_session_key("agent:agent_x:main", "org_1") == "org_1"
    assert _member_from_session_key("agent:agent_x:u1", "org_1") == "org_1"
    assert _member_from_session_key("garbage", "org_1") == "org_1"


# ---------------------------------------------------------------------------
# send_chat — end-to-end usage metering on run.completed
# ---------------------------------------------------------------------------


async def test_run_completed_meters_uncached_input(monkeypatch):
    # (a) Patched fields present → record_usage called with input=uncached
    # (NOT the cache-inclusive input_tokens), output, cache_read, cache_write.
    sse_body = _sse(
        [
            ("message.delta", {"delta": "Hi"}),
            (
                "run.completed",
                {
                    "output": "Hi",
                    "model": "amazon-bedrock/anthropic.claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 5000,
                        "uncached_input_tokens": 120,
                        "output_tokens": 80,
                        "cache_read_tokens": 4880,
                        "cache_write_tokens": 30,
                    },
                },
            ),
        ]
    )
    _install(monkeypatch, _handler_with(sse_body))
    recorded = _install_usage_recorder(monkeypatch)

    events, result = await _collect(HermesRuntime())

    assert {"type": "done"} in events
    assert result == ChatResult(run_id=_RUN_ID, status="completed", output="Hi")
    assert len(recorded) == 1
    call = recorded[0]
    assert call["owner_id"] == "org_1"
    assert call["user_id"] == "org_1"  # session "agent:agent_x:u1" → owner fallback
    assert call["model"] == "amazon-bedrock/anthropic.claude-sonnet-4-6"
    # The trap: input is the NON-cache-inclusive figure, never 5000.
    assert call["input_tokens"] == 120
    assert call["output_tokens"] == 80
    assert call["cache_read"] == 4880
    assert call["cache_write"] == 30


async def test_run_completed_computes_uncached_when_patch_field_absent(monkeypatch):
    # (b) uncached_input_tokens missing but input_tokens + cache_read present →
    # adapter computes input_tokens - cache_read.
    sse_body = _sse(
        [
            (
                "run.completed",
                {
                    "output": "ok",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 40,
                        "cache_read_tokens": 800,
                    },
                },
            ),
        ]
    )
    _install(monkeypatch, _handler_with(sse_body))
    recorded = _install_usage_recorder(monkeypatch)

    await _collect(HermesRuntime())

    assert len(recorded) == 1
    call = recorded[0]
    assert call["input_tokens"] == 200  # 1000 - 800, NOT 1000
    assert call["output_tokens"] == 40
    assert call["cache_read"] == 800
    assert call["cache_write"] == 0


async def test_run_completed_falls_back_to_raw_input_and_logs(monkeypatch, caplog):
    # (c) Only input_tokens (no cache fields) → falls back to input_tokens and
    # logs a debug that the fork patch fields were absent (H4 catches unpatched).
    import logging

    sse_body = _sse(
        [
            (
                "run.completed",
                {"output": "ok", "usage": {"input_tokens": 300, "output_tokens": 10}},
            ),
        ]
    )
    _install(monkeypatch, _handler_with(sse_body))
    recorded = _install_usage_recorder(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="core.runtime.hermes.adapter"):
        await _collect(HermesRuntime())

    assert len(recorded) == 1
    call = recorded[0]
    assert call["input_tokens"] == 300  # raw fallback
    assert call["cache_read"] == 0
    assert call["cache_write"] == 0
    # The unpatched-image debug fired.
    assert any("uncached_input_tokens" in r.message for r in caplog.records)


async def test_run_completed_usage_failure_does_not_break_turn(monkeypatch):
    # (d) record_usage raising must NOT break the chat turn — the done event is
    # still emitted, the terminal ChatResult still returns, nothing escapes.
    sse_body = _sse(
        [
            ("message.delta", {"delta": "answer"}),
            ("run.completed", {"output": "answer", "usage": {"input_tokens": 10, "output_tokens": 5}}),
        ]
    )
    _install(monkeypatch, _handler_with(sse_body))

    async def boom(**kwargs):
        raise RuntimeError("ddb down")

    monkeypatch.setattr("core.services.usage_service.record_usage", boom)

    events, result = await _collect(HermesRuntime())

    assert events == [{"type": "chunk", "content": "answer"}, {"type": "done"}]
    assert result == ChatResult(run_id=_RUN_ID, status="completed", output="answer")


async def test_run_completed_without_usage_records_nothing(monkeypatch):
    # A terminal frame with no usage dict → no record_usage call (nothing to meter).
    sse_body = _sse([("run.completed", {"output": "ok"})])
    _install(monkeypatch, _handler_with(sse_body))
    recorded = _install_usage_recorder(monkeypatch)

    events, result = await _collect(HermesRuntime())

    assert {"type": "done"} in events
    assert result == ChatResult(run_id=_RUN_ID, status="completed", output="ok")
    assert recorded == []


async def test_run_completed_attributes_member_from_session_key(monkeypatch):
    # A Clerk-user session key attributes usage to that member (not the owner).
    sse_body = _sse(
        [("run.completed", {"usage": {"input_tokens": 10, "uncached_input_tokens": 4, "output_tokens": 2}})]
    )
    _install(monkeypatch, _handler_with(sse_body))
    recorded = _install_usage_recorder(monkeypatch)

    await _collect(HermesRuntime(), session_key="agent:agent_x:user_zzz")

    assert len(recorded) == 1
    assert recorded[0]["owner_id"] == "org_1"
    assert recorded[0]["user_id"] == "user_zzz"
    assert recorded[0]["input_tokens"] == 4
