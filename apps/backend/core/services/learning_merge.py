"""Three-way merge of what one task attempt learned back into its specialist's profile.

Spec 07 §3.5 (the one design, review C9). A Worker runs on its own AgentCore session, starts from
a copy of the specialist profile ``<owner>/hermes/<agent>/`` (B, recorded in the overlay's
``base.json``), and writes only its overlay ``<owner>/hermes-tasks/<task>/<agent>/`` (T). After the
attempt is sealed, ``LearningWorkflow`` (one per specialist, the only merge writer) calls
:func:`run_merge`, which reads the specialist profile now (C), plans with :func:`plan_merge` and
writes with :func:`apply_plan`.

What merges: ``memories/MEMORY.md`` and ``memories/USER.md`` (entry-level), skill dirs under
``skills/`` as units, and ``skills/.usage.json`` counters. Nothing else, ever: not
``skills/workspace/`` (review G16), not the curator's files, not ``state.db``/``work/``/sessions/
logs/cache/home/media/credentials, not the backend-owned files.

Per-attempt base (review of #4672): attempt k's start is the specialist copy layered with the
overlay attempt k-1 left, so B is per attempt. The bridge writes ``base-{k:04d}.json``
(:func:`base_rel`) at attempt k's first restore::

    {"files": {rel: {"sha256": str, "etag": str, "text"?: str}}}

``text`` is carried for the three small files whose merge needs B's content (the two memory files
and ``skills/.usage.json``); skill dirs only need hashes. The overlay itself is one prefix, so a
later attempt may overwrite attempt k's T before attempt k's merge runs: :func:`check_seal` then
answers ``superseded`` and attempt k is skipped. The workflow merges attempt m against the base of
the earliest attempt of that task not yet applied -- T_m carries every attempt since then -- so
nothing is counted twice and nothing is lost.

Crash safety: every write is conditional on the ETag the merge read (412 -> :class:`CurrentChanged`),
objects already at their planned sha are skipped, ``SKILL.md`` is written last within a skill dir,
``.revision`` is bumped once, last. A rerun after a crash at any write converges on the same
final state (see ``test_crash_after_k_writes_then_rerun_converges``).

Caller contract (V5):
- ``task`` is the LAYERED task view -- B plus the overlay, minus what ``sealed.json`` says the task
  deleted -- never a bare overlay listing: an overlay-only T would archive every skill the task
  did not touch.
- ``plan_for`` writes the ``LEARN#`` intent row (``plan.planned``) before each apply, including the
  replans :func:`run_merge` makes on a 412; a stale first-plan row would let a crash re-add usage
  deltas.
- Usage idempotence is "C is at the planned sha". Another writer rewriting ``.usage.json`` between
  our put and a replan re-adds the deltas; the workspace VM's unconditional ``save_profile`` is such
  a writer (and can clobber merged ``memories/*``) until V5 fences it.

Pure apart from the S3 client the caller passes in and the backend's list of credential dirs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.services.credentials.hermes_cli import OWNED_PATHS, profile_paths

Manifest = dict[str, tuple[str, str]]  # rel -> (sha256, etag)
Snapshot = dict[str, tuple[str, str, Callable[[], bytes]]]  # rel -> (sha256, etag, load)

DELIM = "\n§\n"
MEMORY = "memories/MEMORY.md"
USER = "memories/USER.md"
USAGE = "skills/.usage.json"
SKILL_MD = "SKILL.md"
REVISION = ".revision"
ARCHIVE_DIR = "skills/.archive/"
CONFLICTS_DIR = "learning/conflicts/"
SEALED = "sealed.json"
#: Read with C so archive and park writes are conditional on what is already there.
_AUX_PREFIXES = (ARCHIVE_DIR, CONFLICTS_DIR)
#: ``if_match`` value meaning "the object must not exist yet" (``IfNoneMatch: *``).
ABSENT = ""

_COUNTERS = ("use_count", "view_count", "patch_count")
_STAMPS = ("last_used_at", "last_viewed_at", "last_patched_at")


class CurrentChanged(Exception):
    """A conditional write lost: someone else wrote the specialist profile since we read it."""


#: Skill-shaped dirs that are credentials, written only by ``hermes_cli.project`` (the bridge's
#: ``CLI_CREDENTIAL_PREFIXES``: skills/instagram/, skills/shopify/, skills/vercel/): never merged,
#: or a task VM's rotated or missing token would overwrite or delete the live one.
_CREDENTIAL_SKILL_DIRS = tuple(
    p for m in OWNED_PATHS for p in profile_paths(m) if p.startswith("skills/") and p.endswith("/")
)


def is_learning_file(rel: str) -> bool:
    """Whether ``rel`` (relative to an agent profile root) is something the merge reads."""
    if rel in (MEMORY, USER, USAGE):
        return True
    if not rel.startswith("skills/") or rel.startswith(_CREDENTIAL_SKILL_DIRS):
        return False
    parts = rel.split("/")[1:]
    # Dot dirs/files are the curator's (.archive, .curator_backups, .curator_state) and never
    # merge; skills/workspace/ is backend-owned (08.T1, review G16).
    return not (parts[0] == "workspace" or any(p.startswith(".") for p in parts))


@dataclass(frozen=True)
class PlannedPut:
    key_rel: str
    body: bytes
    sha256: str
    #: ETag to match; :data:`ABSENT` = must not exist; ``None`` = unconditional.
    if_match: str | None


@dataclass(frozen=True)
class PlannedDelete:
    key_rel: str
    if_match: str


@dataclass
class MergePlan:
    ops: list[PlannedPut | PlannedDelete]
    parked: list[dict]
    report: dict

    @property
    def puts(self) -> list[PlannedPut]:
        return [op for op in self.ops if isinstance(op, PlannedPut)]

    @property
    def deletes(self) -> list[PlannedDelete]:
        return [op for op in self.ops if isinstance(op, PlannedDelete)]

    @property
    def planned(self) -> dict[str, str]:
        """``{rel: sha256}`` of every put -- what the ``LEARN#`` intent row records."""
        return {op.key_rel: op.sha256 for op in self.puts}


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _empty_report(skipped: str | None = None) -> dict:
    return {
        "applied": False,
        "added": {"memory": 0, "user": 0, "skills": []},
        "archived": [],
        "parked": [],
        "overflow": 0,
        "dropped_threats": 0,
        "skipped": skipped,
    }


