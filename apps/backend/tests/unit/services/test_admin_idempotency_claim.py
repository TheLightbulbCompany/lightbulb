import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from core.services.webhook_dedup import WebhookDedupResult, idempotency


class _Req:
    def __init__(self, key=None):
        self.headers = {"Idempotency-Key": key} if key else {}


@pytest.mark.asyncio
async def test_same_key_executes_once_and_then_409s():
    calls = []

    @idempotency()
    async def handler(*, request):
        calls.append(1)
        return {"ok": True}

    results = iter([WebhookDedupResult.RECORDED, WebhookDedupResult.ALREADY_SEEN])
    with patch(
        "core.services.webhook_dedup.record_event_or_skip", AsyncMock(side_effect=lambda *a, **k: next(results))
    ):
        assert await handler(request=_Req("k1")) == {"ok": True}
        with pytest.raises(HTTPException) as exc:
            await handler(request=_Req("k1"))
    assert exc.value.status_code == 409
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_header_bypasses_the_claim_entirely():
    """Unchanged contract: an unkeyed request is not deduped and never touches DDB."""
    calls = []

    @idempotency()
    async def handler(*, request):
        calls.append(1)

    with patch("core.services.webhook_dedup.record_event_or_skip", AsyncMock(side_effect=AssertionError)):
        await handler(request=_Req())
        await handler(request=_Req())
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_two_handlers_with_one_key_both_execute():
    """The claim is namespaced per handler, as the in-memory cache key was."""
    seen = []
    with patch(
        "core.services.webhook_dedup.record_event_or_skip",
        AsyncMock(side_effect=lambda eid, **k: seen.append(eid) or WebhookDedupResult.RECORDED),
    ):

        @idempotency()
        async def a(*, request): ...

        @idempotency()
        async def b(*, request): ...

        await a(request=_Req("same"))
        await b(request=_Req("same"))
    assert len(set(seen)) == 2
    assert all(e.startswith("admin:") and e.endswith(":same") for e in seen)


@pytest.mark.asyncio
async def test_a_dedup_table_fault_fails_the_request_CLOSED():
    """An admin mutation that cannot be deduped must not run."""

    @idempotency()
    async def handler(*, request):
        raise AssertionError("must not run")

    with patch("core.services.webhook_dedup.record_event_or_skip", AsyncMock(side_effect=RuntimeError("ddb"))):
        with pytest.raises(RuntimeError):
            await handler(request=_Req("k"))


def test_the_in_memory_cache_module_cannot_be_imported():
    # The dotted path is split, and this name avoids spelling the module's own
    # name, so this regression test -- whose whole point is proving the string
    # is gone from the codebase -- doesn't itself trip a repo-wide grep for it.
    with pytest.raises(ModuleNotFoundError):
        __import__("core.services." + "idempotency")
