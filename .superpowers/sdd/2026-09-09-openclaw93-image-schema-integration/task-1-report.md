# Task 1 report — NEEDS_CONTEXT

## Status

Independent plugin and artifact-identity preparation is implemented and locally checked. Task 1 is **not complete**: the final publisher-owned fork tag, source commit, and private GHCR manifest digest do not exist in the supplied context, so `openclaw-version.json#upstream` and the final Docker `FROM` remain unchanged. The new metadata gate rejects the current un-digested `FROM`.

Checkout preflight:

```text
$ git rev-parse --show-toplevel
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-openclaw93-image
$ git rev-parse HEAD
3ae747294a9290085e0e04dcfc1c4866b55a00ba
$ git branch --show-current
codex/founder-openclaw93-image
$ git merge-base HEAD 3ae747294a9290085e0e04dcfc1c4866b55a00ba
3ae747294a9290085e0e04dcfc1c4866b55a00ba
$ git status --short
(no output)
```

## Implemented preparation

- Added dependency-free `scripts/openclaw-image-proof.mjs` with pure validators and stdin-based CLI adapters for metadata, exact ECR absence, canonical digest, base digest equality, and post-push identity/digest proof.
- Added 21 table-driven native Node tests covering multi-stage Docker metadata, tag/Docker mismatch, missing digest, wrong extended registry/repository, existing image, API/auth failure, malformed/duplicate absence, wrong registry/repository/tag, duplicate post-push images, wrong post-push digest, and valid pre/post responses.
- Replaced the workflow's string-split metadata step with verified `image`, `tag`, `base_image`, and `base_digest` outputs. The tag remains `<upstream-tag-without-v>-<git short SHA>`.
- Added exact OIDC account assertion and `BatchGetImage` preflight before build; the helper, rather than AWS CLI exit status, classifies absence.
- Added private-GHCR base digest resolution after login and before build.
- Added actual containerd `.Target.Digest` capture immediately before the single `nerdctl push`, canonical validation, exact post-push `BatchGetImage` verification, and one summary record containing OCI target, base digest, source commit, and workflow URL.
- Corrected the ECR comment: the repository is mutable; this workflow enforces a one-push/non-overwrite/digest-readback contract. Existing cleanup is untouched.
- Added workflow/path-filtered native proof tests for the helper and both exec plugins.
- Updated all eight official Docker package pins together to `2026.9.3`; preserved `@gendigital/sage-openclaw@0.11.0` and the Sage policy.
- Updated only the marketplace plugin's dev host to `openclaw@2026.9.3` and runtime dependency to `typebox@1.3.18`; regenerated its lockfile and build. `.dockerignore` still excludes plugin-local `node_modules`, and tracked dist rebuilt byte-identically.
- Inspected frozen OpenClaw source `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`: `definePluginEntry`, `resolve_exec_env`, source `extensions` plus built `runtimeExtensions`, and the `2026.9.3` TypeBox dependency remain compatible. The two hand-written source/dist registration twins rebuilt byte-identically, so source/dist files were not churned. Their tests now execute both entrypoints and verify absent/foreign behavior plus per-agent Paperclip credential selection.
- Preserved `openclaw-version.json` in full, including `image`, `tag`, `full`, `extendedImage`, `dev.tag`, and `prod.tag`. No image, AWS, registry, provider, deployment, push, PR, merge, or production command ran.

## Exact npm metadata evidence

Command (exit 0):

```sh
npm view @openclaw/slack@2026.9.3 version dist.integrity --json && npm view @openclaw/discord@2026.9.3 version dist.integrity --json && npm view @openclaw/amazon-bedrock-provider@2026.9.3 version dist.integrity --json && npm view @openclaw/lobster@2026.9.3 version dist.integrity --json && npm view @openclaw/parallel-plugin@2026.9.3 version dist.integrity --json && npm view @openclaw/firecrawl-plugin@2026.9.3 version dist.integrity --json && npm view @openclaw/acpx@2026.9.3 version dist.integrity --json && npm view @openclaw/tokenjuice@2026.9.3 version dist.integrity --json && npm view openclaw@2026.9.3 version dist.integrity --json && npm view typebox@1.3.18 version dist.integrity --json
```

Raw output, in command order:

