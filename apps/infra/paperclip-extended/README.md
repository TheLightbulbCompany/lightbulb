# `paperclip-extended` — temporary overlay image

This directory builds an **extended Paperclip image**: the official upstream image
(`ghcr.io/paperclipai/paperclip`, pinned by digest) with **one patch** layered on
top. It mirrors the `apps/infra/openclaw/` (`openclaw-extended`) pattern.

It is **temporary** — it exists only until upstream ships the fix, at which point
we delete it and point back at the official image (see [Reverting](#reverting)).

## Why it exists

Paperclip's `openclaw-gateway` adapter is incompatible with the OpenClaw gateway
version our per-user containers run:

1. The adapter hard-codes **gateway protocol v3**; our containers require **v4**
   and reject a v3 client at the handshake (`protocol mismatch, expectedProtocol: 4`).
2. The adapter injects a `paperclip` field into `agentParams`; the v4 gateway's
   schema is strict (`additionalProperties: false`) and rejects it.

Either one blocks every Paperclip agent connect. Upstream tracks both
(`paperclipai/paperclip` issues **#6747** and **#6606**) but has not released a
fix, and we deploy the floating `:latest` tag, so we can neither wait for them nor
rely on the tag staying put.

## What the patch does

A 2-edit patch to `packages/adapters/openclaw-gateway/src/server/execute.ts`
(the adapter runs from TypeScript source via the `tsx` loader — no compiled dist —
so editing the source file is sufficient, no rebuild):

- `const PROTOCOL_VERSION = 3;` → `4` (negotiate v4)
- delete `agentParams.paperclip = paperclipPayload;` (drop the rejected field; the
  same context already rides in the wake text via `buildWakeText`)

The Dockerfile applies these as content-matched `sed` edits and **fails the build**
if either didn't apply — so a base-image re-pin can never silently ship a still-v3
adapter.

## How it's wired (mirrors `openclaw-extended`)

- **Pin:** `paperclip-version.json` at the repo root — `upstream` (the official
  image, by digest), `extendedImage` (our ECR repo), and per-env `dev.tag`/`prod.tag`.
- **Build:** `.github/workflows/build-paperclip-image.yml` builds this Dockerfile
  and pushes to ECR `isol8/paperclip-extended` on changes to the Dockerfile or the
  version file. Tag = `<upstream-commit-short>-<repo-commit-short>`. The workflow
  reads `upstream` from the version file and passes it to the Dockerfile as the
  `UPSTREAM_IMAGE` build-arg, so the base `FROM` is sourced from the same single
  field the tag and CDK deploy derive from — it can't drift.
- **Deploy:** `paperclip-stack.ts` reads `paperclip-version.json` and uses the
  extended ECR image once the per-env tag is promoted past `bootstrap`; while it's
  `bootstrap` it falls back to the unpatched `upstream` image (so the env keeps
  deploying before the extended image exists — no regression, heartbeats just stay
  broken as before).

### One-time prerequisite (out-of-band, like `openclaw-extended`)
The ECR repo `isol8/paperclip-extended` is account-scoped and imported by name (not
created by CDK). Create it once before the first build/deploy:

```
aws ecr create-repository --repository-name isol8/paperclip-extended \
  --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true \
  --profile isol8-admin --region us-east-1
```

**Pre-grant the builder role's push at the same time.** The build workflow
pushes as the `isol8-openclaw-image-builder` OIDC role. That role's push grant on
this repo is added by `container-stack.ts` (`grantPullPush`) and only lands via the
`deploy.yml` CDK pipeline. On the introducing merge, `build-paperclip-image`
(triggered by the Dockerfile / version-file change) races that CDK deploy and the
first push fails `AccessDenied` until the stack update has run once. Avoid the race
by attaching a repository policy when you create the repo, so the first build can
push regardless of CDK timing (the CDK identity grant then layers on as the durable
source of truth):

```
aws ecr set-repository-policy --repository-name isol8/paperclip-extended \
  --profile isol8-admin --region us-east-1 \
  --policy-text '{"Version":"2012-10-17","Statement":[{"Sid":"BuilderPush",
    "Effect":"Allow",
    "Principal":{"AWS":"arn:aws:iam::877352799272:role/isol8-openclaw-image-builder"},
    "Action":["ecr:BatchCheckLayerAvailability","ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart","ecr:CompleteLayerUpload","ecr:PutImage"]}]}'
```

(`ecr:GetAuthorizationToken` is registry-level and the builder role already holds it
from the `openclaw-extended` grant, so the repo policy only needs the push actions.)
Alternatively, let the `deploy.yml` ContainerStack update land once before the first
image build and skip the repo policy.

### Bumping the base image
Re-pin `paperclip-version.json#upstream` to a newer digest — that's the only
place the base digest lives; the workflow feeds it to the Dockerfile `FROM` via
the `UPSTREAM_IMAGE` build-arg. Let CI build, then bump `dev.tag`/`prod.tag` to
the built tag in a PR.

## Reverting

When upstream releases a build whose `openclaw-gateway` adapter is already v4 and
no longer sends the `paperclip` field:

1. Point `paperclip-stack.ts` back at the official image — pin
   `paperclip-version.json#upstream` to that release digest and set `dev.tag`/`prod.tag`
   back to `bootstrap` (which deploys `upstream` directly), **or** delete the
   `paperclipImage` selection block and hardcode
   `ecs.ContainerImage.fromRegistry("ghcr.io/paperclipai/paperclip:<pin>")` at both
   container call sites (the server task def and the migrate task def).
2. Delete this directory, `paperclip-version.json`, the build workflow, and the CDK
   wiring (the ECR-repo import + builder-role grant).

The pin is explicit and the patch is small, so the revert is a contained diff.
