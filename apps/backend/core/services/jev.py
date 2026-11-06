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


async def decide(
    state: dict, questions: dict, *, timeout_s: float = _TIMEOUT_S, metric_prefix: str = "messaging.jev"
) -> dict | str | None:
    """One Jev call. Returns the ``answers`` dict, ``RATE_LIMITED`` on a 429, or ``None`` on any
    other failure (never raises). The watchdog passes its own timeout and prefix, so P26 stays routing's."""
    key = await _gateway_key()
    if not key:
        put_metric(f"{metric_prefix}.decision", dimensions={"outcome": "disabled"})
        return None
    started = time.perf_counter()
    outcome = "error"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
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
            put_metric(f"{metric_prefix}.cost", value=float(cost), unit="None")
        tokens = (payload.get("usage") or {}).get("inputTokens")
        if tokens is not None:
            put_metric(f"{metric_prefix}.input_tokens", value=float(tokens))
        return answers
    except httpx.TimeoutException:
        outcome = "timeout"
        return None
    except asyncio.CancelledError:
        outcome = "cancelled"  # the caller gave up (a hedged twin answered first): not a Jev failure
        raise
    except Exception as exc:  # noqa: BLE001 -- a decision failure must never reach dispatch
        logger.warning("Jev call failed: %s", type(exc).__name__)
        return None
    finally:
        put_metric(f"{metric_prefix}.decision", dimensions={"outcome": outcome})
        put_metric(f"{metric_prefix}.latency", value=(time.perf_counter() - started) * 1000, unit="Milliseconds")


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


def _name(author: dict) -> str:
    return str(author.get("displayName") or author.get("id"))


def _to(message: dict, names: dict[str, str], by_id: dict[str, dict]) -> list[str]:
    """Who a line was said to (routing v2 §5). A human line: the agents it addressed (@mention or
    name) and the people it @mentioned or spoke to. An agent line: the author of the message it
    answers (``inReplyTo``), else of its thread root. A Live row carries its own ``to``."""
    if message.get("to") is not None:
        return list(message["to"])
    author = message.get("author") or {}
    if author.get("kind") == "agent":
        for ref in (message.get("inReplyTo"), message.get("threadRootMessageId")):
            row = by_id.get(ref) if ref else None
            if row and (row.get("author") or {}).get("id") != author.get("id"):
                return [_name(row.get("author") or {})]
        return []
    ids = message.get("addressedAgentIds") or [
        t["agentId"] for t in message.get("dispatchTargets") or [] if t.get("eventKind") == "user_request"
    ]
    ids = [*ids, *(r["id"] for r in message.get("mentions") or [] if r.get("kind") == "human")]
    addressing = message.get("addressing") or {}
    ids += addressing.get("people") or []
    # The addressing stage heard something asked of the room beyond the named ("..., and Sam ...").
    return list(dict.fromkeys([*(names.get(i, i) for i in ids), *(["everyone"] if addressing.get("anyone") else [])]))


def _line(message: dict, names: dict[str, str] | None = None, by_id: dict | None = None, options=()) -> dict:
    """``{author, kind, to, self, text}``; ``to``/``self`` only when set. ``self``: one of the
    teammates being chosen among wrote it."""
    author = message.get("author") or {}
    line = {"author": _name(author), "kind": author.get("kind")}
    if to := _to(message, names or {}, by_id or {}):
        line["to"] = to
    if author.get("id") in options:
        line["self"] = True
    line["text"] = messaging_repo.agent_text(str(message.get("text") or ""))[:1000] or " ".join(
        "[image]"
        if str(a.get("contentType", "")).startswith("image/")
        else "[video]"
        if str(a.get("contentType", "")).startswith("video/")
        else f"[file: {a.get('fileName') or 'attachment'}]"
        for a in message.get("attachments") or []
    )
    return line


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
    "colleague has already answered. A teammate already working on a request owns its "
    "follow-ups; don't add a second responder to it. Pick at most ONE: the teammate spoken to, "
    "else the teammate whose role and tools fit best, or nobody."
)
NONE_CRITERION = (
    "Nobody: a good employee would stay quiet here -- chatter between people ('lol', 'thanks', "
    "'brb'), an acknowledgement or thanks (even by name), a personal matter between specific "
    "people, a message addressed by name to a person or to a teammate who is not an option here, "
    "something a colleague has just answered when it is repeated straight away (a question asked "
    "again after a gap or after other talk is a new request: answer it), unless a teammate knows "
    "that answer is wrong, or a reply that would only prolong agent-to-agent back-and-forth. A "
    "question only partly answered is still open for the rest. Never an open question or request "
    "put to the room"
)
#: Two structured answers beside ``who`` (routing v2 §12.8/§12.9). Policy, not hope: when either is
#: high on a human's message to nobody, Jev's "none" is not allowed and the fallback chain picks.
REASK_INSTRUCTIONS = (
    "Is the latest message asking again something that was already asked, and answered or "
    "attempted, earlier in this conversation, after other talk or a gap?"
)
QUESTION_INSTRUCTIONS = (
    "Is the latest message a finished question or request about the work (a fact, figure, file, "
    "status, task or decision) put to the room as a whole rather than to a particular person, that "
    "nobody in the conversation has answered yet? Lines under 'after' were posted since it: a person "
    "answering it there means it has been answered; the asker adding to it, or unrelated talk, does "
    "not. No for an unfinished or trailing-off message, small talk, an invitation, a personal "
    "matter, or something only a particular person can answer."
)
#: A re-ask forces an answer only when the earlier ask is at least this old: an immediate repeat
#: of a question a person just answered stays quiet (bench D16), a re-ask after talk does not (D03).
REASK_AFTER_S = 120
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


