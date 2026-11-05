# E-rules: Jev rule matching

Does the `rule` question that rides Jev's per-message call (spec 12 §3.3, `core/services/rules.py`) pick the right
messages? It uses 3 rules, each scoped to its own room: bug reports in `#dev`, refunds over $100 in `#support`, and
"someone asks for the deck" in `#sales`. Each rule has 60 labelled lines: 25 positives (5 of them need the thread
history), 20 topic near-misses, 10 chatter lines and 5 questions about the rule itself.

| File | What it does |
|---|---|
| `dataset.jsonl` | 180 rows `{rule_id, room, kind, label, latest, history}` |
| `run_eval.py` | Judges each row through `jev._decide_for_message` with the room's rules, and once more without them as the latency baseline. Prints the misses and a summary (precision, recall at 0.7, false positives by kind, p95 latency with and without the question, cost) |
| `test_dataset.py` | Dataset shape and scorer, no model calls |

    cd apps/backend
    uv run pytest ../../benchmarks/rules -q --rootdir ../../benchmarks/rules      # no calls
    ENVIRONMENT=dev uv run python ../../benchmarks/rules/run_eval.py --key-from dev --out /tmp/e-rules.jsonl   # 360 calls

`--key-from dev` reads `lightbulb/dev/vercel-ai-gateway` the way the backend does, so it needs AWS credentials.
`--key-from env` reads `AI_GATEWAY_API_KEY`. Both refuse to run unless `ENVIRONMENT` is dev (or unset). The founder approved about 360 calls on the dev key (contracts §18).
That is one full run with baseline. `--no-baseline` halves it.

Bar: precision ≥ 0.90 and recall ≥ 0.60 at 0.7; p95 latency at most +300 ms. If a run lands below the bar, tune
`RULE_INSTRUCTIONS` / `RULE_NONE` once and re-run once. If it is still below, report it. Observe stays on and
nothing acts.
