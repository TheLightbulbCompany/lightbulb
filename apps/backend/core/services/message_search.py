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
from datetime import datetime, timedelta, timezone

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

# New messages reach the index in a batch drain (MESSAGE_SEARCH_BATCH_MINUTES = 39 in
# apps/infra/lib/stacks/api-stack.ts), so the last hour is read live from DynamoDB.
_FRESH_WINDOW = timedelta(minutes=60)
_FRESH_ROOMS = 20  # the most recently active rooms
_FRESH_PER_ROOM = 30  # newest rows read per room
# DynamoDB reads one search may run at once (live scan and room recheck together): the
# process's thread pool is 6 (core/dynamodb.py), and one search must never take it over.
_READS_AT_ONCE = 3
# A cold index takes 10-30 s to wake (_SEARCH_TIMEOUT_S). Live rows wait this long for it, then answer alone:
# W13 "pumpkin" sat behind a waking index for 8 s and the palette read the wait as "No results".
_FRESH_ALONE_AFTER_S = 2.0

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


def build_query(q: str, room_ids: list[str], limit: int, *, field: str = "roomId", source: list[str] = _SOURCE) -> dict:
    fields = ["text^3", "authorName"]
    # Phrases, -exclusions, prefix*; never throws on bad syntax.
    should: list[dict] = [
        {"simple_query_string": {"query": q, "fields": fields, "default_operator": "and", "boost": 2}}
    ]
    if not _OPERATORS & set(q):
        # Typo tolerance -- only when the person typed no operators, or this OR
        # would match what a phrase or an exclusion asked to leave out.
        # prefix_length 2: a typo may not change the first two letters. Without it the
        # stemmed "taglin" reached "again" (2 edits) and flooded prod results.
        should.append(
            {"multi_match": {"query": q, "fields": fields, "operator": "and", "fuzziness": "AUTO", "prefix_length": 2}}
        )
    return {
        "size": limit,
        "_source": source,
        "query": {
            "function_score": {
                "query": {
                    "bool": {"filter": [{"terms": {field: room_ids}}], "should": should, "minimum_should_match": 1}
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
    metas = await messaging.searchable_room_metas(resolve_owner_id(auth), auth.user_id)
    if request.room_id is not None:
        metas = [meta for meta in metas if meta["roomId"] == str(request.room_id)]
        if not metas:
            raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    rooms = {meta["roomId"]: meta["ownerId"] for meta in metas}
    if not q or not rooms:
        return MessageSearchResponse(hits=[])
    indexes = ",".join(sorted({index_name(owner) for owner in rooms.values()}))
    gate = asyncio.Semaphore(_READS_AT_ONCE)
    # The live scan runs while the index answers, not after it.
    index = asyncio.create_task(
        _request("POST", f"/{indexes}/_search?ignore_unavailable=true", build_query(q, list(rooms), request.limit))
    )
    fresh: list[dict] = []
    response: httpx.Response | None = None
    try:
        fresh = await _unindexed_rows(q, metas, gate)
        response = await (asyncio.wait_for(index, _FRESH_ALONE_AFTER_S) if fresh else index)
    except (TimeoutError, httpx.TimeoutException) as exc:
        if not fresh:
            raise _unavailable() from exc
    finally:
        index.cancel()  # a no-op once it answered
    if response is not None and response.status_code >= 500:
        logger.warning("message search unavailable status=%s", response.status_code)
        if not fresh:
            raise _unavailable()
        response = None
    if response is not None and response.status_code != 200:
        logger.error("message search rejected status=%s", response.status_code)
        raise ApiProblem("INTERNAL_ERROR", "Search failed.")
    found = response.json().get("hits", {}).get("hits", []) if response is not None else []
    candidates = [h for h in found if h["_source"].get("roomId") in rooms]
    # The index is eventually consistent; DynamoDB is the truth. A hit survives only
    # while its row exists, is not deleted, and still carries the text that was indexed.
    rows = await messaging_repo.get_messages_by_keys(
        [(h["_source"]["roomId"], h["_source"]["messageSeq"]) for h in candidates]
    )
    # The search call can outlast a membership removal: for exactly the rooms that have hits,
    # apply the room door's full rule (membership, plus session, app and task rooms) again.
    owner_id = resolve_owner_id(auth)

    async def reads(room_id: str) -> bool:
        async with gate:
            return await messaging.still_opens_room(owner_id, auth.user_id, room_id)

    room_ids = sorted({h["_source"]["roomId"] for h in candidates} | {row["roomId"] for row in fresh})
    readable = {r for r, ok in zip(room_ids, await asyncio.gather(*(reads(r) for r in room_ids))) if ok}
    hits = [_fresh_hit(row, q) for row in fresh if row["roomId"] in readable]
    seen = {hit.message_id for hit in hits}
    for hit in candidates:
        source = hit["_source"]
        row = rows.get((source["roomId"], source["messageSeq"]))
        if source["roomId"] not in readable or row is None or row.get("deletedAt") or source["messageId"] in seen:
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
    return MessageSearchResponse(hits=hits[: request.limit])


def _row_text(row: dict) -> str:
    return row["editedText"] if "editedText" in row else row.get("text") or ""


def _terms(q: str) -> list[str]:
    return "".join(" " if c in _OPERATORS else c for c in q).lower().split()


async def _unindexed_rows(q: str, metas: list[dict], gate: asyncio.Semaphore) -> list[dict]:
    """Messages too new for the index: the last hour of the most recently active rooms,
    read from DynamoDB and kept when they hold every word of ``q``. Newest first.

    ponytail: a plain substring match (no stemming, no typos) over <= 20 rooms x 30 rows;
    the index answers everything older, so this only bridges the drain."""
    terms = _terms(q)
    since = datetime.now(timezone.utc) - _FRESH_WINDOW

    def recent(stamp: object) -> bool:
        try:
            return isinstance(stamp, str) and datetime.fromisoformat(stamp) >= since
        except ValueError:
            return False

    active = sorted((m for m in metas if recent(m.get("lastMessageAt"))), key=lambda m: m["lastMessageAt"])
    active = active[-_FRESH_ROOMS:]
    if not terms or not active:
        return []

    async def tail(meta: dict) -> list[dict]:
        async with gate:
            return await messaging_repo.list_messages_before(
                meta["roomId"], int(meta.get("lastSeq") or 0) + 1, _FRESH_PER_ROOM
            )

    rows = [row for rows in await asyncio.gather(*(tail(m) for m in active)) for row in rows]
    return sorted(
        (
            row
            for row in rows
            if not row.get("deletedAt")
            and recent(row.get("createdAt"))
            and all(term in _row_text(row).lower() for term in terms)
        ),
        key=lambda row: (row["createdAt"], int(row["messageSeq"])),
        reverse=True,
    )


def _fresh_hit(row: dict, q: str) -> MessageSearchHit:
    """A hit for a row the index has not seen yet; the snippet marks the first word like the index's highlight."""
    text, term = _row_text(row), _terms(q)[0]
    at = max(text.lower().find(term), 0)
    start = max(0, at - 60)
    end = at + len(term)
    snippet = f"{text[start:at]}{MARK_START}{text[at:end]}{MARK_END}{text[end : start + 160]}"
    author = row.get("author") or {}
    return MessageSearchHit(
        message_id=row["messageId"],
        room_id=row["roomId"],
        message_seq=int(row["messageSeq"]),
        thread_root_message_id=row.get("threadRootMessageId"),
        author_name=author.get("displayName") or "",
        author_kind=author.get("kind") or "",
        created_at=row["createdAt"],
        snippet=snippet,
    )


class SessionSearchRequest(ContractModel):
    q: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=30, ge=1, le=50)


class SessionSearchHit(ContractModel):
    session_id: str
    seq: int
    #: "person" (a prompt) or "agent" (the assistant's reply).
    author_kind: str
    #: The person who typed it; empty for the assistant.
    author_id: str
    #: The assistant's name; a person's is the client's to resolve from author_id.
    author_name: str
    created_at: str
    snippet: str
    session_title: str
    provider: str
    human_user_id: str


class SessionSearchResponse(ContractModel):
    hits: list[SessionSearchHit]


_SESSION_SOURCE = ["sessionId", "seq", "authorKind", "authorId", "authorName", "createdAt", "text"]


async def search_sessions(auth: AuthContext, request: SessionSearchRequest) -> SessionSearchResponse:
    """What was said in coding sessions. The sessions list decides what is searchable (from
    DynamoDB, on this request); the index only ranks text inside that set."""
    from core.repositories import local_sessions_repo
    from core.services import local_sessions  # lazy: it imports messaging, like this module

    if not settings.SEARCH_ENDPOINT:
        raise ApiProblem("SEARCH_UNAVAILABLE", "Search is not available yet.")
    q = request.q.strip()
    sessions = await local_sessions.searchable_sessions(auth)
    if not q or not sessions:
        return SessionSearchResponse(hits=[])
    owner_id = resolve_owner_id(auth)
    query = build_query(q, sorted(sessions), request.limit, field="sessionId", source=_SESSION_SOURCE)
    try:
        response = await _request("POST", f"/{index_name(owner_id)}/_search?ignore_unavailable=true", query)
    except httpx.TimeoutException as exc:
        raise _unavailable() from exc
    if response.status_code >= 500:
        logger.warning("session search unavailable status=%s", response.status_code)
        raise _unavailable()
    if response.status_code != 200:
        logger.error("session search rejected status=%s", response.status_code)
        raise ApiProblem("INTERNAL_ERROR", "Search failed.")
    candidates = [
        h for h in response.json().get("hits", {}).get("hits", []) if h["_source"].get("sessionId") in sessions
    ]
    # The search can outlast a pause or a removal: re-read the rows with hits (strongly).
    ids = sorted({h["_source"]["sessionId"] for h in candidates})
    # A member removed while the index answered gets nothing (rooms re-check membership the same way).
    if ids and not await local_sessions.still_in_workspace(owner_id, auth.user_id):
        return SessionSearchResponse(hits=[])
    rows = await local_sessions_repo.get_sessions(owner_id, ids)
    readable = {sid: row for sid, row in zip(ids, rows) if local_sessions.still_searchable(row, auth.user_id)}
    # A teammate removed meanwhile keeps a shared row; their sessions leave search with them.
    others = sorted({row["humanUserId"] for row in readable.values()} - {auth.user_id})
    members = await asyncio.gather(*(local_sessions.still_in_workspace(owner_id, human) for human in others))
    gone = {human for human, ok in zip(others, members) if not ok}
    readable = {sid for sid, row in readable.items() if row["humanUserId"] not in gone}
    hits = []
    for hit in candidates:
        source = hit["_source"]
        session = sessions[source["sessionId"]]
        if session.session_id not in readable:
            continue
        hits.append(
            SessionSearchHit(
                session_id=session.session_id,
                seq=source["seq"],
                author_kind=source.get("authorKind", ""),
                author_id=source.get("authorId", ""),
                author_name=source.get("authorName", ""),
                created_at=source["createdAt"],
                snippet=(hit.get("highlight", {}).get("text") or [source.get("text", "")[:160]])[0],
                session_title=session.display_title or session.title or session.cwd_name or "",
                provider=session.provider,
                human_user_id=session.human_user_id,
            )
        )
    return SessionSearchResponse(hits=hits)


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
