"""Multi-party end-to-end runner: plays a scenario into a REAL dev room as several test humans,
waits for the agents, records who answered and what they said, then scores every step with hard
checks and an LLM judge. Dev only -- it refuses any API base that is not ``api-dev``.

    cd apps/backend
    uv run python -m scripts.multiparty_e2e.runner setup                  # once: test humans in the org
    uv run python -m scripts.multiparty_e2e.runner run scenarios/A.yaml   # one scenario file
    uv run python -m scripts.multiparty_e2e.runner run scenarios/*.yaml --only A-dev-01

See README.md in this folder for the scenario format and what each check means.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

HERE = Path(__file__).parent
DEV_HOST = "api-dev.thelightbulb.company"
API = os.environ.get("MP_API_BASE", f"https://{DEV_HOST}/api/v1")
#: The dev Clerk instance (publishable key pk_test_...: base64 of "up-moth-55.clerk.accounts.dev$").
FAPI = os.environ.get("MP_CLERK_FAPI", "https://up-moth-55.clerk.accounts.dev")
#: "Jev Listening Test": a dev test workspace with a running runtime and the six-agent starter roster.
ORG = os.environ.get("MP_ORG_ID", "org_3JhR0rr1ZGMFnZc2D6dQ8BHhh9G")
#: The test humans. Emails sit on a reserved domain: nothing is ever delivered to them.
CAST = {key: f"mp-e2e-{key}@example.com" for key in ("maya", "leo", "aden")}
ROLES = ("ceo", "engineering", "finance", "growth", "analytics", "customer operations")
JUDGE_MODEL = os.environ.get("MP_JUDGE_MODEL", "google/gemini-2.5-flash")
UA = {"User-Agent": "lightbulb-multiparty-e2e"}


def _secret(name: str) -> str:
    out = subprocess.check_output(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            name,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        env={**os.environ, "AWS_PROFILE": os.environ.get("AWS_PROFILE", "isol8-admin"), "AWS_REGION": "us-east-1"},
    ).decode()
    value = out.strip()
    return json.loads(value).get("CLERK_SECRET_KEY", value) if value.startswith("{") else value


# ------------------------------------------------------------------ Clerk (dev): admin API + FAPI ticket sign-in
#: Backend API pacing: at most 5 calls a second, and a 429 is waited out (Retry-After, else 2 s x attempt)
#: before the caller sees any response. Every Clerk call, every verb: a 16-user fixture setup 429'd on its
#: user lookups and membership adds on 2026-10-08 when only the session POSTs were paced.
BAPI_SPACING_S = 0.2
BAPI_TRIES = 6


class PacedClient(httpx.Client):
    """httpx.Client whose every request is paced and whose 429s are retried; the last answer is returned."""

    _last_call = 0.0

    def send(self, request, **kwargs):
        for attempt in range(1, BAPI_TRIES + 1):
            wait = PacedClient._last_call + BAPI_SPACING_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            PacedClient._last_call = time.monotonic()
            resp = super().send(request, **kwargs)
            if resp.status_code != 429 or attempt == BAPI_TRIES:
                return resp
            resp.read()
            time.sleep(min(float(resp.headers.get("retry-after") or 2 * attempt), 60))
        raise AssertionError("unreachable")


class Clerk:
    def __init__(self):
        self.sk = _secret("lightbulb/dev/clerk_secret_key")
        self.http = PacedClient(
            base_url="https://api.clerk.com/v1", headers={"Authorization": f"Bearer {self.sk}", **UA}
        )
        if self.http.get("/instance").json().get("environment_type") != "development":
            raise SystemExit("refusing: the Clerk secret is not a development instance")

    def user(self, email: str) -> dict | None:
        found = self.http.get("/users", params={"email_address": email}).raise_for_status().json()
        return found[0] if found else None

    def ensure_member(self, key: str) -> str:
        email = CAST[key]
        user = (
            self.user(email)
            or self.http.post(
                "/users",
                json={
                    "email_address": [email],
                    "first_name": key.title(),
                    "last_name": "Test",
                    "skip_password_requirement": True,
                },
            )
            .raise_for_status()
            .json()
        )
        resp = self.http.post(f"/organizations/{ORG}/memberships", json={"user_id": user["id"], "role": "org:member"})
        if resp.status_code not in (200, 400, 422):  # 4xx here = already a member
            resp.raise_for_status()
        return user["id"]

    def session(self, user_id: str, org: str | None = None) -> "Session":
        ticket = (
            self.http.post("/sign_in_tokens", json={"user_id": user_id, "expires_in_seconds": 300})
            .raise_for_status()
            .json()["token"]
        )
        return Session(ticket, org)


class Session:
    """A native FAPI client signed in with a one-time ticket; mints 60 s session JWTs on demand."""

    def __init__(self, ticket: str, org: str | None = None):
        self.http = httpx.Client(base_url=FAPI, params={"_is_native": "1"}, headers=UA, timeout=20)
        self.client_jwt = self.http.post("/v1/client").headers["authorization"]
        resp = self._post("/v1/client/sign_ins", {"strategy": "ticket", "ticket": ticket})
        self.sid = resp["response"]["created_session_id"]
        # ``org``: the workspace every minted JWT carries (load fixtures pass their own org).
        self._post(f"/v1/client/sessions/{self.sid}/touch", {"active_organization_id": org or ORG})
        self._jwt, self._at = "", 0.0

    def _post(self, path: str, data: dict | None = None) -> dict:
        resp = self.http.post(path, data=data or {}, headers={"Authorization": self.client_jwt}).raise_for_status()
        self.client_jwt = resp.headers.get("authorization") or self.client_jwt
        return resp.json()

    def token(self, template: str | None = None) -> str:
        if template:  # a JWT template (e.g. ``lightbulb-appsync``), minted fresh, never cached
            return self._post(f"/v1/client/sessions/{self.sid}/tokens/{template}")["jwt"]
        if time.monotonic() - self._at > 40:
            self._jwt, self._at = self._post(f"/v1/client/sessions/{self.sid}/tokens")["jwt"], time.monotonic()
        return self._jwt


def dev_only(url: str) -> None:
    """Refuse anything but the dev API host itself (no look-alike suffix, no userinfo trick)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != DEV_HOST or parts.username or parts.port:
        raise SystemExit(f"refusing to run against {url}: dev only")


