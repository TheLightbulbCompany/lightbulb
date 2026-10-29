"""/api/v1/internal/bench -- the BEAM runner's door to the workspace engine. Dev only.

A benchmark conversation is a synthetic owner ``bench-<ulid>`` with no container, runtime or
member: only its AgentCore namespaces. The caller authenticates with a real owner's hook token
(``container_auth._require_hook_token``, the gate of the other internal routes), and every route
refuses an owner id that is not ``bench-``, so the token reaches nothing but bench namespaces.
Outside dev (and local/test) every route is 404.

- ingest: each user turn and the assistant turn after it become one ``CreateEvent`` under
  ``actorId = <bench owner>`` with ``eventTimestamp`` = the dataset's time, as the runtime plugin
  writes a room exchange; the ``workspace_claims`` strategy extracts from them as from any room.
  Each turn is also written as a turn record in ``/turns/<o>/`` (what the sync's turn mirror
  writes), stamped with the dataset's time and a ``validFrom`` so recall as of a moment can
  filter it. There is no sync for a bench owner: no member, so nothing to import or mirror.
- status: how many fact and turn records the namespaces hold; extraction is asynchronous, so the
  caller polls until the fact count settles.
- recall: ``memory_recall.recall`` as the Memory tab calls it (expansion and rerank on), as of
  the question's moment, turns dated by ``validFrom``.
"""

import hashlib
import hmac
import os
import re
import time
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from ulid import ULID

from core.config import settings
from core.dynamodb import run_in_thread
from core.services import container_auth, memory_recall, workspace_memory
from core.services.memory_sync import _chunks, _strings, _token, _write_ok

router = APIRouter()

_DEV = ("dev", "local", "test")
_TEXT_MAX = 100_000  # an event's Content.text, as the runtime plugin cuts it
_RECORD_TEXT_MAX = 16_000  # a memory record's content.text (BatchCreateMemoryRecords)
_SESSION = "[^a-zA-Z0-9-_]"


async def _gate(owner_id: str, request: Request) -> None:
    if settings.ENVIRONMENT not in _DEV:
        raise HTTPException(status_code=404, detail="Not Found")
    local = os.environ.get("BENCH_LOCAL_TOKEN", "")
    if settings.ENVIRONMENT == "local" and local:
        # A local run (scripts/bench_local.py) has no container row to hold a hook token: the
        # token comes from env, and only for a bench- caller. Never on a deployed env.
        _bench(owner_id)
        presented = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(presented.encode("utf-8"), local.encode("utf-8")):
            raise HTTPException(status_code=401, detail="invalid bench token")
        return
    await container_auth._require_hook_token(owner_id, request)


def _bench(owner: str) -> str:
    if not owner.startswith("bench-"):
        raise HTTPException(status_code=422, detail="not a bench owner")
    workspace_memory.owner_namespace(owner)  # refuses / .. :
    return owner


def _memory_id() -> str:
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id or "prod" in memory_id.lower():
        raise HTTPException(status_code=503, detail="no bench-safe workspace memory in this environment")
    return memory_id


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class BenchTurn(BaseModel):
    session_id: str = Field(min_length=1, max_length=80)
    ordinal: int = Field(ge=0)
    speaker: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(min_length=1)
    at: datetime


class IngestBody(BaseModel):
    bench_owner: str
    turns: list[BenchTurn] = Field(min_length=1, max_length=100)


class BenchRecallBody(BaseModel):
    bench_owner: str
    question: str = Field(min_length=1, max_length=2000)
    as_of: datetime | None = None
    budget_tokens: int = Field(default=8000, ge=1, le=50_000)
    limit: int = Field(default=100, ge=1, le=200)


@router.post("/{owner_id}/projects")
async def create_project(owner_id: str, body: ProjectBody, request: Request) -> dict:
    await _gate(owner_id, request)
    return {"owner_id": f"bench-{str(ULID()).lower()}"}


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _exchanges(turns: list[BenchTurn]) -> list[list[BenchTurn]]:
    """A user turn and the assistant turn after it, as the plugin's ``sync_turn`` pairs them."""
    out: list[list[BenchTurn]] = []
    for t in turns:
        if out and t.role == "assistant" and len(out[-1]) == 1 and out[-1][0].role == "user":
            out[-1].append(t)
        else:
            out.append([t])
    return out


