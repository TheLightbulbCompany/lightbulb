"""Per-workspace A2A rules and the deterministic decision over them (no model in the loop)."""

from dataclasses import dataclass
from typing import Literal

from core.services.messaging import ContractModel

Effect = Literal["allow", "deny", "hold"]


class Rule(ContractModel):
    id: str
    resource: str
    skill: str
    requesters: list[str]
    effect: Effect


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
        if matches(r.resource, resource) and r.skill == skill and any(requester_matches(q) for q in r.requesters)
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
