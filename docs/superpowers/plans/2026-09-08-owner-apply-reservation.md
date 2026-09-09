# Owner Apply Reservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Atomically reserve an existing owner and queued update, retaining the exact original container identity and immutable release intent without touching the runtime.

**Architecture:** Add one two-row DynamoDB transaction and one consistent lookup to the existing pending-update repository. Reuse the configured resource-derived client and existing container/pending-update tables. The accepted generic-writer fences protect the reservation; no service calls this new API until the file/ECS boundaries are integrated.

**Tech Stack:** Existing Python3.12, boto3/botocore1.42.68, DynamoDB transactions, pytest/Moto. No dependency changes.

**Spec:** Submitted founder goal section6; `docs/superpowers/plans/2026-09-08-release-fencing-contract.md` reserve/assign contract; read-only API analysis at `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/docs/superpowers/plans/2026-09-08-release-repository-api.md`. This plan covers reservation only. Checkpoint, target persistence, takeover, finish, cancellation, guarded runtime effects and deployment are separate implementation gates, not completed by this task.

## Global Constraints

- Do not upgrade other production owners or change the new-user production image default under this goal.
- Never bypass sandbox approvals, branch protection, identity checks, billing limits or failing release gates.
- Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion.
- Do not expose secrets in commands, logs, reports, screenshots or transcripts.
- Keep one implementation writer at a time in a shared worktree.
- No candidate assignment may be enabled until paired-row transactions and all config/lifecycle effect boundaries participate.
- Existing container `id` is retained by normal upsert and changes on deletion/recreation. Reservation must pin it and persist it on the update, never adopt a recreated row's identity during recovery.

## Boundaries and rulings

Ruling: use the existing repository file and client, not a release table/client or generic transaction framework. Two small field lists and a private transaction-cancellation predicate are sufficient. Cost if wrong: a later coherent extraction may be needed; no new runtime architecture is introduced now.

Ruling: a release queue payload names `changes.target_release_id`, which must equal the explicit reservation argument. Pin the entire queued changes map and scheduling facts. Cost if wrong: the future dedicated queue endpoint must follow this contract; no existing caller currently creates this payload.

Ruling: release digest acceptance authority remains the immutable approved release record resolved by the service, keyed by the journal's exact `target_release_id`. Do not duplicate a mutable tag/digest field in this reservation. The eventual acceptance transaction must fence the same target ID and persisted ARN, after service verification of the final ECR/running digest. No registry record or healthy completion is created here. Cost if wrong: the later finish contract may require an additional immutable digest field before release enablement.

Ruling: legal release progression is reserved → prepared → stop_requested → drained → staged → activating → activated → start_requested → validating → healthy. Stop/start intents are set atomically when entering their requested phases; only the same owning executor clears the intent after conclusive completion. `needs_recovery` is a halt preserving phase/effect evidence, not an automatic takeover phase. Same-phase heartbeat cannot rewrite identity/target/hash facts. Staged hashes are write-once-or-equal. Finish requires validating, no unresolved effect, exact persisted target, and service-verified acceptance. This binds the next journal plan; this task implements only the initial reserved phase.

Ruling: require stable prior assignment (both desired/active absent, or equal nonempty IDs) before reservation. A desired/active mismatch without an existing reservation is repair-needed state, not permission to begin a different update. Cost if wrong: an explicitly supported migration state will need a dedicated repair path rather than silent normalization.

## File ownership / proof coverage

Modify only `apps/backend/core/repositories/update_repo.py` and `apps/backend/tests/unit/repositories/test_update_repo.py`. The controller owns plans/ledgers. Extend the existing Moto fixture with the containers table; no conftest or new fixture framework. Existing container repository and service callers are read-only context.

This task proves atomic reservation, identity/intent CAS, absence/NULL distinctions, native SDK serialization, and operational-error propagation. It does not prove real AWS fault settlement, configured IAM transaction permissions, filesystem locking, migration, candidate compatibility or live acceptance.