```json
{"version":"2026.9.3","dist.integrity":"sha512-0J4J85rtEgeIctkjZTU/tK4PixOEPqL0M6s6mZ8zPCnIQHB64XG90GEXmjq8cl6JDxX7i0PmPWhuYShTrPDdog=="}
{"version":"2026.9.3","dist.integrity":"sha512-m6V9wg6OljheSXwOJlT+ZZlyfYrzkNGAA5gky0PfwSg64aEsjdkSEqsPg3acCuufHlyBVerToEpmK4PVc7rrgw=="}
{"version":"2026.9.3","dist.integrity":"sha512-f+7M91kqoRwjz3zSve610AygCFNtN2qSyuvSUCsnix6sEcTkeCAMC8wfwfjf02zKExZkkvQFKmXrIjgpBbFlHA=="}
{"version":"2026.9.3","dist.integrity":"sha512-0DAcAzkwPPziCQeAjJJ5HyclQeu/zBlkqwhGjvvrDLw6sXjp2q6vTqrUjR0u4VMgBQTkLdWHDvltHyGYhL4u/Q=="}
{"version":"2026.9.3","dist.integrity":"sha512-j9SDNKN7CVvMrwuA4kg1E1d0n1J8yLKvoQCxD4zUeuT1GqnPwugD3yjYYrv4EGHenBdtiRudKZvgbMZnpWBYNw=="}
{"version":"2026.9.3","dist.integrity":"sha512-7i/pEaes7laEqEhCERQiMh30MK+aKalokur8+gRYn54BV9whOx41PDkCIhvFyeCYJ6APm1b50ufmXHOrc2H4lg=="}
{"version":"2026.9.3","dist.integrity":"sha512-0YZN0YZmYbe/8GzbTj8jsYBE1fgJxF703CVfS4RX6nrVKdTdJgAUgo2Q3eJCvQnBK1/bnvlw5mSJDbsEsHzpYQ=="}
{"version":"2026.9.3","dist.integrity":"sha512-TrtSo6yvYx32h31gglJB4d/zdkI7f1w+rV3ySU98M5PQCYgucUfDy9TF04MTRmhnZwQPoZDXy1bapzJXLucg3A=="}
{"version":"2026.9.3","dist.integrity":"sha512-CzDHMeHdnjlIZ76ZyBb1lvLO4H/yBIMYXupFGGBN87x0853y3hg5nLAnKfxSKqLzqhbUKqy9ebDRAWWV4t8aew=="}
{"version":"1.3.18","dist.integrity":"sha512-/wYPoDqxWZSxV/XD8Eskzr3YluXC9CaWJOuUYMkj+lLVLkyeEIQKzHvMuS/IRc3OLTIBC32LAtHgXo/WFEOMHQ=="}
```

Local resolution (exit 0):

```text
$ npm --prefix apps/infra/openclaw/plugins/lightbulb-marketplace ls openclaw typebox --depth=0
@lightbulb/marketplace-plugin@0.1.0 .../apps/infra/openclaw/plugins/lightbulb-marketplace
├── openclaw@2026.9.3
└── typebox@1.3.18
```

## Red/green and setup log

Initial helper RED (exit 1):

```text
$ node --test scripts/openclaw-image-proof.test.mjs
Error [ERR_MODULE_NOT_FOUND]: Cannot find module '.../scripts/openclaw-image-proof.mjs'
ℹ pass 0
ℹ fail 1
```

Multi-stage Docker regression RED after the first implementation (exit 1):

```text
$ node --test scripts/openclaw-image-proof.test.mjs
✖ metadata proves the Docker base pin and derives the extended tag
Error: Dockerfile base must exactly match metadata upstream with a canonical digest
ℹ pass 19
ℹ fail 1
```

Fix: metadata validation now checks the last Docker `FROM` (the runtime stage), not the Debian builder stage. Subsequent helper runs pass 21/21.

Plugin-local install (exit 0):

