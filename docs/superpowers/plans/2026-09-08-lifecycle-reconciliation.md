# OpenClaw 9.x founder lifecycle and reconciliation inventory

**Inventory date:** 2026-09-08  
**Source inspected:** `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync` at `ba58f69b47514aca8ca9a47d400d3a40a8f91256` (`main`)  
**Mode:** read-only source/PR inventory. No Lightbulb source, git state, AWS state, or containers were changed. The Sep 1 graph was used only as a locator; every conclusion below was re-checked against current source.

## Executive finding

The current system has **target-image-aware config generation**, but it does not have a durable per-owner release assignment or a release transaction. An explicit founder image can be installed by the existing update path, yet a later size-only resize, task-definition drift refresh, stopped-service reprovision, silent wake, provider cold switch, plan conversion, or admin reprovision can clone the environment-global CDK base and silently move that owner back to the fleet image.

The most dangerous upgrade ordering bug is also present: image update writes the target-version config directly over the active `openclaw.json` while the old task is still running, then starts the ECS roll. OpenClaw's watcher can therefore expose 9.x config to the 8.1 process. Conversely, the stopped reprovision path catches and ignores a failed config write and still starts the replacement image, so a 9.x task can boot against stale 8.1 config.

The necessary primitives mostly exist already: per-owner container rows, conditional DDB writes, a fenced update ledger with a durable target task-definition ARN, base-clone task definition construction, register-only mode, stop/drain, a gateway-health foreground waiter, version-aware config rendering, and transition-aware EFS locking. The missing work is to compose them around one release object, two schema artifacts, a staged config file, and an explicit phase machine.

## Current release and schema truth

### Fleet-only selection

- `apps/infra/lib/stacks/container-stack.ts:19-36` calls `openclaw-version.json` the single image source. `container-stack.ts:918-929` chooses exactly one environment tag and puts it on the CDK base task definition.
- `apps/infra/lib/stacks/service-stack.ts:1283-1291` injects only the global dev/prod tags into the backend.
- `apps/backend/core/openclaw_version.py:92-113` resolves the deployed upstream/tag solely from the environment pin. It has no `owner_id` input.
- `openclaw-version.json:3-12` currently has one upstream/fleet line: `2026.8.1-isol8.3`, with dev and prod both on `2026.8.1-isol8.3-4e22eb8`.

There is no current model for release id, owner assignment, schema id, capability set, desired release, or confirmed active release.

### Useful target-aware logic already present

- `apps/backend/core/openclaw_version.py:132-156` has reusable dotted-version comparison; its own comment says the fleet pin can be wrong for a lagging live task.
- `apps/backend/core/openclaw_version.py:174-201` accepts an explicit extended tag for fork patch gates, correctly distinguishing `None` (fresh/global fallback) from `""` (unknown, gate closed).
- `apps/backend/core/containers/config.py:44-66` derives config shape from an explicit target tag when supplied. `config.py:1714`, `:2560`, and `:2831` use that target for metadata and shape/capability gates.
- `apps/backend/core/containers/ecs_manager.py:619-655` can inspect the image tag on either an owner's registered task definition or the CDK base family. This is useful observation, but it is not desired-state assignment.

### Single-schema limitation

- `apps/backend/core/services/config_schema.py:50-83` binds one `_SCHEMA_PATH` and caches one validator (`maxsize=1`).
- `config_schema.py:129-150`, `:153-206`, and `:231-263` expose no schema/release selector for validation, restore pruning, or write gating.
- `apps/backend/scripts/vendor_openclaw_schema.sh:22-29` reads one fork path and labels output with `openclaw-version.json["upstream"]`. It never verifies the actual checkout/ref/commit from which the bundle is built. `:70-72` records that manifest value only after generation, so an arbitrary checkout can be falsely labeled as the pin.
- `apps/backend/tests/chaos/test_schema_staleness.py:84-101` proves only that `PINNED_TAG` equals the manifest. It cannot prove the bundle came from that Git object. `:52-81` does correctly prove that the JSON schema was generated from the vendored zod bundle.

