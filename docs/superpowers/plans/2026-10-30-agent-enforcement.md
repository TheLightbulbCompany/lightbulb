# Agent enforcement, slice 1: ask a person, report blocked, count from the board

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give workspace agents two named tools, so that two things happen in code instead of depending on the agent remembering:
- a blocked agent's question reaches a person's Needs you and wakes the agent when it is answered;
- a stated count of tasks comes from the board, not from memory.

**Why (G1, 42 real failures):**
- 12 failures came from an agent stating something it never checked. One example: "only 3 open cards" when 55 were open.
- 10 were stalls or waits nobody was told about. The skill tells a blocked agent to leave a comment (`skills-src/paperclip/references/heartbeat.md:196-220`), and nobody reads comments.
- Paperclip already has issue-thread interactions. An `ask_user_questions` interaction with `continuationPolicy: "wake_assignee"` shows in Activity as "has a question for you" (`apps/backend/core/services/activity.py:1172-1246`) and wakes the assignee when answered.
- Agents don't use them, because the only route is a hand-built raw API call. Lesson already recorded at `plugins/lightbulb/__init__.py:529-534`: one named tool is what the model reaches for.

**Architecture:** Two new tools in the Lightbulb Hermes plugin, `ask_person` and `lightbulb_board`. They follow `request_approval`'s pattern exactly:
- auth comes from the profile's secret scope (`_scope_secret`);
- calls go through `_paperclip_call`;
- errors come back as short strings, never tracebacks.

Then the skill text points blocked and decision moments at `ask_person`, and counting moments at `lightbulb_board`. No Paperclip fork change and no backend change.

**Tech stack:** Python 3 (Hermes plugin), pytest. Tests run with:

```
cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests -q
```

That is the command from `.github/workflows/infra-ci.yml:69`.

**Spec:** `docs/research/2026-10-30-production-agents-decision.md` §0 "What this means", items 1–3. The spec is on branch `docs/specialized-agents-research`, commit e8264f85c. Supporting tracks G1 and G2 are in `docs/research/2026-10-30-production-agents/` on that branch.

## Global Constraints

- Work only in this worktree, `feat/agent-enforcement`, based on `origin/main` 0f10ae6bc. Never `git stash`.
- Tool results never contain a traceback, URL, token or HTTP body beyond what `_http_error_text` already allows, which is 200 characters of the server's `error` field.
- Every Paperclip write carries `X-Paperclip-Run-Id` when `PAPERCLIP_RUN_ID` is in scope. Use `_paperclip_call(..., run_id)`.
- Interaction creates carry a deterministic `idempotencyKey`, so a retried tool call never makes a second card.
- Register each new tool in **both** `register()` and `plugins/lightbulb/plugin.yaml` `provides_tools`.
- Copy style: plain words, no "please", no exclamation marks, matching the existing schema descriptions.

## Review Focus

1. **A retried `ask_person` call with the same question** returns the same interaction and creates no second card. This is tested in Task 1 via the idempotency key.
2. **`ask_person` called from a room turn with no task** returns a sentence telling the agent to ask in its reply, and makes no network call. Tested in Task 1.
3. **`lightbulb_board` on a board with more issues than one page** must report the true total, not the page size. That exact error caused the "3 vs 55" failure. Tested in Task 2.
4. **`blocked: true` when the task is already `blocked`** still creates the question and does not fail on the PATCH. Tested in Task 1.
5. **Paperclip returns 403 or 422 on the interaction create**: the agent gets "Question not created: HTTP 4xx: …" and is not told it was asked. Tested in Task 1.

---

### Task 1: `ask_person`, a structured question or a blocked report that reaches Needs you

**Files:**
- Modify: `apps/infra/hermes/plugins/lightbulb/__init__.py`. Add the schema and handler after `_request_approval`, around `:658`, and register them in `register()`.
- Modify: `apps/infra/hermes/plugins/lightbulb/plugin.yaml`. Add `ask_person` to `provides_tools`.
- Test: `apps/infra/hermes/tests/test_plugin_ask_person.py` (new). Copy the `paperclip` fixture shape from `tests/test_plugin_request_approval.py:25-58`, and add `PAPERCLIP_TASK_ID` and `PAPERCLIP_RUN_ID` to the scope.

**Interfaces:**
- Consumes: `_scope_secret(name) -> str`, `_paperclip_call(method, url, key, body=None, run_id=None)`, `_http_error_text(exc) -> str`, `_app_url() -> str`. All exist in `__init__.py`.
- Produces: `ASK_PERSON_SCHEMA: dict` and `_ask_person(args: dict, **_kw) -> str`. The function returns JSON `{"interactionId", "link", "blocked": bool}` on success, or a plain sentence on refusal.

**Schema:** `name: "ask_person"`. Parameters:
- `question` (string, required): one sentence;
- `options` (array of strings, 2–4 items, optional): the likely answers, recommended first;
- `details` (string, optional): what the person needs to know to answer;
- `task` (string, optional): an identifier like `WAL-6`, defaulting to the run's `PAPERCLIP_TASK_ID`;
- `blocked` (boolean, optional): true when the work cannot continue until the answer comes.