def base_rel(attempt: int) -> str:
    """The overlay file holding attempt ``attempt``'s base (relative to the overlay agent root)."""
    return f"base-{attempt:04d}.json"


def check_seal(seal_raw: bytes | None, attempt: int, task: Snapshot) -> str | None:
    """None when T is exactly what attempt ``attempt`` sealed; ``"unsealed"`` when it never
    sealed (the VM died: its learning is not merged); ``"superseded"`` when a later attempt has
    sealed over it or is writing the overlay (that attempt's merge takes T instead)."""
    if not seal_raw:
        return "unsealed"
    seal = json.loads(seal_raw)
    sealed_attempt = int(seal.get("attempt") or 0)
    if sealed_attempt > attempt:
        return "superseded"
    if sealed_attempt < attempt:
        return "unsealed"
    sealed_files = {r: sha for r, sha in (seal.get("files") or {}).items() if is_learning_file(r)}
    now = {r: v[0] for r, v in task.items() if is_learning_file(r)}
    return None if sealed_files == now else "superseded"


def parse_base(raw: bytes) -> tuple[Manifest, dict[str, str]]:
    """``base.json`` -> (manifest, ``{rel: text}`` for the files that carry it)."""
    files = json.loads(raw).get("files") or {}
    manifest = {rel: (f["sha256"], f.get("etag", "")) for rel, f in files.items() if is_learning_file(rel)}
    texts = {rel: f["text"] for rel, f in files.items() if isinstance(f.get("text"), str)}
    return manifest, texts


# --- memory files -------------------------------------------------------------------------------


def _entries(text: str) -> list[str]:
    return [e.strip() for e in text.split(DELIM) if e.strip()]


def _norm(entry: str) -> str:
    return " ".join(entry.split())


