"""The live rig's scenarios (`scenarios/`), judged offline against the Meet gate by `gate.py`."""

import json
import os

HERE = os.path.dirname(__file__)
SCENARIOS = sorted(f[:-5] for f in os.listdir(os.path.join(HERE, "scenarios")) if f.endswith(".json"))


def load(name: str) -> dict:
    with open(os.path.join(HERE, "scenarios", f"{name}.json")) as f:
        return json.load(f)