def _ingest(client, memory_id: str, owner: str, turns: list[BenchTurn]) -> dict:
    events = 0
    for exchange in _exchanges(turns):
        session = re.sub(_SESSION, "-", f"{owner}-{exchange[0].session_id}")[:100]
        payload = [
            {
                # The day leads the text: the extraction model reads "the date of the conversation"
                # from the turns themselves (a room's turns are dated now; these are not).
                "conversational": {
                    "content": {"text": f"[{_utc(t.at):%Y-%m-%d}] {t.speaker}: {t.text}"[:_TEXT_MAX]},
                    "role": "USER" if t.role == "user" else "ASSISTANT",
                }
            }
            for t in exchange
        ]
        client.create_event(
            memoryId=memory_id, actorId=owner, sessionId=session, eventTimestamp=_utc(exchange[0].at), payload=payload
        )
        events += 1
    ns = workspace_memory.namespaces_for(owner, None)["turns"]
    records = [
        {
            "requestIdentifier": hashlib.sha256(f"{owner}:{t.session_id}:{t.ordinal}".encode()).hexdigest(),
            "namespaces": [ns],
            "content": {"text": f"{t.speaker}: {t.text}"[:_RECORD_TEXT_MAX]},
            "timestamp": _utc(t.at),
            "metadata": {
                **_strings(speaker=t.speaker, session=t.session_id),
                "validFrom": {"numberValue": int(_utc(t.at).timestamp())},
            },
        }
        for t in turns
    ]
    for chunk in _chunks(records):
        _write_ok(
            client.batch_create_memory_records(
                memoryId=memory_id, records=chunk, clientToken=_token(r["requestIdentifier"] for r in chunk)
            ),
            "bench turn",
        )
    return {"events": events, "turns": len(records)}


@router.post("/{owner_id}/ingest")
async def ingest(owner_id: str, body: IngestBody, request: Request) -> dict:
    await _gate(owner_id, request)
    owner, memory_id = _bench(body.bench_owner), _memory_id()
    client = workspace_memory._client()
    return await run_in_thread(_ingest, client, memory_id, owner, body.turns)


def _count(client, memory_id: str, namespace: str) -> int:
    return len(
        workspace_memory._pages(
            client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespace=namespace
        )
    )


@router.get("/{owner_id}/status/{bench_owner}")
async def status(owner_id: str, bench_owner: str, request: Request) -> dict:
    await _gate(owner_id, request)
    ns, memory_id = workspace_memory.namespaces_for(_bench(bench_owner), None), _memory_id()
    client = workspace_memory._client()
    facts = await run_in_thread(_count, client, memory_id, ns["facts"])
    turns = await run_in_thread(_count, client, memory_id, ns["turns"])
    return {"facts": facts, "turns": turns}


@router.post("/{owner_id}/recall")
async def recall(owner_id: str, body: BenchRecallBody, request: Request) -> dict:
    await _gate(owner_id, request)
    owner, memory_id = _bench(body.bench_owner), _memory_id()
    return await memory_recall.recall(
        memory_id=memory_id,
        namespaces=workspace_memory.namespaces_for(owner, None),
        question=body.question.strip(),
        as_of=_utc(body.as_of) if body.as_of else None,
        budget_tokens=body.budget_tokens,
        limit=body.limit,
        dated_turns=True,
    )


def _delete(client, memory_id: str, owner: str) -> int:
    """The bench owner's records and events only (no drafts or sessions: it has none)."""
    ns = list(workspace_memory.namespaces_for(owner, None).values())
    workspace_memory._delete_events(client, owner, memory_id)
    for attempt in range(3):  # deletes are eventually consistent: a record can fail once
        try:
            return workspace_memory._delete_records(client, f"bench={owner}", memory_id, ns)
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(10)
    return 0


@router.delete("/{owner_id}/projects/{bench_owner}")
async def delete_project(owner_id: str, bench_owner: str, request: Request) -> dict:
    await _gate(owner_id, request)
    owner, memory_id = _bench(bench_owner), _memory_id()
    return {"records": await run_in_thread(_delete, workspace_memory._client(), memory_id, owner)}