class Api:
    def __init__(self, session: Session):
        dev_only(API)
        self.s = session
        self.http = httpx.Client(base_url=API, headers=UA, timeout=30)

    def __call__(self, method: str, path: str, **kw) -> dict:
        resp = self.http.request(method, path, headers={"Authorization": f"Bearer {self.s.token()}"}, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}


# ------------------------------------------------------------------ pure helpers (unit-tested)
_WORD = re.compile(r"[a-z0-9$%.]+")
_STOP = set(
    "a an the is are was were it its to of in on for and or but so i you we they he she this that with at by be "
    "as from our your my me us them do did does just can could would will".split()
)


def words(text: str) -> set[str]:
    return {w.strip(".") for w in _WORD.findall(text.lower()) if w.strip(".") and w.strip(".") not in _STOP}


def overlap(reply: str, other: str) -> float:
    """Share of ``other``'s content words the reply repeats: 1.0 = everything the human said is restated."""
    theirs = words(other)
    return len(theirs & words(reply)) / len(theirs) if theirs else 0.0


def render(text: str, names: dict[str, dict]) -> tuple[str, list[dict]]:
    """``{finance}`` -> the agent's name; ``@{finance}`` -> its @handle plus a structured mention."""
    mentions = []

    def sub(m):
        at, key = m.group(1), m.group(2).lower()
        member = names[key]
        if at:
            mentions.append({"kind": member["kind"], "id": member["id"]})
            return "@" + member["mentionHandle"]
        return member["displayName"].split()[0] if member["kind"] == "human" else member["displayName"]

    return re.sub(r"(@?)\{([a-z ]+)\}", sub, text), mentions