### Task 1: Add paired owner/update reservation

**Files:**
- Modify: `apps/backend/core/repositories/update_repo.py`
- Modify: `apps/backend/tests/unit/repositories/test_update_repo.py`

**Interfaces:**

```python
async def get_by_id(owner_id: str, update_id: str, *, consistent: bool = False) -> dict | None:
    ...

async def reserve_owner_apply(
    expected_container: dict,
    expected_update: dict,
    claim_id: str,
    *,
    target_release_id: str | None,
    prior_task_definition_arn: str,
    prior_desired_count: int,
    target_desired_count: int,
) -> bool:
    ...
```

Read this task first. Existing `get_table`, `_get_table`, `run_in_thread`, `utc_now_iso`, `time` and `uuid` are the reuse points. Add only stdlib `Decimal` and existing `botocore.exceptions.ClientError` imports when needed. The caller supplies strongly consistent snapshots and must hold the canonical owner recovery guard before invoking reservation; repository code acquires no filesystem lock, performs no ECS/credential/config operation and schedules no work.

- [ ] **Step 1: Add focused real-Moto tests, using test names containing `owner_apply` (including lookup tests).** Extend `dynamodb_table` to create `test-containers` with `owner_id` HASH in the same resource/context. Seed rows through that resource; do not mock away `transact_write_items` for success or race tests.

Representative behavioral test:

```python
@pytest.mark.asyncio
async def test_owner_apply_reservation_is_paired(dynamodb_table):
    from core.dynamodb import get_table
    from core.repositories import update_repo

    containers = get_table("containers")
    owner = {"owner_id": "u", "id": "original", "status": "running"}
    containers.put_item(Item=owner)
    queued = await update_repo.create("u", "release", "Candidate", {"target_release_id": "candidate"})
    assert await update_repo.reserve_owner_apply(
        owner, queued, "worker", target_release_id="candidate",
        prior_task_definition_arn="arn:aws:ecs:us-east-1:123456789012:task-definition/test:1",
        prior_desired_count=1, target_desired_count=1,
    )
    stored_c = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    stored_u = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert stored_c["release_update_id"] == queued["update_id"]
    assert stored_c["desired_release_id"] == stored_u["target_release_id"] == "candidate"
    assert stored_c["release_revision"] == stored_u["release_revision"] == 1
    assert stored_c["provision_claim_id"] == stored_c["provision_attempt_token"] == stored_u["apply_claim_id"] == "worker"
    assert stored_u["container_id"] == "original"
    assert stored_u["status"] == "applying" and stored_u["phase"] == "reserved"
    assert "active_release_id" not in stored_c and "prior_release_id" not in stored_u
    assert "prior_desired_release_id" not in stored_u
    assert stored_c["status"] == "running"
    assert stored_c["provision_claimed_at"] == stored_u["apply_claimed_at"]
    assert stored_c["updated_at"] == stored_u["updated_at"]
```

Also cover these exact cases, parameterizing same-shape cases:

