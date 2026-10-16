# Ideas: deliverable — design

Date: 2026-10-15. Status: for owner review. Research behind it: `docs/research/2026-10-15-ideas-execution-system.md` and `2026-10-15-ideas-agent-playbook.md`.

Out of scope here, owned by the other track: the Paperclip upgrade, turn slots and parallel runs. Later, by owner decision: Stripe Issuing, a phone capability, Microsoft 365 write, model choice.

## 1. Goal

When a person presses Let's do it, the agent either delivers the idea or ends in one of three visible states: done with proof, waiting on one clear ask in Needs you, or stuck with a reason in the DM. Nothing silently stops.

## 2. What changes

### 2.1 Each idea says what it needs and what done means

Each idea YAML gets two fields. `needs`: the services it uses, each marked read or write, the websites it works in the browser, and the steps only a person can do. `done_means`: one line a check can read. Both are rendered into the Work item's description under "Needs" and "Done means". The person's DM message stays the plain prompt it is today.

### 2.2 The agent checks needs first and asks once

Before any work, the agent compares `needs` with `connected_integrations`. If anything is missing it asks once, in one place, for all of it: a Needs-you item naming each service with its Connect link, or, for a write it cannot get (Outlook send), what it will hand over as text instead. It keeps doing the parts that need nothing. This is a rule in `references/ideas.md`.

### 2.3 Connecting a service resumes the work

When a connection completes, the backend looks at the workspace's open idea starts. For each whose `needs` include that provider, it posts one comment on the idea's task ("Gmail is connected") or on the project's open agent tasks. The comment wakes the assignee through the normal path. No approval card, no extra step for the person.

### 2.4 Codes from Work-started runs reach the person

A run that Paperclip starts for a task bound to a DM already runs inside that DM. The bridge now passes that room to the sign-in and code prompts, so "go to the Browser panel" lands in the DM and the run waits as a chat turn would. A run with no bound room (a task delegated to another agent) still cannot wait; the instruction tells the agent to hand browser steps back to the DM in that case.

### 2.5 A step only a person can do becomes a task for the person

The helper gets `--assignee-user <userId>`. The Ideas instruction says: identity checks, signatures, verification videos, phone-only vendors and payments outside the rails become one task assigned to the person, with the prepared text, number or amount in it. It shows in their Inbox. The agent's own task waits on it as a blocker, so finishing it wakes the agent.

### 2.6 Project tasks are filed in the project, as todo

The helper's `create` and `subtask` get `--project <id>`. The Ideas instruction says: for a project idea, file each task inside the project with `--status todo`, assigned, and leave a one-line "next action" comment after each unit of work. The Paperclip skill's "backlog is the normal case" gets an exception for idea work.

### 2.7 Tasks from ideas run the Paperclip way (owner, 2026-10-16)

A task idea creates an assigned **`todo`** task, and Paperclip starts the run at once, the way it starts any assigned task. A project idea creates the project and one assigned `todo` task inside it that carries the idea, so the lead starts on it and files the rest. The person's DM message is still posted as their record, with the Work link, but it no longer wakes the agent: one run, not two. A quick idea stays as today: the DM message is the work. No backlog, no timer.

### 2.8 Done means proof

Before setting `done`, the agent posts one line per deliverable with a link to the thing that now exists, and checks it against `done_means`. For a project idea, the lead reviews each child task's proof before closing the project. An idea with no proof is not done; it is stuck, and the agent says why in the DM.

### 2.9 The agent knows the box's limits

Three lines in the Ideas instruction: turns are 30 minutes, so work in units and leave a next-action note; the team mailbox sends 50 a day; there is no phone yet.

### 2.10 Stripe Projects lands on AgentCore

`land_credentials` stops refusing. It keeps its three writes in order: Secrets Manager, the integrations row, then each roster agent's profile `.env` through `patch_profile_env_and_config`, the same path connector secrets use. The Projects client is checked against the public API version (it was written on the preview header). The prod switch `STRIPE_PROVISIONING_ENABLED` is turned on by the owner once a dev provision has reached `active`.

## 3. Mechanisms

- Fields: `needs` is a list of `{service, access: read|write}` plus `browser: [sites]` and `person: [steps]`; `done_means` is one string. Validated like the other fields; every catalog idea must have both.
- Render: `ideas.py` adds "Needs" and "Done means" sections to the task or project description it already writes. Suggested ideas (`suggest_idea`) may carry `needs` and `done_means`; without them the sections are omitted.
- Connect wake: `apply_connection` success → `open_idea_starts(owner)` → match provider against each idea's `needs` → `add_issue_comment` on the task, or on each open agent task of the project. Idempotent per (start token, provider).
- Room for codes: `_run_paperclip_session(identity, request, conversation)` passes the conversation's room id into `prompt_scope` and the turn context, replacing the two `None`s at `turn.py:3804-3806`.
- Statuses at start: the task body carries `status: "todo"`; the project gets a first task `{title, description: work_description, status: "todo", assigneeAgentId, projectId, idempotencyKey: <key>:task}`. `create_human_message` takes `wake=False` for a tracked start, which stores and shows the message but skips the agent dispatch; a quick idea keeps the default.
- Helper flags: `--assignee-user` sends `assigneeUserId`; `--project` sends `projectId` on create and subtask.
- Landing: `land_credentials` → `integration_secret_store.put`, `integrations_repo` pointer row, then `patch_profile_env_and_config(owner, agent, env, {})` for every roster agent, then the bridge's profile re-pull as for connectors.

## 4. How we confirm it works

Mechanics on dev; real connectors on the founder workspace with approvals on. Five ideas first: chase-overdue-invoices, answer-leads-fast, inbox-triage, monthly-receipts-package, seo-audit-fixes. Three trials each. Every run scored yes/no on: started within 2 minutes; left backlog and carries a next-action comment after each run; never sat idle more than 5 minutes without a Needs-you item; every send or payment had an approval; `done_means` holds with proof links; the Work item closed and the DM got the result; no duplicate side effect after a forced stop mid-run (one trial per idea). An idea ships at 3 of 3 on the first six and a clean seventh.

Accounts the owner creates: a test Google account with its own Cloud app (Gmail, Calendar), Stripe test mode, a QuickBooks sandbox, one small GitHub repo with a deployed site.

## 5. Open decisions

1. The nine Person ideas: hide them until the rails exist, or show them with a "mostly you" line. (Playbook §3 lists them.)
2. A one-agent workspace has no second reviewer for project proofs; the person reviews, or the proof rule alone is enough.
