"""Sync between AgentCore Memory and the network memory log (the workspace's cloud member).

Per owner ``<o>``, three sibling namespaces (``namespaces`` = ``{"facts", "turns", "entries"}``):
``/owners/<o>/`` holds what AgentCore extracted, ``/turns/<o>/`` mirrored raw room turns,
``/entries/<o>/`` the log's team entries. A private scope ``/owners/<o>:<user>/`` is never read
from; the one thing written there is that person's own (``me``) log entries.

1. facts -> log: a first-seen fact record becomes a ``memory.import`` remember, a mapped record
   whose text changed becomes a replace; the record <-> entry mapping advances.
2. log -> entries: ``memory.list`` entries are mirrored so recall can search them (team entries
   and the workspace's own notes to ``/entries/<o>/``, a person's own to their private scope;
   facts imported in step 1 are not mirrored back); a replace marks the replaced records
   ``replaced``, a tombstone deletes its record (and the imported facts record, so it is not
   re-imported).
3. events -> turns: conversational payloads become searchable turn records.

State lives in the messaging table under the owner: ``MEMSYNC`` (watermarks), ``MEMLINK#<record>``
(a facts record's entry id and text hash, or a mirrored record's entry id and ``mirror``),
``MEMENTRY#<entry>`` (its mirrored record, and the facts record it was imported from) and
``MEMTURN#<turn>`` (a mirrored turn, until its event has expired).
AgentCore assigns record ids itself and ``requestIdentifier`` only correlates results, so a write
is idempotent by these rows (one already mirrored is skipped) and by ``clientToken`` per batch.
Nothing here logs record or entry text.
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo
from core.services import network_member
from core.services.workspace_memory import _MEMBER_ID, _client, _pages, namespaces_for

logger = logging.getLogger(__name__)

_BATCH = 100  # AgentCore batch calls and memory.import
_STATE = "MEMSYNC"
_TS_FLOOR = "1970-01-01T00:00:00+00:00"
_TURN_TTL_S = 8 * 86400  # past the memory's 7-day event expiry: the event can no longer be listed
# AgentCore kind -> the log's (fact|decision|lesson|preference): a standing instruction is a
# decision; claim, observation, anchor and mention are facts.
_LOG_KIND = {"rule": "decision"}
# botocore's metadata StringValue: 1-256 of these. botocore does not check the pattern, the server does.
_STRING_VALUE = re.compile(r"[a-zA-Z0-9\s._:/=+@-]{1,256}")


def _t():
    return messaging_repo._table()


def _key(owner_id: str, sk: str) -> dict:
    return {"pk": messaging_repo.owner_pk(owner_id), "sk": sk}


def _dt(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat(value or _TS_FLOOR)
    except ValueError:
        return datetime.now(timezone.utc)


def _meta(rec: dict, name: str) -> str:
    return ((rec.get("metadata") or {}).get(name) or {}).get("stringValue") or ""


def _number(rec: dict, name: str) -> int | None:
    """A NUMBER key (validFrom, validTo: epoch seconds), or None."""
    value = ((rec.get("metadata") or {}).get(name) or {}).get("numberValue")
    return None if value is None else int(value)


def _strings(**values: str) -> dict:
    """String metadata, leaving out any value AgentCore would refuse (empty, too long, off-pattern)."""
    return {k: {"stringValue": v} for k, v in values.items() if v and _STRING_VALUE.fullmatch(v)}


def _token(ids) -> str:
    """A batch's ``clientToken``: the same records, the same token."""
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def _at(seconds) -> datetime:
    """A log entry's ``at`` (epoch seconds)."""
    return datetime.fromtimestamp(int(seconds or 0), timezone.utc)


def _chunks(items: list):
    for i in range(0, len(items), _BATCH):
        yield items[i : i + _BATCH]


def _text(rec: dict) -> str:
    return (rec.get("content") or {}).get("text", "")


def parse_fact(text: str, entity: str, attribute: str) -> tuple[str, str | None]:
    """(value, speaker) from ``<Name>: <entity> <attribute>: <value> (since ...; was ...)``.
    ``Contested: ...`` has no speaker."""
    name, _, rest = text.partition(": ")
    rest = re.sub(r"\s*\((?:since|was)[^)]*\)\s*$", "", rest)
    prefix = f"{entity} {attribute}: "
    value = rest[len(prefix) :] if rest.startswith(prefix) else (rest.partition(": ")[2] or rest)
    return value, (None if name == "Contested" else name)