def _merge_entries(
    base: list[str], task: list[str], current: list[str], *, limit: int, threat_scan, report: dict, kind: str
) -> list[str]:
    base_n, task_n = {_norm(e) for e in base}, {_norm(e) for e in task}
    removed = base_n - task_n
    result = [e for e in current if _norm(e) not in removed]
    present = {_norm(e) for e in result}
    for entry in task:
        n = _norm(entry)
        if n in base_n or n in present:
            continue  # not new in T, or already there (a replay, or a human added it too)
        if threat_scan(entry):
            report["dropped_threats"] += 1
            continue
        if len(DELIM.join([*result, entry])) > limit:
            report["overflow"] += 1
            continue
        result.append(entry)
        present.add(n)
        report["added"][kind] += 1
    return result


# --- skills -------------------------------------------------------------------------------------


def _units(*file_sets: set[str]) -> set[str]:
    return {rel[: -len(SKILL_MD) - 1] for files in file_sets for rel in files if rel.endswith("/" + SKILL_MD)}


def _unit_of(rel: str, units: set[str]) -> str | None:
    best = None
    for unit in units:
        if rel.startswith(unit + "/") and (best is None or len(unit) > len(best)):
            best = unit
    return best


def _skill_md_last(rels):
    return sorted(rels, key=lambda r: (r.endswith("/" + SKILL_MD), r))


# --- plan ---------------------------------------------------------------------------------------


