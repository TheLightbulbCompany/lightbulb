# Software projects: from nothing to live

A new app, site, tool or dashboard someone asks for: `references/apps.md`.

Read this when asked to build something new and put it online (an app, a site,
a service). For work on an existing repo, start at **Repository** and skip the
step that creates it: every other rule from there on still applies.

## First move

Confirm scope with the person in the room: who it is for, the first thing it
must do, and anything they already chose (stack, host, domain). Write a short
plan as a document on a task (`references/chat-vs-document.md`), get it
confirmed, and open one Paperclip issue per piece (repo, first feature, deploy,
CI). The paperclip skill owns issues, confirmations and monitors.

## Check what is connected before promising anything

| Service | Check | Missing → ask the owner |
|---|---|---|
| GitHub | `gh auth status` | Connections → GitHub (one click) |
| Vercel | `[ -n "$VERCEL_TOKEN" ] && echo set` (the `vercel` skill appears too) | Connections → Vercel: they paste an Account Token |
| AWS | `aws sts get-caller-identity` | Connections → AWS: they launch a connector stack at a tier they pick |

Cloudflare, Sentry and PostHog arrive as MCP tools; there is no database or
hosting connection. Ask for everything missing in one message, then keep going.

## Default stack

A web app defaults to **Next.js on Vercel**: one pasted token, and every pull
request gets a preview. Use AWS only when the owner asks or the job needs what
Vercel cannot do (long-running workers, queues, a VPC). If the owner names a
stack or host, that is the choice. You have `git`, `gh`, `aws`, `node`/`npm`/
`npx`, `uv`, `python3`; `npm i -g` fails (use `npx` or `uv tool run`), and
there is no Docker daemon, so container images build in CI.

## Repository

**New project only.** Create the repo under the owner's account or org with a
first commit, so there is a branch to protect and to open pull requests
against: `gh repo create OWNER/REPO --private --add-readme`. Without
`--add-readme` the repo is empty and has no `main`. The first branch takes the
account's configured default name, so read it
(`gh repo view OWNER/REPO --json defaultBranchRef`) and, if it is not `main`,
rename it: `gh api -X POST repos/OWNER/REPO/branches/NAME/rename -f new_name=main`
(the rename also moves the default).

**Every repo, new or existing.** Protect `main` and work only on branches and
pull requests; never push an untested change to `main`. The mechanics
(creation, branch protection, Actions secrets, workflows) are in Hermes's
`github` skill, `references/repo-management.md`.

## Keep the workspace small

Your home directory is saved at the end of every turn and restored file by
file on a cold start, so a big tree slows every cold start. A file you delete is
removed from the saved copy at your next save.