The operational target supplied to this inventory is upstream tag `v2026.9.3` at exact commit `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`; the extended image tag/digest and carried-fork commit do not exist yet. Fork installation is currently blocked pending approval for the pinned `pnpm@12.3.4` age exception (released Sep 4 versus the seven-day guard).

### Global version checks outside config generation

These will be wrong for mixed 8.1/9.x owners unless passed the resolved owner release:

- `apps/backend/core/services/catalog_slice.py:101-110` removes agent keys based on `deployed_upstream_at_least("2026.8.1")`, i.e. the environment rather than buyer/target owner.
- `apps/backend/routers/marketplace_import.py:217` and `:440-448` pass global `deployed_upstream_version()` into backup import compatibility.

## Lifecycle matrix

| Trigger/path | Current image/config behavior | Founder 9.x risk | Current evidence |
|---|---|---|---|
| Fresh provision | Registers from global CDK base and writes config for `_base_image_tag()` | Cannot select founder release | `_register_task_definition` omits `new_image` at `ecs_manager.py:822-866`; fresh config uses global base at `:3586-3595` |
| Stop/park | Scales desired count to zero; task definition remains | Safe by itself | `ecs_manager.py:1384-1453`; trial idle caller `connection_pool.py:3962-3965` |
| Clean explicit wake | Scales the existing service up without replacing its task definition | Preserves founder image | `container_wake.py:80-106`; `start_user_service` at `ecs_manager.py:1589-1727` |
| Silent wake/message arrival | Always dispatches full provision instead of the direct wake path | A stopped founder reaches the base-clone branch and downgrades | `container_wake.py:152-188`, especially `:185` |
| Stopped full provision/retry/recovery | Rewrites active config for the base image, registers from global base, and updates service | Downgrades founder and loses desired assignment | `ecs_manager.py:3214-3349`; target is explicitly `_base_image_tag()` at `:3284-3288` |
| Running full provision | Force-redeploys the current service revision | Preserves current image | `ecs_manager.py:3445-3487` (running branch uses `forceNewDeployment` without a new task definition) |
| Size-only resize | Clones global base; `new_image=None` means preserve **base**, not owner image | Silently downgrades founder on plan/admin/marketplace resize | `ecs_manager.py:1752-1826`; doc says keep current at `:1768-1770`, implementation passes `new_image=None` into base clone at `:1818-1826` |
| Explicit image update | Regenerates config for `new_image`, then registers base clone with explicit image | Installs 9.x once, but no durable assignment; config ordering is unsafe | `update_service.py:526-560`, `:649-665` |
| Config-lock/base drift refresh | Calls register-only size resize with no image | Silently returns founder to global base | `ecs_manager.py:1944-1969`; worker call at `update_service.py:490-518` |
| Slack secret refresh | Clones the owner's current task definition | Preserves image | `ecs_manager.py:887-1015`; callers `slack_install_service.py:197,245` |
| Admin reprovision | Stop/drain/dispatch full provision | Downgrades founder | `routers/admin.py:267-285`; `ecs_manager.py:2189-2258` |
| Admin canonical resize | Size-only base clone | Downgrades founder | `routers/admin.py:326-339`; `ecs_manager.py:2261-2282` |
| Provider cold switch | Stops, drains, dispatches full provision | Downgrades founder | `billing_service.py:1777` and `:1859` |
| Trial conversion | Calls full reprovision | Downgrades founder | `stripe_webhook_handlers.py:480-513` |
| Ultra plan up/down | Calls size-only resize | Downgrades founder | `stripe_webhook_handlers.py:830-896` |
| Marketplace size reconciliation | Calls size-only resize | Downgrades founder | `marketplace_listings.py:841-866` |
| Backend startup reconciliation | Resumes transition pollers and trial-stop checks only | No release/image drift detection or repair | `main.py:268-350` |

