"""The live rig's Meet scenarios (`scenarios/`): side talk, mentions, follow-ups, stops. Their offline judge went with
the code gate (Task 3, GPT-Live decides); they are the cases to measure GPT-Live against in a real Meet."""

import json
import os

HERE = os.path.dirname(__file__)
SCENARIOS = sorted(f[:-5] for f in os.listdir(os.path.join(HERE, "scenarios")) if f.endswith(".json"))


def load(name: str) -> dict:
    with open(os.path.join(HERE, "scenarios", f"{name}.json")) as f:
        return json.load(f)