The description must say three things:
- the question shows in the person's Needs you, and answering it wakes you with the answer;
- use it instead of guessing and instead of a comment when you are blocked;
- in a room conversation, ask in your reply instead.

**Wire format.** Verify field names against the fork at `pc:server/src/services/issue-thread-interactions.ts` (Paperclip fork at `/Users/prasiddhaparthsarthy/Desktop/paperclip-fork.nosync`, commit `6eb76876`, read with `git show`) and against `skills-src/paperclip/references/api-reference.md:700-800`.

```
POST /api/issues/{issueId}/interactions
{"kind": "ask_user_questions", "resolverPolicy": "board_only",
 "continuationPolicy": "wake_assignee",
 "idempotencyKey": "ask:{issueId}:{sha256(question)[:16]}",
 "title": <question, clipped to 100 chars>,
 "payload": {"version": 1, "supersedeOnUserComment": true,
             "questions": [{"id": "q1", "prompt": <question>, "selectionMode": "single", "required": true,
                            "options": [{"id": "o1", "label": <options[0]>}, ...]}],
             "detailsMarkdown": <details, if given>}}
```

With no `options`, send a single free-text option, in whatever shape the fork's validator accepts for free text. Find it in the fork's validator.

When `blocked` is true, first send:

```
PATCH /api/issues/{issueId}
{"status": "blocked",
 "unblockDescriptor": {"owner": {"agentId": <me.id>},
                       "action": "Resume when the person answers: <question>"}}
```

`me` comes from `GET /api/agents/me`. Self-ownership is the only shape the server accepts (`heartbeat.md:194-200`).

A 422 on that PATCH means the task is already blocked. Treat it as success and keep going.

- [ ] **Step 1: Write the failing tests** in `tests/test_plugin_ask_person.py`.
  - `test_question_with_options_creates_one_ask_user_questions_interaction_on_the_runs_task`. Assert:
    - exactly one POST, to `/api/issues/{ISSUE}/interactions`;
    - `body["kind"] == "ask_user_questions"` and `body["continuationPolicy"] == "wake_assignee"`;
    - the option labels, in order, equal the input;
    - the run id passed is the scope's `PAPERCLIP_RUN_ID`;
    - the output JSON has `blocked: False` and a `link` that starts with `https://dev.example/`.
  - `test_same_question_twice_uses_the_same_idempotency_key`. Two calls, then assert the two POST bodies' `idempotencyKey` values are equal.
  - `test_blocked_sets_the_task_blocked_self_owned_before_asking`. Assert:
    - the PATCH comes before the POST;
    - the PATCH body has `status == "blocked"` and `unblockDescriptor.owner == {"agentId": ME}`.
  - `test_blocked_when_already_blocked_still_asks`. Make the PATCH raise a 422, then assert the POST still happens and the output has `blocked: True`.
  - `test_room_turn_without_a_task_asks_in_the_reply_and_calls_nothing`. Remove `PAPERCLIP_TASK_ID` from the scope and pass no `task`. Assert there are no calls, and the output contains "ask in your reply".
  - `test_refused_create_says_not_created`. Make the POST raise a 403 with `{"error": "Forbidden"}`, then assert the output starts with "Question not created: HTTP 403".
  - `test_more_than_four_options_is_refused_before_any_call`.
- [ ] **Step 2: Run the tests and see them fail.** Run `python -m pytest tests/test_plugin_ask_person.py -q` with the command prefix above. Expected: an `AttributeError`, `_ask_person` not defined.
- [ ] **Step 3: Implement `ASK_PERSON_SCHEMA` and `_ask_person`.** Resolve `task` the way `_request_approval` resolves `tasks`, through `GET /api/issues/{ref}`; a 404 returns "Question not created: task {ref} was not found." The link is `f"{_app_url()}/{prefix}/issues/{identifier}"`, where `prefix` comes from `GET /api/companies/{companyId}`, the same as `request_approval`. Register it with emoji "❓".
- [ ] **Step 4: Confirm the fork's interaction validator** accepts the body, by reading `issue-thread-interactions.ts` and its validator in `packages/shared`. Adjust the field names if needed, and the test bodies with them.
- [ ] **Step 5: Run the full Hermes suite** with the command above. Expected: all pass.
- [ ] **Step 6: Commit** with message `hermes: ask_person tool, a question or blocked report in Needs you that wakes the agent`.

### Task 2: `lightbulb_board`, counts and lists that come from the board

**Files:**
- Modify: `apps/infra/hermes/plugins/lightbulb/__init__.py` and `plugin.yaml`, the same places as Task 1.
- Test: `apps/infra/hermes/tests/test_plugin_board.py` (new), with the same fixture.

**Interfaces:**
- Consumes: the same helpers as Task 1.
- Produces: `BOARD_SCHEMA: dict` and `_board(args: dict, **_kw) -> str`. The function returns JSON `{"total": int, "issues": [{"identifier", "title", "status", "assignee"}], "truncated": bool}`. The list has at most 50 rows; `total` counts every match.

