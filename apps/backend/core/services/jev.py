"""Jev (TypeSafe AI's decision model) over the Vercel AI Gateway, and the Listening routing decision.

Spec: docs/superpowers/specs/2026-09-22-jev-routing-proactive.md §3a/§5/§5a. Jev picks at most one
Listening (``room_event``) responder per message and where it replies; ``openclaw_work`` enforces it.
No flag: a missing ``lightbulb/{env}/vercel-ai-gateway`` secret disables it (dispatch falls back).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import boto3
import botocore.exceptions
import httpx
import yaml
from botocore.config import Config

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services import hermes_profiles

logger = logging.getLogger(__name__)

MODEL = "typesafe-ai/jev"
_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
_TIMEOUT_S = 3.0
#: A failed key read retries quickly: one key-less task in a fleet splits routing decisions.
_KEY_RETRY_S = 30
_key_lock = asyncio.Lock()
_KEY_TTL_S = 3600
_CARD_TTL_S = 600
#: Whole decision budget: cards (S3 + Paperclip) plus the 3 s Jev call, so it can never stall dispatch.
_DECIDE_BUDGET_S = 8.0
#: A claim loser polls for the winner's row this often, for at most this long, then falls back.
#: ponytail: a DynamoDB poll, <=32 strongly consistent reads per loser per message; a push
#: (stream/notify) only if Listening fan-out grows past a handful of candidates.
_LOSER_POLL_S = 0.25
#: Longer than the winner can take (its budget plus the un-timed decision write), so a slow
#: winner never lets a loser fall back to dispatch-all beside the winner: two responders.
_LOSER_WAIT_S = _DECIDE_BUDGET_S + 3.0
#: What Jev sees: the last 30 room messages (1,000 chars each), trimmed oldest-first until the
#: whole state fits ~24k chars -- generous headroom under Jev's 32k-token state limit.
_RECENT_LIMIT = 30
_STATE_MAX_CHARS = 24_000
#: ``decide()``'s answer to a gateway 429. A burst of posts throttles Jev; falling straight back
#: to one responder then runs an agent on every chit-chat line (dev 2026-09-23: 13 turns from a
#: 44-message paste). So a throttled message retries through the queue's own redelivery, and only
#: after this many retries (~1 min of backoff) takes the single-responder fallback.
RATE_LIMITED = "rate_limited"
RATE_LIMIT_RETRIES = 3
#: Seconds before retry n may re-claim: 10, 20, 30 -- ~1 min before the fallback.
_RATE_LIMIT_BACKOFF_S = 10
#: A throttled message holds its room+agent FIFO group while it backs off, so only a fresh one
#: retries: one queued behind it is already this old and falls back at once, bounding the stall.
_RATE_LIMIT_WINDOW_S = 90

#: Its own small pool and short, no-retry clients: ``wait_for`` cancels the await, not the thread,
#: so a slow S3/Secrets Manager read must never pin the default executor DynamoDB shares.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="jev")
_AWS_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1})
_s3_client = None

_key: str | None = None
_key_checked_at = 0.0
#: (owner, agent) -> (profile .revision, built_at, (head, soul, tools))
_cards: dict[tuple[str, str], tuple[str | None, float, tuple[str, str, str]]] = {}

#: The starter roster seeds no capabilities, and name+role alone left Jev near-tied (0.65); a
#: one-line description took it to 0.86-0.99. Keyed on the Paperclip ROLE code, not the name.
_ROLE_DESCRIPTIONS = {
    "ceo": "Leads the company, sets goals, delegates to the team",
    "cto": "Builds and fixes software and infrastructure",
    "engineer": "Builds and fixes software and infrastructure",
    "cfo": "Spend, budgets, invoices, revenue",
    "cmo": "Marketing, launches, campaigns, newsletters",
    "researcher": "Research, metrics, dashboards, data analysis",
    "general": "Customer support, onboarding, operations",
}


async def _off(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, fn, *args)


def _read_secret(name: str) -> str | None:
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION, config=_AWS_CONFIG)
    return (sm.get_secret_value(SecretId=name).get("SecretString") or "").strip() or None


def _read(key: str) -> bytes | None:
    """One owner-state object on jev's bounded client; ``None`` means absent."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", config=_AWS_CONFIG)
    try:
        return _s3_client.get_object(Bucket=settings.OWNER_STATE_BUCKET, Key=key)["Body"].read()
    except botocore.exceptions.ClientError as exc:
        if (exc.response or {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


async def _gateway_key() -> str | None:
    """The gateway key, re-read hourly (rotation needs no restart); a failed/missing read is
    retried after 5 min."""
    global _key, _key_checked_at
    # One read at a time: without the lock a second concurrent caller saw the timestamp the
    # first had just stamped, no key yet, and reported Jev "disabled" (dev 2026-09-23).
    async with _key_lock:
        if _key_checked_at and time.monotonic() - _key_checked_at < (_KEY_TTL_S if _key else _KEY_RETRY_S):
            return _key
        name = f"lightbulb/{settings.ENVIRONMENT or 'dev'}/vercel-ai-gateway"
        try:
            _key = await _off(_read_secret, name)
        except Exception as exc:  # noqa: BLE001 -- missing secret == feature off
            logger.warning("Jev key unreadable: %s (%s)", name, type(exc).__name__)
            _key = None
        _key_checked_at = time.monotonic()
        return _key


async def decide(state: dict, questions: dict) -> dict | str | None:
    """One Jev call. Returns the ``answers`` dict, ``RATE_LIMITED`` on a 429, or ``None`` on any
    other failure (never raises)."""
    key = await _gateway_key()
    if not key:
        put_metric("messaging.jev.decision", dimensions={"outcome": "disabled"})
        return None
    started = time.perf_counter()
    outcome = "error"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(
                _URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": MODEL,
                    "state": state,
                    "questions": questions,
                    "providerOptions": {"gateway": {"zeroDataRetention": True, "only": ["typesafe-ai"]}},
                },
            )
        if response.status_code == 429:
            outcome = "rate_limited"
            return RATE_LIMITED
        if response.status_code != 200:
            try:
                error = response.json().get("error") or {}
            except Exception:
                error = {}
            err_type = error.get("type") if isinstance(error, dict) else None
            err_message = str(error.get("message") or "")[:120] if isinstance(error, dict) else ""
            logger.warning("Jev returned %s type=%s message=%s", response.status_code, err_type, err_message)
            if response.status_code in (401, 403):
                outcome = "forbidden"
            return None
        payload = response.json()
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            return None
        outcome = "ok"
        cost = ((payload.get("providerMetadata") or {}).get("gateway") or {}).get("cost")
        if cost is not None:
            put_metric("messaging.jev.cost", value=float(cost), unit="None")
        tokens = (payload.get("usage") or {}).get("inputTokens")
        if tokens is not None:
            put_metric("messaging.jev.input_tokens", value=float(tokens))
        return answers
    except httpx.TimeoutException:
        outcome = "timeout"
        return None
    except Exception as exc:  # noqa: BLE001 -- a decision failure must never reach dispatch
        logger.warning("Jev call failed: %s", type(exc).__name__)
        return None
    finally:
        put_metric("messaging.jev.decision", dimensions={"outcome": outcome})
        put_metric("messaging.jev.latency", value=(time.perf_counter() - started) * 1000, unit="Milliseconds")


# --------------------------------------------------------------------------- agent cards
#
# The HERMES PROFILE is the source of truth for what an agent does (owner ruling 2026-09-22):
# SOUL.md for persona + "## What you do", config.yaml / lightbulb-facts.json for tool NAMES.
# Paperclip supplies identity (name, role code) only. Names are user-editable, so a card that
# is just a name is not enough to route on.

_CARD_MAX = 600  # ~150 tokens


def _clip(text: str, limit: int) -> str:
    """Truncate at a sentence boundary when one exists inside ``limit``."""
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > 0 else cut.rstrip() + "…"


def _plain_md(text: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"[#*_`>]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _soul_parts(text: str) -> tuple[str, str]:
    """``(persona line, "What you do" section)`` from SOUL.md, markdown stripped."""
    body = hermes_profiles.strip_generated_marker(text)
    sections = re.split(r"^##\s+", body, flags=re.M)
    persona = ""
    for line in sections[0].splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("<!--") and not re.fullmatch(r"\*\*.*\*\*", line):
            persona = _plain_md(line)
            break
    what = next((s.split("\n", 1)[1] for s in sections[1:] if s.lower().startswith("what you do") and "\n" in s), "")
    return persona, _plain_md(what)


async def _paperclip_agent(agent_id: str) -> dict:
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie

    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=_TIMEOUT_S) as client:
        cookie = await get_admin_session_cookie(client)
        return await PaperclipAdminClient(http_client=client).get_agent(session_cookie=cookie, agent_id=agent_id)


def _tool_names(config: dict, facts: dict) -> list[str]:
    """NAMES only: mcp_servers keys, listed skills, connected integrations. Never a value."""
    names = set()
    servers = config.get("mcp_servers")
    if isinstance(servers, dict):
        names.update(str(k) for k in servers)
    skills = config.get("skills")
    listed = skills if isinstance(skills, list) else skills.get("enabled") if isinstance(skills, dict) else None
    if isinstance(listed, list):
        names.update(str(k) for k in listed if isinstance(k, str))
    if facts.get("company_stripe") == "connected":
        names.add("stripe")
    for row in facts.get("provisioning") or []:
        if isinstance(row, dict) and row.get("state") == "active" and row.get("provider"):
            names.add(str(row["provider"]))
    return sorted(names)


async def _soft(label: str, awaitable, parse=lambda raw: raw) -> Any:
    """One optional card source: ``parse(await awaitable)``, or ``None`` on absence or ANY failure."""
    try:
        raw = await awaitable
        return parse(raw) if raw else None
    except Exception as exc:  # noqa: BLE001 -- a card degrades, never raises
        logger.info("Jev card: %s unavailable (%s)", label, type(exc).__name__)
        return None


async def _card_parts(owner_id: str, agent_id: str, display_name: str) -> tuple[str, str, str]:
    """``(head, soul text, tools)`` -- every source optional; a failure degrades, never raises."""
    prefix = hermes_profiles.profile_prefix(owner_id, agent_id)
    agent = await _soft("paperclip agent", _paperclip_agent(agent_id)) or {}
    soul = _off(_read, f"{prefix}SOUL.md")
    persona, what = await _soft("SOUL.md", soul, lambda raw: _soul_parts(raw.decode())) or ("", "")
    # config.yaml only -- read_profile_config never opens .env (the secrets file).
    config = await _soft("config.yaml", _off(_read, f"{prefix}config.yaml"), yaml.safe_load) or {}
    facts = await _soft("facts", _off(_read, f"{prefix}{hermes_profiles.FACTS_FILENAME}"), json.loads) or {}
    role = agent.get("role")
    head = f"{agent.get('name') or display_name} (role: {role})." if role else f"{agent.get('name') or display_name}."
    # The role-code line only stands in when SOUL.md has no "What you do" section.
    does = what or _ROLE_DESCRIPTIONS.get(role or "", "")
    soul = " ".join(p.rstrip(".") + "." for p in (persona, does) if p)
    tools = f"Tools: {', '.join(_tool_names(config, facts if isinstance(facts, dict) else {})) or 'none'}."
    return head, soul, tools


def _assemble(parts: tuple[str, str, str], recency: str = "") -> str:
    """``<Name> (role: r). <persona>. <what you do>. Tools: a, b. Last spoke 2h ago.`` <= 600 chars;
    the SOUL text is what gets truncated."""
    head, soul, tools = parts
    fixed = " ".join(p for p in (head, tools, recency) if p)
    soul = _clip(soul, _CARD_MAX - len(fixed) - 1)
    return " ".join(p for p in (head, soul, tools, recency) if p)[:_CARD_MAX]


async def agent_card(owner_id: str, agent_id: str, display_name: str, recency: str = "") -> str:
    """Cached per (owner, agent), keyed on the profile's ``.revision`` (a SOUL edit, a new
    connection or a facts write bumps it), with a 10-minute backstop for Paperclip-only edits."""
    revision_key = f"{hermes_profiles.profile_prefix(owner_id, agent_id)}.revision"
    revision = await _soft(".revision", _off(_read, revision_key), bytes.decode)
    cached = _cards.get((owner_id, agent_id))
    if cached and revision is not None and cached[0] == revision and time.monotonic() - cached[1] < _CARD_TTL_S:
        return _assemble(cached[2], recency)
    parts = await _card_parts(owner_id, agent_id, display_name)
    _cards[(owner_id, agent_id)] = (revision, time.monotonic(), parts)
    return _assemble(parts, recency)


# --------------------------------------------------------------------------- routing decision


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return ""
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 60:
        return f"Last spoke {minutes}m ago."
    return f"Last spoke {minutes // 60}h ago."


def _line(message: dict) -> dict:
    author = message.get("author") or {}
    return {
        "author": f"{author.get('displayName') or author.get('id')} ({author.get('kind')})",
        "text": str(message.get("text") or "")[:1000]
        or " ".join(
            "[image]"
            if str(a.get("contentType", "")).startswith("image/")
            else "[video]"
            if str(a.get("contentType", "")).startswith("video/")
            else f"[file: {a.get('fileName') or 'attachment'}]"
            for a in message.get("attachments") or []
        ),
    }


#: The routing bar (owner ruling 2026-09-24): "would a good employee in this room naturally
#: respond?" -- employee judgement, not a checklist; the examples are examples. Framed as a
#: colleague because Jev read the old "should act on" as "only when asked" and skipped a shared
#: link (prod 21:38Z); a thin agent card (name + role) made that worse, so the bar must carry it.
#: Who the message SPEAKS TO comes before role fit (eval 2026-09-27): the old closing line,
#: "judge by role and tools, not by name", sent "Hey Forge, can you write three taglines" to the
#: marketer (0.83), a thread follow-up to whoever fit the topic, and "Leo, can you review ..." to
#: an agent. 13 of 30 such cases missed; this wording misses 4.
WHO_INSTRUCTIONS = (
    "These AI teammates are listening in on a conversation among colleagues. Decide who, if "
    "anyone, answers the latest message. First see who it is speaking to. When it addresses one "
    "teammate by name ('Hey Friday, ...', 'Forge can you ...', 'I think Pulse should take this'), "
    "that teammate answers, whatever their role: the person chose them. When it continues an "
    "exchange a teammate is already part of (a follow-up to what that teammate just said or is "
    "working on, in the thread or in the main conversation), that same teammate answers. When it "
    "is addressed by name to a person, or to a teammate who is not one of the options, nobody "
    "answers. Only when it speaks to nobody in particular, ask: "
    "would a good, helpful employee in this room naturally respond to the latest message? "
    "A good teammate who sees a link shared with 'we should study this' reads it and posts the "
    "takeaways; when a question lands in their area, they answer it; when they are mentioned "
    "indirectly, they chime in; when they have context for a decision or a commitment they can "
    "track, they speak up. An open question or request put to the room ('guys, what's going on "
    "here?', 'what about the tasks on the board?') that nobody has answered yet is addressed to "
    "whoever is listening: a good teammate answers it, above all when they are the only teammate "
    "listening in the room. They stay out "
    "of personal back-and-forth, thanks and acknowledgements, and they don't pile on after a "
    "colleague has already answered. Pick at most ONE: the teammate spoken to, else the teammate "
    "whose role and tools fit best, or nobody."
)
NONE_CRITERION = (
    "Nobody: a good employee would stay quiet here -- chatter between people ('lol', 'thanks', "
    "'brb'), an acknowledgement or thanks (even by name), a personal matter between specific "
    "people, a message addressed by name to a person or to a teammate who is not an option here, "
    "something a colleague has just answered when it is repeated straight away (a question asked "
    "again after a gap or after other talk is a new request: answer it), or a reply that "
    "would only prolong agent-to-agent back-and-forth. Never an open question or request put to "
    "the room"
)
#: Jev's "none" stands. A rule that overruled a "none" not 0.4 clear of the top agent (added
#: 2026-09-25, when Jev went silent on a question put to the room) fired 7 times in 85 prod
#: decisions over 2026-09-27/28 and never helped -- every reply it forced was to chatter or to a
#: message for a person ("No action needed from me here", at none 0.66). The room-question case it
#: covered is now in the instructions and routes directly (eval: 0.86).


#: Messages where silence is the default: a human's message that @mentions only people, and any
#: agent's message. An agent speaks there only on a confident pick (prod 2026-09-27: Leo's
#: "@prasiddha" drew Friday via the coin-flip rule, none 0.56 vs 0.44, to say she was staying out;
#: Friday's own reply drew Forge at 0.51, who repeated it). A 0.9 "Finance has the answer" still speaks.
CONFIDENT_P = 0.7


def addressed_to_people(message: dict, members: list[dict]) -> bool:
    """A human's message that OPENS with a mentioned person's @handle and names no agent:
    "@prasiddha ...", "@aden @leo". A mention in passing ("Friday, ... update from @aden") is not
    one, nor is an opening handle that resolves to nobody ("@ghost can anyone help? cc @aden").
    ``members`` must carry the humans' ``mentionHandle`` (``messaging.with_human_handles``)."""
    mentions = message.get("mentions") or []
    if (message.get("author") or {}).get("kind") != "human" or any(r.get("kind") == "agent" for r in mentions):
        return False
    opening = re.match(r"\s*@([A-Za-z0-9_-]+)", str(message.get("text") or ""))
    if not opening:
        return False
    people = {r.get("id") for r in mentions if r.get("kind") == "human"}
    handle = opening.group(1).lower()
    return any(
        m.get("kind") == "human" and m.get("id") in people and str(m.get("mentionHandle") or "").lower() == handle
        for m in members
    )


#: Placement on the same bar (owner ruling 2026-09-24): judgement from the conversation, not rules.
#: Only a fallback (Jev unavailable) uses the fixed default: reply where the message is.
PLACEMENT_INSTRUCTIONS = (
    "Where would a good employee put their reply to the latest message? Judge it from the "
    "conversation: whether the latest message is in a thread and how active that thread is, who "
    "is talking, and how broadly the answer matters. They keep the main conversation readable: a "
    "quick answer in a side discussion, a narrow question or work that needs back-and-forth goes "
    "in a thread; a new topic, or an answer or decision the whole room should see, goes in the "
    "main conversation, even when it was asked in a thread. In a one-to-one DM most replies "
    "belong in the main conversation."
)
PLACEMENT_CRITERIA = {
    "main": "Main conversation: the reply matters to the whole room (a new topic, a broadly relevant "
    "answer or decision), even if the latest message is in a thread",
    "thread": "Thread: the reply belongs to a narrower exchange (continuing a thread, a side question "
    "or side discussion, work that needs back-and-forth), even if the latest message is top level",
}


async def _decide_for_message(
    meta: dict, message: dict, latest: dict, members: list[dict], history: list[dict]
) -> dict:
    candidates = [
        target["agentId"]
        for target in message.get("dispatchTargets") or []
        if target.get("eventKind") == "room_event" and target.get("agentId")
    ]
    names = {m["id"]: m.get("displayName") or m["id"] for m in members if m.get("kind") == "agent"}
    now = _parse_ts(latest.get("createdAt")) or datetime.now(timezone.utc)

    def recency(agent_id: str) -> str:
        spoke = [_parse_ts(m.get("createdAt")) for m in history if (m.get("author") or {}).get("id") == agent_id]
        return _ago(spoke[-1] if spoke else None, now)

    cards = await asyncio.gather(*(agent_card(meta["ownerId"], a, names.get(a, a), recency(a)) for a in candidates))
    criteria = dict(zip(candidates, cards))
    criteria["none"] = NONE_CRITERION
    state = _state(meta, latest, history)
    to_people = addressed_to_people(message, members)
    if to_people:
        people = {m["id"]: m.get("displayName") or m["id"] for m in members if m.get("kind") == "human"}
        state["latest"]["addressedTo"] = [
            people.get(ref["id"], ref["id"]) for ref in message.get("mentions") or [] if ref.get("kind") == "human"
        ]
    questions = {
        "who": {"type": "choice", "instructions": WHO_INSTRUCTIONS, "criteria": criteria},
        "placement": {"type": "choice", "instructions": PLACEMENT_INSTRUCTIONS, "criteria": PLACEMENT_CRITERIA},
    }
    # Store only what dispatch reads: who, its probability, and placement.
    answers = await decide(state, questions)
    if answers == RATE_LIMITED:
        return {"rateLimited": True}
    who, placement = (answers or {}).get("who") or {}, (answers or {}).get("placement") or {}
    if answers is None:
        return {"fallback": True}
    if who.get("choice") not in (*candidates, "none") or placement.get("choice") not in ("main", "thread"):
        return {"fallback": True, "reason": "invalid_choice"}
    probabilities = who.get("probabilities") or {}
    human = (latest.get("author") or {}).get("kind") == "human"
    final = who["choice"]
    if not human or to_people:
        # Spoken to a person, or an agent's message: silence unless the pick is confident.
        if final != "none" and float(probabilities.get(final) or 0) < CONFIDENT_P:
            final = "none"
            put_metric("messaging.jev.unconfident_quiet")
    return {"who": final, "whoP": probabilities, "placement": placement["choice"]}


def _state(meta: dict, latest: dict, history: list[dict]) -> dict:
    """Room, the trigger's thread (root + replies) ahead of the last 30 messages, and the trigger.
    Over budget, the oldest general message goes first and the thread only once that is empty."""
    root = latest.get("threadRootMessageId")
    thread = [m for m in history if root and root in (m.get("messageId"), m.get("threadRootMessageId"))]
    in_thread = {m.get("messageId") for m in thread}
    recent = [_line(m) for m in history[-_RECENT_LIMIT:] if m.get("messageId") not in in_thread]
    thread_lines = [_line(m) for m in thread]
    state: dict = {"room": {"kind": meta.get("kind"), "name": meta.get("name")}}
    if thread_lines:
        state["thread"] = thread_lines
    state["recent"] = recent
    state["latest"] = {**_line(latest), "inThread": bool(root)}
    while len(json.dumps(state, ensure_ascii=False)) > _STATE_MAX_CHARS and (recent or thread_lines):
        (recent or thread_lines).pop(0)
    return state


async def route(
    meta: dict, agent_id: str, message: dict, *, latest: dict, members: list[dict], history: list[dict]
) -> dict | None:
    """The final decision for this message, computed once across every candidate's consumer.
    ``None`` means routing itself errored. A ``{"retry": True, "attempts": n}`` row means Jev was
    rate-limited: every consumer retries the message, and the first back re-claims it. Never raises."""
    try:
        room_id, message_id = meta["roomId"], message["messageId"]
        prior = await messaging_repo.get_decision(room_id, message_id)
        if prior and not prior.get("pending") and (not prior.get("retry") or prior.get("retryAt", 0) > time.time()):
            # A final decision, or a rate-limit retry still backing off: nothing to compute yet.
            return prior
        # A pending row re-claimed from a retry carries the count too: a stale takeover keeps it.
        attempts = int(prior.get("attempts") or 0) if prior else 0
        if not await _gateway_key():
            # No key on THIS task: do no Jev/card work, but never act alone -- another backend
            # task may hold a key and route this message (dev 2026-09-23: a key-less task fell
            # back while another routed, so two agents answered). Follow a decision already
            # made, wait on one in flight, else store one shared fallback everyone follows.
            put_metric("messaging.jev.decision", dimensions={"outcome": "disabled"})
            stored = prior
            if stored is None or stored.get("retry"):
                # Only into an ABSENT (or still-retry) row: never overwrite a keyed task's live claim.
                stored = await messaging_repo.finalize_decision(
                    room_id,
                    message_id,
                    {"fallback": True, "reason": "disabled"},
                    only_if_absent=stored is None,
                    only_if_retry=stored is not None,
                )
            if not stored.get("pending"):
                return stored
        elif await messaging_repo.claim_decision(room_id, message_id, attempts=attempts):
            try:
                decision = await asyncio.wait_for(
                    _decide_for_message(meta, message, latest, members, history), _DECIDE_BUDGET_S
                )
            except Exception:
                # A stored fallback lets the losers stop waiting now, not at their ceiling.
                logger.warning("Jev decision failed", exc_info=True)
                decision = {"fallback": True, "reason": "error"}
            if decision.get("rateLimited"):
                sent = _parse_ts(message.get("createdAt"))
                fresh = sent is not None and (datetime.now(timezone.utc) - sent).total_seconds() < _RATE_LIMIT_WINDOW_S
                decision = (
                    {"fallback": True, "reason": "rate_limited"}
                    if attempts >= RATE_LIMIT_RETRIES or not fresh
                    else {
                        "retry": True,
                        "attempts": attempts + 1,
                        "retryAt": int(time.time()) + _RATE_LIMIT_BACKOFF_S * (attempts + 1),
                    }
                )
            # First final write wins: if a loser already timed out and stored a fallback,
            # follow it, so both sides dispatch the same single agent.
            return await messaging_repo.finalize_decision(room_id, message_id, decision)
        # Another candidate's consumer holds the claim: never call Jev twice for one message.
        deadline = time.monotonic() + _LOSER_WAIT_S
        while True:
            decision = await messaging_repo.get_decision(room_id, message_id)
            if decision and not decision.get("pending"):
                return decision
            if time.monotonic() >= deadline:
                return await messaging_repo.finalize_decision(
                    room_id, message_id, {"fallback": True, "reason": "loser_timeout"}
                )
            await asyncio.sleep(_LOSER_POLL_S)
    except Exception:  # noqa: BLE001 -- routing must never break dispatch
        logger.warning("Jev routing failed", exc_info=True)
        return None


async def pick_agent(owner_id: str, *, room_name: str, team: list[dict], text: str, recent: str) -> str | None:
    """One agent from `team` for a spoken request, or None when unsure/unavailable. Never raises."""
    try:
        cards = await asyncio.gather(*(agent_card(owner_id, m["id"], m["name"]) for m in team))
        criteria = {m["id"]: card for m, card in zip(team, cards)}
        state = {
            "room": {"kind": "channel", "name": room_name},
            "recent": [{"author": "transcript (call)", "text": _clip(recent, 1500)}] if recent else [],
            "latest": {"author": "caller (human)", "text": _clip(text, 1000), "inThread": False},
        }
        answers = await decide(
            state, {"who": {"type": "choice", "instructions": WHO_INSTRUCTIONS, "criteria": criteria}}
        )
        if not isinstance(answers, dict):
            return None
        who = answers.get("who") or {}
        choice, probs = who.get("choice"), who.get("probabilities") or {}
        return choice if choice in criteria and float(probs.get(choice, 0)) >= CONFIDENT_P else None
    except Exception:  # noqa: BLE001 -- routing must never fail a turn
        logger.warning("jev pick_agent failed", exc_info=True)
        return None