def speaks_to_people(message: dict, members: list[dict]) -> bool:
    """The to-people silence rule for a chat message: it opens with a person's @handle, or the
    addressing stage heard it speak to a person -- unless the stage heard something asked of the
    room beyond them ("what's our MRR, and Sam where's the deck?")."""
    addressing = message.get("addressing") or {}
    if addressing.get("anyone"):
        return False
    return bool(addressing.get("people")) or addressed_to_people(message, members)


def _reask_age(latest: dict, history: list[dict], now: datetime) -> float | None:
    """Seconds since the earlier human message the latest one most repeats (2+ shared words of 4+
    letters; the newest wins a tie), or None. ponytail: word overlap, not meaning; a paraphrased
    re-ask with no shared words is not forced (Jev's own pick still stands)."""
    words = lambda m: set(re.findall(r"[a-z0-9']{4,}", str(m.get("text") or "").lower()))  # noqa: E731
    said = words(latest)
    best = None
    for row in history:
        if (row.get("author") or {}).get("kind") == "human" and len(said & words(row)) >= 2:
            if best is None or len(said & words(row)) >= len(said & words(best)):
                best = row
    when = _parse_ts(best.get("createdAt")) if best else None
    return (now - when).total_seconds() if when else None


#: A Google Meet's one line of context. The worker labels a speaker "Someone" when Meet does not
#: say who, and "Priya + Dan" when two talk over each other: neither is a new person.
MEETING_CONTEXT = (
    "This is a Google Meet with several people; agents speak only when asked. A speaker shown as "
    "'Someone' is a participant we could not identify, and 'A + B' is A and B talking at once."
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
    meta: dict,
    message: dict,
    latest: dict,
    members: list[dict],
    history: list[dict],
    to_people: bool | None = None,
    *,
    placement: bool = True,
    open_lines: list[str] | None = None,
    later: list[dict] | None = None,
    setting: str = "live",
    rules: list[dict] | tuple = (),
) -> dict:
    """``to_people``: the caller already knows whether the message speaks to people (Live hears names,
    not @handles); None reads it from the message (``speaks_to_people``). ``placement=False`` (Live) skips
    that question. ``open_lines``: the requests teammates are already working on (``state.open``).
    ``later``: messages posted after the trigger before the decision (``state.after``), so the
    question rule sees whether someone has answered it since. ``setting="meeting"``: a Google Meet
    (Live's voice worker), told to Jev in one line.
    ``rules``: in-scope proactive rule rows (contracts §18); they ride this call as one ``rule`` question and the
    decision gains ``rule``/``ruleP``, parsed independently of ``who``. Empty leaves the request unchanged.
    A ``forced`` reason on a "none" means the policy did not allow it: the caller's fallback picks."""
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
    if to_people is None:
        to_people = speaks_to_people(message, members)
    # The trigger's stored routing fields (raw row) feed its `to`; ``latest`` is its public shape.
    latest = {**latest, **{k: message[k] for k in ("addressing", "to", "dispatchTargets") if k in message}}
    state = _state(meta, latest, history, members=members, options=candidates, open_lines=open_lines)
    if setting == "meeting":
        state["room"]["setting"] = MEETING_CONTEXT
    if later:
        names = {m["id"]: m.get("displayName") or m["id"] for m in members}
        state["after"] = [_line(m, names) for m in later[-10:]]
    if to_people:
        people = {m["id"]: m.get("displayName") or m["id"] for m in members if m.get("kind") == "human"}
        state["latest"]["addressedTo"] = [
            people.get(ref["id"], ref["id"]) for ref in message.get("mentions") or [] if ref.get("kind") == "human"
        ]
    questions = {
        "who": {"type": "choice", "instructions": WHO_INSTRUCTIONS, "criteria": criteria},
        "reask": {"type": "boolean", "instructions": REASK_INSTRUCTIONS},
        "question": {"type": "boolean", "instructions": QUESTION_INSTRUCTIONS},
    }
    if placement:
        questions["placement"] = {
            "type": "choice",
            "instructions": PLACEMENT_INSTRUCTIONS,
            "criteria": PLACEMENT_CRITERIA,
        }
    if rules:
        from core.services import rules as rules_mod  # rules imports jev._clip: import here, not at module top

        questions["rule"] = rules_mod.jev_question(list(rules))
    # Store only what dispatch reads: who, its probability, placement, and why a "none" was refused.
    answers = await decide(state, questions)
    # A rule match never depends on `who`, and Jev down is never a match (spec 12 §3.3).
    no_rule = {"rule": None} if rules else {}
    if answers == RATE_LIMITED:
        return {"rateLimited": True, **no_rule}
    if answers is None:
        return {"fallback": True, **no_rule}
    who, where = answers.get("who") or {}, answers.get("placement") or {}
    if rules:  # parsed on its own: a bad `who` neither drops nor invents a rule match
        no_rule["rule"], no_rule["ruleP"] = rules_mod.parse_match(
            answers, {r["scheduleId"] for r in rules[: rules_mod.RULES_PER_ROOM]}
        )
    if who.get("choice") not in (*candidates, "none") or (placement and where.get("choice") not in ("main", "thread")):
        return {"fallback": True, "reason": "invalid_choice", **no_rule}
    probabilities = who.get("probabilities") or {}
    human = (latest.get("author") or {}).get("kind") == "human"
    final = who["choice"]
    if not human or to_people:
        # Spoken to a person, or an agent's message: silence unless the pick is confident.
        if final != "none" and float(probabilities.get(final) or 0) < CONFIDENT_P:
            final = "none"
            put_metric("messaging.jev.unconfident_quiet")
    elif (
        final != "none"
        and "none" in probabilities
        and float(probabilities.get(final) or 0) <= float(probabilities["none"] or 0)
    ):
        # A pick no likelier than silence is not a pick (prod 2026-10-05: 0.5/0.5 on "Agents will be
        # in the call too" ran Friday for 3 minutes to answer [SILENT]). A question or re-ask still
        # gets its answer through ``forced`` below.
        final = "none"
        put_metric("messaging.jev.tie_quiet")
    decision = {"who": final, "whoP": probabilities, "placement": where.get("choice") if placement else None}
    decision.update(no_rule)
    if final == "none" and human and not to_people:

        def high(key: str) -> bool:
            try:
                return float((answers.get(key) or {}).get("probability") or 0) >= 0.5
            except (TypeError, ValueError):
                return False

        age = _reask_age(latest, history, now) if high("reask") else None
        if age is not None and age >= REASK_AFTER_S:
            decision["forced"] = "reask"
        elif high("question"):
            decision["forced"] = "question"
    return decision


