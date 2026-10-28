"""Sync between AgentCore Memory and the network memory log (the workspace's cloud member).

Per owner ``<o>``, three namespaces (``namespaces`` = ``{"facts", "turns", "entries"}``):
``/owners/<o>/`` holds what AgentCore extracted, ``/owners/<o>/turns/`` mirrored raw room turns,
``/owners/<o>/entries/`` the log's entries. Private scopes ``/owners/<o>:<user>/`` are never synced.

1. facts -> log: a first-seen fact record becomes a ``memory.import`` remember, a mapped record
   whose text changed becomes a replace; the record <-> entry mapping advances.
2. log -> entries: ``memory.list`` entries are mirrored so workspace recall can search them; a
   replace marks the replaced records ``replaced``, a tombstone deletes its record (and the
   imported facts record, so it is not re-imported).
3. events -> turns: conversational payloads become searchable turn records.

State lives in the messaging table under the owner: ``MEMSYNC`` (watermarks), ``MEMLINK#<facts
record>`` (entry id, text hash) and ``MEMENTRY#<entry>`` (its entries-namespace record, and the
facts record it was imported from). AgentCore assigns record ids itself, so an entry's record id
is kept in ``MEMENTRY``; ``requestIdentifier`` (the entry id) is what makes a retry idempotent.
Nothing here logs record or entry text.
"""

import hashlib
import re
import time
from datetime import datetime, timezone

from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo
from core.services import network_member
from core.services.workspace_memory import _client, _pages

_BATCH = 100  # AgentCore batch calls and memory.import
_STATE = "MEMSYNC"
_TS_FLOOR = "1970-01-01T00:00:00+00:00"


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


def _write_ok(resp: dict, what: str) -> dict:
    if resp.get("failedRecords"):
        raise RuntimeError(f"memory sync: {len(resp['failedRecords'])} {what} records failed")
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
    if after:
        kwargs["metadataFilters"] = [
            {
                "left": {"metadataKey": "updatedAt"},
                "operator": "AFTER",
                "right": {"metadataValue": {"stringValue": after}},
            }
        ]
    out = []
    for rec in _pages(client.list_memory_records, "memoryRecordSummaries", **kwargs):
        if rec.get("namespaces") != [ns]:  # namespace is a prefix: turns/ and entries/ sit under it
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
                    "kind": _meta(rec, "kind") or "fact",
                    "scope": "team",
                    "when": _meta(rec, "validFrom") or None,
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
        for item, entry_id in zip(chunk, ids):
            await run_in_thread(_record_import, owner_id, item, entry_id)
        done += len(chunk)
    state["records_after"] = started
    return done, False


# ---------------------------------------------------------------------------
# 2. log -> entries
# ---------------------------------------------------------------------------


def _create_entry_records(client, owner_id: str, memory_id: str, ns: str, entries: list[dict]) -> None:
    for chunk in _chunks(entries):
        records = [
            {
                "requestIdentifier": e["id"],
                "namespaces": [ns],
                "content": {"text": f"{e['op'].get('subject', '')}: {e['op'].get('fact', '')}"},
                "timestamp": _dt(e.get("at")),
                "metadata": {
                    "subject": {"stringValue": e["op"].get("subject", "")},
                    "status": {"stringValue": "current"},
                    "kind": {"stringValue": e["op"].get("kind", "")},
                    "author": {"stringValue": e.get("author", "")},
                    "entryId": {"stringValue": e["id"]},
                },
            }
            for e in chunk
        ]
        resp = _write_ok(client.batch_create_memory_records(memoryId=memory_id, records=records), "entry")
        for ok in resp.get("successfulRecords", []):
            _put(owner_id, f"MEMENTRY#{ok['requestIdentifier']}", arec=ok["memoryRecordId"])


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
        )
    if row.get("facts_rec"):
        _t().delete_item(Key=_key(owner_id, f"MEMLINK#{row['facts_rec']}"))
    if row:
        _t().delete_item(Key=_key(owner_id, f"MEMENTRY#{entry_id}"))