- One clone per repository at `$HERMES_HOME/.run/<repo>`, reused across
  conversations (not in this conversation's folder), partial or shallow:
  `gh repo clone OWNER/REPO "$HERMES_HOME/.run/REPO" -- --filter=blob:none`
  (or `--depth 1`). Without the folder argument it clones into the current
  folder, which is this conversation's.
- Branches get a worktree, not a second clone, under `$HERMES_HOME/.scratch/wt`
  (unlike `/tmp`, your file tools can write there). `.scratch/` is never saved,
  but it is on the one ~4 GB disk every teammate shares, and it is emptied only
  when the microVM is recycled; your shell can write `/tmp`, which is the same
  disk. So keep **one worktree per task**. A Paperclip task's is made for you at
  `$HERMES_HOME/.scratch/wt/task-<issueId>/REPO` on branch `task/<first 8 of the
  issue id>`: work there, continue from its commits on the task's next run, and
  leave it alone at the end (it is removed when the task closes or its merged branch
  is deleted). For work outside a Paperclip task, make your own and remove it when
  the work is pushed. Your shell starts in the conversation's folder, so name the
  clone in every command (`C="$HERMES_HOME/.run/REPO"`):
  `git -C "$C" fetch origin main`, then
  `git -C "$C" worktree add "$HERMES_HOME/.scratch/wt/REPO" -b BRANCH origin/main`;
  at the end, `git -C "$C" worktree remove --force "$HERMES_HOME/.scratch/wt/REPO"`.
  After a cold start it is gone: keep the branch pushed, `git -C "$C" worktree prune`,
  and add it again from `BRANCH`.
- Small projects may install what they need. Do not install dependencies or run
  test suites for a large repository (TheLightbulbCompany/lightbulb's one install
  is 2.4 GB and stalled the workspace twice): push the branch and read the CI result.
- `node_modules`, any `*venv` folder (`.venv`, `.docx-venv`), `.next`, `dist`
  and `build` are never saved: in a small project, rerun `npm ci` / `uv sync`
  after a cold start.
  After a cold start only this conversation's `work/` folder is on disk; the
  other conversations' folders are not.
- Push a work-in-progress branch before any long command: a turn cut short
  loses every file it had not saved.
- `~/.gitconfig` is not saved. At the start of each session set `user.name`
  and `user.email` (the account's noreply address, from `gh api user`) with
  `git config --global`, then run `gh auth setup-git`.

## Deploy to Vercel

Run the CLI as the `vercel` skill says (a pinned `npx --yes vercel@…`; `vercel`
below means that; it reads `VERCEL_TOKEN` itself). Add `.vercel` to
`.gitignore`. The link is stored in the folder, so run
`vercel link --yes --project NAME` in every new or recreated worktree slot
before any other `vercel` command.

- `vercel git connect` gives a preview per pull request and production only
  from `main`. It needs the Vercel GitHub app on the owner's account; if it
  refuses, ask them to install it, or deploy from Actions with `VERCEL_TOKEN`,
  `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` as repo secrets (`printf %s "$VERCEL_TOKEN" | gh secret set VERCEL_TOKEN`).
- Env vars: `printf %s "$VALUE" | vercel env add NAME production` (and
  `preview`). Never commit a secret or a filled `.env`; commit `.env.example`.

## AWS within the connector's limits

**ViewOnly** and **ReadOnly** inspect, never change. **PowerUser** creates most
resources (S3, Lambda, DynamoDB, ECS, RDS, CloudFront) but nothing in IAM, so
`cdk bootstrap`, most CDK/SAM/Serverless stacks and any new Lambda execution
role fail. `AccessDenied` is the tier, not a bug: tell the owner what needs
them. Anything that costs money or is hard to undo (a database, a domain,
deleting data) needs the owner's approval first.

## Continuous integration

The first pull request adds a workflow that runs lint, type check and tests on
every pull request and on `main`, read-only, cancelling superseded runs. Pin
actions to their current major (`gh api repos/actions/checkout/releases/latest`)
and make it a required check once it has passed on real pull requests.

Take the commands from the repo, not from this example: the package manager
from the lockfile (`package-lock.json` npm, `pnpm-lock.yaml` pnpm,
`uv.lock` uv), and each step from a script that exists and passes locally.
A fresh scaffold usually has no `test` script: add a first real test and the
script before the CI step that runs it, or leave that step out and say so.
A Node project on npm looks like this:

```yaml
name: ci
on: { pull_request: {}, push: { branches: [main] } }
permissions: { contents: read }
concurrency: { group: "ci-${{ github.ref }}", cancel-in-progress: true }
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: npm }
      - run: npm ci
      - run: npm run lint
      - run: npx tsc --noEmit
      - run: npm test
```

## Pull-request review

Every pull request is reviewed before it merges, by something other than the
turn that wrote it. Set this up with the first pull request.

1. **Find what reviews this repo already.** Look for a bot's review on recent
   pull requests (`gh pr list --state merged --limit 5 --json number,reviews,comments`)
   and ask the owner whether they already pay for a reviewer (Codex with
   ChatGPT, GitHub Copilot, another).
2. **They have one: help them switch it on.** Installing a GitHub app and
   linking an account are the owner's clicks. Tell them exactly where, and
   have them pick the setting that reviews every pull request on its own: a
   reviewer tied to a person's account ignores a request posted by an agent.
3. **They have none: review on a routine.** Create a Paperclip routine
   assigned to yourself (paperclip skill, `references/routines.md`) named
   "Review open pull requests". Its description is the contract, and it names
   the repository, because a routine run starts in its own empty folder: list
   with `gh pr list --repo OWNER/REPO --state open`; for each non-draft pull
   request whose head commit you have not reviewed, read the diff
   (`gh pr diff N --repo OWNER/REPO`) with the `requesting-code-review` skill
   and post ONE review with
   `gh pr review N --repo OWNER/REPO --comment --body-file FILE` that names
   the commit and lists each finding as `file:line` plus the fix, or says you
   found none. Never approve, never merge, never review one commit twice, and
   finish quietly when nothing is open.
   - Trigger: ask the owner to add a GitHub pull-request trigger to it (Teams
     → Automations → the routine → Triggers → Add → a connected app). Until
     they do, add a `schedule` trigger, hourly at the tightest: every run
     costs a turn even when nothing is open.
4. **Wait for the review.** A pull request is ready when the review of its
   HEAD commit is clean and CI is green. If the review has findings, fix the
   real ones, push once with all the fixes, and wait for the next review.
   Stop after three rounds and tell the owner what is still open. The owner
   merges unless they told you to.

If the owner wants a reviewer that runs in GitHub Actions instead: trigger on
`pull_request` with no `paths:` filter, grant `contents: read` and
`pull-requests: write`, and take the credential through the `secrets` tool. A
pull request that changes the reviewer's own workflow cannot test it, and a
green check is not proof a review happened: confirm one was posted.

## Verify and report

Merged is not deployed. Before saying something is live, confirm the deploy
finished (`vercel inspect <url>`) and fetch the live URL for something from the
change; wait on CI and deploys with a monitor (paperclip skill). Report with
links: the pull request, the live URL (never `localhost`), how to check it.

## Code knowledge graph

A code graph (what calls what, where a symbol lives) lets you answer "what
does this change touch" without reading the whole repo. Never build or keep it
in your home directory: it would be saved and restored on every turn.

- **Keep it current in CI.** In the same pull request as CI, add a workflow
  that runs on every push to `main`, builds the graph with
  `uvx --from graphifyy==0.9.46 graphify update . --no-cluster` (code only, no model
  calls, no key; pin an exact version, the job can write to the repo), gzips `graphify-out/graph.json`, and uploads it as the asset
  of one fixed pre-release named `code-graph`, replacing the previous asset.
  Give it `contents: write`, and never commit the graph: a commit to `main`
  can trigger the deploy. Mark the release pre-release and never latest.
- **Use it before you change code.** Download it into a scratch folder:
  `gh release download code-graph -R OWNER/REPO -p graph.json.gz -D /tmp/graph --clobber && gunzip -f /tmp/graph/graph.json.gz`,
  then query it with
  `uvx --from graphifyy==0.9.46 graphify query "<question>" --graph /tmp/graph/graph.json`.
  No release yet: skip it and read the code.

## A database, hosting or another paid service

Read the provisioning catalog first (`references/money.md`). If it offers the
service, request it there and build against the variable names meanwhile. If
it offers nothing that fits, tell the owner, recommend one provider
with the reason, and ask them to create the account and connect it in
Connections or give you the credential through the `secrets` tool. Never
invent a credential, never sign up on the owner's behalf.