**Schema:** `name: "lightbulb_board"`. Parameters:
- `status`: an array drawn from `backlog, todo, in_progress, in_review, blocked, done, cancelled`. The default is every status except `done` and `cancelled`.
- `assignee`: `"me"`, an agent name, or omitted for everyone.
- `query`: a string to match in titles.

The description: "Count or list the workspace's tasks from the board itself. Use it before stating any number of tasks or any task's status; never state one from memory."

**Endpoint:** `GET /api/companies/{companyId}/issues?status=a,b&assigneeAgentId=…&q=…`.

Before writing the code, read the fork's list route (`pc:server/src/routes/issues.ts`, searching for the companies/:companyId/issues GET) to find two things:
- whether it paginates, and the parameter names and maximum page size;
- the name of the free-text search parameter.

If it paginates, page until the end, up to a hard cap of 2,000 rows. `total` is the number of rows read, and `truncated` is true only if the cap was hit. Resolve an agent name to an id with `GET /api/companies/{companyId}/agents`.

- [ ] **Step 1: Write the failing tests.**
  - `test_default_counts_every_open_status_across_all_pages`. The fake returns 55 open issues over the fork's real page size. Assert `total == 55`, `len(issues) == 50` and `truncated is False`.
  - `test_assignee_me_filters_by_my_agent_id`. Assert that `assigneeAgentId=ME` is on the request URL.
  - `test_unknown_assignee_name_is_refused_without_listing`.
  - `test_status_outside_the_enum_is_refused`.
  - `test_board_unavailable_returns_a_sentence_not_a_traceback`. The GET raises a `URLError`, and the output equals "The board is unavailable right now. Do not state a number."
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Read the fork's list route** and record the page parameters in a comment above `_board`, citing `issues.ts:<line>`.
- [ ] **Step 4: Implement `_board`** and register it with emoji "📋".
- [ ] **Step 5: Run the full Hermes suite.** Expected: all pass.
- [ ] **Step 6: Commit** with message `hermes: lightbulb_board tool, task counts and lists from the board, never from memory`.

### Task 3: Point the skills at the tools

**Files:**
- Modify: `apps/infra/hermes/skills-src/paperclip/references/heartbeat.md:213-218`. In "If the block needs a human decision", replace "Say so in a comment … the comment is what gets a human's attention" with: call `ask_person(question=…, options=[…], blocked=true)`. That parks the task as blocked, with you as owner, and puts the question in the person's Needs you; the answer wakes you. Keep the paragraph about self-ownership above it.
- Modify: `apps/infra/hermes/skills-src/paperclip/references/interactions.md`. Add a section "When you need an answer, not a sign-off: ask_person" after the approval section. Approvals are for sign-offs; `ask_person` is for a question with likely answers. Give one example call.
- Modify: `apps/infra/hermes/skills-src/paperclip/SKILL.md:80` and `apps/infra/hermes/skills-src/lightbulb/SKILL.md`, in the rules list near `:110`. Add two one-line rules:
  - "Blocked, or unsure between options: `ask_person`; never guess, never only a comment."
  - "Any count or status of tasks: `lightbulb_board` first."
- Modify: `apps/infra/hermes/skills-src/paperclip/MAINTENANCE.md` and `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md`. Add the new sections to their maps, in the same format as the existing rows (for example `MAINTENANCE.md:73`).
- Test: `apps/infra/hermes/tests/test_paperclip_skill.py`, if it exists (`git grep -l "heartbeat.md" apps/infra/hermes/tests`). Otherwise extend the nearest skill-text test. Assert that `heartbeat.md` no longer contains "the comment is what gets a human's attention" and does contain "ask_person".

- [ ] **Step 1: Write the failing skill-text assertions.**
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Edit the five skill files** as listed.
- [ ] **Step 4: Run `apps/infra/hermes/build-skills.sh`**, if the tests check its output, then the full Hermes suite. Expected: all pass.
- [ ] **Step 5: Commit** with message `skills: blocked and unsure go to ask_person; counts come from lightbulb_board`.

---

## Not in this slice (separate plans, in this order)

1. **A test set from the 42 failures.**
   - A plain-language cases file: what was asked, what went wrong, what good looks like. Aden can write most of it from G1's case table.
   - Then a runner against a dev workspace.
   - We measure before and after this slice.
2. **Delegated results no longer dropped.** `bridge/followups.py:188-192` drops results for sessions without a room turn. Post them as a comment on the delegating task and wake its assignee.
3. **Connector health.** A periodic check per connected Google account (and the other CLI connectors) that raises a Needs-you card when auth breaks.
4. **Paperclip engine fixes** in the fork (F2 §C, seven fixes, including re-attach and `remindAfter`).
5. **Separating agents inside a workspace:** credentials, sandbox and tool lists per agent.
6. **Check before "done" for outside actions** (send, post, file). This is deferred until the test set shows which actions fail. Hermes's `verify_on_stop` targets code, not business actions.
