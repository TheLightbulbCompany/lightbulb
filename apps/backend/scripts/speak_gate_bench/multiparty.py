"""Multi-party chat routing bench: Addlesee et al. 2023's six conditions (A-F) plus our own regressions,
run through the REAL chat routing -- ``messaging.select_dispatch_targets`` (opening names, @mentions,
listen modes, the agent-reply cap), then ``jev._decide_for_message`` for ambient candidates (the
0.7 rule included), then ``openclaw_work``'s fallback when Jev fails. Only the agent cards are stubbed
(name + role + the role line ``jev`` itself falls back to), so no S3/Paperclip read happens.

    export AI_GATEWAY_API_KEY=$(AWS_PROFILE=isol8-admin aws secretsmanager get-secret-value \\
        --secret-id lightbulb/dev/vercel-ai-gateway --query SecretString --output text)
    uv run python -m scripts.speak_gate_bench.multiparty                 # all rows, both variants
    uv run python -m scripts.speak_gate_bench.multiparty --offline       # deterministic layer only
    uv run python -m scripts.speak_gate_bench.multiparty --report-only

``multiparty.jsonl`` extends ``golden.jsonl``'s shape (id, category, expected, people, transcript,
note) with: ``agents`` (preset names or ``{"name", "listen"}``), ``expected_responder`` (agent name,
list of names, or null), ``goal_status`` (open/answered/closed, for goal tracking later), ``cue``
(cued = an agent is named; implicit = nobody is), optional ``subtype``, ``mentions`` (structured
@refs in the chat variant), ``agent_hop`` (an agent-authored latest message's hop), ``room_kind``,
``voice`` (a transcript override for the voice variant) and ``voice_expected_responder``. Transcript
lines may carry ``id`` and ``thread`` (the id of their thread root).

Every row runs twice. ``chat``: as typed, with its structured mentions and listen modes. ``voice``:
how a Live line reaches chat routing (feat/live-jev-routing, #3591) -- no @ or structured mentions,
every agent listening whatever its mode. Both variants run the production addressing stage
(``messaging.address``, routing v2 §3.1) on a human's latest line.
Raw rows land in ``results/raw/`` (gitignored), the summary in ``results/<tag>-multiparty.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://bench.clerk.accounts.dev")  # Settings() refuses to load without it

from core.services import jev, messaging  # noqa: E402

HERE = Path(__file__).parent
RAW = HERE / "results" / "raw"
DATA = HERE / "multiparty.jsonl"

#: name -> (agent id, Paperclip role code). The starter roster plus the renames people actually use.
PRESETS = {
    "CEO": ("ceo", "ceo"),
    "Engineering": ("eng", "engineer"),
    "Finance": ("fin", "cfo"),
    "Growth": ("growth", "cmo"),
    "Analytics": ("analytics", "researcher"),
    "Customer Operations": ("ops", "general"),
    "Friday": ("friday", "ceo"),
    "Forge": ("forge", "engineer"),
    "Gauge": ("gauge", "researcher"),
    "Pulse": ("pulse", "cmo"),
    "Ledger": ("ledger", "cfo"),
    "Atlas": ("atlas", "general"),
    "Max": ("max", "engineer"),
}
CATEGORIES = {
    "A": "helpful companion",
    "B": "shared goal / split utterance",
    "C": "reluctant / intermediary",
    "D": "different goals",
    "E": "missing info",
    "F": "disagreement",
    "H": "implicit handoff",
    "T": "structure (threads)",
    "R": "regressions",
}


def _handle(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _responders(value) -> list[str]:
    return [] if value is None else [value] if isinstance(value, str) else list(value)


# ------------------------------------------------------------------ loading
def expand(row: dict) -> list[dict]:
    """One JSONL row -> its chat and voice cases (or the one ``variants`` names)."""
    exp = _responders(row["expected_responder"])
    if (row["expected"] == "SPEAK") != bool(exp):
        raise ValueError(f"{row['id']}: expected {row['expected']} contradicts responder {exp}")
    cases = []
    for variant in row.get("variants", ["chat", "voice"]):
        voice = variant == "voice"
        lines = row.get("voice") if voice and row.get("voice") else row["transcript"]
        expected = _responders(row["voice_expected_responder"]) if voice and "voice_expected_responder" in row else exp
        cases.append(
            {
                "id": f"{row['id']}-{variant}",
                "row": row["id"],
                "category": row["category"],
                "subtype": row.get("subtype"),
                "cue": row.get("cue", "implicit"),
                "variant": variant,
                "goal_status": row.get("goal_status"),
                "expected_responder": sorted(expected),
                "expected": "SPEAK" if expected else "SILENT",
                "people": row["people"],
                "agents": row["agents"],
                "transcript": [_voiced(t, row["people"]) for t in lines] if voice else lines,
                "mentions": [] if voice else row.get("mentions", []),
                "agent_hop": row.get("agent_hop", 1),
                "room_kind": row.get("room_kind", "channel"),
            }
        )
    return cases


def _voiced(line: dict, people: list[str]) -> dict:
    """What a speech transcript carries: no @handles (a person's handle reads as their name)."""
    text = line["text"]
    for name in people:
        text = re.sub(rf"@{re.escape(_handle(name))}\b", name, text, flags=re.I)
    return {**line, "text": re.sub(r"@(?=\w)", "", text)}


def load(path: Path = DATA) -> list[dict]:
    rows = [json.loads(raw) for raw in path.read_text().splitlines() if raw.strip()]
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate row ids")
    return [case for row in rows for case in expand(row)]


# ------------------------------------------------------------------ the routing state, as prod builds it
def build(case: dict) -> dict:
    voice = case["variant"] == "voice"
    agents = []
    for entry in case["agents"]:
        spec = {"name": entry} if isinstance(entry, str) else entry
        agent_id, role = PRESETS[spec["name"]]
        agents.append(
            {
                "kind": "agent",
                "id": agent_id,
                "displayName": spec["name"],
                "mentionHandle": _handle(spec["name"]),
                # Live ignores listen modes (#3591): any agent in the room can be asked by voice.
                "listenMode": "active" if voice else spec.get("listen", "active"),
                "role": role,
            }
        )
    humans = [
        {"kind": "human", "id": f"user-{_handle(p)}", "displayName": p, "mentionHandle": _handle(p), "listenMode": None}
        for p in case["people"]
    ]
    members = humans + agents
    by_name = {m["displayName"]: m for m in members}
    start = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
    ids: dict[str, str] = {}
    history = []
    for i, line in enumerate(case["transcript"]):
        who = by_name.get(line["speaker"]) or {"kind": "human", "id": f"user-{_handle(line['speaker'])}"}
        msg_id = f"m{i:03d}"
        if line.get("id"):
            ids[line["id"]] = msg_id
        history.append(
            {
                "messageId": msg_id,
                "author": {"kind": who["kind"], "id": who["id"], "displayName": line["speaker"]},
                "text": line["text"],
                "createdAt": (start + timedelta(seconds=20 * i)).isoformat(),
                "threadRootMessageId": ids.get(line["thread"]) if line.get("thread") else None,
            }
        )
    latest = history.pop()
    mentions = [{"kind": by_name[n]["kind"], "id": by_name[n]["id"]} for n in case["mentions"]]
    return {
        "meta": {"roomId": "room-bench", "ownerId": "org_bench", "kind": case["room_kind"], "name": "general"},
        "members": members,
        "agents": agents,
        "latest": {**latest, "mentions": mentions},
        "history": history,
    }


def deterministic(case: dict, state: dict, stage: dict | None = None) -> dict:
    """Everything before Jev: who is addressed (the production addressing stage's agents join the
    structured mentions), who is merely a Listening candidate, the cap."""
    latest, members = state["latest"], state["members"]
    author = latest["author"]
    if author["kind"] == "agent":
        mentions, _ = messaging.parse_agent_mentions(latest["text"], members)
        targets = messaging.select_dispatch_targets(members, mentions, author["id"], room_kind=case["room_kind"])
        if case["agent_hop"] >= messaging.AGENT_REPLY_CAP:
            return {"how": "capped", "addressed": [], "candidates": [], "targets": []}
    else:
        named = [{"kind": "agent", "id": a} for a in (stage or {}).get("agents", [])]
        targets = messaging.select_dispatch_targets(
            members, latest["mentions"] + named, None, room_kind=case["room_kind"]
        )
    addressed = [t["agentId"] for t in targets if t["eventKind"] == "user_request"]
    candidates = [t["agentId"] for t in targets if t["eventKind"] == "room_event"]
    how = "addressed" if addressed else "jev" if candidates else "no_candidates"
    return {"how": how, "addressed": addressed, "candidates": candidates, "targets": targets}


def fallback(state: dict, candidates: list[str], to_people: bool) -> list[str]:
    """``openclaw_work``'s answer when Jev cannot decide."""
    if state["latest"]["author"]["kind"] != "human" or to_people:
        return []
    if len(candidates) == 1:
        return candidates
    for row in reversed(state["history"]):
        if row["author"]["kind"] == "agent" and row["author"]["id"] in candidates:
            return [row["author"]["id"]]
    ceo = next((a["id"] for a in state["agents"] if a["role"] == "ceo"), None)
    return [ceo if ceo in candidates else min(candidates)]


#: Paperclip role -> the tool names a connected agent of that role typically carries, in the card's
#: own shape (``jev._tool_names``: lowercase connector / MCP server names). "Tools: none." made Jev
#: answer "nobody here can know this" on calendar and data questions no real card would lose.
STUB_TOOLS = {
    "ceo": ["gmail", "google-calendar", "google-drive", "paperclip"],
    "cfo": ["google-sheets", "paperclip", "stripe"],
    "engineer": ["ci", "github", "terminal"],
    "cmo": ["gmail", "posthog", "x"],
    "researcher": ["posthog", "sql"],
    "general": ["gmail", "zendesk"],
}


async def _stub_card(owner_id: str, agent_id: str, display_name: str, recency: str = "") -> str:
    role = next(r for i, r in PRESETS.values() if i == agent_id)
    tools = f"Tools: {', '.join(STUB_TOOLS[role])}."
    return jev._assemble((f"{display_name} (role: {role}).", jev._ROLE_DESCRIPTIONS[role], tools), recency)


#: What the voice variant scores: "shipped" = what today's worker delivers (/route's agentId alone; it
#: ignores "hold"), "next" = step 4's worker (queues every agentIds entry, waits on "hold").
VOICE_WORKER = "shipped"


async def route(case: dict, *, offline: bool = False) -> dict:
    """The production router: chat as create_human_message + openclaw_work run it; voice through the
    same addressing stage with every agent listening (the Live router it once mirrored is gone, #5057)."""
    state = build(case)
    voice = case["variant"] == "voice"
    latest = state["latest"]
    stage = None
    human = latest["author"]["kind"] == "human"
    if human and not offline and (voice or case["room_kind"] != "dm"):
        stage = await messaging.address(latest, state["members"], history=state["history"], live=voice)
        latest["addressing"] = messaging._stored_addressing(stage)
    det = deterministic(case, state, stage)
    rec = {k: case[k] for k in ("id", "row", "category", "subtype", "cue", "variant", "goal_status", "expected")}
    rec |= {"expected_responder": case["expected_responder"], "how": det["how"], "whoP": None}
    rec["stage"] = {k: stage[k] for k in ("path", "agents", "humans", "hold", "anyone")} if stage else None
    names = {a["id"]: a["displayName"] for a in state["agents"]}
    next_worker = VOICE_WORKER == "next"
    if voice and next_worker and stage and stage["hold"]:
        rec["how"], rec["responders"] = "hold", []
        return rec
    if det["how"] != "jev":
        addressed = det["addressed"]
        if voice and not next_worker and addressed:
            # The shipped worker summons /route's agentId alone: the agent named first.
            addressed = [next((a for a in (stage or {}).get("agents", []) if a in addressed), addressed[0])]
        rec["responders"] = sorted(names[a] for a in addressed)
        return rec
    if offline:
        rec["how"], rec["responders"] = "skipped_offline", None
        return rec
    latest = {**latest, "dispatchTargets": det["targets"]}
    if voice:
        people = stage["humans"] if stage else []
        latest["mentions"] = [{"kind": "human", "id": p} for p in people]
        to_people = bool(people) and not (stage or {}).get("agents") and not (stage or {}).get("anyone")
    else:
        to_people = jev.speaks_to_people(latest, state["members"])
    decision = await jev._decide_for_message(
        state["meta"], latest, latest, state["members"], state["history"], to_people=to_people, placement=not voice
    )
    who = decision.get("who")
    if decision.get("fallback") or decision.get("rateLimited") or decision.get("forced"):
        rec["how"] = f"forced_{decision['forced']}" if decision.get("forced") else "fallback"
        rec["responders"] = sorted(names[a] for a in fallback(state, det["candidates"], to_people))
    elif voice and who == "none" and len(case["people"]) <= 1:
        rec["how"] = "fallback_lone"  # a lone speaker's "none" counts as Jev down
        rec["responders"] = sorted(names[a] for a in fallback(state, det["candidates"], to_people))
    else:
        rec["responders"] = [] if who == "none" else [names[who]]
    rec["whoP"] = {names.get(k, k): round(float(v), 3) for k, v in (decision.get("whoP") or {}).items()}
    return rec


# ------------------------------------------------------------------ scoring
def score(recs: list[dict]) -> dict:
    """Per-class, never plain accuracy: ~80% of real turns address nobody, so "always none" would look
    good (Inoue et al., IWSDS 2025). SPEAK rows: recall (the right agent spoke) and wrong-agent rate.
    SILENT rows: false-speak rate. Precision: of the turns where an agent spoke, how many were right."""
    judged = [r for r in recs if r.get("responders") is not None]
    speak = [r for r in judged if r["expected"] == "SPEAK"]
    silent = [r for r in judged if r["expected"] == "SILENT"]
    spoke = [r for r in judged if r["responders"]]
    right = [r for r in speak if sorted(r["responders"]) == r["expected_responder"]]
    # Wrong agent = someone answered who was not expected; missed = only expected agents (or nobody)
    # answered but not all of them -- a partial ["Finance"] for ["Finance", "Growth"] is a miss.
    wrong = [r for r in speak if set(r["responders"]) - set(r["expected_responder"])]
    return {
        "n": len(judged),
        "speak_n": len(speak),
        "silent_n": len(silent),
        "recall": len(right) / len(speak) if speak else None,
        "wrong_agent": len(wrong) / len(speak) if speak else None,
        "missed": sum(1 for r in speak if r not in right and r not in wrong) / len(speak) if speak else None,
        "false_speak": sum(1 for r in silent if r["responders"]) / len(silent) if silent else None,
        "precision": sum(1 for r in spoke if r in right) / len(spoke) if spoke else None,
    }


def _f(x):
    return "–" if x is None else f"{x:.2f}"


def report(recs: list[dict]) -> str:
    out = [
        "# Multi-party routing bench (Addlesee A–F + regressions)",
        "",
        f"Full chat routing, Jev `{jev.MODEL}` with stub role cards. Recall = the expected agent(s) and "
        "nobody else; false-speak = any agent on a SILENT row. `n` counts judged cases (offline skips Jev rows).",
        "",
    ]
    hdr = "| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |"
    sep = "|---|---|---|---|---|---|---|---|---|"

    def line(label, rs):
        s = score(rs)
        return (
            f"| {label} | {s['n']} | {s['speak_n']} | {_f(s['recall'])} | {_f(s['wrong_agent'])} | {_f(s['missed'])} "
            f"| {s['silent_n']} | {_f(s['false_speak'])} | {_f(s['precision'])} |"
        )

    for variant in ("chat", "voice", None):
        rs = [r for r in recs if variant is None or r["variant"] == variant]
        out += [f"## {variant or 'all'}", "", hdr, sep]
        out += [line(f"{c} {CATEGORIES[c]}", [r for r in rs if r["category"] == c]) for c in CATEGORIES]
        out += [line("cued (agent named)", [r for r in rs if r["cue"] == "cued"])]
        out += [line("implicit (nobody named)", [r for r in rs if r["cue"] == "implicit"])]
        out += [line("**total**", rs), ""]
    by_sub = defaultdict(list)
    for r in recs:
        if r["subtype"]:
            by_sub[(r["category"], r["subtype"], r["variant"])].append(r)
    if by_sub:
        out += (
            ["## Subtypes", "", hdr, sep]
            + [line(" / ".join(k), v) for k, v in sorted(by_sub.items()) if score(v)["n"]]
            + [""]
        )
    misses = [r for r in recs if r.get("responders") is not None and sorted(r["responders"]) != r["expected_responder"]]
    out += [f"## Misses ({len(misses)})", "", "| case | expected | got | how | P(who) |", "|---|---|---|---|---|"]
    for r in sorted(misses, key=lambda r: r["id"]):
        p = ", ".join(f"{k} {v:.2f}" for k, v in sorted((r["whoP"] or {}).items(), key=lambda kv: -kv[1])[:3])
        out += [f"| {r['id']} | {r['expected_responder'] or 'none'} | {r['responders'] or 'none'} | {r['how']} | {p} |"]
    return "\n".join(out)


# ------------------------------------------------------------------ runner
async def run(cases: list[dict], out: Path, concurrency: int, offline: bool) -> None:
    if not offline:
        jev.put_metric = messaging.put_metric = lambda *a, **k: None
        jev._key, jev._key_checked_at, jev._KEY_TTL_S = os.environ["AI_GATEWAY_API_KEY"], time.monotonic(), 10**9
    jev.agent_card = _stub_card
    done = {json.loads(line)["id"] for line in out.read_text().splitlines()} if out.exists() else set()
    sem = asyncio.Semaphore(concurrency)

    async def one(case):
        async with sem:
            for attempt in range(4):
                rec = await route(case, offline=offline)
                if rec["how"] != "fallback":
                    return rec
                await asyncio.sleep(2**attempt)  # a failed Jev call: retry before recording prod's fallback
            return rec

    with out.open("a") as fh:
        for coro in asyncio.as_completed([one(c) for c in cases if c["id"] not in done]):
            fh.write(json.dumps(await coro) + "\n")
            fh.flush()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="deterministic layer only; no Jev calls")
    ap.add_argument("--only", default="", help="regex over case ids")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--tag", default="")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--voice-worker", choices=("shipped", "next"), default="shipped", help="see VOICE_WORKER")
    args = ap.parse_args(argv)
    tag = (args.tag or date.today().isoformat()) + ("-offline" if args.offline else "")
    cases = [c for c in load() if re.search(args.only, c["id"])]
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / f"{tag}-multiparty.jsonl"
    if not args.report_only:
        global VOICE_WORKER
        VOICE_WORKER = args.voice_worker
        asyncio.run(run(cases, out, args.concurrency, args.offline))
    selected = {c["id"] for c in cases}  # the raw file may hold rows from earlier, wider runs
    recs = [r for r in map(json.loads, out.read_text().splitlines()) if r["id"] in selected]
    md = report(recs)
    (HERE / "results" / f"{tag}-multiparty.md").write_text(md + "\n")
    print(md)


if __name__ == "__main__":
    sys.exit(main())
