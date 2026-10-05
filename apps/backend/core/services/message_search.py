"""Cross-room message search over OpenSearch Serverless.

Access is decided HERE, from DynamoDB, on every request: the caller's current
rooms become both the index list and a ``terms`` filter, so the index never
decides who sees what. The query travels in a POST body and is never logged."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session
from pydantic import Field

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.repositories import messaging_repo
from core.services import messaging
from core.services.messaging import ApiProblem, ContractModel

logger = logging.getLogger(__name__)

MARK_START, MARK_END = "\u0001", "\u0002"
_SEARCH_TIMEOUT_S = 25.0  # under API Gateway's 30 s; a cold start can take 10–30 s
_SOURCE = ["messageId", "roomId", "messageSeq", "threadRootMessageId", "authorName", "authorKind", "createdAt", "text"]
_OPERATORS = set('"-+|*()~')

_credentials = None
_client: httpx.AsyncClient | None = None


class MessageSearchRequest(ContractModel):
    q: str = Field(min_length=1, max_length=200)
    room_id: uuid.UUID | None = None
    limit: int = Field(default=30, ge=1, le=50)


class MessageSearchHit(ContractModel):
    message_id: str
    room_id: uuid.UUID
    message_seq: int
    thread_root_message_id: str | None
    author_name: str
    author_kind: str
    created_at: str
    snippet: str


class MessageSearchResponse(ContractModel):
    hits: list[MessageSearchHit]


def index_name(owner_id: str) -> str:
    # MUST match apps/infra/lambda/messaging-search-indexer/index.py:index_name.
    return "messages-" + hashlib.sha256(owner_id.encode()).hexdigest()[:32]


def build_query(q: str, room_ids: list[str], limit: int) -> dict:
    fields = ["text^3", "authorName"]
    # Phrases, -exclusions, prefix*; never throws on bad syntax.
    should: list[dict] = [
        {"simple_query_string": {"query": q, "fields": fields, "default_operator": "and", "boost": 2}}
    ]
    if not _OPERATORS & set(q):
        # Typo tolerance -- only when the person typed no operators, or this OR
        # would match what a phrase or an exclusion asked to leave out.
        should.append({"multi_match": {"query": q, "fields": fields, "operator": "and", "fuzziness": "AUTO"}})
    return {
        "size": limit,
        "_source": _SOURCE,
        "query": {
            "function_score": {
                "query": {
                    "bool": {"filter": [{"terms": {"roomId": room_ids}}], "should": should, "minimum_should_match": 1}
                },
                # Recency nudges, never buries: the factor runs 0.5–1.5.
                "functions": [
                    {
                        "gauss": {"createdAt": {"origin": "now", "offset": "7d", "scale": "90d", "decay": 0.5}},
                        "weight": 1,
                    },
                    {"weight": 0.5},
                ],
                "score_mode": "sum",
                "boost_mode": "multiply",
            }
        },
        "highlight": {
            "pre_tags": [MARK_START],
            "post_tags": [MARK_END],
            "fields": {"text": {"number_of_fragments": 1, "fragment_size": 160, "no_match_size": 160}},
        },
    }


async def _request(
    method: str, path: str, body: dict | None = None, timeout: float = _SEARCH_TIMEOUT_S
) -> httpx.Response:
    # Credentials and the client are cached like live_events.LiveEventPublisher:
    # a fresh botocore session per call is a blocking credentials fetch on ECS.
    global _credentials, _client
    if _credentials is None:
        _credentials = get_session().get_credentials()
    if _client is None:
        _client = httpx.AsyncClient()
    data = json.dumps(body).encode() if body is not None else b""
    url = f"{settings.SEARCH_ENDPOINT.rstrip('/')}{path}"
    signed = AWSRequest(
        method=method,
        url=url,
        data=data,
        headers={"content-type": "application/json", "x-amz-content-sha256": hashlib.sha256(data).hexdigest()},
    )
    SigV4Auth(_credentials.get_frozen_credentials(), "aoss", settings.AWS_REGION).add_auth(signed)
    return await _client.request(method, url, content=data, headers=dict(signed.headers.items()), timeout=timeout)


def _unavailable() -> ApiProblem:
    return ApiProblem(
        "SEARCH_UNAVAILABLE", "Search is starting up. Try again in a few seconds.", retryable=True, retry_after=3
    )


async def search(auth: AuthContext, request: MessageSearchRequest) -> MessageSearchResponse:
    if not settings.SEARCH_ENDPOINT:
        raise ApiProblem("SEARCH_UNAVAILABLE", "Message search is not available yet.")
    q = request.q.strip()
    rooms = await messaging.searchable_rooms(resolve_owner_id(auth), auth.user_id)
    if request.room_id is not None:
        room_id = str(request.room_id)
        if room_id not in rooms:
            raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
        rooms = {room_id: rooms[room_id]}
    if not q or not rooms:
        return MessageSearchResponse(hits=[])
    indexes = ",".join(sorted({index_name(owner) for owner in rooms.values()}))
    try:
        response = await _request(
            "POST", f"/{indexes}/_search?ignore_unavailable=true", build_query(q, list(rooms), request.limit)
        )
    except httpx.TimeoutException as exc:
        raise _unavailable() from exc
    if response.status_code >= 500:
        logger.warning("message search unavailable status=%s", response.status_code)
        raise _unavailable()
    if response.status_code != 200:
        logger.error("message search rejected status=%s", response.status_code)
        raise ApiProblem("INTERNAL_ERROR", "Search failed.")
    candidates = [h for h in response.json().get("hits", {}).get("hits", []) if h["_source"].get("roomId") in rooms]
    # The index is eventually consistent; DynamoDB is the truth. A hit survives only
    # while its row exists, is not deleted, and still carries the text that was indexed.
    rows = await messaging_repo.get_messages_by_keys(
        [(h["_source"]["roomId"], h["_source"]["messageSeq"]) for h in candidates]
    )
    # The search call can outlast a membership removal: re-read membership (strongly)
    # for exactly the rooms that have hits, so removed members never see their text.
    owner_id, limit = resolve_owner_id(auth), asyncio.Semaphore(messaging._SUMMARY_CONCURRENCY)

    async def reads(room_id: str) -> bool:
        async with limit:
            return await messaging.still_reads_room(owner_id, auth.user_id, room_id, rooms[room_id])

    room_ids = sorted({h["_source"]["roomId"] for h in candidates})
    readable = {r for r, ok in zip(room_ids, await asyncio.gather(*(reads(r) for r in room_ids))) if ok}
    hits = []
    for hit in candidates:
        source = hit["_source"]
        row = rows.get((source["roomId"], source["messageSeq"]))
        if source["roomId"] not in readable or row is None or row.get("deletedAt"):
            continue
        if (row["editedText"] if "editedText" in row else row.get("text") or "") != source.get("text", ""):
            continue
        snippet = (hit.get("highlight", {}).get("text") or [source.get("text", "")[:160]])[0]
        hits.append(
            MessageSearchHit(
                message_id=source["messageId"],
                room_id=source["roomId"],
                message_seq=source["messageSeq"],
                thread_root_message_id=source.get("threadRootMessageId"),
                author_name=source.get("authorName", ""),
                author_kind=source.get("authorKind", ""),
                created_at=source["createdAt"],
                snippet=snippet,
            )
        )
    return MessageSearchResponse(hits=hits)


async def warm(auth: AuthContext) -> None:
    """Wake search capacity when someone opens a search box. Any request wakes a
    scaled-to-zero collection; this waits at most 2 s."""
    if not settings.SEARCH_ENDPOINT:
        return
    try:
        await _request("GET", f"/{index_name(resolve_owner_id(auth))}/_count", timeout=2.0)
    except httpx.HTTPError:
        pass


async def delete_owner_index(owner_id: str) -> str:
    """Drop a wiped owner's index. Runs AFTER the room partitions are gone; an
    index job still in flight then re-reads a missing row and deletes, not re-adds."""
    if not settings.SEARCH_ENDPOINT:
        return "search off"
    try:
        response = await _request("DELETE", f"/{index_name(owner_id)}", timeout=60.0)
    except httpx.TimeoutException as exc:
        raise RuntimeError("search index delete timed out (cold collection); re-run the wipe") from exc
    if response.status_code not in (200, 404):
        raise RuntimeError(f"search index delete failed status={response.status_code}")
    return "dropped" if response.status_code == 200 else "no index"