def expected_set(value) -> list[str]:
    return [] if value is None else sorted([value] if isinstance(value, str) else value)


def hard_checks(step: dict, replies: list[dict], said: list[str]) -> dict:
    """Who answered; whether a reply restates a human's answer; whether it uses supplied info."""
    exp = step.get("expect") or {}
    got = sorted({r["role"] for r in replies})
    checks = {"responder": got == expected_set(exp.get("responder"))}
    text = " ".join(r["text"] for r in replies)
    for i in exp.get("must_not_restate") or []:
        checks[f"no_restate_step{i}"] = not replies or overlap(text, said[i]) < 0.6
    for needle in exp.get("must_reference") or []:
        checks[f"references:{needle}"] = needle.lower() in text.lower()
    for needle in exp.get("must_not_say") or []:
        checks[f"avoids:{needle}"] = needle.lower() not in text.lower()
    return checks


def parse_judge(content: str) -> dict:
    match = re.search(r"\{.*\}", content or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    return {"pass": bool(data.get("pass")), "reason": str(data.get("reason") or content or "")[:400]}


def judge(transcript: list[str], step: dict, replies: list[dict], key: str) -> dict | None:
    rubric = (step.get("expect") or {}).get("judge")
    if not rubric:
        return None
    prompt = (
        "You grade AI teammates in a group chat with several humans. Conversation so far:\n"
        + "\n".join(transcript)
        + "\n\nAgent replies after that last line:\n"
        + ("\n".join(f"{r['name']}: {r['text']}" for r in replies) or "(no agent replied)")
        + f"\n\nExpected behaviour: {rubric}\n"
        'Does the agent behaviour meet the expectation? Answer only JSON: {"pass": true|false, "reason": "<one sentence>"}'
    )
    resp = httpx.post(
        "https://ai-gateway.vercel.sh/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": JUDGE_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0},
        timeout=60,
    )
    if resp.status_code != 200:
        return {"pass": False, "reason": f"judge error {resp.status_code}"}
    return parse_judge(resp.json()["choices"][0]["message"]["content"])


# ------------------------------------------------------------------ playing a scenario
def roster(api: Api) -> list[dict]:
    rooms = api("GET", "/messaging/rooms")["rooms"]
    general = next(r for r in rooms if (r.get("name") or "").lower() == "general")
    return [m for m in api("GET", f"/messaging/rooms/{general['roomId']}")["members"] if m["kind"] == "agent"]


