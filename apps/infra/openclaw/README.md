# Extended OpenClaw Image

Custom Docker image extending **our OpenClaw fork**, `ghcr.io/thelightbulbcompany/agent:<UPSTREAM>`, with the Linux binaries the bundled skills need, our in-repo plugins, the bundled `paperclip`/`lightbulb` skills, and the litestream boot chain.

Built and pushed to ECR `lightbulb/openclaw-extended` by `.github/workflows/build-openclaw-image.yml` on every push to `main` that touches `Dockerfile`, `openclaw-version.json`, or any baked directory (`skills/`, `plugins/`, `chrome-extensions/`, `local-sqlite/`, `sage/`, `warp/`). Published tag is `{upstream}-{short-sha}`.

## Two images, two registries — don't confuse them

`openclaw-version.json` carries three separate image references. Its `notes` field is the authoritative explanation; the short version:

| field | what it is |
|---|---|
| `upstream` | The **fork** base this Dockerfile `FROM`s — `ghcr.io/thelightbulbcompany/agent:<tag>`. A **private** GHCR package, pulled only by the authenticated build workflow. Our patches live on `isol8/*` branches + `v<version>-isol8.<n>` tags in `TheLightbulbCompany/agent`; the fork's own `docker-release.yml` publishes them. **Must stay in lockstep with the tag in the hardcoded `FROM <tag>@<digest>` line below** — there is no build-arg templating. |
| `image` / `tag` / `full` | The **legacy public** upstream image `ghcr.io/openclaw/openclaw`. Kept ONLY as the bootstrap/unknown-env fallback in `container-stack.ts`, which per-user ECS tasks pull with **no registry credentials** — so it must stay anonymously pullable. Also feeds the `OPENCLAW_UPSTREAM_VERSION` env fallback. It is **not** what we run, and it does **not** move when the fork base moves. |
| `extendedImage` + `dev.tag` / `prod.tag` | The ECR extended image actually deployed. The two tags are the per-environment promotion gate. |

The fat (non-slim) fork variant is deliberate: it bundles plugin runtime deps so first-use of browser/codex/channels skips a ~6min lazy npm-install on cold start.

The writable fork checkout is `~/dev/agent-fork`. Read it **at the pinned tag** (`git -C ~/dev/agent-fork show v<tag>:<path>`) — its working tree drifts from what we run.

## Bumping the base OpenClaw version

1. Cut the new fork tag in `TheLightbulbCompany/agent` (rebase the `isol8/*` patches onto the new upstream release, tag `v<version>-isol8.<n>`, let the fork's `docker-release.yml` publish it to `ghcr.io/thelightbulbcompany/agent`)
2. Edit `Dockerfile`: pin `FROM ghcr.io/thelightbulbcompany/agent:<new>@sha256:<verified-manifest-digest>` to the publisher-proven tag and digest
3. Edit `<repo-root>/openclaw-version.json`: change `"upstream"` to the same `ghcr.io/thelightbulbcompany/agent:<new>`, and add a `notes` entry enumerating the carried patches. **Leave `image`/`tag`/`full` alone** — they are the legacy public fallback, not the base
4. Re-pin the baked `@openclaw/*` plugin versions in the Dockerfile to the release line of the new `FROM` tag (the plugin-pin policy comment above the `FROM` explains why a stale channel plugin on a newer host is not a safe skew)
5. Add the candidate schema in the SAME PR under `apps/backend/tests/chaos/_schema/releases/<candidate-id>/` and `apps/backend/core/services/openclaw_config_schemas/`, then register that exact id without replacing the active fleet schema
6. PR the change; PR CI validates it without publishing. Dispatch `build-openclaw-image.yml` once on the final branch head for qualification; merging to `main` publishes the distinct final `{upstream}-{short-sha}` tag automatically
7. Flip `openclaw-version.json#dev.tag` to that tag in a **separate** PR, then boot a dev container on it and verify in the live container (not just artifact digests)
8. After dev verification, copy `dev.tag` to `prod.tag` in a third PR, run ONE prod `workflow_dispatch` of `deploy.yml`, then roll each owner's container via the admin UI. **A pin is not a roll** — running containers keep their own task-def revision until rolled

Local build to sanity-check the Dockerfile (`--platform linux/amd64` is required so the build matches what CI/Fargate run):

```bash
docker build --platform linux/amd64 -t openclaw-extended:local apps/infra/openclaw/
```

This needs `docker login ghcr.io` with a token that can read the private `thelightbulbcompany/agent` package.

## Adding a new skill binary

1. Find the install method (apt / pip / npm / `go install` / build-from-source)
2. Add a `RUN` line to the appropriate layer in `Dockerfile`. Layer order is intentional — least-to-most volatile so caching stays maximal
3. **Pin it.** A version at minimum; a version + SHA256 + smoke test where the artifact has published checksums (the litestream layer is the pattern to copy). No `@latest`, no `--branch` omitted, no `curl | sh` of a default branch
4. Confirm the skill it unlocks is actually reachable on this fleet before adding it: the skill's `requires.bins` must match the binary name you install, its `requires.env` must be something we set, and the extension it lives in must not be in the base image's `EXCLUDED_CORE_BUNDLED_PLUGIN_DIRS`. A binary whose skill can never be eligible is pure image weight, and an eligible skill for a product we don't use is worse — it sits in every agent's prompt
5. Build + verify locally per "Bumping" above
6. PR

## Skipped skills

Skills requiring macOS apps (`apple-notes`, `bear-notes`, `things-mac`, `peekaboo`, `imsg`, `model-usage`, `camsnap`, `sag`) are intentionally NOT installed — they need their host apps which only exist on macOS. These are deferred to the desktop app.

`obsidian` is skipped for the same reason: the official `obsidian` CLI ships inside the Obsidian desktop app and talks to the running app over IPC, so it cannot be installed on a headless container.