def _apply_entries(client, owner_id: str, memory_id: str, ns: str, entries: list[dict]) -> None:
    pending: list[dict] = []

    def flush() -> None:
        if pending:
            _create_entry_records(client, owner_id, memory_id, ns, pending)
            _mark_replaced(client, owner_id, memory_id, [r for e in pending for r in e["op"].get("replaces") or []])
            pending.clear()

    for e in sorted(entries, key=lambda e: e["seq"]):
        if e["op"].get("t") == "forget":
            flush()
            _delete_for_tombstone(client, owner_id, memory_id, e["op"]["target"])
        elif e["op"].get("t") == "remember":
            pending.append(e)
    flush()


async def _mirror_entries(owner_id: str, memory_id: str, ns: str, state: dict) -> tuple[int, bool]:
    since = int(state.get("entries_seq") or 0)
    answer = await network_member.call_op(owner_id, "memory.list", {"since_seq": since}, 30.0)
    if answer is None:
        return 0, True
    entries = [e for e in answer.get("entries") or [] if int(e["seq"]) > since]
    if entries:
        await run_in_thread(lambda: _apply_entries(_client(), owner_id, memory_id, ns, entries))
        state["entries_seq"] = max(int(e["seq"]) for e in entries)
    return len(entries), False


# ---------------------------------------------------------------------------
# 3. events -> turns
# ---------------------------------------------------------------------------


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
            if when <= floor:
                continue
            newest = max(newest, when)
            for i, payload in enumerate(ev.get("payload") or []):
                text = ((payload.get("conversational") or {}).get("content") or {}).get("text")
                if not text:
                    continue
                records.append(
                    {
                        "requestIdentifier": f"{sid}:{ev['eventId']}:{i}",
                        "namespaces": [ns],
                        "content": {"text": text},
                        "timestamp": when,
                        "metadata": {
                            "speaker": {"stringValue": text.partition(": ")[0] if ": " in text else ""},
                            "session": {"stringValue": sid},
                        },
                    }
                )
    for chunk in _chunks(records):
        _write_ok(client.batch_create_memory_records(memoryId=memory_id, records=chunk), "turn")
    return len(records), (newest.isoformat() if newest > floor else after)


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def _owned(owner_id: str, namespaces: dict[str, str]) -> bool:
    """Every namespace is under this owner's team scope: never a ``<owner>:<user>`` private one."""
    root = f"/owners/{owner_id}/"
    return all(namespaces.get(k, "").startswith(root) for k in ("facts", "turns", "entries"))


async def sync_workspace(owner_id: str, *, memory_id: str, namespaces: dict[str, str]) -> dict:
    out = {"imported": 0, "mirrored": 0, "turns": 0}
    if not _owned(owner_id, namespaces):
        return out
    started = utc_now_iso()
    state = await run_in_thread(_get, owner_id, _STATE)
    await network_member.drain_ops(owner_id)
    out["imported"], parked = await _import_facts(owner_id, memory_id, namespaces["facts"], state, started)
    if not parked:
        out["mirrored"], parked = await _mirror_entries(owner_id, memory_id, namespaces["entries"], state)
    out["turns"], state["events_after"] = await run_in_thread(
        lambda: _mirror_turns(_client(), owner_id, memory_id, namespaces["turns"], state.get("events_after"))
    )
    if not parked:
        state["synced_at"] = int(time.time())
    await run_in_thread(lambda: _put(owner_id, _STATE, **{k: v for k, v in state.items() if k not in ("pk", "sk")}))
    return out


async def sync_if_stale(owner_id: str, *, memory_id: str, namespaces: dict[str, str], max_age_s: int = 60) -> None:
    state = await run_in_thread(_get, owner_id, _STATE)
    if time.time() - int(state.get("synced_at") or 0) < max_age_s:
        return
    await sync_workspace(owner_id, memory_id=memory_id, namespaces=namespaces)