Other full-provision entry doors converge through `dispatch_provision` and the stopped/fresh logic above: `routers/container.py:486,526,988,1037`, `routers/container_recover.py:165,196,231`, `routers/admin.py:570`, `core/services/user_provision.py:427`, and `scripts/provision_runner.py:303-329`. `scripts/bedrock_gate_break_glass.py:108` also calls full reprovision. These need not each learn release policy if the canonical task-definition/config builders resolve it.

## Upgrade transaction defects

### Target config is made active too early

`apps/backend/core/services/update_service.py:547-560` calls `reapply_owner_config(... deployed_tag=<new image tag>)` before `resize_user_container`. `reapply_owner_config` documents that it is hot/config-only and that the running process watches the file (`:316-336`), then calls `write_user_configs` at `:377-386`. `write_user_configs` hardcodes `/owner/openclaw.json` at `ecs_manager.py:4290-4310`.

Therefore an 8.1 founder process can observe the 9.x document before ECS drains it. The direct config-patch step is even earlier (`update_service.py:470-474`) and also writes active config.

### Replacement can start after a failed config write

The stopped-service provision branch catches every config write exception and logs “continuing anyway” (`ecs_manager.py:3290-3293`), then registers/starts the task definition at `:3295-3349`. A schema, EFS, or generator failure must instead prevent activation/start.

### “Applied” means API call accepted, not release healthy

`resize_user_container` starts a detached transition poller for running services (`ecs_manager.py:1901-1920`). The update worker immediately marks its update `applied` at `update_service.py:675-689`. It has not awaited gateway reachability, config acceptance, or a smoke session. In contrast, `ecs_manager.wait_for_running` is an existing foreground gateway-health waiter (`ecs_manager.py:3644-3667`; health confirmation occurs at `:3826-3867`).

### Desired and active state are conflated

`queue_image_update` persists only `changes={"new_image": ...}` (`update_service.py:99-109`). The container row has a current task-definition pointer, but no durable release assignment. The update row can persist an immutable `target_task_definition_arn` under a claim (`update_repo.py:233-262`), yet terminal state is limited to `applied|failed` (`:265-307`). There is nowhere to record `staged`, `drained`, `activated`, `validating`, desired release, active release, staged config hash, or rollback target.

## Reusable abstractions

1. **Container row as owner-scoped desired-state home.** `container_repo.update_fields` is the existing scoped writer. If release fields live here, add them to `_UPSERT_PRESERVED_FIELDS`: `container_repo.py:117-141` currently preserves only provision lease and Bedrock gate fields, while `upsert` is a whole-item replace (`:331-412`). Without this, `create_user_service` can erase an assignment mid-reprovision.
2. **One release resolver.** Extend or sit beside `core/openclaw_version.py`; its parsing and conservative unknown handling are reusable. Callers should receive a release object rather than independently parse image strings.
3. **Base-clone builder.** `_build_register_kwargs_from_base(... new_image=...)` at `ecs_manager.py:668-790` is the correct structural drift fence. Always pass the resolved desired image; do not switch size/config refreshes to cloning the prior task definition.
4. **Register-only and drain.** `resize_user_container(... _register_only=True)` (`:1752-1863`) and `wait_until_drained` (`:2180-2187`) already separate preparation from rollout enough to build a safe transaction.
5. **Fenced update ledger.** `update_repo.claim_apply`, heartbeat/takeover, and `set_apply_target` (`update_repo.py:136-262`) already provide ownership and idempotent target persistence. Extend this row instead of adding a second rollout table.
6. **Gateway-health confirmation.** `wait_for_running` is already the required foreground convergence check. Only after it returns should `active_release_id` and the update's terminal phase move to healthy/applied.
7. **EFS locking.** `config_patcher._locked_rmw` (`config_patcher.py:97-176`) is the shared transition-aware lock and uses tempfile + atomic rename. `write_openclaw_config` accepts an arbitrary path (`config.py:2945-2962`) and takes the same sidecar lock, but currently uses direct `Path.write_text` (`:3027-3039`); staged write + atomic activation should reuse the patcher's atomic pattern.