1. Assigned stable old→candidate release preserves active old and both prior IDs; revision increments from a strongly read integral Decimal. Native nested changes map survives unchanged. Reserved pending TTL is removed, other unrelated fields preserved; input snapshots unchanged.
2. `target_release_id=None` for non-release size/base updates preserves desired/active exactly, absent for legacy or equal old IDs for assigned; no target_release_id is stored on U; both get the revision marker/claim. No active/status/runtime mutation. Release type without target, non-release with target, or queued target mismatch raises ValueError before repository I/O.
3. Competing different update IDs for one C: first succeeds, second returns False and changes neither row. A queued row claimed/scheduled/snoozed/edited immediately before the transaction causes False and leaves C untouched. Inject real writes at the outbound `run_in_thread` transaction boundary; no sleeps.
4. Independently move each C pinned field (`id`, `status`, `task_definition_arn`, desired, active, revision) after snapshot; require False and preserve winner data/U unchanged. Include absent revision→numeric0, absent desired→NULL, revision-only ABA, deletion, and delete/recreate with a new id. Include U deletion and owner mismatch validation.
5. Each C exclusion field present with nonempty value OR NULL prevents acquisition: release_update_id, provision_claim_id, provision_attempt_token, provision_execution_name. Include stopped/error C status with a present stale claim: reservation must not steal it. Each U reservation/execution metadata field below present, including NULL, refuses; applying/applied/failed U refuses. Also inject each C/U exclusion field at the outbound transaction boundary after clear snapshots, including NULL, to prove the actual write fence rather than only validation. Assert complete rows unchanged, no TTL or partial claim.
6. Validation rejects missing/empty/whitespace/nonstring owner/id/update/claim/prior ARN; non-dict snapshots/changes; revision negative, fractional Decimal, NaN, infinity, bool, float, string or NULL; counts negative/bool/floats; optional desired/active present NULL/empty or mismatched. Error text names fields only, never values. Integral Decimal revisions and integer zero are accepted; omitted revision means zero but is separately CAS-pinned as absent.
7. Lookup passes ConsistentRead false by default and true explicitly, exact composite key; present returns actual row, missing None; unexpected AWS errors propagate. Validate nonempty lookup keys.
8. Transaction cancellation classifier: return False only for an exactly two-entry reason list, each dictionary with Code in (`ConditionalCheckFailed`, `None` string, actual None), at least one conditional failure. Cover single/either/both conditional failure; propagate missing/malformed/wrong-count reasons, missing Code, no conditional failure, unknown/operational/mixed reasons, non-cancellation ClientError and transport timeout. For injected failures assert no row mutation and one call; no retries or read/adopt.

- [ ] **Step 2: Prove RED before implementation.** From `apps/backend`:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/repositories/test_update_repo.py -k owner_apply -q -o addopts=''
```

Expected: new tests fail because APIs do not exist. Record exact result and representative failures, then implement and rerun focused selection while iterating.

- [ ] **Step 3: Implement the lookup, validation and fixed two-row transaction.** The lookup is ordinary `get_item` with explicit `ConsistentRead`. Reservation validates before resolving tables. Keys/claim/prior ARN must be nonempty strings (reject whitespace-only; do not trim or modify values). Counts require `type(value) is int and value >= 0`. Revision accepts int or finite integral Decimal, excludes bool, rejects negatives, normalizes to int; omitted revision0 remains an absent condition. Optional release IDs are either omitted or nonempty strings; require desired/active equal with exact matching presence. `expected_update` must have nonempty update_type, dictionary changes and matching owner. A release requires matching `changes.target_release_id` and nonempty explicit target; a non-release requires target None. Raise ValueError for malformed arguments. A well-formed but ineligible snapshot returns False without I/O.

Ineligible C fields (must all be absent): `release_update_id`, `provision_claim_id`, `provision_attempt_token`, `provision_execution_name`. Ineligible U fields (must all be absent): `release_revision`, `container_id`, `apply_claim_id`, `apply_claimed_at`, `phase`, `target_release_id`, `prior_release_id`, `prior_desired_release_id`, `prior_task_definition_arn`, `prior_desired_count`, `target_desired_count`, `target_task_definition_arn`, `staged_config_sha256`, `prior_config_sha256`, `effect_kind`, `effect_claim_id`, `effect_executor_id`, `applied_at`, `failure_reason`. U status must be pending or scheduled; TTL alone is permitted and removed atomically.

Build exact-presence comparisons for C `id`, `status`, `task_definition_arn`, `desired_release_id`, `active_release_id`, `release_revision`; U `status`, `update_type`, `changes`, `scheduled_at`, `last_snoozed_at`, `force_by`. Every condition is inside the actual transaction action. Field presence is `field in snapshot`, not `.get()` truthiness. Alias field names with deterministic action-local placeholders. A short private helper for adding an equality/absence predicate to condition/names/values is acceptable if it avoids duplicate code; no expression-builder class.

```python
# For each fixed field f, with unique token n per action:
names[f"#p{n}"] = f
if f in snapshot:
    values[f":p{n}"] = snapshot[f]
    conditions.append(f"#p{n} = :p{n}")
