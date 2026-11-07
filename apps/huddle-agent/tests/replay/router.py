"""The backend's own /route decision, run inside a replay.

`voice_calls.route_live_line` is the real function, loaded from apps/backend by file: the cut into parts,
the name rules, `_understood`, `_engaged` and what `ops` says are the code prod runs, on the worker's actual
/route body. Two things are not real. The room's storage (who is in it) is answered here. And the classifier
(`jev.decide`): its answer to each question the router asks comes from `Jev`, below, which reads the
scenario's table (routing.py) and adds the mistakes a real one makes, seeded.

The backend's I/O imports (AWS, FastAPI, the rest of `core`) are not in this project, and nothing here may
reach them: they load as stand-ins that record calls and return nothing. Only messaging.py, jev.py and
voice_calls.py are real."""

from __future__ import annotations

import contextvars
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import random
import re
import sys
from unittest import mock

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 3, "backend", "core", "services"))
REAL = ("messaging", "jev", "voice_calls")
STAND_INS = ("core", "boto3", "botocore", "fastapi", "yaml")  # what the three modules import and never need here
OWNER, ROOM, HUMAN = "org_replay", "room-replay", "u1"

_SIM: contextvars.ContextVar = contextvars.ContextVar("replay_router_sim")
_vc = None


class _StandIn(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in STAND_INS and name not in sys.modules:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        m = mock.MagicMock(name=spec.name)
        m.__name__, m.__path__, m.__spec__ = spec.name, [], spec
        return m

    def exec_module(self, module) -> None:
        pass


def backend():
    """`core.services.voice_calls`, loaded once, with the room and the classifier answered by the replay."""
    global _vc
    if _vc is not None:
        return _vc
    finder = _StandIn()
    sys.meta_path.append(finder)  # last: only what nothing else can import
    try:
        import core.services  # noqa: F401 -- the stand-in package the real three hang on

        mods = {}
        for n in REAL:
            spec = importlib.util.spec_from_file_location(f"core.services.{n}", os.path.join(BACKEND, f"{n}.py"))
            mods[n] = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
            setattr(sys.modules["core.services"], n, mods[n])
        for m in mods.values():
            m.__spec__.loader.exec_module(m)
    finally:
        sys.meta_path.remove(finder)
    vc, jev = mods["voice_calls"], mods["jev"]

    async def members(room_id: str) -> list[dict]:
        return _SIM.get().router.members

    async def room_team(owner_id: str, user_id: str, room_id: str, **kw) -> tuple[dict, list[dict]]:
        return {"kind": "channel", "ownerId": owner_id, "meeting": True}, []

    async def card(owner_id: str, agent_id: str, name: str, recency: str = "") -> str:
        return name

    async def decide(state: dict, questions: dict) -> dict:
        return _SIM.get().router.jev.decide(state, questions)

    async def fallback(owner_id: str, candidates: list[str], message: dict, history: list[dict]) -> str:
        ceo = _SIM.get().team["ceoId"]
        return ceo if ceo in candidates else min(candidates)

    vc.messaging_repo.list_members = members
    vc.messaging_repo.agent_text = lambda text: text  # the real one only unescapes web-composer markdown
    vc._room_team = room_team
    vc.openclaw_work._fallback_single = fallback
    jev.agent_card, jev.decide = card, decide
    _vc = vc
    return vc


class Router:
    """One replay's /route: `route_live_line` on the worker's body."""

    def __init__(self, sim, table: dict[int, dict], seed: int | str) -> None:
        self.sim, self.vc = sim, backend()
        self.members = [
            {"kind": "agent", "id": a["id"], "displayName": a["name"], "listenMode": a["listenMode"]}
            for a in sim.team["agents"]
        ] + [{"kind": "human", "id": HUMAN, "displayName": p["name"], "role": "owner"} for p in sim.team["people"][:1]]
        self.jev = Jev(sim, table, seed)

    async def route(self, speaker: str, utterance: str, transcript: str, humans: int | None, body: dict) -> dict:
        token = _SIM.set(self.sim)
        try:
            return await self.vc.route_live_line(  # routers/huddles.py `route`, argument for argument
                owner_id=OWNER, room_id=ROOM, speaker_id=speaker, utterance=utterance, transcript=transcript,
                humans=humans or 0, wake=humans is None, floor=body.get("floor"),
                voice_runs=list(body.get("voice_runs") or []), at=list(body.get("at") or []),
                setting=body.get("setting", "live"), unclaimed=bool(body.get("unclaimed")),
                open_requests=list(body.get("open") or []), engagements=list(body.get("engagements") or []),
            )  # fmt: skip
        finally:
            _SIM.reset(token)


# How often the classifier is wrong, per line. Each is an ASSUMPTION sized from one live run (2026-10-08,
# 15 scenarios, about 35 lines), not a measurement: the router logged no labels then.
P_NAMED_MISS = 0.08  # a named ask read as not for the agents: 1 of about 12 (never_mind line 0)
P_WEAK = 0.02  # a request read under voice_calls.REQUEST_P
P_PEOPLE_ASK_ENGAGED = 0.5  # talk between people read as a request while the room says its speaker is talking
P_PEOPLE_ASK = 0.1  # ... with an agent (unnamed_sidetalk line 1: 1 of 1), and when it does not
P_FOLLOW_MISS = 0.1  # a follow-up not tied to the request it continues
P_FOLLOW_FALSE = 0.05  # talk between people tied to an earlier request


class Jev:
    """The classifier's answers, question by question (`messaging._label` asks them). What a line IS comes from
    the scenario's table: per line a verdict (routing.py). What the classifier SAYS it is, is that with a
    probability drawn for each label and, at the rates above, a wrong label. One line gets one reading: the
    same words asked about twice (a question routed again as unclaimed) get the same answer."""

    def __init__(self, sim, table: dict[int, dict], seed: int | str) -> None:
        self.sim, self.table, self.seed = sim, table, seed

    def verdict(self, speaker: str, utterance: str) -> tuple[int | None, dict]:
        i = self.sim._line_of(speaker, utterance)
        v = self.table.get(i, {}) if i is not None else {}
        if "when" in v and not re.search(v["when"], utterance, re.I):
            v = v.get("else", {})  # a piece of the line without its request in it
        return i, v

    def decide(self, state: dict, questions: dict) -> dict:
        text, speaker = state["latest"]["text"], state["latest"]["author"]
        i, v = self.verdict(speaker, text)
        rng = random.Random(f"{self.seed}:jev:{i}:{text}")
        sure = lambda: rng.uniform(0.6, 0.97)  # noqa: E731
        agent = v.get("ask") or v.get("social") or v.get("called") or v.get("agent")
        engaged = "is talking with" in (state["room"].get("floor") or "")
        people = bool(v.get("people"))
        # the mistakes, drawn whether or not this line can make them: one reading per line and seed
        named_miss, weak, follow_miss, follow_false = (rng.random() < p for p in (P_NAMED_MISS, P_WEAK, P_FOLLOW_MISS, P_FOLLOW_FALSE))  # fmt: skip
        miss_kind = rng.random() < 0.5  # how a named ask is missed: its part is people talk, or the name is no name
        misread = rng.random() < (P_PEOPLE_ASK_ENGAGED if engaged else P_PEOPLE_ASK)
        named_miss = named_miss and "ask" in v

        def choice(pick: str, criteria: dict) -> dict:
            p = sure()
            rest = [k for k in criteria if k != pick]
            return {"choice": pick, "probabilities": {pick: p, **({rest[0]: 1 - p} if rest else {})}}

        parts = {k: json.loads(q["instructions"].rsplit(" The part: ", 1)[1][:-1]) for k, q in questions.items() if k[0] == "k"}
        hot = [k for k, part in parts.items() if "when" in v and re.search(v["when"], part, re.I)]
        hot = hot or sorted(parts, key=lambda k: len(parts[k]))[-1:]  # the longest part carries the line
        kind = (
            "request" if "ask" in v else "nudge" if "called" in v else "people" if people
            else {"cancel": "cancel", "hush": "hush"}.get(v.get("stop"), "social")
        )  # fmt: skip
        beside = "people" if v.get("else", {}).get("people") else "social"
        out = {
            "complete": {"probability": rng.uniform(0.05, 0.3) if v.get("hold") else rng.uniform(0.8, 0.99)},
            "anyone": {"probability": rng.uniform(0.01, 0.2)},
        }
        for key, q in questions.items():
            crit = q.get("criteria") or {}
            if key[0] == "n":  # how the line uses this name
                name = re.search(r"The name: '(.*)' \(", q["instructions"]).group(1)
                mine = any(a["id"] == agent for a in self.sim.team["agents"] if a["name"].lower() == name)
                human = "a person" in q["instructions"].rsplit("(", 1)[1]
                label = "addressed" if mine or (human and people) else "mentioned"
                if mine and named_miss and not miss_kind:
                    label = "not_a_name"
                out[key] = choice(label, crit)
            elif key[0] == "k":
                k = kind if key in hot else beside
                if k == "request" and named_miss and miss_kind:
                    k = "people"
                elif k == "people" and misread:
                    k = "request"
                out[key] = choice(k, crit)
                if k == "request" and weak:
                    out[key]["probabilities"]["request"] = rng.uniform(0.35, 0.49)
            elif key[0] == "w":
                out[key] = choice(agent if "ask" in v and "k" + key[1:] in hot and agent in crit else "unsure", crit)
            elif key[0] == "p":  # several agents named: whose part this is
                own = next((c for c in crit if c.startswith("only_") and c[5:] == agent), "nobody")
                out[key] = choice(own, crit)
            elif key == "amends":
                follows = (v.get("follows") or v.get("by") == "follow") and not follow_miss
                rows = [rid for rid, line in crit.items() if rid != "none" and line.lower().startswith(f"{agent}:")]
                if people and follow_false:
                    rows = [rid for rid in crit if rid != "none"]
                out[key] = choice(rows[-1] if rows and (follows or (people and follow_false)) else "none", crit)
            elif key == "drops" or key[0] == "d":
                rows = [rid for rid, line in crit.items() if rid != "none" and line.lower().startswith(f"{agent}:") and "(delivered)" not in line]
                out[key] = choice(rows[0] if rows and v.get("stop") else "none", crit)
        return out