def play(scn: dict, apis: dict[str, Api], ids: dict[str, str], gateway_key: str | None, keep: bool) -> dict:
    owner = scn["cast"][0]
    agents = {a["displayName"].lower(): a for a in roster(apis[owner])}
    missing = [k for k in scn["agents"] if k not in agents]
    if missing:
        raise SystemExit(f"workspace has no agent named {missing}; found {sorted(agents)}")
    members = [{"kind": "human", "id": ids[k], "role": "member", "listenMode": None} for k in scn["cast"][1:]]
    members += [
        {"kind": "agent", "id": agents[k]["id"], "role": "member", "listenMode": mode}
        for k, mode in scn["agents"].items()
    ]
    room = apis[owner](
        "POST",
        "/messaging/rooms",
        json={
            "clientRoomId": str(uuid.uuid4()),
            "name": f"mp-{scn['id']}-{datetime.now(timezone.utc):%m%d-%H%M%S}".lower()[:80],
            "kind": "channel",
            "members": members,
        },
    )
    room_id = room["roomId"]
    names = {m["displayName"].lower(): m for m in room["members"] if m["kind"] == "agent"}
    names |= {k: next(m for m in room["members"] if m["id"] == ids[k]) for k in scn["cast"]}
    role_of = {a["id"]: k for k, a in agents.items()}
    after_seq, posted, said, transcript, steps_out, trigger_of, to_judge = 0, [], [], [], [], {}, []
    for i, step in enumerate(scn["steps"]):
        text, mentions = render(step["say"], names)
        body = {
            "clientMessageId": str(uuid.uuid4()),
            "text": text,
            "mentions": mentions,
            "replyToMessageId": None,
            "threadRootMessageId": posted[step["thread"]]["messageId"] if "thread" in step else None,
        }
        sent = time.monotonic()  # a race's ``after`` counts from here, not from when the POST returns
        msg = apis[step["as"]]("POST", f"/messaging/rooms/{room_id}/messages", json=body)
        posted.append(msg)
        said.append(text)
        transcript.append(f"{names[step['as']]['displayName']}: {text}")
        nxt = scn["steps"][i + 1] if i + 1 < len(scn["steps"]) else {}
        window = nxt.get("after")  # a race: the next human posts this many seconds later, whatever happens
        expect_speak = bool(expected_set((step.get("expect") or {}).get("responder")))
        if window is not None:
            apis[nxt["as"]].s.token()  # mint the racer's JWT now, not inside the timed post
        replies, after_seq = collect(
            apis[owner],
            room_id,
            after_seq,
            role_of,
            trigger_of,
            window=window,
            expect_speak=expect_speak,
            settle=scn.get("settle", {}),
            start=sent,
        )
        # Replies that landed after the previous race window's last poll but before this post
        # belong to the previous step.
        late, replies = split_at(replies, msg["messageSeq"])
        if late and steps_out:
            prev = steps_out[-1]
            prev["replies"] += late
            if "checks" in prev:
                prev["checks"] = hard_checks(scn["steps"][i - 1], prev["replies"], said[:-1])
            transcript[-1:-1] = [f"{r['name']}: {r['text']}" for r in late]
        out = {"step": i, "as": step["as"], "text": text, "replies": replies}
        if step.get("expect"):
            out["checks"] = hard_checks(step, replies, said)
            out["judge"] = None
            to_judge.append((out, step, list(transcript)))
        transcript += [f"{r['name']}: {r['text']}" for r in replies]
        steps_out.append(out)
    # Judge only after every message is posted: a judge call between steps would delay a race post.
    for out, step, seen in to_judge if gateway_key else []:
        out["judge"] = judge(seen, step, out["replies"], gateway_key)
    if not keep:
        try:
            apis[owner]("PATCH", f"/messaging/rooms/{room_id}", json={"archived": True})
        except RuntimeError as exc:
            print(f"  (could not archive {room_id}: {exc})", file=sys.stderr)
    return {"id": scn["id"], "condition": scn.get("condition"), "roomId": room_id, "steps": steps_out}


def collect(
    api: Api,
    room_id: str,
    after_seq: int,
    role_of: dict,
    trigger_of: dict,
    *,
    window,
    expect_speak,
    settle,
    start: float | None = None,
) -> tuple[list[dict], int]:
    """Agent messages after ``after_seq`` until the room settles (no active run, quiet), a race window
    ends, or the step times out. Records each active run's trigger so a late reply can be attributed."""
    quiet_s = settle.get("quiet", 20)
    timeout = (
        window
        if window is not None
        else settle.get("speak_timeout", 240)
        if expect_speak
        else settle.get("silence", 90)
    )
    start = time.monotonic() if start is None else start
    last_change = time.monotonic()
    replies, cost = [], 0.0

    def left():
        return timeout - (time.monotonic() - start)

    while True:
        # Never past the deadline: a race's next post must land on time, so no poll starts unless the
        # last one's duration still fits, and each request is capped to the time left.
        if left() <= cost:
            time.sleep(max(0, left()))
            break
        polled = time.monotonic()
        try:
            detail = api("GET", f"/messaging/rooms/{room_id}", timeout=min(30, left()))
            page = api(
                "GET",
                f"/messaging/rooms/{room_id}/messages",
                params={"afterSeq": after_seq, "limit": 100},
                timeout=max(0.1, min(30, left())),
            )
        except httpx.TimeoutException:
            break
        cost = time.monotonic() - polled
        for run in detail.get("activeRuns") or []:
            trigger_of[run["runId"]] = run.get("triggerMessageId")
        for m in page["messages"]:
            after_seq = max(after_seq, m["messageSeq"])
            last_change = time.monotonic()
            if m["author"]["kind"] == "agent":
                replies.append(
                    {
                        "name": m["author"]["displayName"],
                        "role": role_of.get(m["author"]["id"], m["author"]["id"]),
                        "text": m["text"],
                        "seq": m["messageSeq"],
                        "runId": m.get("runId"),
                        "trigger": trigger_of.get(m.get("runId") or ""),
                        "secondsAfter": round(time.monotonic() - start, 1),
                    }
                )
        idle = not detail.get("activeRuns") and time.monotonic() - last_change > quiet_s
        # Settled once a reply landed and the room went quiet. A silent step waits out the whole
        # silence window: a cold runtime's first reply can take a minute to start.
        if window is None and idle and replies:
            break
        time.sleep(max(0, min(3, left() - cost)))
    return replies, after_seq