def plan_merge(
    base: Manifest,
    task: Snapshot,
    current: Snapshot,
    *,
    memory_limit: int = 2200,
    user_limit: int = 1375,
    private: bool,
    sealed: bool,
    threat_scan: Callable[[str], bool],
    task_id: str,
    base_text: dict[str, str] | None = None,
    planned: dict[str, str] | None = None,
    superseded: bool = False,
) -> MergePlan:
    """Plan the writes that fold T into C. Pure.

    ``planned`` is the intent row of an earlier, interrupted run of this same merge: a file of C
    already at its planned sha is done, which keeps the one non-idempotent rule (usage counter
    deltas) from adding twice on a rerun.
    """
    if superseded:
        return MergePlan([], [], _empty_report("superseded"))
    if not sealed:
        return MergePlan([], [], _empty_report("unsealed"))
    report = _empty_report()
    aux = {rel: v for rel, v in current.items() if rel.startswith(_AUX_PREFIXES)}
    ops: list[PlannedPut | PlannedDelete] = []
    base_text = base_text or {}
    planned = planned or {}
    task = {rel: v for rel, v in task.items() if is_learning_file(rel)}
    current = {rel: v for rel, v in current.items() if is_learning_file(rel)}

    def _text(snap: Snapshot, rel: str) -> str:
        return snap[rel][2]().decode("utf-8") if rel in snap else ""

    def _base_text(rel: str) -> str:
        if rel in base_text:
            return base_text[rel]
        if rel not in base:
            return ""
        if rel in current and current[rel][0] == base[rel][0]:
            return _text(current, rel)
        # ponytail: base without text (should not happen; the bridge always writes it) -- fall
        # back so the merge can only add: no memory entry counts as removed, no usage delta.
        return _text(task, rel) if rel == USAGE else ""

    def _put(rel: str, body: bytes) -> None:
        ops.append(PlannedPut(rel, body, _sha(body), current[rel][1] if rel in current else ABSENT))

    # Memory files.
    for rel, limit, kind in ((MEMORY, memory_limit, "memory"), (USER, user_limit, "user")):
        if rel not in task:
            continue
        cur = _entries(_text(current, rel))
        merged = _merge_entries(
            _entries(_base_text(rel)),
            _entries(_text(task, rel)),
            cur,
            limit=limit,
            threat_scan=threat_scan,
            report=report,
            kind=kind,
        )
        if merged != cur:
            _put(rel, DELIM.join(merged).encode("utf-8"))

    # Usage counters: deltas onto C; state and creation fields stay C's.
    done = USAGE in current and planned.get(USAGE) == current[USAGE][0]
    if USAGE in task and not done:
        b, t = json.loads(_base_text(USAGE) or "{}"), json.loads(_text(task, USAGE) or "{}")
        c = json.loads(_text(current, USAGE) or "{}")
        merged_usage: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in c.items()}
        for name, trec in t.items():
            if not isinstance(trec, dict):
                continue
            brec, crec = b.get(name) or {}, c.get(name)
            if crec is None:
                if name not in b:
                    merged_usage[name] = trec  # new skill: its creator's record
                continue  # C dropped it on purpose
            rec = merged_usage[name]
            for k in _COUNTERS:
                delta = int(trec.get(k) or 0) - int(brec.get(k) or 0)
                if delta > 0:  # never below C
                    rec[k] = int(crec.get(k) or 0) + delta
            for k in _STAMPS:
                stamps = [s for s in (crec.get(k), trec.get(k)) if isinstance(s, str)]
                if stamps:
                    rec[k] = max(stamps)
        if merged_usage != c:
            _put(USAGE, json.dumps(merged_usage, indent=2, sort_keys=True).encode("utf-8"))

    # Skill dirs, one unit each.
    skill = lambda files: {r for r in files if r.startswith("skills/") and r != USAGE}  # noqa: E731
    b_files, t_files, c_files = skill(base), skill(task), skill(current)
    units = _units(b_files, t_files, c_files)
    for unit in sorted(units):

        def files_of(files: set[str], unit: str = unit) -> set[str]:
            return {r for r in files if _unit_of(r, units) == unit}

        bu, tu, cu = files_of(b_files), files_of(t_files), files_of(c_files)
        bs = {r: base[r][0] for r in bu}
        ts = {r: task[r][0] for r in tu}
        cs = {r: current[r][0] for r in cu}
        md = f"{unit}/{SKILL_MD}"
        in_b, in_t, in_c = md in bs, md in ts, md in cs
        name = unit[len("skills/") :]

        def between(target: dict[str, str]) -> bool:
            # C is B, T, or a partial write of B -> T (a crashed earlier run): file by file.
            return all(cs.get(r) in (bs.get(r), target.get(r)) for r in set(bs) | set(target) | set(cs))

        def take_task() -> None:
            for r in _skill_md_last(set(tu) - {md}):
                if cs.get(r) != ts[r]:
                    _put(r, task[r][2]())
            for r in sorted(set(cu) - set(tu), key=lambda r: r == md):
                ops.append(PlannedDelete(r, current[r][1]))
            if cs.get(md) != ts[md]:
                _put(md, task[md][2]())

        def park(reason: str) -> None:
            for r in _skill_md_last(tu):
                body = task[r][2]()
                dest = f"{CONFLICTS_DIR}{task_id}/{r}"
                # Only over the version we read (an earlier attempt's park), never blind.
                ops.append(PlannedPut(dest, body, _sha(body), aux[dest][1] if dest in aux else ABSENT))
            report["parked"].append({"skill": name, "reason": reason})

        if in_t and not in_b:
            if not in_c:
                take_task()
                report["added"]["skills"].append(name)
            elif cs != ts:
                park("both_created")
        elif in_t and in_b and ts != bs:
            if cs == ts:
                continue
            if in_c and between(ts):
                take_task()
            else:
                park("both_changed")
        elif in_b and not in_t and in_c and between({}):
            # Removed in T, untouched in C: archive (the curator never deletes, nor do we).
            dest = f"{ARCHIVE_DIR}{name}/"
            # What we archive is B's dir (C is B or a partial delete of it): compare against B.
            mine = {r[len(unit) + 1 :]: bs[r] for r in bu}
            taken = {r[len(dest) :]: v[0] for r, v in aux.items() if r.startswith(dest)}
            # A partial copy of ours (a crashed earlier run) is ours; anything else is someone's.
            if any(mine.get(r) != sha for r, sha in taken.items()):
                # The curator (or another merge) already archived a different "<name>": never
                # overwrite an archive; this one gets its own dir.
                dest = f"{ARCHIVE_DIR}{name}.{task_id}/"
            for r in _skill_md_last(cu):
                body = current[r][2]()
                key = dest + r[len(unit) + 1 :]
                ops.append(PlannedPut(key, body, _sha(body), aux[key][1] if key in aux else ABSENT))
            for r in sorted(cu, key=lambda r: r == md):  # SKILL.md last: the dir stays whole until then
                ops.append(PlannedDelete(r, current[r][1]))
            report["archived"].append(name)

    if private:
        # Founder Q3: a private-lane task merges nothing; the report says what it learned.
        report["skipped"] = "private"
        return MergePlan([], report["parked"], report)
    return MergePlan(ops, report["parked"], report)


