# ponytail: deterministic cases; add LLM-generated variants on the Codex subscription when the compiled-rules increment lands
"""Replay named A2A authority cases through the real rule engine; exit 1 on any mismatch.

Run from apps/backend: ``CLERK_ISSUER=<issuer url> uv run python -m scripts.a2a_replay``
(importing core.services loads Settings, which requires CLERK_ISSUER; the test suite sets it). No model call.
"""

import json
import sys
from pathlib import Path

from core.services.a2a_rules import Rule, decide, default_rules

ORG = "org_1"
ADMINS = ["user_admin"]


def run(cases: list[dict]) -> list[dict]:
    rows = []
    for c in cases:
        rules = default_rules(ORG, ADMINS)
        if "rules" in c:
            rules = rules.model_copy(update={"rules": [Rule(**r) for r in c["rules"]]})
        d = decide(
            rules,
            org=ORG,
            requester_person=c["requester_person"],
            requester_org=c["requester_org"],
            chain_agents=c["chain_agents"],
            resource=c["resource"],
            skill=c["skill"],
            session_person=c["session_person"],
            exception=c.get("exception"),
            origin_is_person=c.get("origin_is_person", True),
        )
        ok = d.effect == c["expected"] and ("rule" not in c or d.rule == c["rule"])
        rows.append({"name": c["name"], "expected": c["expected"], "got": d.effect, "rule": d.rule, "ok": ok})
    return rows


def main() -> int:
    cases = json.loads((Path(__file__).parent / "cases.json").read_text())["cases"]
    rows = run(cases)
    for r in rows:
        print(f"{r['name']} | {r['expected']} | {r['got']} | {'ok' if r['ok'] else 'MISMATCH'}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
