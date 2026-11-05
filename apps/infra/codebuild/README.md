# Dev deploy runner (CodeBuild)

`lightbulb-dev-deploy` is a CodeBuild project, defined in
`lib/stacks/deploy-runner-stack.ts` as the dev-only stack
`lightbulb-dev-deploy-runner`, that runs `deploy-dev.sh` on every push to
`main`: the dev stage of the generated `.github/workflows/deploy.yml` (synth,
`cdk deploy --app cdk.out 'dev/*'`, the two Vercel previews). Prod promotion
stays on GitHub Actions. Stood up 2026-11-05 because the self-hosted deploy
runner is offline and there is no Actions budget.

| file | role |
|---|---|
| `buildspec-deploy-dev.yml` | what CodeBuild runs: Node 20, then `deploy-dev.sh` |
| `deploy-dev.sh` | the deploy, step for step against deploy.yml, plus the serialisation and the secret load |
| `role-policy.json` | everything the service role may do beyond CodeBuild's own log/report permissions; the stack attaches it verbatim |

## One-time setup (lead, admin profile, us-east-1)

1. **GitHub credential** -- CodeBuild clones with it and creates the push
   webhook on the repo. A fine-grained PAT with the org as resource owner, on
   `TheLightbulbCompany/lightbulb`: Contents read, Commit statuses read/write,
   Webhooks read/write (docs: codebuild/latest/userguide/access-tokens-github).
   One per account+region; `list-source-credentials` is empty today.

   ```sh
   AWS_PROFILE=isol8-admin aws codebuild import-source-credentials \
     --server-type GITHUB --auth-type PERSONAL_ACCESS_TOKEN --token "$GITHUB_PAT"
   ```

   The console's "GitHub App" connection (CodeConnections) works too and is not
   tied to a person; then the service role also needs
   `codeconnections:GetConnectionToken` + `GetConnection` on the connection
   ARN (add a statement to `role-policy.json`).

2. **Secret** `lightbulb/dev/deploy-runner`: one JSON object; every key
   becomes an env var in the build. These are the dev half of the GitHub
   secrets/vars deploy.yml reads (`*_PROD` is not needed: prod synths as `""`
   and is never deployed from here).

   ```sh
   cat > /tmp/deploy-runner.json <<'JSON'
   {
     "STRIPE_WORKSPACE_PRICE_ID_DEV": "", "STRIPE_BASE_PRICE_ID_DEV": "",
     "STRIPE_ULTRA_PRICE_ID_DEV": "", "STRIPE_SEAT_PRICE_ID_DEV": "",
     "STRIPE_FRONT_DESK_PRICE_ID_DEV": "", "STRIPE_PUBLISHABLE_KEY_DEV": "",
     "OPENROUTER_WORKSPACE_ID_DEV": "", "OPENROUTER_MANAGED_MODEL_ID_DEV": "",
     "OPENROUTER_MODEL_POLICY_JSON_DEV": "{}",
     "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_DEV": "pk_test_...",
     "DESKTOP_EXECUTION_OWNER_DEV": "", "OPENAI_APPS_CHALLENGE_TOKEN_DEV": "",
     "VERCEL_TOKEN": "", "VERCEL_ORG_ID": "", "VERCEL_PROJECT_ID": "",
     "VERCEL_PROJECT_ID_COMPANY": "",
     "NEXT_PUBLIC_API_URL_DEV": "https://api-dev.thelightbulb.company/api/v1",
     "DOCKERHUB_USERNAME": "", "DOCKERHUB_TOKEN": ""
   }
   JSON
   AWS_PROFILE=isol8-admin aws secretsmanager create-secret \
     --name lightbulb/dev/deploy-runner --secret-string file:///tmp/deploy-runner.json
   rm /tmp/deploy-runner.json
   ```

   The synth values are baked into the running dev backend task, so
   `aws ecs describe-task-definition` on the current `lightbulb-dev-service`
   task recovers them if the dashboards are slow. `DOCKERHUB_*` is optional:
   set it if a backend image build fails on `toomanyrequests` (CodeBuild's
   egress IPs are shared). Later changes: `put-secret-value --secret-id
   lightbulb/dev/deploy-runner --secret-string file://...`.

