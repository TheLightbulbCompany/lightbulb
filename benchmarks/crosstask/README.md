# Cross-task recall eval

This eval asks whether a teammate's question, asked weeks later from another room, reaches the right task without leaking private text. Design: spec 07 §8.6 (super-agent memory). Plan: wave2-M Tasks 8 and 9.

| File | What it does |
|---|---|
| `fixture.py` | Builds the synthetic workspace from templates: 6 people, 3 specialists, 4 channels, 6 DMs with Lightbulb, 1 group DM, 1 people-only DM, 582 messages, 40 tasks (8 private, 3 hidden, 4 supersession pairs), 12 canaries and 200 questions in 8 categories. `python fixture.py --seed 7` writes `fixtures/workspace-v1.json` |
| `fake_backend.py` | In-memory recall and link routes that follow the design's lane and link rules. Retrieval is word overlap, so it tests the harness, not memory quality |
| `agent.py` | The hop loop. Hop 1 is a recall. Each later step makes one model call that chooses `answer`, `follow` or `recall`, up to 3 hops. `HeuristicLLM` is the no-model stand-in |
| `score.py` | Scores leaks (hard gate 0), task_hit@1/2/3 (and @2 on oblique questions), fact accuracy, revoked unreachable, abstention, lift and p95 latency |
| `run.py` | Runs a question set, resumes from `<out>/results.jsonl`, caches Codex calls in `<out>/codex-cache.jsonl`, and writes `<out>/summary.json` |

    python run.py --out /tmp/xt/links                 # fake backend, no model calls
    python run.py --no-links --out /tmp/xt/nolinks
    uv run --with pytest pytest -q                    # harness tests, no model calls

The model calls go through Codex on the ChatGPT subscription only (`--llm codex`, `BEAM_CODEX_HOME`, `--budget`), using `../beam/llm_codex.py`. No API key is read.

Differences from spec 07 §8.6:
- **No judge.** Gold facts are template tokens, so fact accuracy is a token match. Abstention is the agent's own `abstain` flag. Both are deterministic, so a run makes about half the spec's ~1,000 calls per arm.
- **Oblique questions name neither the client nor the subject.** They use a client description ("the candy factory") that shares no word with the client's name or with the one #sales line that introduces the client, so word overlap cannot resolve them (fake backend: oblique taskHit@2 1.0 → 0.46).
- **Superseded values are penalised.** A supersession answer that names the old value is wrong even if it also names the new one (`staleFacts`). The heuristic stand-in quotes everything it read, so it scores 0 there by design.
- **Facts are still stated verbatim in a stored turn**, so fact accuracy on direct questions does not need links. Quote fact accuracy per category.
- **No Codex pass on wording.** The spec allows one to rewrite the fixture's wording. It was not run. If you want more natural phrasing, run it and review the result before committing.
- **Live backend comes later.** The live backend and the seeding (`POST /tasks`, `POST /messages` on the bench routes) land with Task 9. Until then `--backend` accepts only `fake`.
- **Reach comes from links only.** A task counts as reached only through a link or `read_task`, so the `--no-links` arm reaches nothing by construction. `score.lift` therefore also reports the fact-accuracy lift.