def _open_lines(runs: list[dict], members: list[dict], history: list[dict], now: datetime) -> list[str]:
    """``state.open`` (<= 5): 'Finance is answering Alice's "what's our MRR?" (main, 40 s ago)'."""
    names = {m["id"]: m.get("displayName") or m["id"] for m in members}
    by_id = {m.get("messageId"): m for m in history}
    lines = []
    for run in sorted(runs, key=lambda r: str(r.get("dispatchedAt") or ""))[-5:]:
        trigger = by_id.get(run.get("triggerMessageId"))
        when = _parse_ts(run.get("dispatchedAt"))
        ago = f", {int((now - when).total_seconds())} s ago" if when else ""
        what = (
            f'{_name(trigger.get("author") or {})}\'s "{messaging_repo.agent_text(str(trigger.get("text") or ""))[:120]}"'
            if trigger
            else "an earlier request"
        )
        where = "thread" if run.get("threadRootId") else "main"
        lines.append(f"{names.get(run.get('agentId'), run.get('agentId'))} is answering {what} ({where}{ago})")
    return lines


def _state(
    meta: dict,
    latest: dict,
    history: list[dict],
    *,
    members: list[dict] = (),
    options=(),
    open_lines: list[str] | None = None,
) -> dict:
    """Room, the trigger's thread (root + replies) ahead of the last 30 messages, the open requests,
    and the trigger. Over budget, the oldest general message goes first and the thread only once that
    is empty."""
    names = {m["id"]: m.get("displayName") or m["id"] for m in members}
    by_id = {m.get("messageId"): m for m in history if m.get("messageId")}
    line = lambda m: _line(m, names, by_id, options)  # noqa: E731
    root = latest.get("threadRootMessageId")
    thread = [m for m in history if root and root in (m.get("messageId"), m.get("threadRootMessageId"))]
    in_thread = {m.get("messageId") for m in thread}
    recent = [line(m) for m in history[-_RECENT_LIMIT:] if m.get("messageId") not in in_thread]
    thread_lines = [line(m) for m in thread]
    state: dict = {"room": {"kind": meta.get("kind"), "name": meta.get("name")}}
    if thread_lines:
        state["thread"] = thread_lines
    state["recent"] = recent
    if open_lines:
        state["open"] = open_lines[:5]
    state["latest"] = {**line(latest), "inThread": bool(root)}
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
                runs = await _soft("open runs", messaging_repo.list_open_runs_for_room(meta["ownerId"], room_id)) or []
                # Posted since the trigger (a queued or retried decision): Jev judges whether it was answered.
                seq = int(message.get("messageSeq") or 0)
                later = (
                    (await _soft("later", messaging_repo.list_messages(room_id, seq, 10)) or ([], None))[0]
                    if seq and int(meta.get("lastSeq") or 0) > seq
                    else []
                )
                now = _parse_ts(latest.get("createdAt")) or datetime.now(timezone.utc)
                decision = await asyncio.wait_for(
                    _decide_for_message(
                        meta,
                        message,
                        latest,
                        members,
                        history,
                        open_lines=_open_lines(runs, members, history, now),
                        later=later,
                    ),
                    _DECIDE_BUDGET_S,
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


# --- Super-agent Router admission (contracts-v2 §10, §14.7; wave-2 O Task 6) ------------------------

ROUTER_CRITERION = (
    "Lightbulb, the workspace's coordinator: anything someone in the room wants done, answered, checked "
    "or remembered, when no specialist below clearly owns it; a message that speaks to Lightbulb or the "
    "team's agents, or asks the team for help; information told to Lightbulb or the whole team to keep "
    '("FYI", "for the record", "note that", "heads up", or a standing fact about the business said to '
    "no one in particular: a price, a limit, hours, a rule, a preference, or who does what: "
    '"Priya will run the sign-up table"), which Lightbulb notes.'
)
ROUTER_WHO_INSTRUCTIONS = (
    "No agent was @mentioned in this message. Pick who should handle it: a specialist whose criterion "
    "clearly fits, Lightbulb for any other request of the agents or the team or a fact told to the team to "
    "keep (who does what in the team is one), or none when people are talking among themselves (to each other "
    'by name, chatting, thanks, small talk, or sorting out their own day even about the work: "Priya, the '
    'meeting moved to 3pm", "Priya, you run the sign-up table", "I\'ll call the supplier", "running late").'
)


async def route_for_router(
    meta: dict,
    message: dict,
    members: list[dict],
    history: list[dict],
    specialists: dict[str, str],
    *,
    rules: list[dict] | tuple = (),
) -> dict:
    """One Jev call for one unaddressed message in a Router room: `{"to": "router"|"none", "specialist",
    "confidence"}`, or `{"fallback": True}` when Jev cannot answer (the caller admits addressed messages only).
    `specialists`: `{agentId: name}` of `AGENT#` rows with listenMode active (observe ones never run).
    `rules`: the room's proactive rules (contracts §18); they ride the call as one `rule` question and the verdict
    gains `rule` / `ruleP`, parsed on their own (a bad `who` neither drops nor invents a match). Empty leaves the
    request and the verdict unchanged. Jev down carries no `rule` key: the caller counts it as unavailable."""
    cards = await asyncio.gather(*(agent_card(meta["ownerId"], a, n) for a, n in specialists.items()))
    criteria = {"lightbulb": ROUTER_CRITERION, **dict(zip(specialists, cards)), "none": NONE_CRITERION}
    state = _state(meta, message, history, members=members, options=list(specialists))
    questions = {"who": {"type": "choice", "instructions": ROUTER_WHO_INSTRUCTIONS, "criteria": criteria}}
    if rules:
        from core.services import rules as rules_mod  # rules imports jev._clip: import here, not at module top

        questions["rule"] = rules_mod.jev_question(list(rules))
    answers = await decide(state, questions)
    if answers is None:
        # One more try on a timeout or error (p99 ~0.7 s against a 3 s cap): one slow call left "FYI: we have floor
        # cushions for 15 kids" unread and silent (dev walk 16). A rate limit is not retried.
        answers = await decide(state, questions)
    if not isinstance(answers, dict):  # None (down) or RATE_LIMITED
        return {"fallback": True}
    extra: dict = {}
    if rules:
        ids = {r["scheduleId"] for r in rules[: rules_mod.RULES_PER_ROOM]}
        extra["rule"], extra["ruleP"] = rules_mod.parse_match(answers, ids)
    who = answers.get("who") or {}
    choice = who.get("choice")
    if choice not in criteria:
        return {"fallback": True, **extra}
    confidence = float((who.get("probabilities") or {}).get(choice) or 0)
    if choice == "none":
        return {"to": "none", "specialist": None, "confidence": confidence, **extra}
    return {"to": "router", "specialist": None if choice == "lightbulb" else choice, "confidence": confidence, **extra}
