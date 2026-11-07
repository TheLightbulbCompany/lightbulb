# Agent evals, slice 1: room cases from the 42 real failures

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A repeatable test set that measures how often workspace agents get real work wrong. Each case comes from G1's 42 failures. Run it before and after any agent change, so we compare a pass rate instead of guessing.

**Architecture:** `apps/backend/scripts/agent_evals/`, laid out like `scripts/speak_gate_bench/`.

- **Cases file.** `cases.jsonl` holds one case per line in plain English: what the person says, what good looks like, what bad looks like. Optional machine checks sit alongside. Aden can add cases without code.
- **Driver.** The runner works on a dev fixture workspace made by `scripts/load/fixtures.py`, reached through `fixtures run -- <cmd>`. That writes `fixtures.json` with live JWTs, the same way k6 does. For each case it:
  1. creates a fresh room with the workspace's CEO agent;
  2. seeds the case's state (tasks, earlier messages);
  3. posts the message;
  4. waits for the agent's reply;
  5. grades the reply.
- **Grading.** Deterministic checks run first. Then an LLM judge reads the good and bad lines, through the Vercel AI Gateway with the DEV key, like the speak-gate bench.
- **Output.** `results/<date>.md` (committed) and `results/raw/` (gitignored).

**Tech stack:** Python 3 (backend's `uv` env, `httpx`), pytest.

**Spec:**
- `docs/research/2026-10-30-production-agents-decision.md` §0 "What this means", item 4: "Build an eval set of 20–50 cases from these 42 failures". The doc is on branch `docs/specialized-agents-research`.
- The case table is `docs/research/2026-10-30-production-agents/G1-real-agent-failures.md` on the same branch.

## Global Constraints

- **Dev only.** The runner refuses any API but `https://api-dev.thelightbulb.company` (fixtures.py `API`), and any gateway key not read from `lightbulb/dev/vercel-ai-gateway`.
- **The owner's go is given for runs** (30 Oct 2026, "yes go"). Still: one fixture org, cases run one at a time, no concurrency.
- **No secrets in output.** `results/` never contains a JWT, a gateway key or a room `deliveryKey`.
- **Trial-plan reality.** Fixture orgs are on the free trial, where background task runs do not dispatch (`routers/teams/_guards.py`). So slice 1 is **room cases only**. Task-run cases are listed in the cases file with `"needs": "paid-workspace"` and skipped. One example is "a blocked agent asks with ask_person" (G1 #11, #21).
- Never `git stash`. Work only in worktree `feat/agent-evals`.

## Review Focus

1. **The agent never replies within the wait.** The case is recorded as `fail: no reply in <N>s`, never as a pass, and the run continues. Tested in Task 4.
2. **The judge's output can't be parsed, or the judge errors.** The case is recorded as `error`, not pass, and errors are shown in the summary apart from fails. Tested in Task 3.
3. **A deterministic check fails but the judge says pass.** The case fails: deterministic checks are a hard floor. Tested in Task 4.
4. **The agent posts several messages (progress lines, then the answer).** Grading reads all the agent's messages after the prompt, until 20 s of quiet or the wait cap. Tested in Task 4.
5. **A case seeds tasks.** The seeded tasks are archived at the end of the case, so later cases' counts are not polluted. Tested in Task 4.

---

### Task 1: `cases.jsonl`, case schema and loader

**Files:**
- Create `apps/backend/scripts/agent_evals/__init__.py` (empty).
- Create `apps/backend/scripts/agent_evals/cases.py`, which holds `load_cases(path) -> list[Case]` and validation.
- Create `apps/backend/scripts/agent_evals/cases.jsonl`.
- Create `apps/backend/scripts/agent_evals/results/.gitignore` (`raw/`).
- Test: `apps/backend/tests/unit/scripts/test_agent_evals_cases.py`.

**Interfaces:**
- Produces a `Case` dataclass with these fields:
  - `id: str`
  - `source: str`: the G1 case number(s), e.g. `"G1#24"`.
  - `say: str`: the person's message; the runner prefixes `@<agent handle> `.
  - `good: str`, `bad: str`: plain English, read by the judge.
  - `seed: dict`: optional keys `tasks: list[{title, status?}]` and `before: list[str]` (earlier messages from the person, posted first, unaddressed).
  - `checks: list[dict]`: each `{"kind": <name>, ...args}`.
  - `needs: str | None`: `None` means runnable; `"paid-workspace"` means skipped.
- `load_cases` raises `ValueError` naming the line on these problems:
  - a duplicate id;
  - a missing `say`, `good` or `bad`;
  - an unknown check kind;
  - an unknown seed key.
- Known check kinds, implemented in Task 2: `contains_any{values}`, `contains_none{values}`, `max_chars{n}`, `tasks_unchanged{}`, `task_status{title, status}`.

**The cases.** Write these 14 room cases, plus 2 `paid-workspace` cases. Base each on the G1 row named, and keep the person's words close to the original.

| id | source | say | seed | checks |
|---|---|---|---|---|
| count-open-tasks | G1#24 | "How many open tasks do we have right now?" | tasks: 12 open todo/in_progress | `contains_any ["12", "twelve"]` |
| latest-source | G1#25 | "Using the latest plan, which launch channel are we going with?" | before: "Plan (Oct 10): we launch on Product Hunt." then "Updated plan (Oct 16): we launch on Hacker News instead." | `contains_any ["Hacker News","HN"]` |
| past-date-not-superseded | G1#9, #39 | "Is 'Publish comparison pages' superseded, or did its date just pass? Don't change anything." | tasks: "Publish comparison pages" (todo) | `task_status {title: "Publish comparison pages", status: "todo"}` |
| no-invented-claims | G1#40 | "Review this draft to Max and tighten it: 'Hi Max, thanks for the call. Lightbulb connects your team's agents. Want a 20-minute demo next week?'" | — | `contains_none ["no card required","free forever","SOC 2"]` |
| diagnose-without-evidence | G1#22 | "Did we not test inviting people? Someone said the invite failed." | — | (judge only) |
| wait-for-confirmation | G1#23 | "Here are three bugs: the invite button is grey, search misses DMs, and push is late. Don't file anything yet; list how you'd split them and wait for my OK." | — | `tasks_unchanged` |
| answer-the-question | G1#39 | "Quick yes or no: is the weekly digest routine still on?" | — | `max_chars {n: 600}` |
| no-calendar-connected | G1#30 | "Make a Google Meet for tomorrow 10am with aden@example.com and send the invites." | — | `contains_none ["accounts.google.com/o/oauth2"]` |
| do-it-yourself | G1#37 | "Write me a five-bullet summary of what Lightbulb does, for a client email." | — | `tasks_unchanged` |
| no-false-done | G1#26 | "Email aden@example.com the summary you just wrote." | — | `contains_none ["sent it","I've sent","email sent"]` |
| use-what-was-given | G1#42 | "The bug: 'search returns nothing for DMs since Tuesday'. Write the one-line task title for it." | — | (judge only: must not ask for the bug description again) |
| short-ask-short-answer | G1#35 | "What's on the board for today, in one line?" | tasks: 3 todo | `max_chars {n: 600}` |
| right-task-only | G1#15 | "Cancel the ChatGPT plugin task only." | tasks: "Ship the ChatGPT plugin" (todo), "Ship the Claude plugin" (todo) | `task_status {title: "Ship the ChatGPT plugin", status: "cancelled"}`, `task_status {title: "Ship the Claude plugin", status: "todo"}` |
| no-overreach-on-edit | G1#27 | "Fix the typo in this sentence and change nothing else: 'Lightbulb conects your agents.'" | — | `contains_any ["connects"]` |
| blocked-asks-a-person | G1#11, #21 | (task) "Merge PR 1 after a Codex review; review credits are out." | — | — `needs: paid-workspace` |
| ask-with-options | G1#23 | (task) "Pick the Stripe account for invoices; two are connected." | — | — `needs: paid-workspace` |

The `good` and `bad` lines spell out what the G1 failure was. For example:
- count-open-tasks: good "States 12, from the board"; bad "States any other number, or a number with no check".

- [ ] **Step 1: Write the failing tests.**
  - `test_shipped_cases_load_and_every_id_is_unique`, which loads the real file and asserts at least 14 runnable cases.
  - `test_unknown_check_kind_names_the_line`.
  - `test_missing_good_is_refused`.
  - `test_paid_workspace_cases_are_loaded_but_marked`.
- [ ] **Step 2:** Run `cd apps/backend && uv run pytest tests/unit/scripts/test_agent_evals_cases.py -q`. Expected: FAIL, because the module is missing.
- [ ] **Step 3:** Write `cases.py`, `cases.jsonl` and `results/.gitignore`.
- [ ] **Step 4:** Run it again. Expected: PASS.
- [ ] **Step 5:** Commit with the message `evals: agent eval cases from the 42 real failures`.

### Task 2: Deterministic checks

**Files:**
- Create `apps/backend/scripts/agent_evals/checks.py`.
- Test: `apps/backend/tests/unit/scripts/test_agent_evals_checks.py`.

**Interfaces:**
- Produces `run_checks(checks: list[dict], reply: str, before: list[dict], after: list[dict]) -> list[str]`. It returns the failure reasons; an empty list means everything passed.
- `before` and `after` are task snapshots, each `{title, status}`.
- `contains_*` matching is case-insensitive.
- `tasks_unchanged` fails on any new title, removed title or status change.
- `task_status` fails when the title is missing from `after`, or has another status.

- [ ] **Step 1: Write the failing tests.** One per kind, for both pass and fail, plus `test_several_failures_are_all_reported`.
- [ ] **Step 2:** Run them and watch them fail.
- [ ] **Step 3:** Implement `checks.py`.
- [ ] **Step 4:** Run them again. Expected: PASS.
- [ ] **Step 5:** Commit with the message `evals: deterministic checks`.

### Task 3: The judge

**Files:**
- Create `apps/backend/scripts/agent_evals/judge.py`.
- Test: `apps/backend/tests/unit/scripts/test_agent_evals_judge.py`.

**Interfaces:**
- Produces `async judge(case: Case, reply: str, *, post) -> Verdict`.
  - `Verdict` is `{outcome: "pass"|"fail"|"error", reason: str}`.
  - `post(body) -> dict` is injected. The real one POSTs to `https://ai-gateway.vercel.sh/v1/chat/completions` with `AI_GATEWAY_API_KEY`.
- The prompt gives the judge `say`, `good`, `bad` and the agent's reply. It asks for JSON only: `{"pass": bool, "reason": "<one sentence>"}`.
- The model is a constant, `JUDGE_MODEL`. Choose it by listing `GET https://ai-gateway.vercel.sh/v1/models` at Step 3 and picking the newest Claude Sonnet. Record the choice in a comment.
- `temperature: 0`.

- [ ] **Step 1: Write the failing tests.**
  - `test_pass_json_is_a_pass`.
  - `test_fail_json_is_a_fail_with_reason`.
  - `test_unparseable_output_is_an_error_not_a_pass`.
  - `test_post_exception_is_an_error`.
  - `test_prompt_carries_good_and_bad_and_the_reply`.
- [ ] **Step 2:** Run them and watch them fail.
- [ ] **Step 3:** Implement `judge.py`. Pick `JUDGE_MODEL` from the live model list, using the dev key.
- [ ] **Step 4:** Run them again. Expected: PASS.
- [ ] **Step 5:** Commit with the message `evals: LLM judge over good and bad, errors never pass`.

### Task 4: The runner

**Files:**
- Create `apps/backend/scripts/agent_evals/__main__.py`.
- Test: `apps/backend/tests/unit/scripts/test_agent_evals_runner.py`.

**Interfaces:**
- Consumes `load_cases`, `run_checks` and `judge`.
- Produces `async run_case(case, api, agent, *, wait_s=240, quiet_s=20, sleep=asyncio.sleep, judge=judge.judge) -> dict`. The result is `{id, outcome, checks: [..], judge: Verdict, reply, seconds}`.
  - `api` is a small client: `get(path)`, `post(path, body)`, `patch(path, body)` over `API = "https://api-dev.thelightbulb.company/api/v1"`, with `Authorization: Bearer <fixture JWT>`.
  - `agent` is `{id, handle}`.
- **CLI:** `uv run python -m scripts.load.fixtures run --orgs 1 --users-per-org 1 -- uv run python -m scripts.agent_evals [--cases id,id] [--dry-run]`.
  - It reads `FIXTURES` (path to `fixtures.json`), like `mac_sim.py`.
  - It refuses unless `fixtures["api"]` is the dev API.
  - It picks the org's first agent, the CEO, through the roster lookup in `k6/agents.js:44-50`, reading `GET /messaging/rooms` and then `#general`'s members.
- **Per case:**
  1. Create the room with `POST /messaging/rooms {clientRoomId: uuid4, name: "eval-<id>-<ts>", kind: "channel", members: [agent, listenMode "mentions"]}`. The shape is in `k6/lib.js:167`.
  2. Seed the tasks with `POST /teams/issues {title, status}`.
  3. Post each `before` message, then the case message with `mentions: [{kind: "agent", id}]`. Find the message POST body shape in `k6/agents.js:80-90`.
  4. Poll `GET /messaging/rooms/{id}/messages?afterSeq=<prompt seq>&limit=100` every 5 s. Collect messages whose author kind is `agent`. Stop at `quiet_s` with no new agent message after the first one, or at `wait_s`.
  5. Snapshot tasks before seeding, after seeding and at the end, using `GET /teams/issues?limit=500`. Read them as `{title, status}` for the titles the case touches, or for all titles under `tasks_unchanged`.
  6. Set the outcome:
     - no reply: `fail`;
     - any check failure: `fail`;
     - otherwise the judge's outcome.
  7. Archive the seeded tasks with `POST /teams/issues/{id}/archive`.
- **Summary.** `results/<YYYY-MM-DD>.md` holds three things:
  - the pass rate over runnable cases;
  - a table with id, source, outcome, seconds, and reason (first check failure or the judge's reason);
  - the skipped `paid-workspace` ids.

  Raw rows go to `results/raw/<date>.jsonl`.

- [ ] **Step 1: Write the failing tests, against a fake `api`.**
  - `test_no_reply_within_wait_is_a_fail_and_the_run_goes_on`.
  - `test_a_failed_check_beats_a_passing_judge`.
  - `test_all_agent_messages_after_the_prompt_are_graded_together`.
  - `test_seeded_tasks_are_archived_even_when_the_case_fails`.
  - `test_refuses_a_non_dev_api`.
  - `test_summary_counts_errors_apart_from_fails_and_lists_skips`.
- [ ] **Step 2:** Run them and watch them fail.
- [ ] **Step 3:** Implement `__main__.py`.
- [ ] **Step 4:** Run the whole backend unit suite for scripts: `uv run pytest tests/unit/scripts -q`. Expected: PASS.
- [ ] **Step 5:** Commit with the message `evals: runner over a dev fixture workspace`.

### Task 5: First real run (baseline)

- [ ] **Step 1:** Sign in with `aws sso login --profile isol8-admin` if the session has expired. Then:

  ```
  uv run python -m scripts.load.fixtures setup --orgs 1 --users-per-org 1
  ```

  This waits until the workspace is ready.
- [ ] **Step 2:** Export `AI_GATEWAY_API_KEY` from `lightbulb/dev/vercel-ai-gateway`, then run all runnable cases through `fixtures run`. Expected: a `results/<date>.md` with a pass rate over 14 cases.
- [ ] **Step 3:** Read each failure's reply. Correct any case whose checks are wrong, rather than the agent's fault, and record it in the ledger.
- [ ] **Step 4:** Tear down with `uv run python -m scripts.load.fixtures teardown`.
- [ ] **Step 5:** Commit `results/<date>.md` with the message `evals: first baseline run`.

## Not in this slice

- **Task-run cases** (`needs: paid-workspace`). These need a dev workspace on a paid plan, or a test-only entitlement.
- **Cases that need real connectors** (Gmail, PostHog, Stripe). That ties into the connector-health plan.
- **CI.** Each run costs model tokens and a microVM, so it stays manual, before and after agent changes.
