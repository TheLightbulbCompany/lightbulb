"""Understanding bench: one spoken Live line -> the requests on it, who owns each, and what is no request.

Runs the REAL ``voice_calls.route_live_line`` (live Jev on the dev gateway). Only the room is stubbed: its
members, the speaker's name, the CEO id and the agent cards (``multiparty._stub_card``), so nothing reads
DynamoDB, S3 or Paperclip. A /route answer without ``ops`` (the router before conversation understanding)
is read as the worker treats it: every routed agent gets the whole line, or its phase-1 task.

``understand.jsonl``, one scene per row: ``{id, cat, agents, people, speaker?, transcript, floor, floor_with?,
open, setting?, wake?, line, expect}``. ``expect``: ``requests`` (each ``{has, not, owner?}``: phrases its task
must and must not contain, and the agents that may own it), ``social`` (nothing asked; one agent may
reply), ``silent`` (nothing asked; nobody replies), ``more`` (requests were announced), ``cancel`` and
``steer`` (ids of ``open`` requests).

    export AI_GATEWAY_API_KEY=$(AWS_PROFILE=isol8-admin aws secretsmanager get-secret-value \\
        --secret-id lightbulb/dev/vercel-ai-gateway --query SecretString --output text)
    uv run python -m scripts.speak_gate_bench.understand --check          # the dataset only, no calls
    uv run python -m scripts.speak_gate_bench.understand --runs 3 --tag baseline
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://bench.clerk.accounts.dev")  # Settings() refuses to load without it

from core.services import jev, messaging, voice_calls  # noqa: E402

from .multiparty import PRESETS, ROOMS, _handle, _stub_card, stub_room  # noqa: E402

HERE = Path(__file__).parent
SCENES: dict[str, dict] = {}  # scene id (its room and owner id) -> built scene
KINDS_SEEN: dict[str, list] = {}  # line -> [(part, kind, p)] as Jev read it, for fitting REQUEST_P


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' -]", " ", text.lower().replace("’", "'"))).strip()


def build(case: dict) -> dict:
    agents = [
        {
            "kind": "agent",
            "id": PRESETS[n][0],
            "displayName": n,
            "mentionHandle": _handle(n),
            "listenMode": "active",
            "role": PRESETS[n][1],
        }
        for n in case["agents"]
    ]
    people = [
        {"kind": "human", "id": f"user-{_handle(p)}", "displayName": p, "mentionHandle": _handle(p), "role": "owner"}
        for p in case["people"]
    ]
    ids = {m["displayName"]: m["id"] for m in agents + people}
    return {"members": people + agents, "ids": ids, "names": {v: k for k, v in ids.items()}}


def check(cases: list[dict]) -> list[str]:
    """What is wrong with the dataset itself (a phrase that is not in its line can never pass)."""
    bad, seen = [], set()
    for c in cases:
        e, line = c["expect"], _norm(c["line"])
        why = []
        if c["id"] in seen:
            why.append("duplicate id")
        seen.add(c["id"])
        if any(n not in PRESETS for n in c["agents"]):
            why.append("unknown agent")
        names = set(c["agents"])
        if c.get("floor") and c["floor"] not in names:
            why.append("floor not in agents")
        if c.get("speaker", c["people"][0]) not in c["people"]:
            why.append("speaker not in people")
        open_ids = {o["id"]: o for o in c.get("open") or []}
        why += [f"open agent {o['agent']}" for o in open_ids.values() if o["agent"] not in names]
        why += [f"unknown open id {i}" for i in [*e.get("cancel", []), *e.get("steer", [])] if i not in open_ids]
        if (e.get("social") or e.get("silent")) and e.get("requests"):
            why.append("social/silent with requests")
        if e.get("social") and e.get("silent"):
            why.append("social and silent")
        for r in e.get("requests", []):
            why += [f"owner {o}" for o in r.get("owner", []) if o not in names]
            why += [
                f"phrase not in line: {p!r}" for p in [*r.get("has", []), *r.get("not", [])] if _norm(p) not in line
            ]
        bad += [f"{c['id']}: {w}" for w in why]
    return bad


def produced(answer: dict, line: str) -> dict:
    ops = answer.get("ops")
    if ops is None:  # the router before understanding: each routed agent gets the line, or its phase-1 task
        reqs = answer.get("requests") or [{"agentId": a, "task": line} for a in answer.get("agentIds") or []]
        ops = {"requests": reqs}
    return {"requests": [], "social": None, "more": False, "cancel": [], "steer": [], **ops}


def grade(case: dict, ops: dict, names: dict) -> dict:
    e = case["expect"]
    want, got = e.get("requests", []), ops["requests"]
    why, used, owners = [], set(), []
    for w in want:
        hit = next(
            (
                i
                for i, g in enumerate(got)
                if i not in used
                and all(_norm(p) in _norm(g["task"]) for p in w.get("has", []))
                and not any(_norm(p) in _norm(g["task"]) for p in w.get("not", []))
            ),
            None,
        )
        if hit is None:
            why.append(f"missing {w.get('has')}")
            continue
        used.add(hit)
        if w.get("owner"):
            owner = names.get(got[hit]["agentId"], got[hit]["agentId"])
            owners.append(owner in w["owner"])
            if not owners[-1]:
                why.append(f"owner {owner} not in {w['owner']} for {w.get('has')}")
    # an ask that reached some agent, even inside a row with another ask (a missed cut, not a dropped ask)
    reached = sum(any(all(_norm(p) in _norm(g["task"]) for p in w.get("has", [])) for g in got) for w in want)
    extra = len(got) - len(used)
    if extra:
        why.append(f"{extra} extra request(s)")
    replied = bool(ops["social"])
    if e.get("social") and not replied and not got:
        why.append("nobody replies to small talk")
    if e.get("silent") and replied:
        why.append("an agent replies to people talk")
    # ``more`` beside a request changes nothing the speaker hears: a missed announcement is graded, and a
    # ``more`` where nothing was asked (a greeting answered with "go ahead" is wrong); never an extra one
    # beside a request ("One more, has the patch merged?").
    if (e.get("more") or not want) and bool(e.get("more")) != bool(ops["more"]):
        why.append(f"more={bool(ops['more'])}")
    steered = [s["requestId"] for s in ops["steer"]]
    for key, have in (("cancel", ops["cancel"]), ("steer", steered)):
        if sorted(e.get(key, [])) != sorted(have):
            why.append(f"{key}={have}")
    return {
        "ok": not why,
        "why": why,
        "want": len(want),
        "matched": len(used),
        "reached": reached,
        "rows": len(got),
        "extra": extra,
        "owners": owners,
        "idle": not want,  # nothing was asked: any row is work for nothing
    }


async def one(case: dict) -> dict:
    scene = SCENES[case["id"]]
    ids = scene["ids"]
    speaker = case.get("speaker", case["people"][0])
    # ``floor_with``: who the floor agent is talking with (default: the first person).
    with_ = case.get("floor_with", case["people"][0])
    floor = {"agent": ids[case["floor"]], "speaker": with_, "mine": with_ == speaker} if case.get("floor") else None
    opened = [
        {"id": o["id"], "agentId": ids[o["agent"]], "task": o["task"], "status": o["status"]}
        for o in case.get("open") or []
    ]
    kw = dict(
        owner_id=case["id"],
        room_id=case["id"],
        speaker_id=ids[speaker],
        utterance=case["line"],
        transcript="\n".join([*case.get("transcript", []), f"{speaker}: {case['line']}"]),
        humans=len(case["people"]),
        wake=bool(case.get("wake")),
        floor=floor,
        setting=case.get("setting", "live"),
    )
    if "open_requests" in inspect.signature(voice_calls.route_live_line).parameters:
        kw["open_requests"] = opened
    if floor and floor["mine"] and "engagements" in inspect.signature(voice_calls.route_live_line).parameters:
        # A meeting routes on who the speaker is talking with, not on ``floor.mine``. A scene's floor agent has
        # just answered its speaker (or is still working for them): that is what the worker would send.
        heard = any(o["agentId"] == floor["agent"] and o["status"] == "delivered" for o in opened)
        kw["engagements"] = [
            {
                "agentId": floor["agent"],
                "state": "active",
                "expects": "nothing" if heard or not opened else "answer",
                "last": "delivered" if heard or not opened else "none",
                "lastBy": "",
            }
        ]
    started = time.perf_counter()
    try:
        answer = await voice_calls.route_live_line(**kw)
    except Exception as exc:  # noqa: BLE001 -- a crash is a failed scene, not a failed bench
        answer = {"error": f"{type(exc).__name__}: {exc}"}
    ms = (time.perf_counter() - started) * 1000
    ops = produced(answer, case["line"])
    kinds = KINDS_SEEN.get(case["line"], [])
    out = grade(case, ops, scene["names"])
    if "error" in answer:
        out["ok"], out["why"] = False, [answer["error"]]
    got = [
        f"{scene['names'].get(r['agentId'], r['agentId'])} ({r.get('by', '-')}): {r['task']}" for r in ops["requests"]
    ]
    return {"id": case["id"], "cat": case["cat"], "ms": ms, "got": got, "ops": ops, "kinds": kinds, **out}


async def run(cases: list[dict], runs: int, concurrency: int) -> list[list[dict]]:
    jev._key, jev._key_checked_at, jev._KEY_TTL_S = os.environ["AI_GATEWAY_API_KEY"], time.monotonic(), 10**9
    jev.agent_card = _stub_card
    stub_room()
    for mod in (messaging, jev, voice_calls):
        mod.put_metric = lambda *a, **k: None  # no EMF noise in a bench
    sem = asyncio.Semaphore(concurrency)
    address = messaging.address

    async def seen(latest, *a, **k):
        out = await address(latest, *a, **k)
        parts = [(p["text"], p["kind"][0], round(p["kind"][1], 2)) for p in out.get("parts") or []]
        KINDS_SEEN[latest.get("text")] = parts
        return out

    messaging.address = seen

    async def guarded(case):
        async with sem:
            return await one(case)

    return [await asyncio.gather(*(guarded(c) for c in cases)) for _ in range(runs)]


def report(all_runs: list[list[dict]], tag: str) -> str:
    rows = [r for run in all_runs for r in run]
    by_id: dict[str, list[dict]] = defaultdict(list)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_id[r["id"]].append(r)
        by_cat[r["cat"]].append(r)

    def rate(n, d):
        return f"{n}/{d} = {n / d:.2f}" if d else "n/a"

    want, matched = sum(r["want"] for r in rows), sum(r["matched"] for r in rows)
    made, extra = sum(r["rows"] for r in rows), sum(r["extra"] for r in rows)
    idle = [r for r in rows if r["idle"]]
    owners = [o for r in rows for o in r["owners"]]
    ms = sorted(r["ms"] for r in rows)
    out = [
        f"== {tag}: {len(all_runs)} run(s) of {len(by_id)} scenes",
        f"  asks that reach an agent {rate(sum(r['reached'] for r in rows), want)}   (a miss is a dropped ask)",
        f"  request recall          {rate(matched, want)}   (gate 0.95; each ask in a row of its own)",
        f"  extra rows              {rate(extra, made)}   (gate <= 0.03)",
        f"  work started for nothing {rate(sum(1 for r in idle if r['rows']), len(idle))}   (gate <= 0.02)",
        f"  owner allowed           {rate(sum(owners), len(owners))}   (gate 0.90)",
        f"  scenes passing          {rate(sum(r['ok'] for r in rows), len(rows))}",
        f"  scenes passing all runs {rate(sum(all(r['ok'] for r in rs) for rs in by_id.values()), len(by_id))}   (gate 0.85)",
        f"  latency ms              p50 {statistics.median(ms):.0f}  p95 {ms[int(len(ms) * 0.95) - 1]:.0f}  max {ms[-1]:.0f}",
        "",
        "  by category (scenes passing):",
    ]
    out += [f"    {cat:22} {rate(sum(r['ok'] for r in rs), len(rs))}" for cat, rs in sorted(by_cat.items())]
    out += ["", "-- failures (id: passes/runs)"]
    for cid, rs in by_id.items():
        bad = [r for r in rs if not r["ok"]]
        if bad:
            out.append(f"  {cid}: {len(rs) - len(bad)}/{len(rs)}  {bad[0]['why']}")
            out += [f"      {g[:150]}" for g in bad[0]["got"]]
            out += [f"      kind {k} {p}: {t[:100]}" for t, k, p in bad[0].get("kinds") or []]
    return "\n".join(out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="", help="regex on scene id or category")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--check", action="store_true", help="validate the dataset and exit")
    ap.add_argument("--request-p", type=float, default=None, help="override voice_calls.REQUEST_P for this run")
    ap.add_argument(
        "--file", default="understand.jsonl", help="understand_holdout.jsonl: written blind, never tuned on"
    )
    args = ap.parse_args(argv)
    cases = [json.loads(line) for line in (HERE / args.file).read_text().splitlines() if line.strip()]
    if bad := check(cases):
        raise SystemExit(f"{args.file}:\n  " + "\n  ".join(bad))
    if args.check:
        print(f"{len(cases)} scenes ok")
        return
    cases = [c for c in cases if re.search(args.only, c["id"]) or re.search(args.only, c["cat"])]
    if args.request_p is not None:
        voice_calls.REQUEST_P = args.request_p
    for c in cases:
        SCENES[c["id"]] = build(c)
        ROOMS[c["id"]] = SCENES[c["id"]]["members"]
    all_runs = asyncio.run(run(cases, args.runs, args.concurrency))
    raw = HERE / "results" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"understand-{args.tag}.json").write_text(json.dumps(all_runs, indent=1))
    print(f"REQUEST_P {voice_calls.REQUEST_P}")
    print(report(all_runs, args.tag))


if __name__ == "__main__":
    main()
