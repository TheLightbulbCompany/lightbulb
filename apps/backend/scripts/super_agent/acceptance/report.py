"""The scenario registry, the runner and the report (G1), plus the gate that adds the evidence checks (G2).

A scenario is ``A0``..``A13`` (spec 09 §8.3), one module per scenario in this package, ``async def aNN(ctx) -> Result``.
A scenario whose module or function is not on main yet reports ``not implemented`` with its owning lane; the runner
never imports a body it does not need. A12 has two bodies (lane O's Router part, lane M's memory part) and passes
only when both do.
"""

from __future__ import annotations

import importlib
import json
import re
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.super_agent.acceptance.ctx import Ctx, Result, not_implemented

PACKAGE = __name__.rsplit(".", 1)[0]
#: scenario -> (owning lane, bodies as ``module:function``), from each lane's plan (wave2-{O,Q,A,M}.md).
SCENARIOS: dict[str, tuple[str, tuple[str, ...]]] = {
    "A0": ("G", ("a00_parity:a00",)),
    "A1": ("O", ("a01_one_coordinator:a01",)),
    "A2": ("O", ("a02_many_asks:a02",)),
    "A4": ("O", ("a04_steer:a04",)),
    "A5": ("Q", ("a05_approval:a05",)),
    "A6": ("Q", ("a06_go_ahead:a06",)),
    "A7": ("O", ("a07_one_voice:a07",)),
    "A8": ("O", ("a08_open_thread:a08",)),
    "A9": ("Q", ("a09_landing:a09",)),
    "A10": ("Q", ("a10_a2a:a10",)),
    "A11": ("A", ("a11_recurring:a11",)),
    "A12": ("O+M", ("a12_router_lanes:a12_router", "a12_private_lane:a12")),
    "A13": ("all", ("a13_layers:a13",)),
}
ORDER = list(SCENARIOS)
Body = Callable[[Any], Awaitable[Any]]


def parse_scenarios(spec: str) -> list[str]:
    """``A0-A13`` | ``A1,A2,A7`` | ``A5`` (any mix, order kept, unknown names refused)."""
    names: list[str] = []
    for part in re.split(r"[,\s]+", spec.strip().upper()):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if hi:
            if lo not in ORDER or hi not in ORDER:
                raise SystemExit(f"unknown scenario range {part!r}")
            names += ORDER[ORDER.index(lo) : ORDER.index(hi) + 1]
        elif part in SCENARIOS:
            names.append(part)
        else:
            raise SystemExit(f"unknown scenario {part!r}; one of {', '.join(ORDER)}")
    return list(dict.fromkeys(names))


def resolve(part: str) -> Body | None:
    """The body for ``module:function``, or ``None`` when that lane has not landed it on this checkout."""
    module, _, func = part.partition(":")
    try:
        mod = importlib.import_module(f"{PACKAGE}.{module}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith(PACKAGE):
            return None
        raise
    return getattr(mod, func, None)


def status(name: str) -> str:
    """``implemented`` or ``not implemented (lane X)`` for the dry-run plan."""
    lane, parts = SCENARIOS[name]
    missing = [p for p in parts if resolve(p) is None]
    return "implemented" if not missing else f"not implemented (lane {lane}: {', '.join(missing)})"


async def run_scenario(name: str, ctx: Ctx) -> Result:
    """Every body of the scenario in order; an exception is a failed check, never a crashed run."""
    lane, parts = SCENARIOS[name]
    results: dict[str, Result] = {}
    if name == "A12" and any(resolve(p) for p in parts):
        try:  # both A12 bodies post into the DM rooms; one lookup here so neither lane invents its own
            await ctx.ensure_dm_rooms()
        except Exception as exc:  # noqa: BLE001 -- the report carries it; the bodies then fail on the missing ids
            results["dm_rooms"] = Result(False, {"error": f"{type(exc).__name__}: {exc}"})
    for part in parts:
        body = resolve(part)
        if body is None:
            results[part] = not_implemented(f"not implemented (lane {lane}: {part})")
            continue
        started = time.monotonic()
        try:
            got = await body(ctx)
            result = Result(bool(got.passed), dict(got.evidence))
        except Exception as exc:  # noqa: BLE001 -- the report carries the failure; the next scenario still runs
            result = Result(
                False, {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:]}
            )
        result.evidence.setdefault("elapsedS", round(time.monotonic() - started, 1))
        results[part] = result
    if len(parts) == 1:
        return results[parts[0]]
    return Result(all(r.passed for r in results.values()), {p: r.evidence for p, r in results.items()})