```text
$ npm install
npm warn deprecated node-domexception@1.0.0: Use your platform's native DOMException instead
added 373 packages, and audited 374 packages in 11s
107 packages are looking for funding
6 vulnerabilities (3 moderate, 2 high, 1 critical)
npm warn allow-scripts 7 packages have install scripts not yet covered by allowScripts:
npm warn allow-scripts   esbuild@0.21.5
npm warn allow-scripts   fsevents@2.3.3
npm warn allow-scripts   koffi@3.1.6
npm warn allow-scripts   openclaw@2026.9.3
npm warn allow-scripts   tree-sitter-bash@0.25.1
npm warn allow-scripts   @google/genai@2.19.0
npm warn allow-scripts   protobufjs@7.6.6
```

No audit fix was run: that would be an unrequested dependency-policy change. The lockfile reflects the exact requested host update.

First marketplace build (exit 2) was sandbox-only write denial:

```text
$ npm --prefix apps/infra/openclaw/plugins/lightbulb-marketplace run build
> tsc
error TS5033: Could not write file '.../dist/env.d.ts': EPERM: operation not permitted
error TS5033: Could not write file '.../dist/env.js': EPERM: operation not permitted
error TS5033: Could not write file '.../dist/index.d.ts': EPERM: operation not permitted
error TS5033: Could not write file '.../dist/index.js': EPERM: operation not permitted
```

The same scoped build was approved outside the sandbox and exited 0:

```text
> @lightbulb/marketplace-plugin@0.1.0 build
> tsc
```

An extra direct TypeScript check of the exec plugins (exit 2) was inconclusive because their package directories intentionally ship no local dependencies and `--baseUrl` does not redirect package-export resolution:

```text
error TS2307: Cannot find module 'openclaw/plugin-sdk/plugin-entry' or its corresponding type declarations.
error TS2580: Cannot find name 'process'. Do you need to install type definitions for node?
```

No dependencies were added to those runtime-only plugins. Frozen source inspection plus the source/dist executable registration tests provide the local prep evidence; actual host load/hook evidence remains an image-runner requirement.

## Consolidated affected checks

Command (exit 0):

```sh
node --test scripts/openclaw-image-proof.test.mjs && node --test apps/infra/openclaw/plugins/aws-exec-profile/index.test.mjs && node --test apps/infra/openclaw/plugins/paperclip-exec-creds/index.test.mjs && npm --prefix apps/infra/openclaw/plugins/lightbulb-marketplace run build && npm --prefix apps/infra/openclaw/plugins/lightbulb-marketplace test -- --run && sh apps/infra/openclaw/local-sqlite/smoke-test.sh apps/infra/openclaw/local-sqlite/isol8-local-sqlite-boot.sh && git diff --check
```

Raw result (timings omitted only where nondeterministic):

```text
✔ metadata proves the Docker base pin and derives the extended tag
✔ metadata rejects tag/Docker mismatch
✔ metadata rejects missing digest
✔ metadata rejects wrong extended registry/repository
✔ preflight accepts only the exact tag's single ImageNotFound response
✔ preflight rejects existing image
✔ preflight rejects auth/API failure
✔ preflight rejects malformed absence
✔ preflight rejects duplicate failures
✔ preflight rejects wrong registry
✔ preflight rejects wrong repository
✔ preflight rejects wrong tag
✔ post-push accepts one exact image with the converted OCI target digest
✔ post-push rejects wrong post-push digest
✔ post-push rejects duplicate images
✔ post-push rejects failure alongside image
✔ post-push rejects wrong registry
✔ post-push rejects wrong repository
✔ post-push rejects wrong tag
✔ post-push rejects malformed expected digest
✔ canonical digest validation trims runner output but rejects non-sha256 values
ℹ tests 21
ℹ pass 21
ℹ fail 0

✔ returns AWS_PROFILE when the config file declares the connector profile
✔ returns {} when the config path does not exist
✔ returns {} when the file exists but lacks the connector profile
✔ returns {} when the header appears only as a comment
✔ returns {} for undefined/empty path (falls back to a nonexistent default)
✔ ./index.ts registers source-equivalent connector behavior
✔ ./dist/index.js registers source-equivalent connector behavior
ℹ tests 7
ℹ pass 7
ℹ fail 0

✔ returns all four vars for the agent's own key file
✔ selects PER AGENT — one container hosts many agents with different keys
✔ returns {} when the agent has no key file (never claimed / non-Paperclip agent)
✔ returns {} when there is no agentId on the hook context
✔ returns {} on malformed JSON rather than throwing into the exec tool
✔ returns {} on a JSON non-object (array / null / scalar)
✔ is ALL-OR-NOTHING — a missing or empty field yields {}, never a half-set env
✔ refuses a path-traversing agentId rather than reading outside the root
✔ falls back to the baked default root when none is supplied
✔ ./index.ts injects only the active agent's credentials
✔ ./dist/index.js injects only the active agent's credentials
ℹ tests 11
ℹ pass 11
ℹ fail 0

> @lightbulb/marketplace-plugin@0.1.0 build
> tsc

> @lightbulb/marketplace-plugin@0.1.0 test
> vitest run --run
✓ src/index.test.ts (9 tests)
Test Files  1 passed (1)
Tests  9 passed (9)

PASS 1 cold start
PASS 2 restore
PASS 2b enumeration fail-loud
PASS 2c unparseable keys fail loud
PASS 2d tombstoned agent purged, not restored
PASS 2e tombstone-only prefix boots
PASS 2f import-state hand-off
PASS 2j corrupt hand-off db retained for diagnosis, sessions not installed
PASS 2k import wins over a pre-bounce stub on the first boot
PASS 2l already-applied hand-off does not re-clobber the evolved database
PASS 2g partial purge keeps the tombstone
PASS 2i failed restore aborts the boot under concurrency
PASS 2h both litestream key layouts parse
PASS 4 sentinel
PASS 4b sentinel benign
PASS 4c sentinel retained on non-benign failure
PASS 4d non-default sentinel ignored
PASS 5 mount-order fence aborts on nfs
PASS 5b fence fails CLOSED on an unidentifiable filesystem
ALL SMOKE TESTS PASSED

git diff --check: no output
```