def _not_found(failure: dict) -> bool:
    message = str(failure.get("errorMessage", "")).lower().replace(" ", "").replace("_", "")
    return failure.get("errorCode") == 404 or "notfound" in message


def _write_ok(resp: dict, what: str, *, gone_ok: bool = False) -> dict:
    """Raise if any record failed. With ``gone_ok`` a not-found failure counts as done (a delete
    that already happened)."""
    failed = [f for f in resp.get("failedRecords") or [] if not (gone_ok and _not_found(f))]
    if failed:
        raise RuntimeError(f"memory sync: {len(failed)} {what} records failed")
    return resp


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def _get(owner_id: str, sk: str) -> dict:
    return _t().get_item(Key=_key(owner_id, sk), ConsistentRead=True).get("Item") or {}


def _put(owner_id: str, sk: str, **attrs) -> None:
    _t().put_item(Item={**_key(owner_id, sk), **attrs})


# ---------------------------------------------------------------------------
# 1. facts -> log
# ---------------------------------------------------------------------------


def _facts_to_import(client, owner_id: str, memory_id: str, ns: str, after: str | None) -> list[dict]:
    kwargs = {"memoryId": memory_id, "namespace": ns}
    if after:  # the watermark: the sync start time, stored by utc_now_iso
        # The system field (a bare ``updatedAt`` is not indexed: ValidationException), ISO-8601 UTC.
        at = datetime.fromisoformat(after).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        kwargs["metadataFilters"] = [
            {
                "left": {"metadataKey": "x-amz-agentcore-memory-updatedAt"},
                "operator": "AFTER",
                "right": {"metadataValue": {"dateTimeValue": at}},
            }
        ]
    out = []
    for rec in _pages(client.list_memory_records, "memoryRecordSummaries", **kwargs):
        if rec.get("namespaces") != [ns]:  # the listing matches by prefix: keep this namespace only
            continue
        link = _get(owner_id, f"MEMLINK#{rec['memoryRecordId']}")
        digest = hashlib.sha256(_text(rec).encode()).hexdigest()
        if link.get("hash") == digest:
            continue
        entity, attribute = _meta(rec, "entity"), _meta(rec, "attribute")
        value, speaker = parse_fact(_text(rec), entity, attribute)
        out.append(
            {
                "rec": rec["memoryRecordId"],
                "hash": digest,
                "prior": link.get("entry"),
                "entry": {
                    "subject": f"{entity} {attribute}".strip(),
                    "fact": value,
                    "kind": _LOG_KIND.get(_meta(rec, "kind"), "fact"),
                    "scope": "team",
                    "when": _number(rec, "validFrom"),  # the log's when is epoch seconds (i64)
                    "key": f"{rec['memoryRecordId']}:{digest}",  # memory.import is idempotent on it
                    "source": {"harness": "workspace", "repo": None, "session": None, "on_behalf_of": speaker},
                    "replaces": [link["entry"]] if link.get("entry") else [],
                },
            }
        )
    return out


def _record_import(owner_id: str, item: dict, entry_id: str) -> None:
    table = _t()
    _put(owner_id, f"MEMLINK#{item['rec']}", entry=entry_id, hash=item["hash"])
    if item["prior"]:
        table.update_item(Key=_key(owner_id, f"MEMENTRY#{item['prior']}"), UpdateExpression="REMOVE facts_rec")
    table.update_item(
        Key=_key(owner_id, f"MEMENTRY#{entry_id}"),
        UpdateExpression="SET facts_rec = :r",
        ExpressionAttributeValues={":r": item["rec"]},
    )


