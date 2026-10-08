"""A fast offline loop for the call controller: the live rig's scenarios, replayed against a real `Huddle` in
milliseconds and judged by the rig's own checks.

    uv run python -m tests.replay all              every scenario, 20 seeds each
    uv run python -m tests.replay gap --seeds 50   one scenario, more seeds
    uv run python -m tests.replay gap --seed 7 --trace   one replay, with the controller's own log

`sim.py` is the simulation, `routing.py` the router's assumed verdicts, `live.py` a live rig run turned into
a recording (and the harness checked against it), `check.py` and `scenarios/` the rig's, vendored."""

import json
import os

from .routing import ROUTING, known
from .sim import Evidence, Recording, Sim, judge

HERE = os.path.dirname(__file__)
SCENARIOS = sorted(f[:-5] for f in os.listdir(os.path.join(HERE, "scenarios")) if f.endswith(".json"))
SEEDS = 20

__all__ = ["ROUTING", "SCENARIOS", "SEEDS", "Evidence", "Recording", "Sim", "judge", "known", "load", "replay"]


def load(name: str) -> dict:
    with open(os.path.join(HERE, "scenarios", f"{name}.json")) as f:
        return json.load(f)


def replay(name: str, seed: int, trace: bool = False, keep: str | None = None) -> tuple[list[tuple[str, str, str]], Evidence]:
    """One synthetic replay of a scenario: its checks, [(PASS|FAIL|SKIP, check, evidence)], and what it left."""
    scenario = load(name)
    # seeded by name too: seed 3 of one scenario is not seed 3 of another, with the same first delays
    ev = Sim(scenario, ROUTING[name], f"{name}:{seed}", trace=trace).run()
    return judge(scenario, ev, keep), ev
