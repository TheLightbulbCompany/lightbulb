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

Conditions and the table: docs/research/2026-10-29-beam-first-run.md.