async def _import_facts(owner_id: str, memory_id: str, ns: str, state: dict, started: str) -> tuple[int, bool]:
    """(imported, parked). The watermark advances only when everything was sent."""
    items = await run_in_thread(
        lambda: _facts_to_import(_client(), owner_id, memory_id, ns, state.get("records_after"))
    )
    done = 0
    for chunk in _chunks(items):
        answer = await network_member.call_op(owner_id, "memory.import", {"entries": [i["entry"] for i in chunk]}, 60.0)
        if answer is None:
            return done, True
        ids = answer.get("ids") or []
        if len(ids) != len(chunk):
            raise RuntimeError("memory sync: memory.import answered a different number of ids")
        if answer.get("refused"):
            # Final, not a retry: a secret or an oversize fact. Reasons only, never the fact.
            logger.warning(
                "memory sync: memory.import refused owner=%s refused=%s",
                owner_id,
                [(r.get("index"), r.get("reason")) for r in answer["refused"]],
            )
        for item, entry_id in zip(chunk, ids):
            if entry_id is None:
                continue
            await run_in_thread(_record_import, owner_id, item, entry_id)
            done += 1
    state["records_after"] = started
    return done, False


async def entry_for(owner_id: str, record_id: str) -> str | None:
    """The log entry a record stands for: the one a facts record was imported as, or the one a
    mirrored record copies (both ``MEMLINK`` rows), if any."""
    return (await run_in_thread(_get, owner_id, f"MEMLINK#{record_id}")).get("entry")


async def drop_mirror(owner_id: str, entry_id: str) -> None:
    """Delete an entry's ``entries`` record and its mapping rows (what a tombstone does)."""
    memory_id = settings.AGENTCORE_MEMORY_ID
    if memory_id:
        await run_in_thread(_delete_for_tombstone, _client(), owner_id, memory_id, entry_id)


# ---------------------------------------------------------------------------
# 2. log -> entries
# ---------------------------------------------------------------------------


def _entry_namespace(owner_id: str, op: dict) -> str | None:
    """Where a log entry is mirrored. A person's own (``me`` on behalf of a member) goes to their
    private scope, the one DM agents read; team entries and the workspace's own notes to
    ``/entries/<o>/``. None (not mirrored): a ``me`` entry for someone no namespace can name."""
    person = (op.get("source") or {}).get("on_behalf_of")
    if op.get("scope") == "me" and person:
        return namespaces_for(owner_id, person)["private"] if _MEMBER_ID.fullmatch(person) else None
    return namespaces_for(owner_id, None)["entries"]


def _create_entry_records(client, owner_id: str, memory_id: str, entries: list[dict]) -> None:
    for chunk in _chunks(entries):
        records = []
        for e in chunk:
            op = e["op"]
            records.append(
                {
                    "requestIdentifier": e["id"],
                    "namespaces": [e["namespace"]],
                    "content": {"text": f"{op.get('subject', '')}: {op.get('fact', '')}"},
                    "timestamp": _at(e.get("at")),
                    "metadata": {
                        **_strings(
                            subject=op.get("subject", ""),
                            status="current",
                            kind="entry",
                            logKind=op.get("kind", ""),
                            author=e.get("author", ""),
                            entryId=e["id"],
                        ),
                        # So a private scope's as-of filter (validFrom <= as_of) keeps it.
                        "validFrom": {"numberValue": int(op.get("when") or e.get("at") or 0)},
                    },
                }
            )
        resp = client.batch_create_memory_records(
            memoryId=memory_id, records=records, clientToken=_token(r["requestIdentifier"] for r in records)
        )
        # Keep the mapping of every record that was created BEFORE raising for the ones that were
        # not: a tombstone or a replace finds its record only through this row.
        for ok in resp.get("successfulRecords", []):
            _put(owner_id, f"MEMENTRY#{ok['requestIdentifier']}", arec=ok["memoryRecordId"])
            # The way back, so a tab delete of the mirror finds (and forgets) its entry.
            _put(owner_id, f"MEMLINK#{ok['memoryRecordId']}", entry=ok["requestIdentifier"], mirror=True)
        _write_ok(resp, "entry")


def _mark_replaced(client, owner_id: str, memory_id: str, entry_ids: list[str]) -> None:
    recs = [_get(owner_id, f"MEMENTRY#{i}").get("arec") for i in dict.fromkeys(entry_ids)]
    now = datetime.now(timezone.utc)
    for chunk in _chunks([r for r in recs if r]):
        records = [
            {"memoryRecordId": r, "timestamp": now, "metadata": {"status": {"stringValue": "replaced"}}} for r in chunk
        ]
        _write_ok(client.batch_update_memory_records(memoryId=memory_id, records=records), "replaced")


