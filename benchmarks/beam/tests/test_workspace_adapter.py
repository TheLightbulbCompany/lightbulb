import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from adapters.base import Turn, recall_json
from adapters.workspace import SETTLE_S, WorkspaceBackend

AT = datetime(2024, 3, 15, 12, tzinfo=timezone.utc) + timedelta(microseconds=187)


class Server:
    def __init__(self):
        self.seen, self.facts, self.fail = [], 0, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, body, request.headers.get("authorization")))
        if self.fail:
            self.fail -= 1
            return httpx.Response(429)
        path = request.url.path.split("/api/v1/internal/bench/org_dev")[1]
        if path == "/projects":
            return httpx.Response(200, json={"owner_id": "bench-01abc"})
        if path == "/ingest":
            return httpx.Response(200, json={"events": 1, "turns": len(body["turns"])})
        if path.startswith("/status/"):
            return httpx.Response(200, json={"facts": self.facts, "turns": 2})
        if path == "/recall":
            results = [{"kind": "claim", "content": "x" * 400, "sources": []} for _ in range(150)]
            return httpx.Response(200, json={"asOf": body["as_of"].replace("Z", "+00:00"), "usedEvidenceTokens": 1, "results": results})
        return httpx.Response(204)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("BEAM_WORKSPACE_API", "https://api-dev.example")
    monkeypatch.setenv("BEAM_WORKSPACE_OWNER", "org_dev")
    monkeypatch.setenv("BEAM_WORKSPACE_HOOK_TOKEN", "tok")
    monkeypatch.setattr("adapters.workspace.RETRY_DELAYS_S", (0, 0, 0))
    server, now = Server(), [0.0]
    b = WorkspaceBackend(transport=httpx.MockTransport(server), clock=lambda: now[0])
    return b, server, now


def test_turns_go_in_with_their_dataset_times_and_the_hook_token(backend):
    b, server, _ = backend
    turns = [Turn("s0", 0, "Ann", "user", "hi", AT), Turn("s0", 1, "Assistant", "assistant", "hello", AT)]
    project = asyncio.run(b.create_project("beam 1"))
    asyncio.run(b.ingest_batch(project, turns))
    method, path, body, auth = server.seen[-1]
    assert path.endswith("/ingest") and auth == "Bearer tok" and body["bench_owner"] == "bench-01abc"
    assert body["turns"][0] == {"session_id": "s0", "ordinal": 0, "speaker": "Ann", "role": "user", "text": "hi",
                                "at": "2024-03-15T12:00:00.000187Z"}


def test_a_project_is_done_once_its_facts_stop_moving(backend):
    b, server, now = backend
    asyncio.run(b.ingest_batch("bench-01abc", [Turn("s0", 0, "Ann", "user", "hi", AT)]))
    server.facts = 3
    assert asyncio.run(b.ingestion_status("bench-01abc")) == "pending"  # moved from nothing to 3
    now[0] += 10
    assert asyncio.run(b.ingestion_status("bench-01abc")) == "pending"  # not polled again yet
    polls = len(server.seen)
    now[0] += SETTLE_S
    assert asyncio.run(b.ingestion_status("bench-01abc")) == "done"
    assert len(server.seen) == polls + 1


def test_recall_sends_as_of_echoes_it_and_the_page_is_trimmed_as_past_dev(backend):
    b, server, _ = backend
    server.fail = 1  # a throttle is retried
    page = asyncio.run(b.recall("bench-01abc", "db?", AT, "ann"))
    assert server.seen[-1][2]["as_of"] == "2024-03-15T12:00:00.000187Z"
    assert page.as_of == AT and b.stats["throttled"] == 1 and b.stats["retries"] == 1
    trimmed = recall_json(page)
    assert trimmed["asOf"] == "2024-03-15T12:00:00.000187Z"
    assert len(trimmed["results"]) == 80 and trimmed["usedEvidenceTokens"] == 8000  # 100 tokens each
