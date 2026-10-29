# BEAM 100K results (Lightbulb runner, Codex path)

Each folder holds a run's `results.jsonl` and `summary.json`; `python verify.py results/100k/<run>` recomputes every score and the summary. Stripped: per question only ids, category, asOf, the model's answer, the judge's verdicts (scores and reasons; no rubric text) and the score. Event-ordering verdicts keep the reference topic list verify.py scores against (BEAM's own `ordering_tested`, CC BY-SA 4.0). No recall pages, questions or gold answers: the full files (with dataset text) stay off the repo.

| Folder | What |
|---|---|
| cal-a-rejudge | our Codex judge on past.dev's saved 100K answers (calibration a) |
| cal-b-reanswer | our Codex answerer + judge on past.dev's saved recall pages (calibration b) |
| free-100k | free engine v0.7.0 (+ bench ops), 50 facts + 50 excerpts per recall |
| free-shipped-100k | free engine, the shipped page (5 turns, 1,600 chars) |
| free-excerpt-100k | + matched 240-char excerpt window (cli loop/turn-excerpt-window) |
| free-length-100k | + matched 800-char excerpts (cli loop/turn-excerpt-length) |
| free-v071-100k | free engine, shipped page (v0.7.1: cli feat/recall-page 35c2fa5), the daemon's page text as an agent gets it |
| free-contested-100k | shipped page v0.7.1 + Contested only across authors, full dates (cli loop/contested-line 94d73ff) |
| workspace-100k | workspace engine + validTo-0 fix (dev AgentCore memory via the local bench routes, bench/workspace-routes 1f8906af2), the route's recall page |
| workspace-page-100k | workspace engine + validTo-0 fix + page fix (feat/workspace-recall-page eae5d4b6a + bench routes), the text page an agent reads |
| free-dates-100k | + dated fact lines, newest first within a subject (cli loop/fact-dates) |

Conditions and the table: docs/research/2026-10-29-beam-first-run.md.
