"""Shared stub for the live-routing tests: Jev's answer to the one call per spoken line."""


def jev_says(kinds=(), whos=(), names=(), asked=(), complete=1.0, amends=None, drops=None, p=0.9):
    """Jev's answer to the one call: ``kinds``/``whos`` per part, ``names`` per name candidate (in text
    order), ``asked`` per part when two agents are named."""
    out = {"complete": {"probability": complete}, "anyone": {"probability": 0.0}}
    for i, kind in enumerate(kinds):
        out[f"k{i}"] = {"choice": kind, "probabilities": {kind: p, "other": round(1 - p, 2)}}
    for i, who in enumerate(whos):
        out[f"w{i}"] = {"choice": who or "unsure", "probabilities": {who or "unsure": 0.8, "other": 0.1}}
    for i, label in enumerate(names):
        out[f"n{i}"] = {"choice": label}
    for i, key in enumerate(asked):
        out[f"p{i}"] = {"choice": key}
    for key, row in (("amends", amends), ("drops", drops)):
        out[key] = {"choice": row or "none", "probabilities": {row or "none": 0.9}}
    return out