async def run_scenarios(names: list[str], ctx: Ctx, *, log: Callable[[str], None] = print) -> dict[str, Result]:
    results: dict[str, Result] = {}
    for name in names:
        results[name] = await run_scenario(name, ctx)
        log(f"{name}: {'PASS' if results[name].passed else 'FAIL'} {failed_checks(results[name]) or ''}".rstrip())
    return results


# ---------------------------------------------------------------------- the report
def flatten(evidence: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested evidence (a scenario's parts, a member dict) as dotted keys: ``part.key``."""
    out: dict[str, Any] = {}
    for key, value in evidence.items():
        if isinstance(value, Mapping):
            out.update(flatten(value, f"{prefix}{key}."))
        else:
            out[prefix + key] = value
    return out


def checks_of(evidence: Mapping[str, Any]) -> dict[str, bool]:
    """Every bool-valued evidence key: the checks the report lists."""
    return {key: value for key, value in flatten(evidence).items() if isinstance(value, bool)}


def failed_checks(result: Any) -> list[str]:
    flat = flatten(result.evidence)
    reasons = [str(v) for k, v in flat.items() if k.rsplit(".", 1)[-1] in ("reason", "error") and v]
    return [k for k, v in flat.items() if isinstance(v, bool) and not v] + reasons


def _cell(value: Any) -> str:
    text = json.dumps(value, default=str, separators=(",", ":"))
    return (text[:300] + "…" if len(text) > 300 else text).replace("|", "\\|")


def render_report(results: Mapping[str, Any], *, sha: str, workspace: str, faults: str = "") -> str:
    """Markdown the gate ledger keeps: one row per check with its evidence (row keys, ids, timings)."""
    lines = [
        "# Acceptance report",
        "",
        f"- run at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- dev SHA: {sha}",
        f"- faults: {faults or 'none'}",
        f"- workspace: {workspace}",
        f"- verdict: {'ALL GREEN' if results and all(r.passed for r in results.values()) else 'NOT GREEN'}",
        "",
        "| scenario | result | check | value |",
        "|---|---|---|---|",
    ]
    for name, result in results.items():
        verdict = "PASS" if result.passed else "FAIL"
        flat = flatten(result.evidence)
        for key, value in flat.items():
            if isinstance(value, bool):
                lines.append(f"| {name} | {verdict} | {key} | {'ok' if value else 'FAIL'} |")
        for key, value in flat.items():
            if not isinstance(value, bool) and not key.endswith("traceback"):
                lines.append(f"| {name} | {verdict} | {key} | {_cell(value)} |")
        if not result.evidence:
            lines.append(f"| {name} | {verdict} | – | – |")
    return "\n".join(lines) + "\n"


def write_report(path: Path, results: Mapping[str, Any], **meta: Any) -> None:
    """``path`` (markdown) plus ``path.json`` (the full evidence, for diffing two runs on one SHA)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(results, **meta))
    path.with_suffix(path.suffix + ".json").write_text(
        json.dumps({k: {"passed": r.passed, "evidence": r.evidence} for k, r in results.items()}, indent=1, default=str)
    )


# ---------------------------------------------------------------------- G2: the gate
async def _guarded(call: Awaitable[Any]) -> Any:
    """An evidence check that raises is a failed check; it must not lose the run's report."""
    try:
        return await call
    except Exception as exc:  # noqa: BLE001
        return Result(False, {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:]})


async def gate(
    ctx: Ctx, *, rules_expect: Any | None = None, skip: tuple[str, ...] = (), log: Callable[[str], None] = print
) -> dict[str, Any]:
    """A1–A13 on the one dev workspace (A0 needs the switch off: run it before the flip), then the evidence
    checks: ``vm_evidence`` over the task ids the scenarios left in ``ctx.state["taskIds"]`` (V17; per-task or workspace
    session by the workspace's token custody)
    and ``rules_observe`` over the lead's ``Expect`` (S9; skipped without one). UI checks and W's digest stay by hand."""
    from scripts.super_agent.acceptance.rules_observe import rules_observe
    from scripts.super_agent.acceptance.vm_evidence import vm_evidence

    results: dict[str, Any] = await run_scenarios([n for n in ORDER[1:] if n not in skip], ctx, log=log)
    results["VM"] = await _guarded(vm_evidence(ctx, ws=ctx.ws, task_ids=list(ctx.state.get("taskIds") or [])))
    results["RULES"] = (
        await _guarded(rules_observe(ctx, rules_expect))
        if rules_expect
        else not_implemented("no --rules-expect (lane S, manual)")
    )
    for name in ("VM", "RULES"):
        log(f"{name}: {'PASS' if results[name].passed else 'FAIL'} {failed_checks(results[name]) or ''}".rstrip())
    return results
