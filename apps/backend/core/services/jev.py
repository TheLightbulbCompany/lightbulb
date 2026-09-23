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
_KEY_RETRY_S = 300
_KEY_TTL_S = 3600
_CARD_TTL_S = 600
_QUIET_S = 600
#: Whole decision budget: cards (S3 + Paperclip) plus the 3 s Jev call, so it can never stall dispatch.
_DECIDE_BUDGET_S = 8.0
#: A claim loser polls for the winner's row this often, for at most this long, then falls back.
#: ponytail: a DynamoDB poll, <=32 strongly consistent reads per loser per message; a push
#: (stream/notify) only if Listening fan-out grows past a handful of candidates.
_LOSER_POLL_S = 0.25
_LOSER_WAIT_S = 8.0
#: What Jev sees: the last 30 room messages (1,000 chars each), trimmed oldest-first until the
#: whole state fits ~24k chars -- generous headroom under Jev's 32k-token state limit.
_RECENT_LIMIT = 30
_STATE_MAX_CHARS = 24_000

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
    if _key_checked_at and time.monotonic() - _key_checked_at < (_KEY_TTL_S if _key else _KEY_RETRY_S):
        return _key
    _key_checked_at = time.monotonic()
    name = f"lightbulb/{settings.ENVIRONMENT or 'dev'}/vercel-ai-gateway"
    try:
        _key = await _off(_read_secret, name)
    except Exception as exc:  # noqa: BLE001 -- missing secret == feature off
        logger.info("Jev disabled: %s unreadable (%s)", name, type(exc).__name__)
        _key = None
    return _key


async def decide(state: dict, questions: dict) -> dict | None:
    """One Jev call. Returns the ``answers`` dict, or ``None`` on ANY failure (never raises)."""
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
            return None
        if response.status_code != 200:
            logger.warning("Jev returned %s", response.status_code)
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
        "text": str(message.get("text") or "")[:1000],
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
    last_times = [_parse_ts(m.get("createdAt")) for m in history[-5:]]
    quiet = not any(t is not None and abs((now - t).total_seconds()) <= _QUIET_S for t in last_times)

    def recency(agent_id: str) -> str:
        spoke = [_parse_ts(m.get("createdAt")) for m in history if (m.get("author") or {}).get("id") == agent_id]
        return _ago(spoke[-1] if spoke else None, now)

    cards = await asyncio.gather(*(agent_card(meta["ownerId"], a, names.get(a, a), recency(a)) for a in candidates))
    criteria = dict(zip(candidates, cards))
    criteria["none"] = (
        "Nobody: chatter, acknowledgement, addressed to a specific human, "
        "or would prolong agent-to-agent back-and-forth"
    )
    state = _state(meta, latest, history)
    questions = {
        "who": {
            "type": "choice",
            "instructions": (
                "Which AI teammate, if any, should act on the latest message? "
                "Judge by each teammate's role and tools, not by name."
            ),
            "criteria": criteria,
        },
        "placement": {
            "type": "choice",
            "instructions": "Where should a reply to the latest message go?",
            "criteria": {
                "main": "A short contribution to a flowing conversation in the channel",
                "thread": "A specific request or task that needs work or back-and-forth",
            },
        },
    }
    answers = await decide(state, questions)
    decided_at = datetime.now(timezone.utc).isoformat()
    who, placement = (answers or {}).get("who") or {}, (answers or {}).get("placement") or {}
    if answers is None:
        return {"fallback": True, "quietChannel": quiet, "candidates": candidates, "decidedAt": decided_at}
    if who.get("choice") not in (*candidates, "none") or placement.get("choice") not in ("main", "thread"):
        return {
            "fallback": True,
            "reason": "invalid_choice",
            "quietChannel": quiet,
            "candidates": candidates,
            "decidedAt": decided_at,
        }
    return {
        "who": who["choice"],
        "whoP": who.get("probabilities") or {},
        "placement": placement["choice"],
        "placementP": placement.get("probabilities") or {},
        "candidates": candidates,
        "quietChannel": quiet,
        "model": MODEL,
        "decidedAt": decided_at,
    }


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
    state["latest"] = _line(latest)
    while len(json.dumps(state, ensure_ascii=False)) > _STATE_MAX_CHARS and (recent or thread_lines):
        (recent or thread_lines).pop(0)
    return state


async def route(
    meta: dict, agent_id: str, message: dict, *, latest: dict, members: list[dict], history: list[dict]
) -> dict | None:
    """The final decision for this message, computed once across every candidate's consumer.
    ``None`` means fall back (Jev disabled, down, or the winner never finished). Never raises."""
    try:
        # Disabled means NO work: no cards, no S3/Paperclip reads, no DECISION row.
        if not await _gateway_key():
            put_metric("messaging.jev.decision", dimensions={"outcome": "disabled"})
            return None
        room_id, message_id = meta["roomId"], message["messageId"]
        if await messaging_repo.claim_decision(room_id, message_id):
            try:
                decision = await asyncio.wait_for(
                    _decide_for_message(meta, message, latest, members, history), _DECIDE_BUDGET_S
                )
            except Exception:
                # Store a fallback so the losers stop waiting now, not at their 8 s ceiling.
                logger.warning("Jev decision failed", exc_info=True)
                await messaging_repo.put_decision(room_id, message_id, {"fallback": True, "reason": "error"})
                return None
            await messaging_repo.put_decision(room_id, message_id, decision)
            return decision
        # Another candidate's consumer holds the claim: never call Jev twice for one message.
        deadline = time.monotonic() + _LOSER_WAIT_S
        while True:
            decision = await messaging_repo.get_decision(room_id, message_id)
            if decision and not decision.get("pending"):
                return decision
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_LOSER_POLL_S)
    except Exception:  # noqa: BLE001 -- routing must never break dispatch
        logger.warning("Jev routing failed", exc_info=True)
        return None
