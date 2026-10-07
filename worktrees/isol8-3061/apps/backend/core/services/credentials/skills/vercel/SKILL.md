---
name: vercel
description: Deploy, inspect, and manage Vercel projects via the Vercel CLI.
primaryEnv: VERCEL_TOKEN
---

# Vercel CLI

You can use the Vercel CLI to manage the user's Vercel account. Authentication is
already configured — the `VERCEL_TOKEN` environment variable is set for you.

**Always invoke the CLI via npx with the pinned version:**

```bash
npx --yes {{PKG}} <command>
```

(The first call downloads + extracts to local scratch and may take ~1 minute; later
calls are fast.)

## Common commands

- Who am I / sanity check: `npx --yes {{PKG}} whoami`
- List projects: `npx --yes {{PKG}} project ls`
- List recent deployments: `npx --yes {{PKG}} ls`
- Read a deployment's build/runtime logs: `npx --yes {{PKG}} logs <deployment-url>`
- Inspect a deployment: `npx --yes {{PKG}} inspect <deployment-url>`

## Deploying

Deploy/rollback/env commands are project-scoped. If `VERCEL_ORG_ID` and
`VERCEL_PROJECT_ID` are set in the environment, deploys target that project. Otherwise,
from the project's working directory run once: `npx --yes {{PKG}} link --yes`,
then:

- Deploy a preview: `npx --yes {{PKG}} deploy`
- Promote to production: `npx --yes {{PKG}} deploy --prod`
- Roll back: `npx --yes {{PKG}} rollback <deployment-url>`

## On auth errors

If a command fails with a 401 / "not authorized" / "token expired", the user's Vercel
token has expired or been revoked. Tell the user to reconnect Vercel in Settings →
Integrations; do not retry.