else:
    conditions.append(f"attribute_not_exists(#p{n})")
```

Add `attribute_not_exists` predicates for every exclusion field even if the snapshot was clear, so a concurrent claim/marker is refused. Use different placeholder prefixes for update values and conditions. Do not generate unused bindings.

Take `now_epoch = int(time.time())` and `now_iso = utc_now_iso()` once. C SET: release_revision=r+1, release_update_id=U.update_id, provision_claim_id=claim, provision_attempt_token=claim, provision_claimed_at=now_epoch, updated_at=now_iso, plus desired_release_id=target only for release. No status/active/id or other change. U SET: status=applying, phase=reserved, container_id=C.id, apply_claim_id=claim, apply_claimed_at=now_epoch, updated_at=now_iso, release_revision=r+1, prior_task_definition_arn, prior_desired_count, target_desired_count; target_release_id only for release; prior_release_id and prior_desired_release_id only when corresponding C fields were present. REMOVE ttl. Preserve queued changes, scheduling, creation and description fields.

```python
containers = get_table("containers")
updates = _get_table()
await run_in_thread(
    containers.meta.client.transact_write_items,
    TransactItems=[
        {"Update": {"TableName": containers.name, "Key": {"owner_id": owner_id},
                    "ConditionExpression": " AND ".join(container_conditions),
                    "UpdateExpression": container_update_expression,
                    "ExpressionAttributeNames": container_names,
                    "ExpressionAttributeValues": container_values}},
        {"Update": {"TableName": updates.name,
                    "Key": {"owner_id": owner_id, "update_id": update_id},
                    "ConditionExpression": " AND ".join(update_conditions),
                    "UpdateExpression": update_update_expression,
                    "ExpressionAttributeNames": update_names,
                    "ExpressionAttributeValues": update_values}},
    ],
)
```

These are native Python Key/value dictionaries: the configured resource client serializes them recursively. Do not use TypeSerializer, typed DynamoDB JSON, a bare boto3 client, nested Attr condition objects, separate ConditionCheck on an updated row, or ReturnValues=ALL_NEW. Use one request, return True on success. No explicit ClientRequestToken reuse, external retry loop, post-write reread, new UUID owner identity, broad catch, logging config/change values, or fallback to one-row writes. On ClientError, return False only for the exact proven conditional-cancellation matrix from Step1; all other errors re-raise unchanged. A timeout is not proof of failed commit.

- [ ] **Step 4: Verify, self-review and commit.** Focused GREEN then this broader regression command once after final changes:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/repositories/test_update_repo.py tests/unit/repositories/test_container_repo.py tests/unit/repositories/test_container_repo_provision_claim.py tests/unit/services/test_update_service.py -q -o addopts=''
RUFF_CACHE_DIR=/private/tmp/lightbulb-owner-reserve-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff check core/repositories/update_repo.py tests/unit/repositories/test_update_repo.py
RUFF_CACHE_DIR=/private/tmp/lightbulb-owner-reserve-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff format --check core/repositories/update_repo.py tests/unit/repositories/test_update_repo.py
git diff --check
git add apps/backend/core/repositories/update_repo.py apps/backend/tests/unit/repositories/test_update_repo.py
git commit -m "feat(releases): atomically reserve owner apply records"
```

Run Git add/commit from repository root. Preserve all parent-owned documentation changes. Self-review must verify complete persisted rows in success/refusal tests, snapshot immutability, each CAS/exclusion field, no repeated transaction on refusal, and no service activation. Write the full report to the controller-provided report path, including RED/GREEN commands/results, static checks, scope, commit and concerns. Independent task review remains required.