def _delete_for_tombstone(client, owner_id: str, memory_id: str, entry_id: str) -> None:
    row = _get(owner_id, f"MEMENTRY#{entry_id}")
    ids = [r for r in (row.get("arec"), row.get("facts_rec")) if r]
    if ids:
        _write_ok(
            client.batch_delete_memory_records(memoryId=memory_id, records=[{"memoryRecordId": r} for r in ids]),
            "tombstoned",
            gone_ok=True,
        )
    for rec in ids:
        _t().delete_item(Key=_key(owner_id, f"MEMLINK#{rec}"))
    if row:
        _t().delete_item(Key=_key(owner_id, f"MEMENTRY#{entry_id}"))


def _save_seq(owner_id: str, seq: int) -> None:
    _t().update_item(
        Key=_key(owner_id, _STATE), UpdateExpression="SET entries_seq = :s", ExpressionAttributeValues={":s": seq}
    )


def _apply_entries(client, owner_id: str, memory_id: str, entries: list[dict]) -> None:
    """Mirror ``memory.list`` rows in order; ``entries_seq`` is saved after every flushed batch, so a
    failure later in the page does not replay what was already written."""
    pending: list[dict] = []
    replaced: list[str] = []
    seq: int | None = None

    def flush() -> None:
        if pending:
            _create_entry_records(client, owner_id, memory_id, pending)
        if replaced:
            _mark_replaced(client, owner_id, memory_id, replaced)
        pending.clear()
        replaced.clear()
        if seq is not None:
            _save_seq(owner_id, seq)

    for e in sorted(entries, key=lambda e: int(e["seq"])):
        op = e.get("op")
        if e.get("tombstone") or not op:
            pass  # a tombstoned row ({id, author, tombstone, seq}): its body is gone; its Forget is a row of its own
        elif op.get("t") == "forget":
            flush()
            _delete_for_tombstone(client, owner_id, memory_id, op["target"])
        elif op.get("t") == "remember":
            replaced.extend(op.get("replaces") or [])
            namespace = _entry_namespace(owner_id, op)
            row = _get(owner_id, f"MEMENTRY#{e['id']}")
            # Already mirrored, or a fact imported from the facts namespace (recall finds it there).
            if namespace and not (row.get("arec") or row.get("facts_rec")):
                pending.append({**e, "namespace": namespace})
        seq = int(e["seq"])
        if len(pending) >= _BATCH:
            flush()
    flush()


async def _mirror_entries(owner_id: str, memory_id: str, state: dict) -> tuple[int, bool]:
    since = int(state.get("entries_seq") or 0)
    answer = await network_member.call_op(owner_id, "memory.list", {"since_seq": since}, 30.0)
    if answer is None:
        return 0, True
    entries = [e for e in answer.get("entries") or [] if int(e["seq"]) > since]
    if entries:
        await run_in_thread(lambda: _apply_entries(_client(), owner_id, memory_id, entries))
        state["entries_seq"] = max(int(e["seq"]) for e in entries)
    return len(entries), False


# ---------------------------------------------------------------------------
# 3. events -> turns
# ---------------------------------------------------------------------------


def _turn_id(sid: str, event_id: str, i: int) -> str:
    """A turn's ``requestIdentifier`` ([a-zA-Z0-9_-], at most 80): session and event ids may not fit."""
    return hashlib.sha256(f"{sid}:{event_id}:{i}".encode()).hexdigest()


