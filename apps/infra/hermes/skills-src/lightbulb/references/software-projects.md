# Software projects: from nothing to live

Read this when asked to build something new and put it online (an app, a site,
a service). For work on an existing repo, the rules from **Repository** on hold.

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

Create it under the owner's account or org (`gh repo create --private`),
protect `main`, and work only on branches and pull requests; never push an
untested change to `main`. The mechanics (creation, branch protection, Actions
secrets, workflows) are in Hermes's `github` skill, `references/repo-management.md`.

## Keep the workspace small

Your home directory is saved at the end of every turn and restored file by
file on a cold start, so a big tree slows every cold start. **Deleting does not
shrink it**: a removed file stays saved and returns on the next cold start.

- One clone per repository at `$HERMES_HOME/.run/<repo>`, reused across
  conversations (not in this conversation's folder), partial or shallow:
  `gh repo clone OWNER/REPO -- --filter=blob:none` (or `--depth 1`).
- Parallel branches get worktrees (`git worktree add`), not second clones.
  When the pull request merges or closes, `git worktree remove` and
  `git worktree prune`; if one reappears after a cold start, remove it again.
- `node_modules`, `.venv`, `.next`, `dist` and `build` are never saved: rerun
  `npm ci` / `uv sync` after a cold start.
- Push a work-in-progress branch before any long command: a turn cut short
  loses every file it had not saved.
- `~/.gitconfig` is not saved. At the start of each session set `user.name`
  and `user.email` (the account's noreply address, from `gh api user`) with
  `git config --global`, then run `gh auth setup-git`.

## Deploy to Vercel

Run the CLI as the `vercel` skill says (a pinned `npx --yes vercel@…`; `vercel`
below means that; it reads `VERCEL_TOKEN` itself). `vercel link --yes` once,
and add `.vercel` to `.gitignore`.

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

## Automated pull-request review

Self-review each pull request with Hermes's `requesting-code-review` skill.
When the owner wants an AI reviewer on the repo:

- Trigger on `pull_request` (`opened, synchronize, reopened, ready_for_review`),
  no `paths:` filter: a required check that never starts blocks the PR.
- Pin the action's current major and read its inputs from the latest release.
  Grant `contents: read`, `pull-requests: write`. The credential is the
  owner's: ask with the `secrets` tool, store it as a repo secret.
- A pull request that adds or changes the reviewer cannot test it (review
  actions commonly refuse a workflow that differs from the default branch):
  merge, then judge it on the next ordinary pull request.
- A green check is not proof a review happened: confirm a review was posted.
  Require the check only after several real reviews. Prefer GitHub-hosted
  runners; a self-hosted runner's home keeps stale config between jobs.

## Verify and report

Merged is not deployed. Before saying something is live, confirm the deploy
finished (`vercel inspect <url>`) and fetch the live URL for something from the
change; wait on CI and deploys with a monitor (paperclip skill). Report with
links: the pull request, the live URL (never `localhost`), how to check it.

## Code knowledge graph

Only when the owner asks, and never in your home directory (it would be saved
every turn). Shallow-clone into `/tmp` and run
`uv tool run --from graphifyy graphify update . --no-cluster` there.

## A database, hosting or another paid service

Read the provisioning catalog first (`references/money.md`). If it offers the
service, request it there and build against the variable names meanwhile. If
it answers `provisioning_unavailable`, tell the owner, recommend one provider
with the reason, and ask them to create the account and connect it in
Connections or give you the credential through the `secrets` tool. Never
invent a credential, never sign up on the owner's behalf.
