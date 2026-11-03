"""Per-workspace A2A rules and the deterministic decision over them (no model in the loop)."""

from dataclasses import dataclass
from typing import Literal

from core.services.messaging import ContractModel

Effect = Literal["allow", "deny", "hold"]


class When(ContractModel):
    """Optional extra conditions on a rule; every set clause must match."""

    operands_equal: dict[str, str] | None = None
    intent_matches: str | None = None
    verified_by_in: list[str] | None = None


class Rule(ContractModel):
    id: str
    resource: str
    skill: str
    requesters: list[str]
    effect: Effect
    when: When | None = None


class Rules(ContractModel):
    revision: str = ""  # assigned by the server on store (a2a_tasks.put_rules); a client's is ignored
    holders: dict[str, list[str]]
    rules: list[Rule]
    default: Effect = "hold"


@dataclass
class Decision:
    effect: Literal["allow", "deny", "hold"]
    rule: str
    holders: list[str]


def default_rules(org: str, admin_ids: list[str]) -> Rules:
    return Rules(
        revision="default",
        holders={"agent:*": admin_ids, "session:*": ["@session-person"], "room:*": admin_ids},
        rules=[Rule(id="r1", resource="agent:*", skill="message", requesters=[f"org:{org}"], effect="allow")],
    )


def matches(pattern: str, value: str) -> bool:
    return value.startswith(pattern[:-1]) if pattern.endswith("*") else pattern == value


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def when_matches(when: When | None, *, operands: dict | None, intent: str | None, verified_by: str | None) -> bool:
    if when is None:
        return True
    if when.operands_equal is not None and (
        operands is None or any(k not in operands or operands[k] != v for k, v in when.operands_equal.items())
    ):
        return False
    if when.intent_matches is not None and (intent is None or _norm(intent) != _norm(when.intent_matches)):
        return False
    return when.verified_by_in is None or (verified_by is not None and verified_by in when.verified_by_in)


def _holders(rules: Rules, resource: str, session_person: str | None) -> list[str]:
    keys = [k for k in rules.holders if matches(k, resource)]
    if not keys:
        return []
    # exact key first, then the longest wildcard prefix
    best = max(keys, key=lambda k: (not k.endswith("*"), len(k)))
    return [
        session_person if h == "@session-person" else h
        for h in rules.holders[best]
        if h != "@session-person" or session_person
    ]


def decide(
    rules: Rules,
    *,
    org: str,
    requester_person: str,
    requester_org: str,
    chain_agents: list[str],
    resource: str,
    skill: str,
    session_person: str | None,
    exception: dict | None,
    origin_is_person: bool = True,
    operands: dict | None = None,
    intent: str | None = None,
    verified_by: str | None = None,
) -> Decision:
    if requester_org != org:
        return Decision("deny", "outside_org", [])
    holders = _holders(rules, resource, session_person)

    def requester_matches(r: str) -> bool:
        return (
            r == requester_person or r == f"org:{requester_org}" or (r.startswith("agent:") and r[6:] in chain_agents)
        )

    hits = [
        r
        for r in rules.rules
        if matches(r.resource, resource)
        and r.skill == skill
        and any(requester_matches(q) for q in r.requesters)
        and when_matches(r.when, operands=operands, intent=intent, verified_by=verified_by)
    ]
    deny = next((r for r in hits if r.effect == "deny"), None)
    if deny:
        return Decision("deny", deny.id, holders)
    if exception:
        return Decision("allow", f"exception:{exception['id']}", holders)
    first = hits[0] if hits else None
    effect, rule = (first.effect, first.id) if first else (rules.default, "default")
    # A verified person asking for a resource they hold already has its holder's OK (ruling 2 Nov 2026).
    # An agent-originated ask (no person origin) stays held: its stand-in requester answers it.
    if effect == "hold" and origin_is_person and requester_person in holders:
        return Decision("allow", "holder", holders)
    return Decision(effect, rule, holders)