def split_at(replies: list[dict], seq: int) -> tuple[list[dict], list[dict]]:
    """(replies before message ``seq``, replies after it)."""
    return [r for r in replies if r["seq"] < seq], [r for r in replies if r["seq"] > seq]


# ------------------------------------------------------------------ scoring summary
def summarize(results: list[dict]) -> str:
    out = ["| scenario | step | expected | got | checks | judge |", "|---|---|---|---|---|---|"]
    for res in results:
        for s in res["steps"]:
            if "checks" not in s:
                continue
            bad = [k for k, v in s["checks"].items() if not v]
            j = s.get("judge")
            out.append(
                f"| {res['id']} | {s['step']} | {s.get('expected', '')} | {', '.join(sorted({r['role'] for r in s['replies']})) or 'none'} "
                f"| {'ok' if not bad else 'FAIL ' + ', '.join(bad)} | {'–' if j is None else ('pass' if j['pass'] else 'FAIL') + ': ' + j['reason']} |"
            )
    return "\n".join(out)


def annotate(res: dict, scn: dict) -> dict:
    """Copy each step's expected responder and goal status onto its result row."""
    for s, step in zip(res["steps"], scn["steps"]):
        exp = step.get("expect") or {}
        s["expected"] = ", ".join(expected_set(exp.get("responder"))) or "none"
        s["goal"] = exp.get("goal")
    return res


def load_scenarios(paths: list[str], only: str) -> list[dict]:
    scns = []
    for p in paths:
        doc = yaml.safe_load(Path(p).read_text())
        for scn in doc if isinstance(doc, list) else [doc]:
            if re.search(only, scn["id"]):
                scns.append(scn)
    return scns


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    run = sub.add_parser("run")
    run.add_argument("files", nargs="+")
    run.add_argument("--only", default="")
    run.add_argument("--keep", action="store_true", help="leave the rooms unarchived")
    run.add_argument("--no-judge", action="store_true")
    args = ap.parse_args(argv)
    dev_only(API)  # before any Clerk session is minted
    clerk = Clerk()
    ids = {key: clerk.ensure_member(key) for key in CAST}
    if args.cmd == "setup":
        print(f"test humans in {ORG}: {sorted(ids)}")
        return 0
    scns = load_scenarios(args.files, args.only)
    cast = sorted({k for s in scns for k in s["cast"]})
    apis = {k: Api(clerk.session(ids[k])) for k in cast}
    key = None if args.no_judge else _secret("lightbulb/dev/vercel-ai-gateway")
    results = []
    for scn in scns:
        print(f"== {scn['id']}", file=sys.stderr)
        res = play(scn, apis, ids, key, args.keep)
        results.append(annotate(res, scn))
    outdir = HERE / "results"
    outdir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (outdir / f"{stamp}.json").write_text(json.dumps(results, indent=2))
    print(summarize(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