def _mirror_turns(client, owner_id: str, memory_id: str, ns: str, after: str | None) -> tuple[int, str | None]:
    floor = _dt(after)
    records, newest = [], floor
    # ponytail: re-lists every session's events each run and filters by time; keep a per-session
    # watermark if an owner's history gets long.
    for s in _pages(client.list_sessions, "sessionSummaries", memoryId=memory_id, actorId=owner_id):
        sid = s["sessionId"]
        for ev in _pages(
            client.list_events, "events", memoryId=memory_id, sessionId=sid, actorId=owner_id, includePayloads=True
        ):
            when = ev["eventTimestamp"]
            if when < floor:
                continue
            newest = max(newest, when)
            tags = {k: v["stringValue"] for k, v in (ev.get("metadata") or {}).items() if "stringValue" in v}
            if tags.get("source") == "room":
                continue  # room ingestion wrote this turn's record itself (room_memory)
            for i, payload in enumerate(ev.get("payload") or []):
                text = ((payload.get("conversational") or {}).get("content") or {}).get("text")
                turn = _turn_id(sid, ev["eventId"], i)
                # The newest event sits on the watermark and is listed again: skip what is mirrored.
                if not text or _get(owner_id, f"MEMTURN#{turn}"):
                    continue
                records.append(
                    {
                        "requestIdentifier": turn,
                        "namespaces": [ns],
                        "content": {"text": text},
                        "timestamp": when,
                        "metadata": _strings(
                            **dict(list(tags.items())[:18]),  # AgentCore takes at most 20 metadata keys
                            speaker=text.partition(": ")[0] if ": " in text else "",
                            session=sid,
                        ),
                    }
                )
    for chunk in _chunks(records):
        resp = client.batch_create_memory_records(
            memoryId=memory_id, records=chunk, clientToken=_token(r["requestIdentifier"] for r in chunk)
        )
        expires = int(time.time()) + _TURN_TTL_S
        for ok in resp.get("successfulRecords", []):
            _put(owner_id, f"MEMTURN#{ok['requestIdentifier']}", expiresAt=expires)  # the table's TTL key
        _write_ok(resp, "turn")
    return len(records), (newest.isoformat() if newest > floor else after)


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def _owned(owner_id: str, namespaces: dict[str, str]) -> bool:
    """The namespaces the sync writes are exactly this owner's team ones: never a ``<owner>:<user>``
    private one. (The sync never writes ``tasks``; the task indexer does.)"""
    team = namespaces_for(owner_id, None)
    return all(namespaces.get(k) == team[k] for k in ("facts", "turns", "entries"))


_LEASE_S = 300


def _take_lease(owner_id: str) -> bool:
    """One sync per owner at a time: a conditional write on the state row."""
    from botocore.exceptions import ClientError

    now = int(time.time())
    try:
        _t().update_item(
            Key=_key(owner_id, _STATE),
            UpdateExpression="SET lease_until = :u",
            ConditionExpression="attribute_not_exists(lease_until) OR lease_until < :now",
            ExpressionAttributeValues={":u": now + _LEASE_S, ":now": now},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _release_lease(owner_id: str) -> None:
    _t().update_item(Key=_key(owner_id, _STATE), UpdateExpression="REMOVE lease_until")


async def _sync_locked(owner_id: str, memory_id: str, namespaces: dict[str, str]) -> dict:
    out = {"imported": 0, "mirrored": 0, "turns": 0}
    started = utc_now_iso()
    state = await run_in_thread(_get, owner_id, _STATE)
    await network_member.drain_ops(owner_id)
    out["imported"], parked = await _import_facts(owner_id, memory_id, namespaces["facts"], state, started)
    if not parked:
        out["mirrored"], parked = await _mirror_entries(owner_id, memory_id, state)
    out["turns"], state["events_after"] = await run_in_thread(
        lambda: _mirror_turns(_client(), owner_id, memory_id, namespaces["turns"], state.get("events_after"))
    )
    if not parked:
        state["synced_at"] = int(time.time())
    # The put replaces the row, which also releases the lease (not carried over from the read).
    await run_in_thread(
        lambda: _put(owner_id, _STATE, **{k: v for k, v in state.items() if k not in ("pk", "sk", "lease_until")})
    )
    return out


async def sync_workspace(owner_id: str, *, memory_id: str, namespaces: dict[str, str]) -> dict:
    """A caller that cannot take the owner's lease (a sync is running) returns zeros at once."""
    out = {"imported": 0, "mirrored": 0, "turns": 0}
    if not _owned(owner_id, namespaces) or not await run_in_thread(_take_lease, owner_id):
        return out
    try:
        return await _sync_locked(owner_id, memory_id, namespaces)
    finally:
        await run_in_thread(_release_lease, owner_id)


async def sync_if_stale(owner_id: str, *, memory_id: str, namespaces: dict[str, str], max_age_s: int = 60) -> bool:
    """True when a sync ran (the last one was older than ``max_age_s``)."""
    state = await run_in_thread(_get, owner_id, _STATE)
    if time.time() - int(state.get("synced_at") or 0) < max_age_s:
        return False
    await sync_workspace(owner_id, memory_id=memory_id, namespaces=namespaces)
    return True