Workflow lint baseline (exit 1) reported two pre-existing/non-change findings only:

```text
$ actionlint .github/workflows/build-openclaw-image.yml
line 127: label "isol8-wsl" is unknown (custom self-hosted label)
line 336: SC2129 (existing final Summary step uses individual redirects)
```

Filtering those known baseline findings proves the edited workflow has no additional actionlint/shellcheck finding (exit 0, no output):

```sh
actionlint -ignore 'label "isol8-wsl" is unknown' -ignore 'SC2129' .github/workflows/build-openclaw-image.yml
```

Current fail-closed metadata evidence (exit 1, expected while publisher inputs are absent):

```text
$ node scripts/openclaw-image-proof.mjs metadata "$(git rev-parse --short HEAD)"
Dockerfile base must exactly match metadata upstream with a canonical digest
```

## Exact pending controller inputs

All three must describe the same final publisher artifact; no placeholder is acceptable:

1. Final annotated fork tag, e.g. the actual `v2026.9.3-isol8.N` (not an inferred branch name).
2. Full final fork source commit SHA the tag resolves to.
3. Canonical private GHCR manifest digest for `ghcr.io/thelightbulbcompany/agent:<tag-without-v>`, in `sha256:<64 lowercase hex>` form.

When supplied, the remaining source integration is:

- set only `openclaw-version.json#upstream` to the exact tagged GHCR reference;
- set the final Docker `FROM` to that exact tagged reference plus `@<digest>`;
- update the now-stale 8.1 host-policy comment adjacent to `FROM`;
- rerun the final affected checks and metadata CLI;
- obtain controller-owned amd64 runner evidence for private base resolution, full image build, all baked plugin loads/hooks (including Sage compatibility), converted SOCI target capture, single ECR push, exact digest readback, and workflow URL.

Local arm64 checks are not runtime evidence. Task 1 remains `NEEDS_CONTEXT`; no publisher tag/digest, image qualification, publication, or promotion is claimed.

Controller coordination received after checks: UI PR 2402 is merged at combined main `149c5d2c6b72bd4afdfc4efd1de23c46b6edf694` and contains two Docker-baked skill docs. Per controller instruction this isolated prep was not rebased or merged; controller will integrate main before the combined image PR. That merge supplies no 9.3 publisher/default evidence.

The first scoped `git add` attempt exited 128 because the sandbox could not create the linked-worktree `index.lock`; the identical explicit file list was approved outside the sandbox and staged successfully. No broad add, reset, cleanup, rebase, or merge ran.
