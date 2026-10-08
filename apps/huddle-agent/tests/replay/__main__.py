import argparse
import json
import sys
import time

from . import ROUTING, SCENARIOS, SEEDS, Sim, judge, known, live, load, replay, routing
from .sim import PROFILES


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.replay", description=__doc__)
    ap.add_argument("scenario", nargs="?", default="all", help="a scenario name, or all (the default)")
    ap.add_argument("--seeds", type=int, default=SEEDS, help=f"replays of each scenario (default {SEEDS})")
    ap.add_argument("--first", type=int, default=0, help="the first seed (default 0): another range tells a change from noise")
    ap.add_argument("--profile", choices=PROFILES, default="current", help="the transcriber's timing (sim.Model)")
    ap.add_argument("--noise", type=float, default=0.0, metavar="RATE", help="non-speech sound bursts a second on each silent person's track (sim.Model; default none)")
    ap.add_argument("--rebase", action="store_true", help="write this run's failure counts as the profile's baseline (routing.BASELINE_PATH)")
    ap.add_argument("--json", metavar="PATH", help="write {scenario | check: [seeds failing, seeds skipped]} there, to compare two runs")
    ap.add_argument("--seed", type=int, help="one replay, with its timeline")
    ap.add_argument("--trace", action="store_true", help="with --seed: the controller's own log too")
    ap.add_argument("--keep", help="with --seed: write rig.txt, worker.log and transcript.json here")
    ap.add_argument("--live", nargs="+", metavar="RUN_DIR", help="live rig runs: replay each and compare with its report.txt")
    ap.add_argument("--recording", metavar="JSONL", help="replay this recorded call (HUDDLE_RECORD_DIR) of the scenario")
    ap.add_argument("--rig", help="with --recording: the call's rig.txt, for when each line was said")
    a = ap.parse_args()
    if a.live:
        return live.compare(a.live)
    names = SCENARIOS if a.scenario == "all" else [a.scenario]
    if unknown := [n for n in names if n not in SCENARIOS]:
        ap.error(f"no scenario {unknown[0]}; there are: {', '.join(SCENARIOS)}")
    if a.seed is not None or a.recording:
        if a.recording:  # the router's own answers are in the recording; the table only fills what is missing
            scenario = load(names[0])
            rec = live.load(a.recording, scenario, a.rig)
            ev = Sim(scenario, ROUTING[names[0]], f"{names[0]}:{a.seed or 0}", recording=rec, trace=a.trace, profile=a.profile).run()
            results = judge(scenario, ev, a.keep)
        else:
            results, ev = replay(names[0], a.seed, trace=a.trace, keep=a.keep, profile=a.profile, noise=a.noise)
        rows = [(t, "rig", f"{kind} {detail}") for t, kind, detail in ev.rig]
        rows += [(t, "worker", f"{n}={v} {d or ''}") for t, n, v, d in ev.emf if n.startswith("live.request")]
        rows += [(x.at, "said", f"{x.name}{' → ' + x.to if x.to else ''}: {x.text}") for x in ev.lines]
        rows += [(ev.t0 + t, "huddle", text) for t, text in ev.trace]
        for t, src, text in sorted(rows, key=lambda r: r[0]):
            print(f"{t - ev.t0:8.2f}  {src:<7} {text}")
        for status, what, why in results:
            print(f"{status}  {what}\n      {why}")
        for n in ev.notes:
            print("NOTE ", n)
        return int(any(s == "FAIL" for s, _, _ in results))
    began, failed, table = time.perf_counter(), 0, {}
    try:
        base = routing.baseline()
    except FileNotFoundError:
        base = {}
    base.setdefault(a.profile, {"seeds": 1, "fails": {}})
    for name in names:
        runs = {seed: replay(name, seed, profile=a.profile, noise=a.noise)[0] for seed in range(a.first, a.first + a.seeds)}
        bad = sorted(s for s, r in runs.items() if any(x[0] == "FAIL" for x in r))
        failed += bool(bad)
        print(f"{name:<20} {a.seeds - len(bad):>3}/{a.seeds} seeds pass" + (f"   failing seeds: {' '.join(map(str, bad))}" if bad else ""))
        checks: dict[str, dict[str, list]] = {}  # check -> status -> [(seed, evidence)]
        for seed, results in runs.items():
            for status, what, why in results:
                checks.setdefault(what, {}).setdefault(status, []).append((seed, why))
        for what, by in checks.items():
            table[f"{name} | {what.split('  #')[0]}"] = [len(by.get("FAIL", [])), len(by.get("SKIP", []))]
            if fails := by.get("FAIL"):
                skips = f", {len(by['SKIP'])} skipped" if by.get("SKIP") else ""
                why = known(name, what)
                key = f"{name} | {what.split('  #')[0]}"
                was, of = base[a.profile]["fails"].get(key, 0), base[a.profile]["seeds"]
                print(f"  FAIL {len(fails):>2}/{a.seeds}{skips}  (baseline {was}/{of})  {what}")
                print(f"        seed {fails[0][0]}: {fails[0][1][:400]}")
                print(f"        {'known: ' + why if why else 'no cause recorded in routing.KNOWN_FAILING'}")
            elif len(by.get("SKIP", [])) == a.seeds:
                print(f"  SKIP {a.seeds:>2}/{a.seeds}  {what}\n        {by['SKIP'][0][1][:200]}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(table, f, indent=1)
    fails = {key: f for key, (f, _) in table.items() if f}
    if a.rebase:
        if a.scenario != "all":
            ap.error("--rebase takes every scenario")
        base[a.profile] = {"seeds": a.seeds, "fails": fails}
        with open(routing.BASELINE_PATH, "w") as f:
            json.dump(base, f, indent=1, sort_keys=True)
        print(f"baseline for {a.profile} written: {sum(fails.values())} failing check-seeds in {len(fails)} checks, {a.seeds} seeds")
    # a run of some scenarios is held to those scenarios' part of the baseline
    part = {**base[a.profile], "fails": {k: f for k, f in base[a.profile]["fails"].items() if k.split(" | ")[0] in names}}
    worse = routing.gate(part, fails, a.seeds)
    for line in worse:
        print("REGRESSION  " + line)
    print(
        f"{len(names) - failed}/{len(names)} scenarios pass every seed; {sum(fails.values())} failing check-seeds; "
        f"{len(worse)} regression(s) against the baseline; {len(names) * a.seeds} replays in {time.perf_counter() - began:.1f}s"
    )
    return int(bool(worse))  # a known failure at its known rate is listed. A new one, or one that got worse, fails.


if __name__ == "__main__":
    sys.exit(main())
