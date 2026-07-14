# First-run: "I don't know yet — you pick"

Follow-up to the onboarding aha first-run (PR #1240, spec
`2026-07-12-onboarding-aha-first-run-design.md`).

## Problem

`FirstRunFunnel`'s "Start something new" path forces the user to type an idea:
`input` is `min_length=3` server-side and the Continue button is disabled below
three characters. A signup who wants a business but has no idea yet — a real
and probably common segment for the no-card trial — has nowhere to go. There is
no skip (#1243 removed it deliberately), so the funnel is a dead end for them.

Polsia's equivalent move is a "random business" button that looks the user up
and proposes something. We already have every ingredient for that:

- `BRIEF.md` already carries the user's **name and email** and already tells
  Friday to "research the person as well as the business."
- Friday already has `web_search`, routed through Parallel's keyless provider
  (`core/containers/config.py:1345`).

So there is no lookup to build and no idea-generation service to write. The
only missing thing is a way for the user to say *"I don't have one."*

## Design

Add a secondary button on the existing "What's your idea?" step that submits
the brief with **no input**. The absence of an idea is itself the signal:
`BRIEF.md` then tells Friday that picking one is job zero, and hands her the
email + name she already had.

Deliberately NOT built:

- No backend inference. The backend stays dumb — it stages files, it does not
  think. Every model call still originates in the owner's container, so the
  $3/day meter fence (`trial_cap`) still sees all of it.
- No third top-level path, no new screen, no enrichment vendor, no LinkedIn
  OAuth, no reroll button.
- No orchestration of what Friday *says*. We give her context and get out of
  the way; her narration is her business.

The path stays `"new"` end to end, so the existing `new` branch in
`BOOTSTRAP.md` step 5 (website task first) applies unchanged.

## Tasks

### 1. Backend — make `input` optional on the `new` path

`apps/backend/routers/onboarding.py`

- `BriefBody.input` becomes `str | None = Field(default=None, max_length=2000)`.
- Add a `model_validator(mode="after")` enforcing what the type can no longer
  express: `grow` **requires** a non-blank `input` of >= 3 chars; `new` may omit
  it. A `grow` body with no input is a 422, not a silently-empty brief.
- Normalize: a whitespace-only `input` on `new` is treated as absent (`None`),
  so a client that posts `""` gets the pick-for-me brief rather than a brief
  containing an empty string.

### 2. Backend — the no-idea brief

`apps/backend/core/services/onboarding_brief.py`

- `render_brief_md(user_input: str | None, ...)`. When `user_input` is None,
  swap the "Their words, verbatim" line for a line stating they have no idea
  yet and that choosing one — using the email/name above and a web lookup — is
  job zero, and that she should say what she chose and why.
- `_FIRST_RUN_BOOTSTRAP_TEMPLATE`: one sentence ahead of step 3 ("Understand
  the business") covering the no-idea case — pick a business first, commit to
  it, then continue exactly as written. No new branch; step 5's `new` branch
  already does the right thing.

### 3. Frontend — the button

`apps/frontend/src/components/onboarding/FirstRunFunnel.tsx`

- `submit` takes an explicit `opts?: { noIdea?: boolean }` rather than reading
  component state, so the two call sites can't drift: the Continue button
  submits `input`, the new button submits nothing.
- Under the textarea on the `new` step, a secondary (outline, not filled)
  button: **"I don't know yet — you pick"**, with a one-line hint
  ("Friday will look you up and choose something"). Enabled regardless of
  `input` length; disabled while busy.
- Keep `input.trim().length < 3` gating the *Continue* button only.
- Analytics: `capture("first_run_brief_submitted", { path, noIdea })` so we can
  measure how many trial signups take it. (`path` stays `"new"`.)

`FirstRunMeta.path` and `FIRST_RUN_PROMPT` are untouched — the path is still
`"new"`, and the kickoff prompt stays generic, per the existing contract that
path-branching lives in `BOOTSTRAP.md` and not in the visible prompt.

### 4. Tests

- `tests/unit/routers/test_onboarding.py` — `new` with no input stages a brief
  (200 `staged`); `new` with whitespace-only input does the same; `grow` with
  no input is 422; existing cases unchanged.
- `tests/unit/services/test_onboarding_brief.py` — `render_brief_md` with
  `user_input=None` emits the no-idea instruction and still carries the email;
  the bootstrap contains the pick-first sentence.
- `apps/frontend/src/components/onboarding/__tests__/FirstRunFunnel.test.tsx` —
  the button appears on the `new` step only (not `grow`), is clickable with an
  empty textarea, and POSTs `{path: "new"}` with no `input`.

## Verification

Backend: `uv run pytest tests/unit/routers/test_onboarding.py tests/unit/services/test_onboarding_brief.py -v`
Frontend: `pnpm vitest run src/components/onboarding/__tests__/FirstRunFunnel.test.tsx`
Then a live dev signup on the trial path, clicking through to Friday's first run.