## Minimal release contract

The smallest general contract that removes image-policy decisions from lifecycle callers is:

```python
@dataclass(frozen=True)
class OpenClawRelease:
    release_id: str
    image: str                 # full ECR ref; digest preferred once built
    upstream_version: str      # e.g. "2026.9.3"; never inferred from digest
    schema_id: str
    capabilities: frozenset[str]

def release_by_id(release_id: str) -> OpenClawRelease: ...

async def resolve_desired_release(
    owner_id: str,
    *,
    container: dict | None = None,
) -> OpenClawRelease: ...

async def resolve_active_release(
    owner_id: str,
    *,
    container: dict | None = None,
) -> OpenClawRelease: ...
```

Minimal container-row fields: `desired_release_id` (persistent assignment), `active_release_id` (written only after health), and optionally `active_config_sha256` for forensic/convergence checks. An absent assignment resolves to the current environment default for backward compatibility; a new/fresh row persists that resolved id during the same fenced provision. Never use the currently registered image as the desired assignment: doing so would make an accidental downgrade self-ratifying.

Required central consumers:

- `_register_task_definition(..., release: OpenClawRelease)` and `resize_user_container(..., release: OpenClawRelease | None = None)`; `None` must resolve owner desired release, not mean “take base image.”
- stopped and fresh branches of `provision_user_container`; `write_user_configs` must receive that exact same release object used to build the task definition.
- `prepare_config_lock_protocol_task_definition`; structural refresh must retain desired release.
- `reapply_owner_config`, `catalog_slice`, marketplace import compatibility, hot config patch schema selection.
- admin image update must assign a registered `release_id`, not accept an unrelated free-form image as the durable policy. A break-glass arbitrary image can remain explicit but must be labeled non-assigned/temporary.

## Dual-schema and staged activation contract

Suggested narrow signatures:

```python
def schema_errors(config: dict, *, schema_id: str) -> list[str] | None: ...
def prune_schema_rejected(base: dict, patch: dict, *, schema_id: str) -> dict: ...
def gate_config(
    config: dict,
    *, owner_id: str,
    context: str,
    schema_id: str,
    enforce: bool = False,
) -> None: ...

async def write_user_configs(
    owner_id: str,
    gateway_token: str,
    *,
    release: OpenClawRelease,
    config_path: Path | None = None,
    ...,
) -> None: ...

async def activate_staged_config(
    owner_id: str,
    *,
    staged_path: Path,
    expected_sha256: str,
    schema_id: str,
) -> None: ...
```

Use versioned artifacts such as `core/services/openclaw_config_schemas/<schema_id>.json` and cache validators by `schema_id`, not a singleton. Hot patches resolve **active** release/schema. A roll renders and validates against **target** release/schema into an owner-local staged filename (for example `openclaw.json.staged.<update_id>`), while `openclaw.json` remains the active 8.1 document.

Safe transaction using existing primitives:

1. Claim update; persist target release/schema/config hash and phase `staged`.
2. Register target task definition without deploying; persist immutable ARN.
3. Scale to zero and `wait_until_drained`; phase `drained`.
4. Under the active config lock, revalidate hash/schema and atomically replace `openclaw.json`; phase `activating`.
5. Point ECS at the persisted target ARN and desired count 1.
6. Foreground-await `wait_for_running`; perform the requested session/smoke validation; phase `healthy`, then set `active_release_id` and terminal `applied` together under the apply claim.
7. On pre-activation failure, leave active config/task unchanged. On post-activation boot failure, keep enough prior task-definition/config metadata to reverse both; never report applied merely because ECS accepted `UpdateService`.

The config patch at `update_service.py:470-474` must be classified: patches intended for the new release are applied to the staged document, while ordinary owner/admin hot patches continue to target active config and active schema.

## Exact source/test surface

### Required implementation files

