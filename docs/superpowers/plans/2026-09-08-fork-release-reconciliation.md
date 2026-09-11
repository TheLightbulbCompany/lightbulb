# OpenClaw 9.3 fork/release reconciliation

Date: 2026-09-08. Scope: read-only reconciliation of official upstream, `TheLightbulbCompany/agent`, the shipped 8.1 fork, and Lightbulb source configuration. No build, branch, release, deployment, or infrastructure mutation was performed for this report.

## Decisions

- Freeze upstream `v2026.9.3`, annotated tag object `a69d657b4b74556017f2d0ef98b0c64aeabb3643`, peeled commit `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`. GitHub published it at `2026-09-08T14:15:53Z` as non-draft, non-prerelease, and currently returns it from `releases/latest`. [Official release](https://github.com/openclaw/openclaw/releases/tag/v2026.9.3) · [commit](https://github.com/openclaw/openclaw/commit/1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7)
- Start the fork candidate directly from that peeled commit. Do not base it on fork `main`, a floating upstream branch, or the stale `/Users/prasiddhaparthsarthy/Desktop/agent` checkout.
- Treat the shipped fork as `v2026.8.1-isol8.3`: annotated tag object `e00896bad5024cf30b298c6e579128eecbf10ecb`, peeled commit `dfb72bf0d187a5d3d6ef35dd9cabcca5049bd31c`. The tag object is provenance, not the build commit.
- Reconcile the 17 carried commits behavior-by-behavior. Do not merge/rebase the old branch wholesale; 9.3 substantially changed MCP, SQLite/auth, browser, plugin, and release code.
- Workshop durability and plugin qualification are release blockers. An image that builds without them is not a candidate.

## Observed fork/release state

At the read-only GitHub check, the fork had no `v2026.9.*` tag, no `isol8/2026.9.*` remote branch, and no GitHub Release objects. Its default branch was unprotected `main` at `2102166f864b4599f0b36e15ad95892735ec5687`; repository `pushed_at` was `2026-09-02T05:30:21Z`.

The active fork Docker workflow is `.github/workflows/docker-release.yml`. The shipped tag run [33594938230](https://github.com/TheLightbulbCompany/agent/actions/runs/33594938230) completed successfully for peeled SHA `dfb72bf0d187a5d3d6ef35dd9cabcca5049bd31c`. That fork workflow is a simple tag-triggered, amd64-only GHCR publisher targeting `ghcr.io/thelightbulbcompany/agent`. Upstream 9.3 replaced it with a reusable, evidence-driven multi-platform/DockerHub publication workflow. Preserve the fork destination and x86 deployment intent, but adapt the fork workflow to require and check an immutable tag/SHA; do not copy upstream publication secrets/routes or retain an unverified free-form manual version input. [Shipped fork workflow](https://github.com/TheLightbulbCompany/agent/blob/v2026.8.1-isol8.3/.github/workflows/docker-release.yml) · [upstream 9.3 workflow](https://github.com/openclaw/openclaw/blob/v2026.9.3/.github/workflows/docker-release.yml)

The only open fork PR was [#4](https://github.com/TheLightbulbCompany/agent/pull/4), `isol8/2026.7.2-agent-id-fence` into `isol8/2026.7.2`, last updated 2026-08-17. It is not part of the 9.3 candidate.

The checked Lightbulb manifest is internally stale and is not runtime evidence:

- `upstream`: `ghcr.io/thelightbulbcompany/agent:2026.8.1-isol8.3`, matching the Dockerfile `FROM`.
- `dev.tag` and `prod.tag`: `2026.8.1-isol8.3-4e22eb8`.
- legacy public fallback `full`: `ghcr.io/openclaw/openclaw:2026.5.28`.
- the notes instead claim dev/prod promotion to suffix `a1fd0d8` and digest `sha256:202f2c...`; that conflicts with the structured fields.

Resolve the actual founder task image/digest independently before any rollout. Do not infer it from these fields or notes.

## Exact shipped patch ledger

Official `v2026.8.1` peels to `ea806575e6450e4d1efdfc72c19f04be982a1b9b`. The shipped fork is exactly 17 commits ahead and zero behind that commit:

| SHA | Intent | 9.3 action |
|---|---|---|
| `34d4923207` | fork GHCR Docker release | Rewrite/adapt on 9.3; preserve immutable fork publishing, not the old workflow file wholesale. |
| `e7f93d9f5e` | non-blocking MCP materialization | Re-prove before carrying. 9.3 changed deferred catalog/tool availability and MCP lifetime. |
| `6998cbbbc1` | convert Lightbulb/Isol8 native-seed sentinel | Carry/adapt: product-specific behavior, with its boot migration test. |
| `504855fb4d` | failed session-store read is unavailable, not empty | Re-prove against 9.3 store APIs; retain the failure semantic if upstream does not. |
| `cb5049e7f5` | `PASSIVE` checkpoint on agent/state DB closes | Re-prove on every new 9.3 close path; do not cherry-pick old SQLite code. |
| `2f64d4c994` | size pruning without prompt cache | Prefer 9.3 native behavior if the regression test passes; otherwise adapt narrowly. |
| `bde2a51eae` | MCP shared runtime by workspace/agent/config hash | Likely superseded in shape by 9.3 session-owned MCP continuity; retire only after parity/lifecycle tests. |
| `4ea540ecbe` | browser session snapshot writer | Carry/adapt as part of the browser feature. |
| `cb290b5ff1` | browser session restore reader | Carry/adapt as part of the browser feature. |
| `542d42f644` | `browser.sessionState` schema/resolver | Carry/adapt; no upstream 9.3 `browser.sessionState` config was found. |
| `2f894131e8` | restore state on managed Chrome launch | Carry/adapt to the 9.3 browser lifecycle. |
| `c967cdcb03` | periodic/shutdown browser snapshots | Carry/adapt to the 9.3 shutdown/lifecycle contracts. |
| `855fc5a278` | normalize restored cookie expiry | Carry/adapt with cookie/login survival tests. |
| `5db969755e` | import externally seeded legacy MCP OAuth stores at resolution | Adapt to 9.3 MCP/auth persistence; Lightbulb still writes external MCP OAuth files, so the behavior cannot be dropped untested. |
| `81e719f857` | use SSRF-pinned Chrome endpoint on session-state launch | Preserve security behavior while rebasing the browser series; pass both `url` and DNS `lookup`. |
| `e932ecddf4` | preserve shell expansions in auth/API-key headers | Reimplement on 9.3 with the original literal-token negative tests. Still missing upstream. |
| `dfb72bf0d1` | run gateway header patterns unchunked | Reimplement on 9.3 with the 16 KiB-boundary leak regression. Still missing upstream. |

Comparison: [official 8.1…shipped fork](https://github.com/TheLightbulbCompany/agent/compare/ea806575e6450e4d1efdfc72c19f04be982a1b9b...dfb72bf0d187a5d3d6ef35dd9cabcca5049bd31c).

### Redactor conclusion

Both final production fixes remain necessary. In 9.3, `SHELL_REFERENCE_PRESERVING_PATTERN_SOURCES` covers only assignment patterns, not Authorization, proxy-auth, credential-style, or gateway headers. `CHUNK_UNSAFE_PATTERN_SOURCES` covers ordinary HTTP-auth patterns but still omits `GATEWAY_SECURITY_COLON_HEADER_REDACT_PATTERN` and `GATEWAY_SECURITY_EQUALS_ASSIGNMENT_REDACT_PATTERN`. [9.3 patterns](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/logging/redact-patterns.ts)

Port the behavior, not the old diff: add a dedicated shell-expansion-preserving pattern set and its `$VAR`, `${VAR:-}`, `$(...)`, literal token, and `$`-shaped literal tests; add both gateway patterns to the chunk-unsafe set and retain the straddled-boundary test.

## Workshop durable-storage blocker

9.3 makes the active Workshop collection agent-owned:

- `resolveWorkshopSkillsDir(...)` returns `<resolveAgentDir>/workshop-skills`. [source](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/skills/workshop/skills-root.ts)
- `resolveSkillCollectionBackupRoot(...)` independently returns `<resolveAgentDir>/skill-workshop/collection-backups`. [source](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/skills/workshop/collection-paths.ts)
- agent auth/profile persistence also uses `<agentDir>/openclaw-agent.sqlite`. [source](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/agents/auth-profiles/sqlite.ts)

Lightbulb deliberately mounts `<stateRoot>/agents` and `<stateRoot>/state` on task-local storage; Litestream replicates the SQLite databases. Everything else below `<stateRoot>` remains EFS. Therefore 9.3's Workshop skills and collection backups would land on task-local storage but are not SQLite replicas: a task replacement can lose them. Moving all of `agentDir` to EFS would also move latency-sensitive SQLite and break the current WAL design.

Required bounded patch:

1. Add one Workshop-owned resolver for a per-agent path under the durable state-root/EFS spine, outside nested `agents` and `state`.
2. Make both the active collection resolver and collection-backup resolver derive from it. Keep `openclaw-agent.sqlite` under the existing local `agentDir`.
3. Route Doctor relocation, collection restore, service/store reads and writes, skill loading/discovery, watcher roots/keys, security audit, and migration through those canonical resolvers. Do not sprinkle path overrides through callers.
4. Preserve the existing containment, symlink, hardlink, atomic-write, and conservative-conflict rules; do not restore removed `allowSymlinkTargetWrites` behavior.

Minimum release checks: old applied and pending proposals migrate; ambiguous/conflicting ownership remains reviewable; approve/update/reject/restore works; a new agent gets its own collection; skills and backups survive two task replacements; no SQLite file appears below the Workshop root; a deleted or renamed agent cannot escape or collide with another agent's root.

## Plugin/runtime compatibility

9.3 requires Node `>=24.16.0 <25` or `>=26.1.0`; Node `26.1+` is recommended. Validate the final container and every node-hosted CLI, not only the build runner. [9.3 package](https://github.com/openclaw/openclaw/blob/v2026.9.3/package.json)

All eight OpenClaw packages currently baked by Lightbulb are published at exact `2026.9.3`, and each declares `openclaw >=2026.9.3`: `@openclaw/slack`, `discord`, `amazon-bedrock-provider`, `lobster`, `parallel-plugin`, `firecrawl-plugin`, `acpx`, and `tokenjuice`. Bump all eight in lockstep with the base; do not run the 8.1 packages on a 9.3 host. The official release reports all 90 npm/ClawHub plugins verified for 9.3. [release evidence](https://github.com/openclaw/openclaw/releases/tag/v2026.9.3)

The three Lightbulb in-repo plugins use APIs that still exist in 9.3:

- `openclaw/plugin-sdk/plugin-entry` and `definePluginEntry` remain exported.
- `resolve_exec_env(event, ctx)` remains registered and `ctx.agentId` remains available.
- `openclaw/plugin-sdk/tool-plugin`, `defineToolPlugin`, `context.signal`, and JSON result wrapping remain exported.

This is source-contract evidence, not a pass for checked-in output. Rebuild/type-check `lightbulb-marketplace` with dev dependency `openclaw@2026.9.3`; verify its checked-in `dist`. Run the source/dist twin tests for `aws-exec-profile` and `paperclip-exec-creds` against the 9.3 host, including per-agent credential isolation. [plugin entry](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/plugin-sdk/plugin-entry.ts) · [tool plugin](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/plugin-sdk/tool-plugin.ts) · [hook signature](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/plugins/hook-types.ts)

9.3 also removes/changes SDK aliases for execution policy, approval routing, Find/Grep details (`details.content`), directory listing (`content`/`nextAfter`), and typed MCP prompt results. The three inspected in-repo plugin sources do not use those contracts, but every external/checked-in plugin must still be loaded and exercised because the SDK is experimental. [9.3 breaking changes](https://github.com/openclaw/openclaw/releases/tag/v2026.9.3)

`@gendigital/sage-openclaw@0.11.0` declares only Node `>=18` and OpenClaw `pluginApi >=2026.3.28`, with no OpenClaw peer-version constraint. That metadata does not prove 9.3 compatibility. Keep its independent pin and require load, hook-registration, allow/ask/deny, timeout-deny, and shutdown smoke tests.

## First implementation tasks

1. Record a candidate manifest with the frozen upstream tag object/commit, shipped fork tag object/commit, the 17-entry ledger, and the intended candidate branch. Leave release/tag creation until checks pass.
2. Port the two redactor fixes and their existing regression cases first; they protect production correctness and secret handling and are demonstrably absent upstream.
3. Implement the single Workshop durable-root seam covering active skills and collection backups; run focused migration/containment/replacement tests before broader patch work.
4. Reconcile remaining commits by behavioral test in this order: browser session persistence/security; MCP OAuth seeding; store/SQLite failure and close semantics; native seed; MCP lifetime/materialization; context pruning. Delete patches whose 9.3 behavior passes the old regression.
5. Change the Docker base and all eight official plugin pins together; rebuild the three in-repo plugins, then qualify Sage and the complete enabled plugin/config inventory on the supported Node runtime.
6. Adapt the fork GHCR workflow to immutable tag/SHA input and the 9.3 Dockerfile, run fork CI and focused behavioral suites, then publish a new immutable fork tag. Only after that should Lightbulb build an extended image or alter environment pins.

Skipped: no runtime/founder inventory, image pull, build, workflow dispatch, tag, release, PR mutation, or deployment. Those belong to the execution gates in the parent delivery plan.