3. **The runner stack, once by hand** (it deploys itself after that, being in
   `dev/*`). Docker running, from the repo:

   ```sh
   cd apps/infra && npm ci && npx cdk synth
   AWS_PROFILE=isol8-admin npx cdk deploy --app cdk.out dev/lightbulb-dev-deploy-runner
   ```

   CloudFormation creates the webhook with the credential from step 1, so 1
   must come first.

4. **First run** (also the manual trigger, any time):

   ```sh
   AWS_PROFILE=isol8-admin aws codebuild start-build \
     --project-name lightbulb-dev-deploy --source-version main
   ```

   Logs: CloudWatch `/aws/codebuild/lightbulb-dev-deploy`, or the build's
   page in the console. The commit gets a `lightbulb-dev-deploy` status on
   GitHub.

## How it behaves

- **Serial, newest wins.** No `concurrentBuildLimit`: CodeBuild drops builds
  past it instead of queueing. `deploy-dev.sh` waits for older in-progress
  builds of the project and stops (green, "superseded") when a newer one
  exists, so a burst of eight merges deploys the first and the last.
- **Same roles as GitHub.** The service role may only assume the
  `cdk-hnb659fds-*-877352799272-us-east-1` bootstrap roles (publishing,
  deploy; CloudFormation runs as cfn-exec), read the one secret, and list its
  own builds. No `sts:TagSession`, no cdk.out S3 transport: the GitHub role
  needed those for `configure-aws-credentials` and for moving cdk.out between
  jobs.
- **Vercel is optional until the token exists.** If `VERCEL_TOKEN` is not
  in the secret, the build deploys the stacks and stops there, green, with
  `VERCEL_TOKEN is not in lightbulb/dev/deploy-runner: ... SKIPPING` in the
  log. Add it (and the other `VERCEL_*` keys) and the next push ships the
  frontend and company previews too.

## When the GitHub self-hosted runner returns

deploy.yml is untouched by this: its dev stage still runs on every push to
main the moment `DEPLOY_RUNNER` is online again. From then on **both**
GitHub Actions and this project deploy the same dev stacks on every push,
and two `cdk deploy`s racing on one stack fail with
`UPDATE_IN_PROGRESS ... can not be updated`. Turn one off before that day:

- Keep CodeBuild, stop GitHub deploying dev: give the `GitHubWorkflow` in
  `lib/app.ts` `workflowTriggers: { workflowDispatch: {} }` (the default is
  push-to-main + dispatch; prod promotion is dispatch and keeps working),
  re-synth, commit deploy.yml.
- Keep GitHub, stop CodeBuild: one line in `lib/stacks/deploy-runner-stack.ts`,
  `webhook: true` -> `webhook: false`, and deploy the stack (any dev deploy
  does it). Instant stop with no deploy:
  `aws codebuild delete-webhook --project-name lightbulb-dev-deploy`
  (the next stack deploy recreates it unless the flag is flipped).

`cdk destroy dev/lightbulb-dev-deploy-runner` removes the project, role
and log group; nothing here holds state.

## Cost per deploy

On-demand Linux x86, us-east-1: general1.small $0.005/min (2 vCPU, 3 GB),
medium $0.01/min (4 vCPU, 7 GB), large $0.02/min (8 vCPU, 15 GB). The free
tier is 100 small-minutes a month. Small cannot hold `next build`, so the
project is medium.

From the last two completed dev deploys on the self-hosted box: synth 1.5
min, 43 asset publishes 19 s max each, 10 unchanged stacks 7-9 s each, the
service stack 5 min, company 2.5-3.5 min, Vercel frontend 7.5-16 min.

| step | min on medium |
|---|---|
| provision + clone + `npm ci` | 2-3 |
| synth (cold: two bundling images pulled) | 3-4 |
| asset checks + 10 no-change stacks + service | 8-10 |
| backend image when apps/backend changed (cold build, no layer cache) | +4-6 |
| Vercel frontend pull/install/build/deploy + 3 aliases | 6-9 |
| company | 2-3 |
| **total** | **22-30, 26-36 with an image** |

That is **$0.22-0.36 per deploy**; 60 deploys a month is about $13-22, plus
cents of CloudWatch logs and $0.40 for the secret. Large would halve the
wall time at double the rate, so about the same money; medium is the fit
unless `next build` runs out of memory.