- `apps/backend/core/openclaw_version.py` or new `core/services/openclaw_release.py`: registry and resolver.
- `openclaw-version.json` plus either a compatible `releases` extension/schema or a separate release manifest; retain the existing CDK default fields during migration.
- `apps/backend/core/repositories/container_repo.py`: preserve and conditionally update desired/active release fields.
- `apps/backend/core/containers/ecs_manager.py`: thread resolved release through fresh, stopped, resize, drift refresh, and config generation; remove swallowed config failure.
- `apps/backend/core/services/config_schema.py`: schema-id selection/cache.
- `apps/backend/core/containers/config.py`: release/version input and atomic staged write support.
- `apps/backend/core/services/config_patcher.py`: active-schema selection and a path-aware/activation primitive.
- `apps/backend/core/services/update_service.py` and `core/repositories/update_repo.py`: staged phases, immutable target metadata, foreground health, terminal assignment.
- `apps/backend/scripts/vendor_openclaw_schema.sh` and `tests/chaos/_schema/*`: versioned artifacts and verifiable source provenance.
- `apps/backend/core/services/catalog_slice.py` and `routers/marketplace_import.py`: owner/target release compatibility.
- `apps/backend/routers/admin.py`: target only known release ids and return truthful staged/healthy status.

### Regression tests to extend

- `tests/unit/test_openclaw_version.py`: default fallback, assigned founder, unknown release fail-closed, explicit upstream version separate from digest/tag.
- `tests/unit/repositories/test_container_repo.py`: `upsert` never erases desired/active release; conditional assignment/activation does not overwrite a newer rollout.
- `tests/unit/containers/test_ecs_manager.py`: size-only running/stopped resize, stopped reprovision, fresh provision, admin reprovision, and config-lock refresh all register the resolved owner image; config failure prevents start.
- `tests/unit/containers/test_deployed_image_tag.py`: task-definition observation remains distinct from desired release.
- `tests/unit/containers/test_provision_plan_sizing.py`: size and release survive together.
- `tests/unit/services/test_container_wake.py`: clean explicit wake preserves current revision; silent/marked full wake reprovisions to assigned release, not fleet base.
- `tests/unit/services/test_update_service.py`: old active config remains byte-identical until drain; target schema validates staged file; crash/reclaim reuses target ARN/config hash; update remains non-terminal until gateway health; rollback restores both prior config and task definition.
- `tests/unit/services/test_config_schema.py` and `tests/chaos/invariants/test_config_schema.py`: both 8.1 and 9.x validators coexist and cannot cross-validate by accident.
- `tests/chaos/test_schema_staleness.py`: artifact provenance is exact commit/ref, not just a copied manifest label.
- `tests/unit/services/test_change_provider_cold_running_container.py`, `tests/unit/services/test_stripe_webhook_handlers.py`, and `tests/unit/routers/test_marketplace_listings.py`: cold provider switch, plan conversion/resize, and marketplace size reconcile retain the assignment.
- `tests/unit/test_catalog_slice.py`, `tests/unit/services/test_catalog_slice.py`, and `tests/unit/routers/test_marketplace_import.py`: mixed-version owner compatibility.
- `tests/unit/test_admin_org_resolution.py`: mutation targeting fails closed when owner identity is ambiguous/unavailable.

## Smallest independently testable first slice

**Recommendation: land schema-vendoring provenance hardening first.** It is the smallest prerequisite that is useful before the 9.x image exists and while the pinned-pnpm approval is blocked; it does not claim to solve per-owner lifecycle.

Concrete command contract:

```text
apps/backend/scripts/vendor_openclaw_schema.sh \
  --source /absolute/fork/path \
  --ref v2026.9.3 \
  --commit 1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7 \
  --schema-id openclaw-2026.9.3
```

Before `pnpm install` or any output write, it should:

1. resolve `--ref^{commit}` and require it equals `--commit`;
2. require checked-out `HEAD` equals `--commit` (and reject relevant tracked modifications that can affect the schema/build);
3. derive the recorded ref/commit from Git, never from `openclaw-version.json["upstream"]`;
4. generate into temporary files and atomically publish the bundle, JSON schema, and provenance record only after all validation succeeds;
5. record `{schema_id, ref, commit, bundle_sha256, json_schema_sha256}`.

