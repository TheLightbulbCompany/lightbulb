"""The G2 evidence check for rules in observe (wave2-S Task 9), on in-memory rows."""

import pytest

from core import tasks_schema as ts
from scripts.super_agent.acceptance.rules_observe import Expect, rules_observe

pytestmark = pytest.mark.asyncio
WS, RULE = "org_dev", "s1"
BUGS = [f"room:dev:m{i}" for i in range(3)]
CHATTER = ["room:dev:c1", "room:dev:c2"]


class _Ctx:
    def __init__(self, rows):
        self.rows = rows

    async def tasks_rows(self, ws):
        return self.rows


def _rows(*, done=(), mode="observe", extra=()):
    entries = [{"threadKey": t, "wouldDo": "Would x", "version": 1, "doneByHuman": t in done, "at": "x"} for t in BUGS]
    rows = [
        {
            "sk": ts.schedule_sk(RULE),
            "type": "schedule",
            "status": "active",
            "mode": mode,
            "ruleVersion": 1,
            "stats": {"matches": entries},
        }
    ]
    for t in BUGS:
        rows.append(
            {"sk": ts.trigger_sk(ts.rule_trigger_id(RULE, t)), "type": "trigger", "ruleMatch": {"decision": "observed"}}
        )
        rows.append({"sk": ts.observed_sk(t), "type": "observed"})
    return [*rows, *extra]  # fmt: skip


async def test_passes_on_the_expected_record():
    out = await rules_observe(_Ctx(_rows(done=[BUGS[0]])), Expect(WS, RULE, BUGS, CHATTER, done_by_human=[BUGS[0]]))
    assert out.passed, out.evidence["failures"]
    assert out.evidence["matchesThisVersion"] == 3 and out.evidence["doneByHuman"] == [BUGS[0]]


@pytest.mark.parametrize(
    ("rows", "expect", "needle"),
    [
        (_rows()[:-1], Expect(WS, RULE, BUGS), "no OBS# marker"),
        (_rows(mode="auto"), Expect(WS, RULE, BUGS), "not active/observe"),
        (_rows(), Expect(WS, RULE, BUGS, done_by_human=[BUGS[1]]), "not marked doneByHuman"),
        (_rows(extra=[{"sk": ts.observed_sk(CHATTER[0]), "type": "observed"}, {"sk": ts.trigger_sk(ts.rule_trigger_id(RULE, CHATTER[0])), "type": "trigger", "ruleMatch": {"decision": "observed"}}]), Expect(WS, RULE, BUGS, CHATTER), "chatter was recorded"),
        (_rows(extra=[{"sk": ts.task_sk("t1"), "type": "task", "taskId": "t1", "policyRef": ts.schedule_sk(RULE)}]), Expect(WS, RULE, BUGS), "observe never acts"),
        ([], Expect(WS, RULE, BUGS), "no row"),
    ],
)  # fmt: skip
async def test_fails_name_the_gap(rows, expect, needle):
    out = await rules_observe(_Ctx(rows), expect)
    assert not out.passed and any(needle in f for f in out.evidence["failures"]), out.evidence["failures"]