# --- apply --------------------------------------------------------------------------------------


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _lost(exc: Exception) -> bool:
    return _code(exc) in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")


def _head(s3, bucket: str, key: str) -> dict | None:
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 -- botocore ClientError; only 404 means "absent"
        if _code(exc) in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def apply_plan(s3, bucket: str, prefix: str, plan: MergePlan, *, force_revision: bool = False) -> dict:
    """Write ``plan`` under ``prefix`` (the specialist profile root, ending in ``/``).

    ``force_revision`` bumps ``.revision`` even when nothing is left to write: the rerun of a
    merge whose earlier run crashed after its writes but before the bump.
    """
    report = dict(plan.report)
    if report.get("skipped"):
        return report
    wrote = False
    for op in plan.ops:
        key = prefix + op.key_rel
        head = _head(s3, bucket, key)
        try:
            if isinstance(op, PlannedPut):
                if head is not None and head.get("Metadata", {}).get("sha256") == op.sha256:
                    continue  # already there (a replay)
                cond: dict[str, str] = {}
                if op.if_match == ABSENT:
                    cond = {"IfNoneMatch": "*"}
                elif op.if_match is not None:
                    cond = {"IfMatch": op.if_match}
                s3.put_object(Bucket=bucket, Key=key, Body=op.body, Metadata={"sha256": op.sha256}, **cond)
            else:
                if head is None:
                    continue
                s3.delete_object(Bucket=bucket, Key=key, IfMatch=op.if_match)
        except Exception as exc:
            if _lost(exc):
                raise CurrentChanged(op.key_rel) from exc
            raise
        wrote = True
    if wrote or force_revision:
        # LAST, once: warm VMs re-pull on a new value (same rule as hermes_profiles uploads).
        s3.put_object(Bucket=bucket, Key=prefix + REVISION, Body=uuid.uuid4().hex.encode())
    report["applied"] = True
    return report


def read_snapshot(s3, bucket: str, prefix: str) -> Snapshot:
    """Every learning file under ``prefix`` with its sha and ETag.

    ponytail: reads every learning file's body (a profile's memory + skills are small); stream or
    trust ``x-amz-meta-sha256`` if a profile ever grows large skill assets.
    """
    snap: Snapshot = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix) :]
            if not (is_learning_file(rel) or rel.startswith(_AUX_PREFIXES)):
                continue
            got = s3.get_object(Bucket=bucket, Key=obj["Key"])
            body = got["Body"].read()
            snap[rel] = (_sha(body), got["ETag"], lambda body=body: body)
    return snap


def run_merge(
    s3,
    bucket: str,
    prefix: str,
    plan_for: Callable[[Snapshot, dict[str, str]], MergePlan],
    *,
    planned: dict[str, str] | None = None,
    attempts: int = 3,
    force_revision: bool = False,
) -> tuple[MergePlan, dict]:
    """Read C, plan, apply; on a lost conditional write re-read C and replan, up to ``attempts``
    times, then raise :class:`CurrentChanged` (Temporal retries the Activity with backoff).

    ``plan_for(current, planned)`` gets every sha planned so far -- the intent row's from an
    interrupted earlier run plus each failed retry's here -- so a write that already landed is
    never planned again (usage deltas would count twice)."""
    planned = dict(planned or {})
    for attempt in range(1, attempts + 1):
        plan = plan_for(read_snapshot(s3, bucket, prefix), planned)
        try:
            return plan, apply_plan(s3, bucket, prefix, plan, force_revision=force_revision)
        except CurrentChanged:
            planned.update(plan.planned)
            if attempt == attempts:
                raise
            # Writes before the 412 may have landed: bump ``.revision`` on the retry regardless.
            force_revision = True
    raise AssertionError("unreachable")