Add a verify-only mode (for example `--verify-source-only`) so `tests/unit/scripts/test_vendor_openclaw_schema.py` can create a tiny temporary Git repo and prove correct ref succeeds, mismatched commit/ref fails before toolchain execution, and modified schema inputs fail. Extend `test_schema_staleness.py` to assert the provenance hashes/commit correspond to the committed artifacts.

Test commands for that slice:

```bash
cd apps/backend
uv run pytest -q tests/unit/scripts/test_vendor_openclaw_schema.py
uv run pytest -q tests/chaos/test_schema_staleness.py
```

First runtime follow-up after an image digest exists: land only `OpenClawRelease` + container-row `desired_release_id` + resolver, then make `_register_task_definition`, `resize_user_container`, stopped/fresh provision, and config-lock refresh always pass the resolved image. Do not expose the 9.x assignment endpoint until dual schema + staged activation is present; otherwise the resolver removes downgrade drift but the active-config race remains.

Focused runtime test command:

```bash
cd apps/backend
uv run pytest -q \
  tests/unit/test_openclaw_version.py \
  tests/unit/repositories/test_container_repo.py \
  tests/unit/containers/test_deployed_image_tag.py \
  tests/unit/containers/test_ecs_manager.py \
  tests/unit/containers/test_provision_plan_sizing.py \
  tests/unit/services/test_container_wake.py \
  tests/unit/services/test_update_service.py
```

## Open PR overlap (read-only, checked 2026-09-08)

- [#1922 — fix(containers): reconcile provider switch desired zero](https://github.com/TheLightbulbCompany/lightbulb/pull/1922) modifies `ecs_manager.py` and `test_ecs_manager.py`. It adds a `PRIMARY/COMPLETED desiredCount=0` rescale inside the running waiter. The symbols are absent from current `main`, so the PR is not already incorporated. It overlaps the cold provider transition/waiter and should be rebased or deliberately superseded, but it does **not** add per-owner releases, dual schemas, or staged activation.
- [#1913 — fix(settings): apply a rotated BYO key instead of storing it silently](https://github.com/TheLightbulbCompany/lightbulb/pull/1913) adds a force redeploy of the current revision from settings-key rotation. It does not rebuild the task definition or choose an image, so it does not duplicate the proposed resolver.
- [#2400 — fix(paperclip): disarm orphaned runs for deleted containers](https://github.com/TheLightbulbCompany/lightbulb/pull/2400) touches only `operator_proxy.py` and its test; no overlap.
- The other seven open PRs shown by GitHub did not advertise image/config/container lifecycle changes. No existing open PR implements release assignment or staged schema activation.

## Blocking decisions and invariants

1. The 9.x extended image ref/digest and final carried-fork commit are not yet available; do not create a release assignment with only an upstream tag.
2. Pinned `pnpm@12.3.4` installation is awaiting the age-guard exception, so 9.x schema generation cannot currently complete.
3. Decide whether a founder/admin release assignment is allowed to target a Clerk **user** or an owner/container id. `resolve_admin_owner_id` currently picks the first org and fails open to personal mode on Clerk timeout/error (`admin_service.py:46-105`). A release mutation must require an explicit resolved `owner_id` or fail closed; a wrong-owner canary is worse than a rejected request.
4. Do not let `openclaw-version.json` global promotion be the founder mechanism. It updates the CDK base and backend global version gates and therefore changes fresh/rebuilt owners fleet-wide.
5. Preserve structural drift healing: use the CDK base for command/env/mounts, but always override its image with the resolved desired release.
6. Treat config-write/schema failures as transaction failures. The existing stopped-path “continuing anyway” behavior must not survive the 9.x path.
7. Keep desired release, observed task image, active release, target schema, and active config distinct. Collapsing any two recreates the current downgrade or activation race.
