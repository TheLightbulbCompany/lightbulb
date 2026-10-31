# BEAM runner

Lightbulb's runner for the BEAM long-term-memory benchmark. It is a fork of past.dev's harness
(`beam/` in https://github.com/pastdotdev/benchmarks), copied from the pinned commit
`a712cd993e249cadd214168c3c3ee674f89bd5e4`. Files copied unchanged except three one-line import
changes (`import prompts as exabase_prompts` in `run.py`, `scoring.py`, `verify.py`), and
`past_api.py`, replaced by one adapter interface (`adapters/base.py`; `run.py --backend NAME`).
The runner trims each recall page to 100 results / 8,000 tokens, counting tokens as past.dev's
`usedEvidenceTokens` does (ceil of content plus excerpt characters over 4 per result; it
reproduces all 400 rows of their published 100K results).

## Licences

- Harness code (`run.py`, `dataset.py`, `llm.py`, `scoring.py`, `verify.py`, `log.py`; `past_api.py` before it was replaced): MIT, Copyright (c) 2026 Past Corp. Notice in `LICENSE-past`; third-party carve-outs in `NOTICE.md`.
- ExaBase's prompts: not committed. `prompts.ensure_exabase_prompts(cache_dir)` downloads ExaBase's published file into `.cache/` (git-ignored) and checks SHA-256 `584b672d957f3e6f75e96f881ec31da22e1039da1df5fbedb53dc6a9145cfc43`; a mismatch raises. The prompts remain ExaBase's work.
- BEAM dataset and any published results containing its text: CC BY-SA 4.0.

## Tests

    uv run --with pytest pytest -q
