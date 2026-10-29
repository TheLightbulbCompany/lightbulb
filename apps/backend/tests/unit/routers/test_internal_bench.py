"""/api/v1/internal/bench: the BEAM runner's dev-only door to the workspace engine."""

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import memory_recall, memory_stages  # noqa: E402
from main import app  # noqa: E402
from routers import internal_bench  # noqa: E402

AT = datetime(2024, 3, 15, 12, 0, 0, 7, tzinfo=timezone.utc)
TURNS = [
    {
        "session_id": "s0",
        "ordinal": 0,
        "speaker": "Ann Lee",
        "role": "user",
        "text": "We use postgres.",
        "at": AT.isoformat(),
    },
    {
        "session_id": "s0",
        "ordinal": 1,
        "speaker": "Assistant",
        "role": "assistant",
        "text": "Noted.",
        "at": AT.isoformat(),
    },
    {"session_id": "s0", "ordinal": 2, "speaker": "Ann Lee", "role": "user", "text": "Bye.", "at": AT.isoformat()},
]


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setattr(internal_bench.settings, "ENVIRONMENT", "dev", raising=False)
    monkeypatch.setattr(internal_bench.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    seen = []

    async def ok(owner_id, request):
        seen.append(owner_id)
        return {}

    monkeypatch.setattr(internal_bench.container_auth, "_require_hook_token", ok)
    client = MagicMock()
    client.batch_create_memory_records.return_value = {"successfulRecords": [], "failedRecords": []}
    monkeypatch.setattr(internal_bench.workspace_memory, "_client", lambda: client)
    return TestClient(app), client, seen


def test_every_route_is_404_outside_dev(dev, monkeypatch):
    http, _, _ = dev
    monkeypatch.setattr(internal_bench.settings, "ENVIRONMENT", "prod", raising=False)
    assert http.post("/api/v1/internal/bench/org_a/projects", json={"name": "x"}).status_code == 404
    assert (
        http.post("/api/v1/internal/bench/org_a/recall", json={"bench_owner": "bench-x", "question": "q"}).status_code
        == 404
    )


def test_routes_refuse_without_the_hook_token(monkeypatch):
    monkeypatch.setattr(internal_bench.settings, "ENVIRONMENT", "dev", raising=False)
    with patch.object(internal_bench.container_auth.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        assert TestClient(app).post("/api/v1/internal/bench/org_a/projects", json={"name": "x"}).status_code == 401


def test_a_project_is_a_synthetic_bench_owner(dev):
    http, _, seen = dev
    owner = http.post("/api/v1/internal/bench/org_a/projects", json={"name": "beam 1"}).json()["owner_id"]
    assert owner.startswith("bench-") and len(owner) == 32 and seen == ["org_a"]


def test_the_token_reaches_only_bench_owners(dev):
    http, client, _ = dev
    r = http.post("/api/v1/internal/bench/org_a/ingest", json={"bench_owner": "org_b", "turns": TURNS})
    assert r.status_code == 422
    assert http.get("/api/v1/internal/bench/org_a/status/org_b").status_code == 422
    client.create_event.assert_not_called()


def test_ingest_writes_dated_exchanges_and_dated_turn_records(dev):
    http, client, _ = dev
    r = http.post("/api/v1/internal/bench/org_a/ingest", json={"bench_owner": "bench-abc", "turns": TURNS})
    assert r.json() == {"events": 2, "turns": 3}
    first, second = (c.kwargs for c in client.create_event.call_args_list)
    assert first["actorId"] == "bench-abc" and first["eventTimestamp"] == AT and first["sessionId"] == "bench-abc-s0"
    assert [p["conversational"]["role"] for p in first["payload"]] == ["USER", "ASSISTANT"]
    assert first["payload"][0]["conversational"]["content"]["text"] == "[2024-03-15] Ann Lee: We use postgres."
    assert len(second["payload"]) == 1
    (batch,) = client.batch_create_memory_records.call_args_list
    rec = batch.kwargs["records"][0]
    assert rec["namespaces"] == ["/turns/bench-abc/"] and rec["timestamp"] == AT
    assert rec["content"]["text"] == "Ann Lee: We use postgres."
    assert rec["metadata"]["validFrom"] == {"numberValue": int(AT.timestamp())}


def test_recall_passes_as_of_and_dates_turns_by_valid_from(dev):
    http, _, _ = dev
    page = {"asOf": AT.isoformat(), "usedEvidenceTokens": 0, "results": []}
    rec = AsyncMock(return_value=page)
    with patch.object(internal_bench.memory_recall, "recall", rec):
        r = http.post(
            "/api/v1/internal/bench/org_a/recall",
            json={"bench_owner": "bench-abc", "question": "db?", "as_of": "2024-03-15T12:00:00.000007Z"},
        )
    assert r.json() == page
    kw = rec.await_args.kwargs
    assert kw["as_of"] == AT and kw["dated_turns"] is True and kw["limit"] == 100 and kw["budget_tokens"] == 8000
    assert kw["namespaces"]["turns"] == "/turns/bench-abc/"


def test_dated_turns_filter_the_turn_arm_on_valid_from():
    client = MagicMock()
    client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
    ns = {"facts": "/owners/bench-a/", "turns": "/turns/bench-a/", "entries": "/entries/bench-a/"}
    asyncio.run(memory_recall._search_part(client, "mem-1", ns, "q", 10, AT, False, set(), True))
    by_ns = {
        c.kwargs["namespace"]: c.kwargs["searchCriteria"].get("metadataFilters")
        for c in client.retrieve_memory_records.call_args_list
    }
    assert by_ns["/turns/bench-a/"] == [memory_recall._flt("validFrom", "LESS_THAN_OR_EQUALS", AT.timestamp())]
    assert by_ns["/entries/bench-a/"][0]["left"]["metadataKey"] == "x-amz-agentcore-memory-createdAt"


def test_a_turn_with_valid_from_happened_then():
    row = {
        "memoryRecordId": "mem-1",
        "content": {"text": "Ann: hi"},
        "createdAt": datetime(2026, 10, 29, tzinfo=timezone.utc),
        "metadata": {"validFrom": {"numberValue": 1710504000}},
    }
    assert memory_stages.from_record(row, "turn").occurred_at == datetime(2024, 3, 15, 12, tzinfo=timezone.utc)
    assert memory_stages.from_record({**row, "metadata": {}}, "turn").occurred_at.year == 2026


def test_a_local_run_takes_the_env_token_for_a_bench_caller_only(monkeypatch):
    monkeypatch.setattr(internal_bench.settings, "ENVIRONMENT", "local", raising=False)
    monkeypatch.setenv("BENCH_LOCAL_TOKEN", "local-secret")
    http = TestClient(app)
    ok = {"Authorization": "Bearer local-secret"}
    assert http.post("/api/v1/internal/bench/bench-01x/projects", json={"name": "x"}, headers=ok).status_code == 200
    bad = {"Authorization": "Bearer nope"}
    assert http.post("/api/v1/internal/bench/bench-01x/projects", json={"name": "x"}, headers=bad).status_code == 401
    assert http.post("/api/v1/internal/bench/org_a/projects", json={"name": "x"}, headers=ok).status_code == 422
    monkeypatch.setattr(internal_bench.settings, "ENVIRONMENT", "dev", raising=False)  # deployed: no env token
    with patch.object(internal_bench.container_auth.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        assert http.post("/api/v1/internal/bench/bench-01x/projects", json={"name": "x"}, headers=ok).status_code == 401


def test_a_prod_memory_is_refused(dev, monkeypatch):
    http, client, _ = dev
    monkeypatch.setattr(
        internal_bench.settings, "AGENTCORE_MEMORY_ID", "lightbulb_prod_workspace_memory-x", raising=False
    )
    assert (
        http.post("/api/v1/internal/bench/org_a/ingest", json={"bench_owner": "bench-abc", "turns": TURNS}).status_code
        == 503
    )
    client.create_event.assert_not_called()


def test_valid_to_zero_is_a_current_value_with_and_without_as_of():
    row = {
        "memoryRecordId": "mem-2",
        "content": {"text": "Ann Lee: garden has tulips."},
        "metadata": {
            "status": {"stringValue": "current"},
            "validFrom": {"numberValue": 1710460800.0},
            "validTo": {"numberValue": 0.0},
        },
    }
    c = memory_stages.from_record(row, "claim")
    assert c.status == "current" and c.valid_to is None
    ended = {**row, "metadata": {**row["metadata"], "validTo": {"numberValue": 1713052800.0}}}
    assert memory_stages.from_record(ended, "claim").status == "replaced"
    variants = memory_recall._as_of_variants(AT)
    assert [
        memory_recall._flt("validFrom", "LESS_THAN_OR_EQUALS", AT.timestamp()),
        memory_recall._flt("validTo", "EQUALS_TO", 0.0),
    ] in variants
