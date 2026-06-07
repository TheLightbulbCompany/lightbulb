# Per-Owner Bedrock AIP + Scoped IAM Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the account-global, tenant-unaware Bedrock path (one shared ECS task role with `bedrock:Invoke*` on `Resource:"*"`, and shared `us.anthropic.*` system inference-profile ids baked into every container's `openclaw.json`) with a **per-owner enforcement boundary**: three per-owner Bedrock application inference profiles (AIPs) — one per tier (opus/sonnet/haiku) — plus a per-owner IAM role scoped by the `bedrock:InferenceProfileArn` condition key to ONLY that owner's AIPs and the underlying foundation-model ARNs, with an explicit `Deny` on `bedrock:CreateInferenceProfile`. The container's model ids become the AIP ARNs (so a run is attributable from the invocation log — Plan 3) and a chat fails-closed if the AIPs are missing. This plan also OWNS the kill-switch primitive (`revoke_owner_bedrock` / `restore_owner_bedrock`, called by Plan 4) and the reverse-lookup (`resolve_owner_from_aip_arn`, called by Plan 3).

**Architecture:** A new free-function module `core/billing/bedrock_aip.py` mints + persists the per-owner AIPs (Bedrock control plane), mints + scopes the per-owner IAM role (IAM control plane), and exposes `resolve_owner_from_aip_arn` / `resolve_tier_from_aip_arn` / `resolve_owner_from_identity_arn` / `revoke_owner_bedrock` / `restore_owner_bedrock` — all importable without constructing `EcsManager` (which eagerly builds boto3 clients). The AIP→owner map is persisted both on the existing `containers` row (`bedrock_aip_arns` / `bedrock_role_arn` / `bedrock_revoked`) and in a new dedicated reverse-index table `bedrock-aip-owners` (PK `aip_arn` → `owner_id`) for Plan 3's hot meter path. `EcsManager.provision_user_container`'s `bedrock_claude` branch calls the mint **synchronously** before the service starts; the AIP ARNs thread into the `openclaw.json` builder (model ids) and the per-owner role ARN threads into `register_task_definition` via a new `task_role_arn` override (covering the register/create/resize/restart paths so a resize never reverts to the shared all-models role). CDK widens the backend role's `iam:PassRole` to a per-owner role-name prefix and grants the runtime IAM/Bedrock-control-plane actions (permissions-boundary-conditioned), removes the shared role's broad Bedrock grant, and adds a CDK-managed `OwnerBedrockBasePolicy` (EFS/logs/ssm) + `OwnerBedrockBoundary` permissions boundary that every backend-minted role must carry.

**Tech Stack:** Python 3.12, `boto3` (`bedrock` control-plane client for `create_inference_profile`/`list_inference_profiles`/`tag_resource`; `iam` client for `create_role`/`put_role_policy`/`delete_role_policy`/`get_role`; DynamoDB resource for the `containers` row + the new reverse table), `pytest` + `pytest-asyncio` + `moto[dynamodb]>=5.1.22` for DDB tests, `MagicMock` boto3 clients (the established repo convention) for Bedrock/IAM control-plane code moto can't fake. CDK in TypeScript (`aws-cdk-lib` + `Template.fromStack`/`Match` assertions). No frontend.

---

## Context for the implementer (read before Task 1)

This is **Plan 2 of 4** for Phase 1 of the Bedrock metering remediation (spec: `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md`). The other three plans:
- **Plan 1** (ALREADY WRITTEN — `docs/superpowers/plans/2026-06-07-ledger-interface-ddb-idempotency.md`) builds the swappable ledger seam: `core/billing/usage_event.py` (`UsageEvent`), `core/billing/ledger_interface.py` (`LedgerBackend` + `get_ledger()`), `core/billing/ddb_ledger_backend.py` (`DdbLedgerBackend`). **Consume, do not redefine.** This plan does not import them, but its module lives alongside them under `core/billing/`.
- **Plan 3** is the AWS invocation-log → Lambda meter. It **imports `resolve_owner_from_aip_arn` from this plan** to attribute a log record to an owner.
- **Plan 4** wires the enforcement loop and **calls `revoke_owner_bedrock` / `restore_owner_bedrock` from this plan** as the kill-switch.

**SHARED CONTRACTS this plan OWNS (Plans 3 & 4 import these exact names):**
- `core/billing/bedrock_aip.py :: async def resolve_owner_from_aip_arn(aip_arn: str) -> str | None` — reverse lookup, AIP-ARN → owner_id (Plan 3).
- `core/billing/bedrock_aip.py :: async def resolve_tier_from_aip_arn(aip_arn: str) -> str | None` — reverse lookup, AIP-ARN → model tier (`opus`/`sonnet`/`haiku`), for Plan 3's `price_from_aip_arn` pricing path.
- `core/billing/bedrock_aip.py :: async def resolve_owner_from_identity_arn(identity_arn: str) -> str | None` — fallback attribution: invocation-log `identity.arn` (the per-owner assumed-role ARN) → owner_id, used by Plan 3 when the AIP-ARN lookup misses.
- `core/billing/bedrock_aip.py :: async def revoke_owner_bedrock(owner_id: str) -> None` and `async def restore_owner_bedrock(owner_id: str) -> None` — kill-switch (Plan 4).
- A persisted per-owner map of AIP ARNs keyed to `owner_id`, stored on the `containers` row (`bedrock_aip_arns`) AND in the new `bedrock-aip-owners` reverse table (each row also carries the `tier` attribute, read by `resolve_tier_from_aip_arn`).

**Open decision RESOLVED (was in the grounding dossier §1d / §6):** the AIP→owner reverse index uses a **dedicated `bedrock-aip-owners` DDB table** (PK `aip_arn` → `owner_id`), written in `ensure_owner_aips` and read by `resolve_owner_from_aip_arn`. Rationale: the `containers` row keys on `owner_id` with no GSI on AIP ARN, and Plan 3's meter needs the reverse direction on a hot path; name-parsing the ARN is fragile. The table is granted **by name** in `service-stack.ts` (matching the integration-tables pattern at `service-stack.ts:384-404`) to avoid the cross-stack-export trap.

**Key existing facts (verified in code):**
- Shared task role with broad Bedrock grant: `container-stack.ts:191-199` (`bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream` on `Resource:["*"]`). EFS/logs/ssm grants on the same role at `:202-238`. Base task-def uses it: `container-stack.ts:333` (`taskRole: this.taskRole`).
- Backend `iam:PassRole` scoped to ONLY the shared roles: `service-stack.ts:251-260` (resources = `props.container.taskExecutionRole.roleArn` + `props.container.taskRole.roleArn`). **This is the first day-one blocker** — the backend cannot pass a per-owner role to ECS until this is widened.
- `_build_register_kwargs_from_base` defaults `taskRoleArn` from the base task-def: `ecs_manager.py:309`. The resize path (`ecs_manager.py:785-792`) and restart re-register (`ecs_manager.py:1384-1390`) clone from base, so without a per-owner override they silently revert the container to the shared role. The `hook_token` self-heal (`ecs_manager.py:766-784`, `:1344-1353`) is the precedent pattern.
- Config model ids hardcode `us.anthropic.*`: `config.py:367,376,389` (`build_provider_block`), `:424-425` (defaults), `premium_model_ref` `:449`, `cheap_model_ref` `:476`.
- The markup is the literal integer math `raw * 14 // 10` with `markup_multiplier=1.4` (`connection_pool.py:1216,1222`). **There is NO `BILLING_MARKUP` setting.** This plan does not price (Plan 3 does), but it must not invent a markup setting.
- FM ids (live-confirmed, 9 ARNs = 3 models × 3 regions): `anthropic.claude-opus-4-6-v1` (note the `-v1` suffix), `anthropic.claude-sonnet-4-6`, `anthropic.claude-haiku-4-5-20251001-v1:0` (date-stamp + `:0`), each in `us-east-1`/`us-east-2`/`us-west-2`. Confirmed against `bedrock_pricing.py:59-83` and `config.py:367,389`.
- System (source) profile ids to `copyFrom`: `us.anthropic.claude-opus-4-6-v1`, `us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-haiku-4-5-20251001-v1:0` (`config.py:367,376,389`).
- AWS account `877352799272`, region `us-east-1`, profile `isol8-admin` (CLAUDE.md). In CDK, `this.account` / `this.region` (e.g. `container-stack.ts:276`, `service-stack.ts:271`).

**Run backend commands from `apps/backend/` with `uv run`. Run CDK commands from `apps/infra/`. Do NOT use `rg` (it swallows stdout); use `grep -rn --include="*.py"` quoted globs or the Grep tool.**

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/backend/core/billing/bedrock_aip.py` (Create) | The owned module: AIP/IAM constants, the pure policy-document builder, AIP minting (`ensure_owner_aips`), role minting (`ensure_owner_bedrock_role`), reverse lookups (`resolve_owner_from_aip_arn`, `resolve_tier_from_aip_arn`, `resolve_owner_from_identity_arn`), kill-switch (`revoke_owner_bedrock` / `restore_owner_bedrock`), and the `containers`-row + reverse-table persistence helpers. Free functions create their own boto3 clients. |
| `apps/backend/core/config.py` (Modify, after line 103) | Add `BEDROCK_AIP_OWNERS_TABLE`, `BEDROCK_OWNER_ROLE_PREFIX`, `BEDROCK_OWNER_BOUNDARY_ARN`, `BEDROCK_AIP_FAN_OUT_REGIONS`, `BEDROCK_OWNER_BASE_POLICY_ARN` settings. |
| `apps/backend/core/containers/config.py` (Modify) | Thread `aip_arns` through `build_provider_block` (`:283`), `premium_model_ref` (`:436`), `cheap_model_ref` (`:459`), `build_openclaw_config_dict` (`:616`), `write_openclaw_config` (`:1137`). Fail-closed: bedrock + `aip_arns=None` raises. |
| `apps/backend/core/services/billing_service.py` (Modify) | Force the cold path for a hot-swap INTO `bedrock_claude` (`:670`) so the fail-closed `build_provider_block` raise never reaches `_hot_swap_provider` and the per-owner role gets attached by the cold-path task-def re-register. |
| `apps/backend/core/containers/ecs_manager.py` (Modify) | Add `task_role_arn` param to `_build_register_kwargs_from_base` (`:221`), `_register_task_definition` (`:328`), `create_user_service` (`:467`); thread it through resize (`:785`), restart re-register (`:1384`); wire the synchronous AIP+role mint into `provision_user_container`'s bedrock branch (`:1293`); pass `aip_arns` to `write_user_configs` (`:1752`). |
| `apps/infra/lib/stacks/service-stack.ts` (Modify) | Widen `iam:PassRole` to the owner-role prefix; add `iam:CreateRole`/`PutRolePolicy`/`DeleteRolePolicy`/`TagRole`/`GetRole`/`GetRolePolicy`/`AttachRolePolicy`/`DetachRolePolicy` (prefix-scoped, boundary-conditioned) + `bedrock:CreateInferenceProfile`/`Delete`/`Get`/`List`/`TagResource`/`UntagResource`/`ListTagsForResource`; grant the reverse table by name; export the role prefix/boundary/base-policy ARNs + reverse-table name as backend env vars. |
| `apps/infra/lib/stacks/container-stack.ts` (Modify) | Remove the shared role's broad Bedrock grant (`:191-199`); add the CDK-managed `OwnerBedrockBasePolicy` (EFS/logs/ssm `ManagedPolicy`) and `OwnerBedrockBoundary` (`ManagedPolicy` used as permissions boundary); expose their ARNs as public readonly members. |
| `apps/infra/lib/stacks/database-stack.ts` (Modify, after line 235) | Add the `bedrockAipOwnersTable` (PK `aip_arn`). |
| `apps/backend/tests/unit/billing/test_bedrock_aip.py` (Create) | All `bedrock_aip.py` unit tests (policy doc, persistence, reverse lookups — owner/tier/identity-arn, mint idempotency, role mint, kill-switch). |
| `apps/backend/tests/unit/containers/test_config_aip.py` (Create) | Config-builder AIP-ARN threading + fail-closed tests. |
| `apps/backend/tests/unit/services/test_billing_service.py` (Modify/Create) | Regression test: a switch INTO `bedrock_claude` takes the cold path (not the hot path) and does not raise. |
| (existing) `apps/backend/tests/unit/containers/test_config*.py`, `test_hook_token_provisioning.py`, `test_write_user_configs_seeds_friday.py` (Modify) | Pass `aip_arns=_AIPS` to every `bedrock_claude`-branch call now that the builder fails closed (Task 7 Step 7b enumerates all 8 files). |
| `apps/backend/tests/unit/containers/test_ecs_manager_aip.py` (Create) | ECS `task_role_arn` threading + provision-mint tests. |
| `apps/infra/test/service-stack.test.ts` (Modify) | Assert the new backend IAM statements + env vars. |
| `apps/infra/test/container-stack.test.ts` (Modify) | Assert the shared-role Bedrock grant removed + the base policy + boundary resources exist. |
| `apps/infra/test/database-stack.test.ts` (Modify) | Assert the new reverse table. |

---

### Task 1: AIP/IAM constants + the pure policy-document builder

**Files:**
- Create: `apps/backend/core/billing/bedrock_aip.py`
- Test: `apps/backend/tests/unit/billing/test_bedrock_aip.py`

This task creates the module with ONLY constants and one pure function (no boto3, no I/O). It is the highest-value test — it asserts the exact tenant-proof policy shape the 8/8 IAM policy-simulation verified.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/billing/test_bedrock_aip.py`:

```python
"""Unit tests for core.billing.bedrock_aip — per-owner AIP + scoped IAM role."""

from core.billing.bedrock_aip import (
    FAN_OUT_REGIONS,
    FM_IDS,
    SYSTEM_PROFILE_ID,
    TIERS,
    build_owner_bedrock_policy,
)


def _aip_arns(account: str = "877352799272") -> dict[str, str]:
    return {
        "opus": f"arn:aws:bedrock:us-east-1:{account}:application-inference-profile/aip-opus-1",
        "sonnet": f"arn:aws:bedrock:us-east-1:{account}:application-inference-profile/aip-sonnet-1",
        "haiku": f"arn:aws:bedrock:us-east-1:{account}:application-inference-profile/aip-haiku-1",
    }


def test_constants_shape():
    assert TIERS == ("opus", "sonnet", "haiku")
    assert FAN_OUT_REGIONS == ("us-east-1", "us-east-2", "us-west-2")
    assert FM_IDS == (
        "anthropic.claude-opus-4-6-v1",
        "anthropic.claude-sonnet-4-6",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    assert set(SYSTEM_PROFILE_ID) == set(TIERS)
    assert SYSTEM_PROFILE_ID["opus"] == "us.anthropic.claude-opus-4-6-v1"
    assert SYSTEM_PROFILE_ID["haiku"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_policy_has_invoke_allow_with_3_aips_and_9_fm_arns():
    arns = _aip_arns()
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=arns,
        account="877352799272",
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    allow = next(
        s for s in doc["Statement"]
        if s["Effect"] == "Allow" and "bedrock:InvokeModel" in s["Action"]
    )
    assert "bedrock:InvokeModelWithResponseStream" in allow["Action"]
    # 3 AIP ARNs are in Resource.
    for arn in arns.values():
        assert arn in allow["Resource"]
    # 9 FM ARNs (3 models x 3 regions) are in Resource.
    expected_fm = {
        f"arn:aws:bedrock:{region}::foundation-model/{model}"
        for region in ("us-east-1", "us-east-2", "us-west-2")
        for model in (
            "anthropic.claude-opus-4-6-v1",
            "anthropic.claude-sonnet-4-6",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        )
    }
    assert expected_fm.issubset(set(allow["Resource"]))
    assert len(expected_fm) == 9


def test_policy_condition_scopes_to_owner_aips_only():
    arns = _aip_arns()
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=arns,
        account="877352799272",
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    allow = next(
        s for s in doc["Statement"]
        if s["Effect"] == "Allow" and "bedrock:InvokeModel" in s["Action"]
    )
    cond = allow["Condition"]["ArnEquals"]["bedrock:InferenceProfileArn"]
    assert sorted(cond) == sorted(arns.values())


def test_policy_explicitly_denies_create_inference_profile():
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=_aip_arns(),
        account="877352799272",
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    deny = next(s for s in doc["Statement"] if s["Effect"] == "Deny")
    assert deny["Action"] == "bedrock:CreateInferenceProfile"
    assert deny["Resource"] == "*"


def test_policy_is_iam_document_version():
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=_aip_arns(),
        account="877352799272",
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    assert doc["Version"] == "2012-10-17"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.bedrock_aip'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/bedrock_aip.py`:

```python
"""Per-owner Bedrock application inference profiles (AIPs) + scoped IAM role.

This module is the Plan-2 enforcement boundary. It mints three per-owner AIPs
(one per tier: opus/sonnet/haiku), each ``copyFrom`` the corresponding system
regional inference profile (which fans out to the foundation model in
us-east-1/us-east-2/us-west-2), and a per-owner IAM role scoped by the
``bedrock:InferenceProfileArn`` condition key to ONLY that owner's AIPs plus the
underlying foundation-model ARNs in every fan-out region. An explicit
``Deny bedrock:CreateInferenceProfile`` stops a tenant from minting a wider
profile to escape the scope.

Everything is a FREE FUNCTION (not a method on EcsManager) so the reverse
lookup (Plan 3) and the kill-switch (Plan 4) can import these without
constructing EcsManager, which eagerly builds boto3 clients in __init__.

Shared contracts other plans import:
  * resolve_owner_from_aip_arn(aip_arn) -> str | None   (Plan 3, meter)
  * resolve_tier_from_aip_arn(aip_arn) -> str | None     (Plan 3, pricing)
  * resolve_owner_from_identity_arn(identity_arn) -> str | None  (Plan 3, fallback attribution)
  * revoke_owner_bedrock(owner_id) / restore_owner_bedrock(owner_id)  (Plan 4)
"""

from __future__ import annotations

# The three AIP tiers. Order matters only for deterministic iteration.
TIERS: tuple[str, str, str] = ("opus", "sonnet", "haiku")

# Bedrock cross-region inference fans out to these regions; the per-owner role
# must permit the foundation-model ARN in EACH of them.
FAN_OUT_REGIONS: tuple[str, str, str] = ("us-east-1", "us-east-2", "us-west-2")

# Foundation-model ids (LIVE-CONFIRMED). opus carries the "-v1" suffix; haiku
# carries the full date-stamp + ":0". An off-by-one suffix breaks invocation.
FM_IDS: tuple[str, str, str] = (
    "anthropic.claude-opus-4-6-v1",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
)

# System (source) regional inference-profile ids to copyFrom, per tier.
SYSTEM_PROFILE_ID: dict[str, str] = {
    "opus": "us.anthropic.claude-opus-4-6-v1",
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _foundation_model_arns(regions: tuple[str, ...]) -> list[str]:
    """The 9 (len(FM_IDS) x len(regions)) foundation-model ARNs.

    Invoking THROUGH an inference profile requires BOTH the profile ARN AND the
    underlying foundation-model ARN to be permitted — AWS evaluates both. The
    foundation-model ARN omits the account segment (it is AWS-owned):
    arn:aws:bedrock:<region>::foundation-model/<model>.
    """
    return [
        f"arn:aws:bedrock:{region}::foundation-model/{model}"
        for region in regions
        for model in FM_IDS
    ]


def build_owner_bedrock_policy(
    *,
    owner_id: str,
    aip_arns: dict[str, str],
    account: str,
    regions: tuple[str, ...],
) -> dict:
    """Build the per-owner Bedrock inline IAM policy document (PURE — no boto3).

    Allows InvokeModel/InvokeModelWithResponseStream on this owner's 3 AIP ARNs
    plus the 9 foundation-model ARNs, conditioned on
    bedrock:InferenceProfileArn == {this owner's AIPs}. Explicitly denies
    bedrock:CreateInferenceProfile so the tenant cannot widen its own scope.
    """
    owner_aip_list = sorted(aip_arns.values())
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OwnerScopedBedrockInvoke",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": owner_aip_list + _foundation_model_arns(regions),
                "Condition": {
                    "ArnEquals": {
                        "bedrock:InferenceProfileArn": owner_aip_list,
                    }
                },
            },
            {
                "Sid": "DenyTenantProfileEscalation",
                "Effect": "Deny",
                "Action": "bedrock:CreateInferenceProfile",
                "Resource": "*",
            },
        ],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): per-owner Bedrock IAM policy builder + AIP constants

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `containers`-row + reverse-table persistence helpers

**Files:**
- Modify: `apps/backend/core/config.py` (add settings after line 103)
- Modify: `apps/backend/core/billing/bedrock_aip.py` (add persistence helpers)
- Test: `apps/backend/tests/unit/billing/test_bedrock_aip.py` (add `TestPersistence`)

- [ ] **Step 1: Add the settings**

In `apps/backend/core/config.py`, immediately after the `OAUTH_TOKENS_TABLE: str = ""` line (currently line 103), add:

```python

    # Per-owner Bedrock AIP + scoped-role enforcement (Plan 2). Empty defaults
    # keep the backend booting in non-AWS unit-test contexts.
    BEDROCK_AIP_OWNERS_TABLE: str = os.getenv("BEDROCK_AIP_OWNERS_TABLE", "")
    """Reverse index PK aip_arn -> owner_id. Read by resolve_owner_from_aip_arn."""
    BEDROCK_OWNER_ROLE_PREFIX: str = os.getenv("BEDROCK_OWNER_ROLE_PREFIX", "")
    """IAM role-name prefix for backend-minted per-owner roles, e.g.
    'isol8-dev-owner-bedrock'. Set by CDK; matches the PassRole/CreateRole scope."""
    BEDROCK_OWNER_BOUNDARY_ARN: str = os.getenv("BEDROCK_OWNER_BOUNDARY_ARN", "")
    """Permissions-boundary policy ARN every backend-minted role must carry."""
    BEDROCK_OWNER_BASE_POLICY_ARN: str = os.getenv("BEDROCK_OWNER_BASE_POLICY_ARN", "")
    """CDK-managed EFS/logs/ssm managed-policy ARN attached to every per-owner role."""
    BEDROCK_AIP_FAN_OUT_REGIONS: str = os.getenv(
        "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2"
    )
    """Comma-separated regions the per-owner role permits the FM ARN in."""
```

- [ ] **Step 2: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_bedrock_aip.py`:

```python
import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aip_tables(monkeypatch):
    """moto-mocked containers + bedrock-aip-owners tables."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-aip-owners",
            KeySchema=[{"AttributeName": "aip_arn", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "aip_arn", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # The containers row must exist before update_fields can patch it.
        client.put_item(
            TableName="test-containers",
            Item={"owner_id": {"S": "org_abc"}, "id": {"S": "c-1"},
                  "created_at": {"S": "2026-06-07T00:00:00Z"}},
        )
        monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "test-")
        monkeypatch.setenv("BEDROCK_AIP_OWNERS_TABLE", "test-aip-owners")
        yield client


class TestPersistence:
    @pytest.mark.asyncio
    async def test_persist_then_read_back_from_row(self, aip_tables):
        from core.billing.bedrock_aip import (
            persist_owner_aips,
            read_owner_aips,
        )

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        got = await read_owner_aips("org_abc")
        assert got == arns

    @pytest.mark.asyncio
    async def test_persist_writes_reverse_rows(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        resp = aip_tables.get_item(
            TableName="test-aip-owners",
            Key={"aip_arn": {"S": arns["opus"]}},
        )
        assert resp["Item"]["owner_id"]["S"] == "org_abc"
        assert resp["Item"]["tier"]["S"] == "opus"

    @pytest.mark.asyncio
    async def test_read_owner_aips_none_when_unset(self, aip_tables):
        from core.billing.bedrock_aip import read_owner_aips

        assert await read_owner_aips("org_abc") is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestPersistence -v`
Expected: FAIL — `ImportError: cannot import name 'persist_owner_aips' from 'core.billing.bedrock_aip'`

- [ ] **Step 4: Write minimal implementation**

Add to the top of `apps/backend/core/billing/bedrock_aip.py` (after the existing `from __future__ import annotations`). `json` + `ClientError` are imported here once for the whole module (Tasks 4/5/6 all use them) so there are no scattered per-task import additions:

```python
import json
import os

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.repositories import container_repo
```

Add these persistence helpers to `apps/backend/core/billing/bedrock_aip.py` (below `build_owner_bedrock_policy`):

```python
def _aip_owners_table_name() -> str:
    name = os.environ.get("BEDROCK_AIP_OWNERS_TABLE") or settings.BEDROCK_AIP_OWNERS_TABLE
    if not name:
        raise RuntimeError("BEDROCK_AIP_OWNERS_TABLE is empty — backend is misconfigured.")
    return name


def _aip_owners_table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(
        _aip_owners_table_name()
    )


async def persist_owner_aips(
    owner_id: str,
    aip_arns: dict[str, str],
    *,
    role_arn: str | None = None,
) -> None:
    """Persist the per-owner AIP map (and optional role ARN) to BOTH the
    containers row and the bedrock-aip-owners reverse table.

    The reverse table is the read path for resolve_owner_from_aip_arn (Plan 3);
    the containers row is the read path for the config builder + resize/restart.
    """
    fields: dict = {"bedrock_aip_arns": aip_arns}
    if role_arn is not None:
        fields["bedrock_role_arn"] = role_arn
    await container_repo.update_fields(owner_id, fields)

    table = _aip_owners_table()
    for tier, arn in aip_arns.items():
        await _put_reverse_row(table, arn=arn, owner_id=owner_id, tier=tier)


async def _put_reverse_row(table, *, arn: str, owner_id: str, tier: str) -> None:
    from core.dynamodb import run_in_thread

    await run_in_thread(
        table.put_item,
        Item={"aip_arn": arn, "owner_id": owner_id, "tier": tier},
    )


async def read_owner_aips(owner_id: str) -> dict[str, str] | None:
    """Read the persisted per-owner AIP map from the containers row, or None."""
    row = await container_repo.get_by_owner_id(owner_id)
    if not row:
        return None
    arns = row.get("bedrock_aip_arns")
    if not arns:
        return None
    return {str(tier): str(arn) for tier, arn in arns.items()}
```

Note: `container_repo.update_fields` reads the env-prefixed `containers` table via `core.dynamodb.get_table("containers")`, so the test's `DYNAMODB_TABLE_PREFIX=test-` makes it hit `test-containers`. The reverse table uses its own explicit env var.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestPersistence -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Prove the module imports with empty env, no AWS creds (review B2)**

The module now imports `boto3`, `from core.config import settings`, and `from core.repositories import container_repo` at top level. Task 13 Step 4 asserts `CONTRACTS_OK` via a bare import — but that assertion is only meaningful if the bare import is proven side-effect-free in a credential-less context FIRST. `_aip_owners_table_name()` raising on empty `BEDROCK_AIP_OWNERS_TABLE` is fine (it's lazy, only on call); the risk is a module-load-time boto3 client/resource build. `core.dynamodb` builds its boto3 resource lazily, so this should pass — but prove it now rather than discover it in final verification.

Run (from `apps/backend/`, with the AIP env vars unset):
```bash
env -u BEDROCK_AIP_OWNERS_TABLE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY \
  uv run python -c "import core.billing.bedrock_aip; print('IMPORT_OK')"
```
Expected: `IMPORT_OK` — the bare import does NOT require AWS creds or a configured reverse table. If it raises (e.g. a boto3 client built at import time), move that client construction into a function (the `_bedrock_client`/`_iam_client`/`_aip_owners_table` factory pattern already does this for the clients added in later tasks).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/config.py apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): persist per-owner AIP map to containers row + reverse table

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `resolve_owner_from_aip_arn` (SHARED CONTRACT — Plan 3)

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py`
- Test: `apps/backend/tests/unit/billing/test_bedrock_aip.py` (add `TestResolveOwner`)

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_bedrock_aip.py`:

```python
class TestResolveOwner:
    @pytest.mark.asyncio
    async def test_round_trips_a_persisted_arn(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips, resolve_owner_from_aip_arn

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        assert await resolve_owner_from_aip_arn(arns["sonnet"]) == "org_abc"
        assert await resolve_owner_from_aip_arn(arns["haiku"]) == "org_abc"

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_arn(self, aip_tables):
        from core.billing.bedrock_aip import resolve_owner_from_aip_arn

        assert await resolve_owner_from_aip_arn(
            "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/nope"
        ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestResolveOwner -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_owner_from_aip_arn'`

- [ ] **Step 3: Write minimal implementation**

Add to `apps/backend/core/billing/bedrock_aip.py` (below `read_owner_aips`):

```python
async def resolve_owner_from_aip_arn(aip_arn: str) -> str | None:
    """SHARED CONTRACT (Plan 3 meter): given an AIP ARN from an invocation-log
    record, return the owning owner_id, or None if unknown.

    Reads the bedrock-aip-owners reverse table by PK — a single strongly
    bounded GetItem, safe on the hot meter path. No name-parsing.
    """
    from core.dynamodb import run_in_thread

    table = _aip_owners_table()
    resp = await run_in_thread(table.get_item, Key={"aip_arn": aip_arn})
    item = resp.get("Item")
    if not item:
        return None
    owner = item.get("owner_id")
    return str(owner) if owner else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestResolveOwner -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): resolve_owner_from_aip_arn reverse lookup (Plan 3 contract)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 6: Write the failing test for `resolve_tier_from_aip_arn` (SHARED CONTRACT — Plan 3 pricing)**

Plan 3's `price_from_aip_arn` needs the model TIER (`opus`/`sonnet`/`haiku`) for an AIP ARN to look up the per-token rate — the AIP ARN itself is opaque (it carries no model name). Task 2's `_put_reverse_row` already stores a `tier` attribute on every `bedrock-aip-owners` row (asserted by `TestPersistence::test_persist_writes_reverse_rows`), so the reader is the exact sibling of `resolve_owner_from_aip_arn`: same PK GetItem, return `item.get("tier")` instead of `item.get("owner_id")`.

Append to `apps/backend/tests/unit/billing/test_bedrock_aip.py` (inside `TestResolveOwner`):

```python
    @pytest.mark.asyncio
    async def test_resolve_tier_round_trips_a_persisted_arn(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips, resolve_tier_from_aip_arn

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        assert await resolve_tier_from_aip_arn(arns["opus"]) == "opus"
        assert await resolve_tier_from_aip_arn(arns["sonnet"]) == "sonnet"
        assert await resolve_tier_from_aip_arn(arns["haiku"]) == "haiku"

    @pytest.mark.asyncio
    async def test_resolve_tier_returns_none_for_unknown_arn(self, aip_tables):
        from core.billing.bedrock_aip import resolve_tier_from_aip_arn

        assert await resolve_tier_from_aip_arn(
            "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/nope"
        ) is None
```

- [ ] **Step 7: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestResolveOwner -k "tier" -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_tier_from_aip_arn'`

- [ ] **Step 8: Write minimal implementation**

Add to `apps/backend/core/billing/bedrock_aip.py` (immediately below `resolve_owner_from_aip_arn`):

```python
async def resolve_tier_from_aip_arn(aip_arn: str) -> str | None:
    """SHARED CONTRACT (Plan 3 pricing): given an AIP ARN from an invocation-log
    record, return its model tier ("opus"/"sonnet"/"haiku"), or None if unknown.

    Mirrors resolve_owner_from_aip_arn — same bedrock-aip-owners reverse-table
    PK GetItem (a single bounded read, safe on the hot pricing path) — but
    returns the `tier` attribute that _put_reverse_row stores on every row.
    Plan 3's price_from_aip_arn maps the tier to the per-token rate; the AIP ARN
    alone carries no model name, so the persisted tier is the only reliable key.
    """
    from core.dynamodb import run_in_thread

    table = _aip_owners_table()
    resp = await run_in_thread(table.get_item, Key={"aip_arn": aip_arn})
    item = resp.get("Item")
    if not item:
        return None
    tier = item.get("tier")
    return str(tier) if tier else None
```

- [ ] **Step 9: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestResolveOwner -v`
Expected: PASS (4 passed — both owner and tier readers green)

- [ ] **Step 10: Commit**

```bash
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): resolve_owner_from_aip_arn + resolve_tier_from_aip_arn reverse lookups (Plan 3 contracts)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3b: `resolve_owner_from_identity_arn` (SHARED CONTRACT — Plan 3 fallback attribution)

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py`
- Test: `apps/backend/tests/unit/billing/test_bedrock_aip.py` (add `TestResolveOwnerFromIdentity`)

**Design (read before coding):** Plan 3 attributes an invocation-log record to an owner primarily via `resolve_owner_from_aip_arn` (the log's `modelId` is the AIP ARN). When that lookup MISSES (e.g. a legacy/manually-cleared reverse row, or a record whose `modelId` is not an AIP), Plan 3 falls back to the log's `identity.arn` — the per-owner role the ECS task assumed. This plan mints that role with a **deterministic** name, `isol8-{env}-owner-bedrock-{owner_id}` (`_owner_role_name`, Task 5), so the owner_id is recoverable by name-parsing alone — NO extra storage needed (approach (a)). The only wrinkle: an invocation log records the **assumed-role STS form** `arn:aws:sts::{account}:assumed-role/{role-name}/{session-name}`, not the bare IAM `arn:aws:iam::{account}:role/{role-name}` form, so the parser must accept BOTH. It extracts the role name, strips the `isol8-{env}-owner-bedrock-` prefix, and returns the remainder as owner_id; anything that does not match the prefix (a non-owner role, the shared task role, an account-root principal) returns None.

It derives the env from `settings.ENVIRONMENT` inline (`settings.ENVIRONMENT or "dev"`, identical to the `_env` helper Task 4 introduces) so this function is **self-contained and order-independent** — it does NOT depend on `_env` / `_owner_role_name` existing yet (Task 3b runs before Tasks 4/5). No new constants. Owner ids legitimately contain the same hyphen/underscore characters as the prefix (e.g. `org_3DBS8...`, `user_2ab...`), so the parse uses a single prefix-strip (NOT a naive split on `-`, which would truncate an owner_id that contains a hyphen).

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_bedrock_aip.py`:

```python
class TestResolveOwnerFromIdentity:
    @pytest.mark.asyncio
    async def test_parses_owner_from_assumed_role_arn(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        # Invocation logs carry the STS assumed-role form, not the bare IAM role.
        arn = "arn:aws:sts::877352799272:assumed-role/isol8-dev-owner-bedrock-org_abc/abc-session-1234"
        assert await mod.resolve_owner_from_identity_arn(arn) == "org_abc"

    @pytest.mark.asyncio
    async def test_parses_owner_from_bare_iam_role_arn(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        arn = "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-user_xyz"
        assert await mod.resolve_owner_from_identity_arn(arn) == "user_xyz"

    @pytest.mark.asyncio
    async def test_owner_id_with_embedded_hyphen_survives(self, aip_tables, monkeypatch):
        """Clerk ids can contain hyphens; the prefix-strip (not a -split) must
        return the FULL owner_id, not a truncated first segment."""
        import core.billing.bedrock_aip as mod

        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        arn = "arn:aws:sts::877352799272:assumed-role/isol8-dev-owner-bedrock-org_a-b-c/sess"
        assert await mod.resolve_owner_from_identity_arn(arn) == "org_a-b-c"

    @pytest.mark.asyncio
    async def test_returns_none_for_non_owner_role(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        # The shared task role, an exec role, an account root — none match the
        # per-owner prefix, so attribution must fall through to None (the caller
        # treats None as "unattributable").
        for arn in (
            "arn:aws:sts::877352799272:assumed-role/isol8-dev-container-SharedTaskRole/sess",
            "arn:aws:iam::877352799272:role/some-other-role",
            "arn:aws:iam::877352799272:root",
            "not-an-arn",
        ):
            assert await mod.resolve_owner_from_identity_arn(arn) is None

    @pytest.mark.asyncio
    async def test_env_scoped_prefix_rejects_wrong_env(self, aip_tables, monkeypatch):
        """A prod identity ARN must NOT resolve under a dev backend (the prefix
        is env-scoped), so cross-env log bleed cannot misattribute."""
        import core.billing.bedrock_aip as mod

        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        arn = "arn:aws:sts::877352799272:assumed-role/isol8-prod-owner-bedrock-org_abc/sess"
        assert await mod.resolve_owner_from_identity_arn(arn) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestResolveOwnerFromIdentity -v`
Expected: FAIL — `AttributeError: module 'core.billing.bedrock_aip' has no attribute 'resolve_owner_from_identity_arn'`

- [ ] **Step 3: Write minimal implementation**

Add to `apps/backend/core/billing/bedrock_aip.py` (below `resolve_tier_from_aip_arn`). It is **self-contained** — it derives the env inline from `settings.ENVIRONMENT`, so it does NOT depend on the `_env` / `_owner_role_name` helpers introduced in Tasks 4/5 (Task 3b runs first). When those helpers later exist they happen to produce the identical prefix, so the parse stays in lockstep with the mint:

```python
async def resolve_owner_from_identity_arn(identity_arn: str) -> str | None:
    """SHARED CONTRACT (Plan 3 fallback attribution): given an invocation-log
    `identity.arn` (the per-owner role the ECS task assumed), return the owning
    owner_id, or None if the ARN is not a per-owner Bedrock role.

    Used by Plan 3 ONLY when resolve_owner_from_aip_arn misses (legacy/cleared
    reverse row, or a record whose modelId is not an AIP). No extra storage:
    the per-owner role name is deterministic — isol8-{env}-owner-bedrock-{owner}
    (matches _owner_role_name, see ensure_owner_bedrock_role) — so the owner_id
    is the role-name suffix after the env-scoped prefix.

    Accepts BOTH the assumed-role STS form an invocation log records
    (arn:aws:sts::<acct>:assumed-role/<role-name>/<session>) AND the bare IAM
    role form (arn:aws:iam::<acct>:role/<role-name>). Uses a single prefix-strip
    (not a '-'-split) so an owner_id containing hyphens (Clerk ids can) is
    returned in full. Env-scoped: a wrong-env prefix returns None (no cross-env
    misattribution). This function does NOT touch DynamoDB — it is pure string
    parsing — but is async to match the sibling resolvers' call shape.
    """
    if not identity_arn or ":" not in identity_arn:
        return None

    # Extract the IAM role NAME from either ARN form.
    resource = identity_arn.split(":", 5)[-1]  # everything after the 5th ':'
    if resource.startswith("assumed-role/"):
        # assumed-role/<role-name>/<session-name>
        parts = resource.split("/")
        if len(parts) < 2:
            return None
        role_name = parts[1]
    elif resource.startswith("role/"):
        # role/<role-name>  (role names can themselves contain '/', so keep the
        # remainder after the first segment).
        role_name = resource.split("/", 1)[1]
    else:
        return None

    # Inline env (identical to the _env helper Task 4 adds) keeps this resolver
    # independent of later-task helpers; the prefix still matches the mint's
    # _owner_role_name.
    env = settings.ENVIRONMENT or "dev"
    prefix = f"isol8-{env}-owner-bedrock-"
    if not role_name.startswith(prefix):
        return None
    owner_id = role_name[len(prefix):]
    return owner_id or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestResolveOwnerFromIdentity -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): resolve_owner_from_identity_arn fallback attribution (Plan 3 contract)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `ensure_owner_aips` — idempotent AIP minting

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py`
- Test: `apps/backend/tests/unit/billing/test_bedrock_aip.py` (add `TestEnsureAips`)

**Design (read before coding):** `ensure_owner_aips(owner_id)` first reads the persisted map (`read_owner_aips`) and returns it unchanged if all 3 tiers are present (idempotent — no Bedrock call on a re-run). Otherwise, for each missing tier, it RECONCILES against Bedrock reality before creating: it tries `create_inference_profile(inferenceProfileName=<env-prefixed name>, modelSource={"copyFrom": SYSTEM_PROFILE_ID[tier]}, tags=[...])`, but if that raises `ConflictException` (a same-named AIP already exists from a prior crashed run where the row was never persisted), it falls back to `list_inference_profiles(typeEquals="APPLICATION")` and adopts the existing AIP's `inferenceProfileArn` by matching the env-prefixed name — instead of hard-failing. It collects the `inferenceProfileArn` per tier, persists via `persist_owner_aips`, and returns the map. The AIP name is env-prefixed (`isol8-{env}-{owner_id}-{tier}`) so dev and prod never collide (W53 lesson); the name is also the join key for the conflict-recovery list. The `bedrock` control-plane client is created inside the function (moto can't fake it; tests inject a MagicMock via `monkeypatch` on the module's `_bedrock_client`).

**Why the conflict reconcile matters (review C2/C3):** without it, `ensure_owner_aips` is only idempotent when the persisted row and Bedrock reality agree. A crash AFTER `create_inference_profile` but BEFORE `persist_owner_aips` (or a manually-cleared row, or a legacy owner) leaves the AIP live in Bedrock but absent from the row; the next provision/restart re-runs `create_inference_profile` with the same `inferenceProfileName` and gets `ConflictException` — a hard 500 on a kill-switch-grade provisioning path. The list-and-adopt fallback makes the mint converge on the existing AIP rather than crash. This mirrors Task 5's role mint, which already `get_role`-then-skips.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_bedrock_aip.py`:

```python
from unittest.mock import MagicMock


class TestEnsureAips:
    @pytest.mark.asyncio
    async def test_creates_one_aip_per_tier_and_persists(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        created = {}

        def _create(**kwargs):
            # Map the copyFrom source profile back to a tier for assertion.
            src = kwargs["modelSource"]["copyFrom"]
            tier = next(t for t, p in mod.SYSTEM_PROFILE_ID.items() if p == src)
            created[tier] = kwargs
            arn = f"arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/{tier}-aip"
            return {"inferenceProfileArn": arn}

        fake = MagicMock()
        fake.create_inference_profile.side_effect = _create
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        arns = await mod.ensure_owner_aips("org_abc")

        assert set(arns) == {"opus", "sonnet", "haiku"}
        assert fake.create_inference_profile.call_count == 3
        # Env-prefixed name, tag carries owner_id.
        assert created["opus"]["inferenceProfileName"] == "isol8-dev-org_abc-opus"
        tags = {t["key"]: t["value"] for t in created["opus"]["tags"]}
        assert tags["owner_id"] == "org_abc"
        assert tags["tier"] == "opus"
        # Persisted: reverse lookup resolves.
        assert await mod.resolve_owner_from_aip_arn(arns["opus"]) == "org_abc"

    @pytest.mark.asyncio
    async def test_idempotent_no_recreate_when_already_present(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        await mod.persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")

        fake = MagicMock()
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        got = await mod.ensure_owner_aips("org_abc")
        assert got == arns
        fake.create_inference_profile.assert_not_called()
        # C1 self-heal: short-circuit re-puts reverse rows so the index is
        # consistent even if a prior run's reverse PutItem partially failed.
        assert await mod.resolve_owner_from_aip_arn(arns["haiku"]) == "org_abc"

    @pytest.mark.asyncio
    async def test_conflict_adopts_existing_aip_instead_of_failing(self, aip_tables, monkeypatch):
        """A crashed prior run left the AIP in Bedrock but never persisted the
        row. The re-mint hits ConflictException and must ADOPT the existing AIP
        (via list_inference_profiles) rather than raise (review C2/C3)."""
        import core.billing.bedrock_aip as mod
        from botocore.exceptions import ClientError

        def _create(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "exists"}},
                "CreateInferenceProfile",
            )

        def _list(**kwargs):
            return {
                "inferenceProfileSummaries": [
                    {"inferenceProfileName": "isol8-dev-org_abc-opus",
                     "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/opus-exist"},
                    {"inferenceProfileName": "isol8-dev-org_abc-sonnet",
                     "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/sonnet-exist"},
                    {"inferenceProfileName": "isol8-dev-org_abc-haiku",
                     "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/haiku-exist"},
                ]
            }

        fake = MagicMock()
        fake.create_inference_profile.side_effect = _create
        fake.list_inference_profiles.side_effect = _list
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        arns = await mod.ensure_owner_aips("org_abc")

        assert arns["opus"].endswith("opus-exist")
        assert set(arns) == {"opus", "sonnet", "haiku"}
        # Adopted AIPs are persisted -> reverse lookup resolves.
        assert await mod.resolve_owner_from_aip_arn(arns["sonnet"]) == "org_abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestEnsureAips -v`
Expected: FAIL — `AttributeError: module 'core.billing.bedrock_aip' has no attribute '_bedrock_client'` (and `ensure_owner_aips`)

- [ ] **Step 3: Write minimal implementation**

Add to `apps/backend/core/billing/bedrock_aip.py` (below `resolve_owner_from_aip_arn`):

```python
def _bedrock_client():
    """Bedrock CONTROL-PLANE client (create/list/tag inference profiles).
    NB: 'bedrock', not 'bedrock-runtime' (which is the invoke client)."""
    return boto3.client("bedrock", region_name=settings.AWS_REGION)


def _env() -> str:
    return settings.ENVIRONMENT or "dev"


def _aip_name(owner_id: str, tier: str) -> str:
    """Env-prefixed AIP name so dev/prod never collide on a global name."""
    return f"isol8-{_env()}-{owner_id}-{tier}"


async def ensure_owner_aips(owner_id: str) -> dict[str, str]:
    """Idempotently create the 3 per-owner AIPs (one per tier); return
    {tier: aip_arn}. Persists to the containers row + reverse table.

    Idempotent: if all 3 tiers are already persisted, returns them without any
    Bedrock control-plane call — but STILL re-puts the reverse-index rows
    (review C1). The row update and the reverse PutItems in persist_owner_aips
    are NOT atomic; a prior partial failure (row written, one reverse PutItem
    failed) leaves resolve_owner_from_aip_arn unable to attribute that AIP. The
    reverse PutItems are idempotent (same key, same value), so re-running them
    on the short-circuit path is cheap and self-heals the index.
    """
    from core.dynamodb import run_in_thread

    existing = await read_owner_aips(owner_id)
    if existing and all(tier in existing for tier in TIERS):
        # Self-heal the reverse index in case a prior run's reverse PutItems
        # partially failed after the row was written (C1 inconsistency window).
        table = _aip_owners_table()
        for tier, arn in existing.items():
            await _put_reverse_row(table, arn=arn, owner_id=owner_id, tier=tier)
        return existing

    client = _bedrock_client()
    arns: dict[str, str] = dict(existing or {})
    for tier in TIERS:
        if tier in arns:
            continue
        name = _aip_name(owner_id, tier)
        try:
            resp = await run_in_thread(
                client.create_inference_profile,
                inferenceProfileName=name,
                modelSource={"copyFrom": SYSTEM_PROFILE_ID[tier]},
                tags=[
                    {"key": "owner_id", "value": owner_id},
                    {"key": "tier", "value": tier},
                    {"key": "ManagedBy", "value": "isol8-backend"},
                ],
            )
            arns[tier] = resp["inferenceProfileArn"]
        except ClientError as e:
            # ConflictException == a same-named AIP already exists from a prior
            # crashed run (created in Bedrock but never persisted to the row).
            # Adopt it instead of hard-failing — makes the mint converge rather
            # than 500 a restart/re-provision. NB: boto3's bedrock typed
            # ConflictException is a ClientError subclass, so this one clause
            # covers both real and unit-test (generic ClientError) raises.
            if e.response["Error"]["Code"] != "ConflictException":
                raise
            adopted = await _find_existing_aip_arn(client, name)
            if not adopted:
                raise  # conflict but we can't find it — surface the real error
            arns[tier] = adopted

    await persist_owner_aips(owner_id, arns)
    return arns


async def _find_existing_aip_arn(client, name: str) -> str | None:
    """List application inference profiles and return the ARN of the one whose
    name matches `name`, or None. Used to recover from a ConflictException on a
    re-mint where the AIP exists in Bedrock but the row was never persisted."""
    from core.dynamodb import run_in_thread

    token: str | None = None
    while True:
        kwargs: dict = {"typeEquals": "APPLICATION", "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = await run_in_thread(client.list_inference_profiles, **kwargs)
        for prof in resp.get("inferenceProfileSummaries", []):
            if prof.get("inferenceProfileName") == name:
                return prof.get("inferenceProfileArn")
        token = resp.get("nextToken")
        if not token:
            return None
```

(`json` and `ClientError` are already imported at module top from Task 2 Step 4 — no new import needed here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestEnsureAips -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): ensure_owner_aips idempotent per-tier AIP minting

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `ensure_owner_bedrock_role` — idempotent scoped-role minting

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py`
- Test: `apps/backend/tests/unit/billing/test_bedrock_aip.py` (add `TestEnsureRole`)

**Design (read before coding):** `ensure_owner_bedrock_role(owner_id, aip_arns)` mints the per-owner IAM role idempotently:
1. Compute the role name `isol8-{env}-owner-bedrock-{owner_id}` (matches `BEDROCK_OWNER_ROLE_PREFIX`).
2. `iam.get_role(RoleName=...)` — if it exists (no `NoSuchEntity`), skip `create_role` (idempotent) and just re-put the inline policy (so a re-mint after a tier-ARN change refreshes the scope).
3. On `create_role`: assume-role policy = `ecs-tasks.amazonaws.com`; `PermissionsBoundary=BEDROCK_OWNER_BOUNDARY_ARN`; tag `owner_id`.
4. `iam.attach_role_policy(PolicyArn=BEDROCK_OWNER_BASE_POLICY_ARN)` — the CDK-managed EFS/logs/ssm base.
5. `iam.put_role_policy(PolicyName="owner-bedrock", PolicyDocument=json.dumps(build_owner_bedrock_policy(...)))` — the scoped Bedrock inline policy.
6. Persist the role ARN to the containers row; return it.

The `iam` client is created in `_iam_client()` (MagicMock-injected in tests). The regions come from `settings.BEDROCK_AIP_FAN_OUT_REGIONS` (split on comma).

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_bedrock_aip.py`:

```python
import json
from botocore.exceptions import ClientError


def _no_such_entity():
    return ClientError(
        {"Error": {"Code": "NoSuchEntity", "Message": "not found"}}, "GetRole"
    )


class TestEnsureRole:
    @pytest.mark.asyncio
    async def test_creates_role_with_boundary_base_and_scoped_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        fake = MagicMock()
        fake.get_role.side_effect = _no_such_entity()
        fake.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_OWNER_BOUNDARY_ARN",
                            "arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-boundary", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN",
                            "arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-base", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS",
                            "us-east-1,us-east-2,us-west-2", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns)

        assert role_arn == "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"
        # create_role carries the permissions boundary.
        ck = fake.create_role.call_args.kwargs
        assert ck["RoleName"] == "isol8-dev-owner-bedrock-org_abc"
        assert ck["PermissionsBoundary"] == "arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-boundary"
        # base policy attached.
        fake.attach_role_policy.assert_called_once_with(
            RoleName="isol8-dev-owner-bedrock-org_abc",
            PolicyArn="arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-base",
        )
        # scoped inline policy put with the 3 AIPs + deny.
        pk = fake.put_role_policy.call_args.kwargs
        assert pk["RoleName"] == "isol8-dev-owner-bedrock-org_abc"
        doc = json.loads(pk["PolicyDocument"])
        allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow")
        assert sorted(allow["Condition"]["ArnEquals"]["bedrock:InferenceProfileArn"]) == sorted(arns.values())
        # role ARN persisted on the row.
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_role_arn"]["S"] == role_arn

    @pytest.mark.asyncio
    async def test_idempotent_existing_role_repts_policy_no_create(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN",
                            "arn:aws:iam::877352799272:policy/base", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS",
                            "us-east-1,us-east-2,us-west-2", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns)
        assert role_arn.endswith("isol8-dev-owner-bedrock-org_abc")
        fake.create_role.assert_not_called()
        fake.put_role_policy.assert_called_once()  # scope refreshed on existing role

    @pytest.mark.asyncio
    async def test_skip_bedrock_grant_does_not_put_scoped_policy(self, aip_tables, monkeypatch):
        """skip_bedrock_grant=True keeps the role + base policy but does NOT
        (re)put the scoped Bedrock inline policy — so a re-provision of a
        revoked owner does not un-revoke (review C6)."""
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN",
                            "arn:aws:iam::877352799272:policy/base", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role(
            "org_abc", arns, skip_bedrock_grant=True
        )
        assert role_arn.endswith("isol8-dev-owner-bedrock-org_abc")
        # base policy still attached (role is bootable), but Bedrock NOT granted.
        fake.attach_role_policy.assert_called_once()
        fake.put_role_policy.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestEnsureRole -v`
Expected: FAIL — `AttributeError: module 'core.billing.bedrock_aip' has no attribute '_iam_client'`

- [ ] **Step 3: Write minimal implementation**

(`json` and `ClientError` are already imported at module top from Task 2 Step 4.)

Add to `apps/backend/core/billing/bedrock_aip.py` (below `ensure_owner_aips`):

```python
def _iam_client():
    return boto3.client("iam", region_name=settings.AWS_REGION)


def _owner_role_name(owner_id: str) -> str:
    """Per-owner role name. The CDK PassRole/CreateRole grants scope to the
    'isol8-{env}-owner-bedrock-*' prefix, so this MUST start with it."""
    return f"isol8-{_env()}-owner-bedrock-{owner_id}"


def _fan_out_regions() -> tuple[str, ...]:
    raw = settings.BEDROCK_AIP_FAN_OUT_REGIONS or "us-east-1,us-east-2,us-west-2"
    return tuple(r.strip() for r in raw.split(",") if r.strip())


_ASSUME_ROLE_DOC = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


async def ensure_owner_bedrock_role(
    owner_id: str,
    aip_arns: dict[str, str],
    *,
    skip_bedrock_grant: bool = False,
) -> str:
    """Idempotently create/update the per-owner IAM role scoped to those AIPs +
    the foundation-model ARNs in every fan-out region; return its ARN.

    Re-running refreshes the scoped inline policy (so a tier-ARN change is
    picked up) but never re-creates an existing role.

    skip_bedrock_grant=True creates/keeps the role + base (EFS/logs/ssm) policy
    but does NOT (re)put the scoped Bedrock inline policy. Used by provision for
    a revoked owner so a re-provision/restart does not silently un-revoke the
    kill-switch (review C6). Plan 4's restore_owner_bedrock is the only path
    that re-grants Bedrock.
    """
    from core.dynamodb import run_in_thread

    client = _iam_client()
    role_name = _owner_role_name(owner_id)

    try:
        got = await run_in_thread(client.get_role, RoleName=role_name)
        role_arn = got["Role"]["Arn"]
    except ClientError as e:
        # boto3's typed iam NoSuchEntityException IS a ClientError subclass, so
        # one `except ClientError` catches both the real-client typed raise and
        # the unit test's generic ClientError. Do NOT use
        # `except client.exceptions.NoSuchEntityException` — with a MagicMock
        # client that attribute is a MagicMock (not an exception class), and
        # `except <non-class>` raises TypeError at except-evaluation time.
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        role_arn = await _create_owner_role(client, role_name, owner_id)

    # (Re)attach the CDK-managed EFS/logs/ssm base policy — idempotent.
    if settings.BEDROCK_OWNER_BASE_POLICY_ARN:
        await run_in_thread(
            client.attach_role_policy,
            RoleName=role_name,
            PolicyArn=settings.BEDROCK_OWNER_BASE_POLICY_ARN,
        )

    # (Re)put the scoped Bedrock inline policy — refreshes on every call,
    # UNLESS the owner is revoked (kill-switch held; Plan 4 restores it).
    if not skip_bedrock_grant:
        account = role_arn.split(":")[4]
        doc = build_owner_bedrock_policy(
            owner_id=owner_id,
            aip_arns=aip_arns,
            account=account,
            regions=_fan_out_regions(),
        )
        await run_in_thread(
            client.put_role_policy,
            RoleName=role_name,
            PolicyName="owner-bedrock",
            PolicyDocument=json.dumps(doc),
        )

    await container_repo.update_fields(owner_id, {"bedrock_role_arn": role_arn})
    return role_arn


async def _create_owner_role(client, role_name: str, owner_id: str) -> str:
    from core.dynamodb import run_in_thread

    create_kwargs: dict = {
        "RoleName": role_name,
        "AssumeRolePolicyDocument": json.dumps(_ASSUME_ROLE_DOC),
        "Description": f"Isol8 per-owner Bedrock role for {owner_id}",
        "Tags": [
            {"Key": "owner_id", "Value": owner_id},
            {"Key": "ManagedBy", "Value": "isol8-backend"},
        ],
    }
    if settings.BEDROCK_OWNER_BOUNDARY_ARN:
        create_kwargs["PermissionsBoundary"] = settings.BEDROCK_OWNER_BOUNDARY_ARN
    resp = await run_in_thread(client.create_role, **create_kwargs)
    return resp["Role"]["Arn"]
```

Note on exception handling: do NOT write `except client.exceptions.NoSuchEntityException`. A `MagicMock()` client auto-creates `.exceptions.NoSuchEntityException` as a child MagicMock — NOT a valid exception class — and `except <non-class>` raises `TypeError: catching classes that do not inherit from BaseException` at except-evaluation time, BEFORE any later clause is reached. Since boto3's real `iam` `NoSuchEntityException` is itself a `ClientError` subclass, a SINGLE `except ClientError as e:` that branches on `e.response["Error"]["Code"] == "NoSuchEntity"` covers BOTH the real-client typed raise and the unit test's generic `ClientError`. The test (`test_creates_role_with_boundary_base_and_scoped_policy`) makes `get_role` raise a real `ClientError` with `Code == "NoSuchEntity"`, which this single clause handles.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestEnsureRole -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): ensure_owner_bedrock_role mints boundary-scoped per-owner role

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `revoke_owner_bedrock` / `restore_owner_bedrock` (SHARED CONTRACT — Plan 4)

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py`
- Test: `apps/backend/tests/unit/billing/test_bedrock_aip.py` (add `TestKillSwitch`)

**Design (read before coding):** the kill-switch toggles the per-owner role's Bedrock access by deleting (revoke) or re-putting (restore) the scoped inline policy, and stamps `bedrock_revoked` on the containers row. `revoke_owner_bedrock(owner_id)` calls `iam.delete_role_policy(RoleName, PolicyName="owner-bedrock")` (idempotent on `NoSuchEntity`) and sets `bedrock_revoked=True`. `restore_owner_bedrock(owner_id)` reads the persisted AIP map (`read_owner_aips`), re-runs the scoped `put_role_policy` (reuses `ensure_owner_bedrock_role`'s policy path), and sets `bedrock_revoked=False`. IAM is ~5-15s eventually consistent — Plan 4 pairs revoke with `desired=0` for the guaranteed instant stop; this module only flips the policy + state.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_bedrock_aip.py`:

```python
class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_revoke_deletes_policy_and_sets_flag(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        await mod.persist_owner_aips("org_abc", arns,
                                     role_arn="arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc")
        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.revoke_owner_bedrock("org_abc")

        fake.delete_role_policy.assert_called_once_with(
            RoleName="isol8-dev-owner-bedrock-org_abc", PolicyName="owner-bedrock"
        )
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is True

    @pytest.mark.asyncio
    async def test_revoke_idempotent_on_missing_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        await mod.persist_owner_aips(
            "org_abc",
            {"opus": "a", "sonnet": "b", "haiku": "c"},
            role_arn="arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc",
        )
        fake = MagicMock()
        fake.delete_role_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity", "Message": "gone"}}, "DeleteRolePolicy"
        )
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.revoke_owner_bedrock("org_abc")  # must not raise
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is True

    @pytest.mark.asyncio
    async def test_restore_reputs_policy_and_clears_flag(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        await mod.persist_owner_aips("org_abc", arns,
                                     role_arn="arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc")
        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS",
                            "us-east-1,us-east-2,us-west-2", raising=False)

        await mod.restore_owner_bedrock("org_abc")

        pk = fake.put_role_policy.call_args.kwargs
        assert pk["RoleName"] == "isol8-dev-owner-bedrock-org_abc"
        assert pk["PolicyName"] == "owner-bedrock"
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestKillSwitch -v`
Expected: FAIL — `AttributeError: module 'core.billing.bedrock_aip' has no attribute 'revoke_owner_bedrock'`

- [ ] **Step 3: Write minimal implementation**

Add to `apps/backend/core/billing/bedrock_aip.py` (below `_create_owner_role`):

```python
async def revoke_owner_bedrock(owner_id: str) -> None:
    """SHARED CONTRACT (Plan 4 kill-switch): remove the per-owner role's scoped
    Bedrock inline policy so the container can no longer invoke any model, and
    stamp bedrock_revoked=True.

    IAM is ~5-15s eventually consistent — Plan 4 pairs this with ECS
    desired-count=0 for the guaranteed instant stop. Idempotent: a missing
    policy (NoSuchEntity) is treated as already-revoked.
    """
    from core.dynamodb import run_in_thread

    client = _iam_client()
    role_name = _owner_role_name(owner_id)
    try:
        await run_in_thread(
            client.delete_role_policy,
            RoleName=role_name,
            PolicyName="owner-bedrock",
        )
    except ClientError as e:
        # NoSuchEntity == already revoked (idempotent). A SINGLE `except
        # ClientError` covers both the real iam typed exception (a ClientError
        # subclass) and the test's generic ClientError. Do NOT use
        # `except getattr(client.exceptions, "NoSuchEntityException", ClientError)`
        # — with a MagicMock client that attribute resolves to a MagicMock, and
        # `except <non-class>` raises TypeError at except-evaluation time before
        # any fallback clause runs.
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    await container_repo.update_fields(owner_id, {"bedrock_revoked": True})


async def restore_owner_bedrock(owner_id: str) -> None:
    """SHARED CONTRACT (Plan 4): re-attach the per-owner role's scoped Bedrock
    inline policy from the persisted AIP map, and clear bedrock_revoked.

    Raises if the owner has no persisted AIPs (nothing to restore — caller
    must re-provision first).
    """
    arns = await read_owner_aips(owner_id)
    if not arns:
        raise RuntimeError(
            f"Cannot restore Bedrock for {owner_id}: no persisted AIP map "
            "(re-provision the container first)."
        )
    await ensure_owner_bedrock_role(owner_id, arns)
    await container_repo.update_fields(owner_id, {"bedrock_revoked": False})
```

Note: a SINGLE `except ClientError as e:` branching on `e.response["Error"]["Code"] == "NoSuchEntity"` is correct here — boto3's iam `NoSuchEntityException` IS a `ClientError` subclass, so it catches both the real-client typed raise and the unit test's generic `ClientError` (`test_revoke_idempotent_on_missing_policy` raises exactly this). The earlier `except getattr(client.exceptions, "NoSuchEntityException", ClientError)` form is WRONG: with a `MagicMock` client `client.exceptions.NoSuchEntityException` is a MagicMock (not an exception class), so the `except` clause itself raises `TypeError` at evaluation time before the fallback clause can run.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py::TestKillSwitch -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full module suite**

Run: `uv run pytest tests/unit/billing/test_bedrock_aip.py -v`
Expected: PASS (all classes green: constants/policy + Persistence + ResolveOwner + EnsureAips + EnsureRole + KillSwitch)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/unit/billing/test_bedrock_aip.py
git commit -m "feat(bedrock): revoke/restore_owner_bedrock kill-switch (Plan 4 contract)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Thread AIP ARNs into the config builder (FAIL-CLOSED)

**Files:**
- Modify: `apps/backend/core/containers/config.py`
- Test: `apps/backend/tests/unit/containers/test_config_aip.py` (Create)

**Design (read before coding):** add an `aip_arns: dict[str, str] | None = None` param to `build_provider_block`, `premium_model_ref`, `cheap_model_ref`, `build_openclaw_config_dict`, `write_openclaw_config`. In the `bedrock_claude` branch:
- When `aip_arns` is provided, substitute `aip_arns["opus"|"sonnet"|"haiku"]` for the three model `id` fields (`config.py:367,376,389`), the `agents.defaults.model.primary`/`fallbacks` refs (`:424-425`), and the premium/cheap refs (`:449,476`). The model ref form is `amazon-bedrock/<AIP-ARN>`.
- When `aip_arns is None` AND `provider_choice == "bedrock_claude"`, **raise `ValueError`** (fail loud at provision, not silently `AccessDeniedException` at first chat). The non-bedrock branches ignore `aip_arns`.

The `cost` blocks (`:371,380,393`) stay keyed by tier — they are priced data, not invocation ids.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/containers/test_config_aip.py`:

```python
"""Tests for per-owner AIP-ARN threading into the openclaw.json builder."""

import pytest

from core.containers.config import (
    build_openclaw_config_dict,
    build_provider_block,
    cheap_model_ref,
    premium_model_ref,
)

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


def test_provider_block_uses_aip_arns_as_model_ids():
    providers, default_model, _plugins = build_provider_block(
        provider_choice="bedrock_claude",
        user_id="org_abc",
        byo_provider=None,
        aip_arns=_AIPS,
    )
    model_ids = {m["id"] for m in providers["amazon-bedrock"]["models"]}
    assert model_ids == set(_AIPS.values())
    assert default_model["primary"] == f"amazon-bedrock/{_AIPS['sonnet']}"
    assert default_model["fallbacks"] == [f"amazon-bedrock/{_AIPS['haiku']}"]


def test_provider_block_bedrock_without_aips_raises():
    with pytest.raises(ValueError, match="aip_arns"):
        build_provider_block(
            provider_choice="bedrock_claude",
            user_id="org_abc",
            byo_provider=None,
            aip_arns=None,
        )


def test_premium_and_cheap_refs_use_aip_arns():
    assert premium_model_ref("bedrock_claude", None, aip_arns=_AIPS) == f"amazon-bedrock/{_AIPS['opus']}"
    assert cheap_model_ref("bedrock_claude", None, aip_arns=_AIPS) == f"amazon-bedrock/{_AIPS['haiku']}"


def test_byo_path_ignores_aip_arns():
    # A non-bedrock provider must not require aip_arns.
    providers, default_model, _ = build_provider_block(
        provider_choice="byo_key",
        user_id="org_abc",
        byo_provider="anthropic",
        aip_arns=None,
    )
    assert default_model["primary"] == "anthropic/claude-sonnet-4-6"


def test_build_config_dict_bedrock_without_aips_raises():
    with pytest.raises(ValueError, match="aip_arns"):
        build_openclaw_config_dict(
            user_id="org_abc",
            gateway_token="tok",
            provider_choice="bedrock_claude",
            aip_arns=None,
        )


def test_build_config_dict_bedrock_with_aips_emits_arn_default():
    config = build_openclaw_config_dict(
        user_id="org_abc",
        gateway_token="tok",
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
    )
    assert config["agents"]["defaults"]["model"]["primary"] == f"amazon-bedrock/{_AIPS['sonnet']}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/containers/test_config_aip.py -v`
Expected: FAIL — `TypeError: build_provider_block() got an unexpected keyword argument 'aip_arns'`

- [ ] **Step 3: Modify `build_provider_block`**

In `apps/backend/core/containers/config.py`, change the signature (line 283-288) from:

```python
def build_provider_block(
    *,
    provider_choice: str,
    user_id: str,
    byo_provider: str | None,
) -> tuple[dict, dict, dict]:
```

to:

```python
def build_provider_block(
    *,
    provider_choice: str,
    user_id: str,
    byo_provider: str | None,
    aip_arns: dict[str, str] | None = None,
) -> tuple[dict, dict, dict]:
```

Then, inside the `if provider_choice == "bedrock_claude":` branch, immediately after the comment block (just before `bedrock_models = [`), insert the fail-closed guard and resolve the per-tier ids:

```python
        # FAIL-CLOSED: once the per-owner IAM role is scoped to AIP ARNs only,
        # emitting the system us.anthropic.* ids produces a config the role
        # cannot invoke -> every chat AccessDeniedException. Require the owner's
        # AIP ARNs here; raise loud at provision rather than silently at first
        # chat. ensure_owner_aips (bedrock_aip.py) is the upstream producer.
        if not aip_arns or not all(t in aip_arns for t in ("opus", "sonnet", "haiku")):
            raise ValueError(
                "bedrock_claude requires aip_arns with opus/sonnet/haiku keys "
                "(per-owner AIP ARNs). Run ensure_owner_aips before write_user_configs."
            )
        opus_id = aip_arns["opus"]
        sonnet_id = aip_arns["sonnet"]
        haiku_id = aip_arns["haiku"]
```

Replace the three `"id": "us.anthropic..."` lines (currently `:367,376,389`) with `"id": opus_id,`, `"id": sonnet_id,`, `"id": haiku_id,` respectively.

Replace the default-model block (currently `:424-425`):

```python
                "primary": "amazon-bedrock/us.anthropic.claude-sonnet-4-6",
                "fallbacks": ["amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"],
```

with:

```python
                "primary": f"amazon-bedrock/{sonnet_id}",
                "fallbacks": [f"amazon-bedrock/{haiku_id}"],
```

- [ ] **Step 4: Modify `premium_model_ref` and `cheap_model_ref`**

Change `premium_model_ref` (line 436) signature and bedrock branch from:

```python
def premium_model_ref(provider_choice: str, byo_provider: str | None) -> str | None:
```
...
```python
    if provider_choice == "bedrock_claude":
        return "amazon-bedrock/us.anthropic.claude-opus-4-6-v1"
```

to:

```python
def premium_model_ref(
    provider_choice: str, byo_provider: str | None, *, aip_arns: dict[str, str] | None = None
) -> str | None:
```
...
```python
    if provider_choice == "bedrock_claude":
        if not aip_arns or "opus" not in aip_arns:
            raise ValueError("bedrock_claude premium_model_ref requires aip_arns['opus']")
        return f"amazon-bedrock/{aip_arns['opus']}"
```

Change `cheap_model_ref` (line 459) signature and bedrock branch from:

```python
def cheap_model_ref(provider_choice: str, byo_provider: str | None) -> str | None:
```
...
```python
    if provider_choice == "bedrock_claude":
        return "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
```

to:

```python
def cheap_model_ref(
    provider_choice: str, byo_provider: str | None, *, aip_arns: dict[str, str] | None = None
) -> str | None:
```
...
```python
    if provider_choice == "bedrock_claude":
        if not aip_arns or "haiku" not in aip_arns:
            raise ValueError("bedrock_claude cheap_model_ref requires aip_arns['haiku']")
        return f"amazon-bedrock/{aip_arns['haiku']}"
```

- [ ] **Step 5: Modify `build_openclaw_config_dict`**

Change the `build_openclaw_config_dict` signature (line 616) by adding `aip_arns: dict[str, str] | None = None` after `integration_mcp_servers: dict | None = None,`:

```python
    integration_mcp_servers: dict | None = None,
    aip_arns: dict[str, str] | None = None,
) -> dict:
```

Change the three call sites inside `build_openclaw_config_dict` (currently `:661-665`, `:668`, `:673`):

```python
    providers_config, default_model, plugin_entries = build_provider_block(
        provider_choice=provider_choice,
        user_id=user_id,
        byo_provider=byo_provider,
        aip_arns=aip_arns,
    )
```
```python
    ceo_model = premium_model_ref(provider_choice, byo_provider, aip_arns=aip_arns)
```
```python
    cheap_model = cheap_model_ref(provider_choice, byo_provider, aip_arns=aip_arns)
```

- [ ] **Step 6: Modify `write_openclaw_config`**

Change the `write_openclaw_config` signature (line 1137) by adding `aip_arns: dict[str, str] | None = None` after `integration_mcp_servers: dict | None = None,`:

```python
    integration_mcp_servers: dict | None = None,
    aip_arns: dict[str, str] | None = None,
) -> None:
```

Change the `build_openclaw_config_dict` call inside `write_openclaw_config` (currently `:1187-1199`) by adding `aip_arns=aip_arns,` to the kwargs (after `integration_mcp_servers=integration_mcp_servers,`):

```python
        integration_mcp_servers=integration_mcp_servers,
        aip_arns=aip_arns,
    )
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/unit/containers/test_config_aip.py -v`
Expected: PASS (6 passed)

- [ ] **Step 7b: Update EVERY existing bedrock-branch test (enumerated — they will now raise)**

The new fail-closed requirement breaks every existing test that drives the `bedrock_claude` branch without `aip_arns`. These are NOT optional follow-ups — they are part of this task and must be edited in this step. The exact files + call sites (verified by `grep -n "bedrock_claude\|build_provider_block\|build_openclaw_config_dict\|premium_model_ref\|cheap_model_ref\|write_openclaw_config" tests/unit/containers/`):

Define a shared fixture at the top of each file (or import it):

```python
_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}
```

1. **`test_config_hooks_rendering.py`** — the `defaults` dict (`:25`) and the two explicit `build_openclaw_config_dict(provider_choice="bedrock_claude")` calls (`:86`, `:92`). Add `aip_arns=_AIPS` to `defaults` and both calls.
2. **`test_config.py`** — the `_cfg(provider_choice="bedrock_claude")` helper `defaults` (`:18-26`) and the two `write_openclaw_config(provider_choice="bedrock_claude")` calls (`:366`, `:385`). Add `aip_arns=_AIPS` to the helper's `defaults` and both write calls. The parametrized rows `("bedrock_claude", {})` (`:315,329,344`) flow through `_cfg`, so fixing the helper covers them.
3. **`test_hook_token_provisioning.py`** — `provision_user_container("user_test_123", provider_choice="bedrock_claude")` (`:214`). This drives the ECS provision path; `write_user_configs` is already patched (`:207`), but the new Task-9 mint (`ensure_owner_aips`/`ensure_owner_bedrock_role`) will fire. Patch them with `AsyncMock` returning `_AIPS` / a role ARN (mirror `TestBedrockProvisionMint` in Task 9) so this test doesn't hit Bedrock/IAM.
4. **`test_config_provider_routing.py`** — the heaviest. The matrix tests `test_premium_model_ref_matrix` (`:158` row + `:170` call) and `test_cheap_model_ref_matrix` (`:319,338` rows + `:331` call) assert the OLD hardcoded `amazon-bedrock/us.anthropic.*` refs, which are now REMOVED. **Move the `bedrock_claude` rows OUT of the no-`aip_arns` matrix** into a dedicated AIP-aware assertion (e.g. `premium_model_ref("bedrock_claude", None, aip_arns=_AIPS) == f"amazon-bedrock/{_AIPS['opus']}"`) — note `test_config_aip.py::test_premium_and_cheap_refs_use_aip_arns` (Step 1) already covers this, so the cleanest fix is to DELETE the `bedrock_claude` rows from both matrices here and rely on `test_config_aip.py`. Also add `aip_arns=_AIPS` to `test_bedrock_claude_branch` (`:102-114`), the `build_provider_block(provider_choice="bedrock_claude")` call (`:415-416`), and every `write_openclaw_config(...provider_choice="bedrock_claude")` invocation in this file (`:240-243`, `:406-409`, and any parametrized `("bedrock_claude", None)` rows at `:261,287` that flow into `write_openclaw_config`).
5. **`test_write_user_configs_seeds_friday.py`** — two `manager.write_user_configs(provider_choice="bedrock_claude")` calls (`:79-82`, `:130-133`). `write_openclaw_config` is patched (`:59`, `:113`), so the raise won't fire from inside; but `write_user_configs` now accepts `aip_arns` (Task 9 Step 4) — pass `aip_arns=_AIPS` for realism (optional since the inner builder is mocked, but keep the call shape current).
6. **`test_config_paperclip_agents.py`** — five `build_openclaw_config_dict(provider_choice="bedrock_claude")` calls (`:15,30,55,73,94`) + two `write_openclaw_config(provider_choice="bedrock_claude")` (`:115,136`). Add `aip_arns=_AIPS` to all seven.
7. **`test_config_paperclip_mcp.py`** — eleven `build_openclaw_config_dict(provider_choice="bedrock_claude")` calls (`:27,53,73,89,106,124,141,169,188,207,223`). Add `aip_arns=_AIPS` to all eleven.

(`test_ecs_manager.py` also matches `bedrock_claude` but only via the provision path; its mocking is handled in Task 8/9's regression-run steps, not here.)

Run: `grep -rn --include="*.py" "bedrock_claude" tests/unit/containers/`
Expected: lists the 8 files above. Confirm every `build_provider_block` / `build_openclaw_config_dict` / `premium_model_ref` / `cheap_model_ref` / `write_openclaw_config` call with `provider_choice="bedrock_claude"` now also passes `aip_arns=_AIPS` (or has its matrix row moved/deleted per item 4).

Run: `uv run pytest tests/unit/containers/test_config_hooks_rendering.py tests/unit/containers/test_config.py tests/unit/containers/test_config_provider_routing.py tests/unit/containers/test_config_paperclip_agents.py tests/unit/containers/test_config_paperclip_mcp.py -v`
Expected: PASS — after the edits above. The `test_config_cost_blocks_match_bedrock_pricing_rates` test (cited at `config.py:363`) asserts the `cost` blocks, which are unchanged (still keyed by tier), so it passes once it provides `aip_arns`.

- [ ] **Step 8: Force the cold path for a hot-swap INTO `bedrock_claude` (REGRESSION FIX)**

`build_provider_block` / `premium_model_ref` / `cheap_model_ref` now `raise ValueError` for `bedrock_claude` when `aip_arns is None`. This BREAKS a second, non-test caller: `core/services/billing_service.py :: _hot_swap_provider` (`:773-786`), which calls all three with `byo_provider=None` and **no `aip_arns`**. The hot path is taken for any Bedrock ↔ ChatGPT-OAuth switch that does not involve BYO (`billing_service.py:670-697`, `involves_byo` gate). A personal owner switching ChatGPT-OAuth → Bedrock would hit `_hot_swap_provider`, raise `ValueError` (NOT caught by `except _HotSwapUnavailableError` / `except ConfigPatchError` at `:693`/`:797`), and propagate as a 500.

Why force the cold path rather than thread `aip_arns` into the hot path: the hot path deliberately re-uses the *running* container's task role (it only Track-1-patches `openclaw.json`, it does NOT re-register the task definition — that is the whole point of "no container restart"). A switch INTO Bedrock requires the container to run on the **per-owner scoped task role**, which only a task-def re-register (the cold path) can attach. Patching the config to AIP-ARN model ids while the task still runs on the (now Bedrock-less) shared role would AccessDenied on first chat. So Bedrock targets MUST take the cold path, where Task 9's mint + `task_role_arn` re-register runs.

In `core/services/billing_service.py`, change the hot/cold path selector (`:670`) from:

```python
    involves_byo = current_choice == "byo_key" or provider_choice == "byo_key"

    if not involves_byo:
```

to:

```python
    involves_byo = current_choice == "byo_key" or provider_choice == "byo_key"

    # A switch INTO bedrock_claude must take the COLD path: the per-owner
    # scoped task role is attached only by a task-def re-register
    # (provision_user_container, Task 9), which the hot path never does. The
    # config builder now fails closed on bedrock without aip_arns, so the hot
    # path cannot synthesize a bedrock provider block anyway.
    targets_bedrock = provider_choice == "bedrock_claude"

    if not involves_byo and not targets_bedrock:
```

(The `else`/cold-path block below `:698` is unchanged — it dispatches `background_provision`, whose `provision_user_container` runs the Task 9 mint.)

- [ ] **Step 9: Write the regression test for the forced cold path**

Append to `apps/backend/tests/unit/services/test_billing_service.py` (read the file head first to match its existing fixture/mocking style; if the module does not exist, create it with the imports below, mirroring the `change_provider` test conventions in `tests/unit/services/`):

```python
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_change_provider_into_bedrock_takes_cold_path(monkeypatch):
    """A switch INTO bedrock_claude must NOT take the hot path (which would
    call build_provider_block(bedrock, aip_arns=None) and raise), and must
    NOT raise — it dispatches background_provision (cold path)."""
    from core.services import billing_service

    # No prior provider_choice -> none of the OLD-provider side-effect cleanup
    # blocks (:613 oauth, :632 byo) fire, keeping the test free of oauth/key
    # I/O while still exercising the bedrock hot-vs-cold selector.
    monkeypatch.setattr(
        billing_service.billing_repo, "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active",
                                "stripe_subscription_id": "sub_1"}),
    )
    # change_provider imports is_subscription_active LAZILY from
    # core.services.provision_gate, so patch it at the source module.
    monkeypatch.setattr(
        "core.services.provision_gate.is_subscription_active", lambda a: True
    )
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    hot = AsyncMock()
    monkeypatch.setattr(billing_service, "_hot_swap_provider", hot)
    bg = AsyncMock()
    monkeypatch.setattr(
        "core.services.container_provision.background_provision", bg
    )
    created = []
    monkeypatch.setattr(
        billing_service.asyncio, "create_task", lambda coro: created.append(coro) or MagicMock()
    )

    result = await billing_service.change_provider(
        owner_id="user_x", clerk_user_id="user_x", owner_type="personal",
        is_org_context=False, provider_choice="bedrock_claude",
    )

    hot.assert_not_awaited()  # hot path skipped for bedrock target
    assert result["restart"] is True
    # background_provision (cold path) was scheduled.
    assert created, "expected background_provision to be dispatched"
    for coro in created:
        coro.close()
```

Run: `uv run pytest tests/unit/services/test_billing_service.py -k "into_bedrock" -v`
Expected: PASS (the hot path is skipped; cold path dispatched without raising). Adjust the mock surface to the file's actual conventions — the load-bearing assertions are `hot.assert_not_awaited()` and that no `ValueError` escapes.

- [ ] **Step 10: Commit**

```bash
git add apps/backend/core/containers/config.py apps/backend/core/services/billing_service.py apps/backend/tests/unit/containers/test_config_aip.py apps/backend/tests/unit/containers/ apps/backend/tests/unit/services/test_billing_service.py
git commit -m "feat(config): thread per-owner AIP ARNs into openclaw.json builder (fail-closed)

Force the cold path for a hot-swap into bedrock_claude so the per-owner
scoped task role is attached by a task-def re-register, not silently left
on the Bedrock-less shared role.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Thread `task_role_arn` into the ECS register/create/resize/restart paths

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py`
- Test: `apps/backend/tests/unit/containers/test_ecs_manager_aip.py` (Create)

**Design (read before coding):** add a `task_role_arn: str | None = None` param to `_build_register_kwargs_from_base` (`:221`), `_register_task_definition` (`:328`), and `create_user_service` (`:467`). When provided, `_build_register_kwargs_from_base` sets `reg_kwargs["taskRoleArn"] = task_role_arn` instead of inheriting the base's shared role (`:309`). The resize path (`:785-792`) and restart re-register (`:1384-1390`) must read the per-owner role from the containers row and pass it (the same self-heal class as `hook_token`). This task only wires the plumbing; Task 9 mints the role + passes it from the bedrock branch.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/containers/test_ecs_manager_aip.py`:

```python
"""Tests for per-owner task-role threading + bedrock AIP provision wiring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager


@pytest.fixture
def mock_settings():
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:123456789:cluster/test-cluster"
        s.ECS_SUBNETS = "subnet-aaa,subnet-bbb"
        s.ECS_SECURITY_GROUP_ID = "sg-12345"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:123456789:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "isol8-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def mock_ecs_client():
    client = MagicMock()
    client.describe_task_definition.return_value = {
        "taskDefinition": {
            "family": "isol8-dev-openclaw",
            "taskRoleArn": "arn:aws:iam::123456789:role/shared-task-role",
            "executionRoleArn": "arn:aws:iam::123456789:role/exec-role",
            "networkMode": "awsvpc",
            "containerDefinitions": [{"name": "openclaw", "image": "ghcr.io/openclaw:1"}],
            "volumes": [
                {
                    "name": "openclaw-workspace",
                    "efsVolumeConfiguration": {
                        "fileSystemId": "fs-test123",
                        "transitEncryption": "ENABLED",
                        "authorizationConfig": {"accessPointId": "fsap-base", "iam": "ENABLED"},
                    },
                }
            ],
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "512",
            "memory": "2048",
            "runtimePlatform": None,
        }
    }
    client.register_task_definition.return_value = {
        "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw-user:9"}
    }
    return client


@pytest.fixture
def manager(mock_settings, mock_ecs_client):
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:
        mock_boto3.client.side_effect = lambda service, **kw: (
            mock_ecs_client if service == "ecs" else MagicMock()
        )
        mgr = EcsManager()
    return mgr


def test_build_kwargs_overrides_task_role(manager, mock_ecs_client):
    kwargs = manager._build_register_kwargs_from_base(
        "fsap-user", task_role_arn="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
    )
    assert kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"


def test_build_kwargs_defaults_to_base_role_when_none(manager, mock_ecs_client):
    kwargs = manager._build_register_kwargs_from_base("fsap-user")
    assert kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/shared-task-role"


def test_register_task_definition_passes_owner_role(manager, mock_ecs_client):
    manager._register_task_definition(
        "fsap-user", task_role_arn="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
    )
    reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
    assert reg_kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/containers/test_ecs_manager_aip.py -v`
Expected: FAIL — `TypeError: _build_register_kwargs_from_base() got an unexpected keyword argument 'task_role_arn'`

- [ ] **Step 3: Modify `_build_register_kwargs_from_base`**

In `apps/backend/core/containers/ecs_manager.py`, add `task_role_arn: str | None = None,` to the signature (after `hook_token: str | None = None,` at `:229`):

```python
        hook_token: str | None = None,
        task_role_arn: str | None = None,
    ) -> dict:
```

Change the `taskRoleArn=base.get("taskRoleArn", ""),` line (`:309`) to:

```python
            taskRoleArn=task_role_arn or base.get("taskRoleArn", ""),
```

- [ ] **Step 4: Modify `_register_task_definition`**

Add `task_role_arn: str | None = None,` to the `_register_task_definition` signature (after `hook_token: str | None = None,` at `:333`):

```python
        hook_token: str | None = None,
        task_role_arn: str | None = None,
    ) -> str:
```

Pass it through in the `_build_register_kwargs_from_base` call (currently `:357-362`):

```python
            reg_kwargs = self._build_register_kwargs_from_base(
                access_point_id,
                secrets_for_task=secrets_for_task,
                environment_for_task=environment_for_task,
                hook_token=hook_token,
                task_role_arn=task_role_arn,
            )
```

- [ ] **Step 5: Modify `create_user_service`**

Add `task_role_arn: str | None = None,` to the `create_user_service` signature (after `hook_token: str | None = None,` at `:474`):

```python
        hook_token: str | None = None,
        task_role_arn: str | None = None,
    ) -> str:
```

Pass it through to `_register_task_definition` (currently `:552-557`):

```python
            task_def_arn = self._register_task_definition(
                access_point_id,
                secrets_for_task=secrets_for_task,
                environment_for_task=environment_for_task,
                hook_token=hook_token,
                task_role_arn=task_role_arn,
            )
```

- [ ] **Step 6: Thread the per-owner role through resize**

In `resize_user_container` (`:785-792`), read the per-owner role from the row and pass it. After the `hook_token` self-heal block (just before the `reg_kwargs = await asyncio.to_thread(` call at `:785`), insert:

```python
            # Per-owner Bedrock role: a resize that re-registers from the CDK
            # base would otherwise revert the container to the shared
            # all-models task role (same self-heal class as hook_token above).
            owner_task_role_arn = container.get("bedrock_role_arn")
```

Change the `_build_register_kwargs_from_base` call (currently `:785-792`) to add the param:

```python
            reg_kwargs = await asyncio.to_thread(
                self._build_register_kwargs_from_base,
                access_point_id,
                new_image=new_image,
                new_cpu=new_cpu,
                new_memory=new_memory,
                hook_token=hook_token,
                task_role_arn=owner_task_role_arn,
            )
```

- [ ] **Step 7: Thread the per-owner role through the restart re-register**

In `provision_user_container`'s restart branch, the `_register_task_definition` call (currently `:1384-1390`) clones from base. Add the per-owner role from the existing row. Change:

```python
                        new_task_def_arn = await asyncio.to_thread(
                            self._register_task_definition,
                            access_point_id,
                            secrets_for_task=secrets_for_task,
                            environment_for_task=environment_for_task,
                            hook_token=existing_hook_token,
                        )
```

to:

```python
                        new_task_def_arn = await asyncio.to_thread(
                            self._register_task_definition,
                            access_point_id,
                            secrets_for_task=secrets_for_task,
                            environment_for_task=environment_for_task,
                            hook_token=existing_hook_token,
                            task_role_arn=(existing.get("bedrock_role_arn") if existing else None),
                        )
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/unit/containers/test_ecs_manager_aip.py -v`
Expected: PASS (3 passed)

- [ ] **Step 9: Run the existing ECS manager suite for regressions**

Run: `uv run pytest tests/unit/containers/test_ecs_manager.py -v`
Expected: PASS — the new `task_role_arn` param defaults to None, so every existing call (which omits it) inherits the base role exactly as before.

- [ ] **Step 10: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager_aip.py
git commit -m "feat(ecs): thread per-owner task_role_arn through register/create/resize/restart

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Wire the synchronous AIP+role mint into `provision_user_container` (bedrock branch)

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py`
- Test: `apps/backend/tests/unit/containers/test_ecs_manager_aip.py` (add `TestBedrockProvisionMint`)

**Design (read before coding):** the bedrock branch at `:1293-1296` is a no-op `pass`. Replace it with a synchronous mint: `aip_arns = await ensure_owner_aips(user_id)` then `role_arn = await ensure_owner_bedrock_role(user_id, aip_arns, skip_bedrock_grant=<is the owner revoked?>)`. Stash both on local vars and thread them into BOTH `create_user_service` (fresh provision, `:1553`) via `task_role_arn=role_arn` AND `write_user_configs` (`:1564`, `:1360`) via a new `aip_arns=aip_arns` kwarg. This must happen BEFORE `start_user_service` (`:1580`) so the first task boots on the per-owner role with AIP-ARN config. `write_user_configs` (`:1752`) must accept and forward `aip_arns` to `write_openclaw_config`.

**Important ordering facts (verified against `ecs_manager.py`):**
- The provider-choice branch is at `:1280-1305`; the `existing = await container_repo.get_by_owner_id(user_id)` fetch is at `:1306` — i.e. the mint block runs BEFORE `existing` is read. The C6 kill-switch guard therefore does its OWN `container_repo.get_by_owner_id(user_id)` inside the mint block to read `bedrock_revoked` (a second bounded GetItem — acceptable; the row is small and provision is not hot-path).
- The mint runs on EVERY `provision_user_container` call for bedrock owners, INCLUDING the restart path. That is safe because (a) `ensure_owner_aips` is conflict-reconciling (review C2 fix — a restart whose row was cleared adopts the live AIP instead of `ConflictException`-ing), and (b) `ensure_owner_bedrock_role` persists `bedrock_role_arn`, so the restart-branch `_register_task_definition` reads `existing.get("bedrock_role_arn")` (Task 8 Step 7) and the freshly-minted role ARN agree.

**Full mock surface for the fresh-provision test (review T1 — enumerate, don't guess):** the fresh path (`_service_exists` → None → "No service exists" branch at `:1516-1594`) calls, between the mint and return: `secrets.token_urlsafe` (pure, no patch needed), `create_user_service`, `write_user_configs`, `start_user_service` (all three mocked on the instance), and `put_metric("container.provision", ...)`. `container_repo.update_fields` / `_update_container` only fire on the `except` error paths, which do NOT trigger when the mocked calls succeed; the error-state cleanup at `:1520` is gated on `existing.get("status") == "error"` and `existing` is None in the fresh test. `_await_running_transition` is only spawned by `start_user_service`, which is mocked, so no poller fires. Therefore the ONLY extra patch beyond the four instance mocks is `put_metric` (patch `core.containers.ecs_manager.put_metric` with a `MagicMock` so the EMF emit is a no-op in tests). Both `TestBedrockProvisionMint` tests below patch it.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/containers/test_ecs_manager_aip.py`:

```python
class TestBedrockProvisionMint:
    @pytest.mark.asyncio
    async def test_bedrock_branch_mints_aips_role_and_threads_them(self, manager, monkeypatch):
        arns = {
            "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
        }
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"

        ensure_aips = AsyncMock(return_value=arns)
        ensure_role = AsyncMock(return_value=role_arn)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", ensure_aips)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        # EMF metric emit -> no-op in tests (see Task 9 mock-surface note).
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())

        # No existing service -> fresh-provision path.
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None)
        )
        manager._service_exists = MagicMock(return_value=None)
        create_svc = AsyncMock(return_value="openclaw-org_abc-hash")
        write_cfg = AsyncMock()
        start_svc = AsyncMock()
        manager.create_user_service = create_svc
        manager.write_user_configs = write_cfg
        manager.start_user_service = start_svc

        await manager.provision_user_container(
            "org_abc", provider_choice="bedrock_claude", owner_type="org"
        )

        ensure_aips.assert_awaited_once_with("org_abc")
        # Fresh (non-revoked) owner -> role minted WITH the Bedrock grant.
        ensure_role.assert_awaited_once_with("org_abc", arns, skip_bedrock_grant=False)
        # create_user_service got the per-owner role.
        assert create_svc.call_args.kwargs["task_role_arn"] == role_arn
        # write_user_configs got the AIP map.
        assert write_cfg.call_args.kwargs["aip_arns"] == arns
        # mint ran before start.
        assert start_svc.await_count == 1

    @pytest.mark.asyncio
    async def test_revoked_owner_provision_does_not_regrant_bedrock(self, manager, monkeypatch):
        """A re-provision of a kill-switched (bedrock_revoked=True) owner mints
        the AIPs + role SHELL but passes skip_bedrock_grant=True so the scoped
        Bedrock policy is NOT re-put — the kill-switch holds (review C6)."""
        arns = {
            "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
        }
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_rev"
        ensure_aips = AsyncMock(return_value=arns)
        ensure_role = AsyncMock(return_value=role_arn)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", ensure_aips)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())

        # Revoked owner row. The C6 guard reads bedrock_revoked via its own
        # get_by_owner_id inside the mint block (runs before the :1306 fetch);
        # _service_exists -> None routes to the fresh-provision branch, which is
        # fine — the load-bearing assertion is skip_bedrock_grant=True.
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_rev", "bedrock_revoked": True,
                                    "service_name": "openclaw-org_rev-hash",
                                    "hook_token": "ht"}),
        )
        manager._service_exists = MagicMock(return_value=None)
        manager.create_user_service = AsyncMock(return_value="openclaw-org_rev-hash")
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container(
            "org_rev", provider_choice="bedrock_claude", owner_type="org"
        )

        ensure_role.assert_awaited_once_with("org_rev", arns, skip_bedrock_grant=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/containers/test_ecs_manager_aip.py::TestBedrockProvisionMint -v`
Expected: FAIL — `AssertionError` on `create_svc.call_args.kwargs["task_role_arn"]` (KeyError / no such kwarg) because the bedrock branch is still a no-op `pass`.

- [ ] **Step 3: Modify the bedrock branch + thread the mint outputs**

In `apps/backend/core/containers/ecs_manager.py`, replace the bedrock branch (`:1293-1296`):

```python
        elif provider_choice == "bedrock_claude":
            # No extra setup — auth comes from the ECS task IAM role granted
            # bedrock:InvokeModel by the container stack.
            pass
```

with:

```python
        bedrock_aip_arns: dict[str, str] | None = None
        bedrock_role_arn: str | None = None
        if provider_choice == "bedrock_claude":
            # Mint the per-owner AIPs + scoped IAM role SYNCHRONOUSLY here,
            # before the service starts. The container's openclaw.json model
            # ids become the AIP ARNs and the task role is the scoped per-owner
            # role; if either were absent at first boot the container would
            # either run on the shared all-models role or AccessDenied on the
            # AIP-scoped role. Lazy import to avoid pulling bedrock_aip's boto3
            # clients into module load.
            from core.billing.bedrock_aip import (
                ensure_owner_aips,
                ensure_owner_bedrock_role,
            )

            bedrock_aip_arns = await ensure_owner_aips(user_id)

            # KILL-SWITCH GUARD (review C6): ensure_owner_bedrock_role always
            # re-puts the scoped inline policy, so calling it for a revoked
            # owner would silently RESTORE Bedrock access — defeating the
            # kill-switch on any re-provision/restart. Mint the AIPs + role
            # SHELL (so the task role exists + carries EFS/logs/ssm), but do
            # NOT re-grant the Bedrock inline policy if the owner is revoked.
            # Plan 4 owns un-revoking (restore_owner_bedrock), not provision.
            existing_row = await container_repo.get_by_owner_id(user_id)
            is_revoked = bool(existing_row and existing_row.get("bedrock_revoked"))
            bedrock_role_arn = await ensure_owner_bedrock_role(
                user_id, bedrock_aip_arns, skip_bedrock_grant=is_revoked
            )
```

Note: the `skip_bedrock_grant` keyword passed here is ALREADY defined on `ensure_owner_bedrock_role` in Task 5 (its signature is `(owner_id, aip_arns, *, skip_bedrock_grant=False)` and its `put_role_policy` is wrapped in `if not skip_bedrock_grant:`). No further edit to Task 5 is needed from this task — this call site just supplies the value. The `is_revoked` value comes from the `bedrock_revoked` flag set by `revoke_owner_bedrock` (Task 6).

Note: the original `if provider_choice == "chatgpt_oauth":` / `elif provider_choice == "byo_key":` / `elif provider_choice == "bedrock_claude":` / `else:` chain (`:1280-1301`) must be restructured so the bedrock branch is no longer an `elif` of the same chain (it now declares the two local vars). Concretely: keep the `chatgpt_oauth` and `byo_key` branches as-is, change the `elif provider_choice == "bedrock_claude":` to the standalone `if provider_choice == "bedrock_claude":` block above, and KEEP the final `else:` unknown-provider guard by converting it to an explicit validation right after the byo_key branch:

```python
        if provider_choice not in ("chatgpt_oauth", "byo_key", "bedrock_claude"):
            raise EcsManagerError(
                f"Unknown provider_choice: {provider_choice!r}",
                user_id,
            )
```

Place that validation immediately before the `bedrock_aip_arns: dict[str, str] | None = None` line so the chain reads: chatgpt_oauth → byo_key → unknown-guard → bedrock mint.

Now thread the mint outputs. In the fresh-provision `create_user_service` call (`:1553-1560`), add `task_role_arn=bedrock_role_arn,`:

```python
        service_name = await self.create_user_service(
            user_id,
            gateway_token,
            owner_type=owner_type,
            secrets_for_task=secrets_for_task,
            environment_for_task=environment_for_task,
            hook_token=hook_token,
            task_role_arn=bedrock_role_arn,
        )
```

In the fresh-provision `write_user_configs` call (`:1564-1571`), add `aip_arns=bedrock_aip_arns,`:

```python
            await self.write_user_configs(
                user_id,
                gateway_token,
                provider_choice=provider_choice,
                byo_provider=byo_provider,
                hook_token=hook_token,
                owner_type=owner_type,
                aip_arns=bedrock_aip_arns,
            )
```

In the restart-branch `write_user_configs` call (`:1360-1367`), add `aip_arns=bedrock_aip_arns,`:

```python
                    await self.write_user_configs(
                        user_id,
                        gateway_token,
                        provider_choice=provider_choice,
                        byo_provider=byo_provider,
                        hook_token=existing_hook_token,
                        owner_type=owner_type,
                        aip_arns=bedrock_aip_arns,
                    )
```

The restart-branch `_register_task_definition` call (modified in Task 8 to read `existing.get("bedrock_role_arn")`) already picks up the row's role; the freshly-minted `bedrock_role_arn` is also persisted on the row by `ensure_owner_bedrock_role`, so both paths agree.

- [ ] **Step 4: Modify `write_user_configs` to accept + forward `aip_arns`**

In `write_user_configs` (`:1752-1761`), add `aip_arns: dict[str, str] | None = None,` to the signature (after `owner_type: str = "personal",`):

```python
        owner_type: str = "personal",
        aip_arns: dict[str, str] | None = None,
    ) -> None:
```

In the `write_openclaw_config` call inside `write_user_configs` (`:1900-1913`), add `aip_arns=aip_arns,` to the kwargs (after `integration_mcp_servers=integration_mcp_servers,`):

```python
            integration_mcp_servers=integration_mcp_servers,
            aip_arns=aip_arns,
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/containers/test_ecs_manager_aip.py -v`
Expected: PASS (all `test_*` + `TestBedrockProvisionMint`'s two tests, 5 passed)

- [ ] **Step 6: Run the broader container suite**

Run: `uv run pytest tests/unit/containers/ -v`
Expected: PASS — `test_ecs_manager.py`, `test_config_aip.py`, `test_ecs_manager_aip.py`, and any other container tests green. If a pre-existing provision test exercises the bedrock branch end-to-end without mocking `ensure_owner_aips`, mock it the same way `TestBedrockProvisionMint` does.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager_aip.py
git commit -m "feat(ecs): mint per-owner Bedrock AIPs+role synchronously at provision

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: CDK — backend role per-owner IAM permissions (`service-stack.ts`)

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts`
- Test: `apps/infra/test/service-stack.test.ts`

**Design (read before coding):** the backend `taskRole` needs, scoped to the `role/isol8-${env}-owner-bedrock-*` prefix:
1. Widen `iam:PassRole` (`:251-260`) to include the prefix, conditioned on `iam:PassedToService == ecs-tasks.amazonaws.com`.
2. `iam:CreateRole`/`PutRolePolicy`/`DeleteRolePolicy`/`AttachRolePolicy`/`DetachRolePolicy`/`TagRole`/`GetRole`/`GetRolePolicy` on the prefix, with `iam:CreateRole` conditioned on `iam:PermissionsBoundary == <boundary-arn>` (privilege-escalation guard).
3. `bedrock:CreateInferenceProfile`/`DeleteInferenceProfile`/`GetInferenceProfile`/`ListInferenceProfiles`/`TagResource`/`UntagResource`/`ListTagsForResource` on `Resource:"*"` (AIP ARNs are created at runtime).
4. Grant the new reverse table by name (matching the integration-tables by-name pattern at `:384-404`).
5. Set the four backend env vars (`BEDROCK_AIP_OWNERS_TABLE`, `BEDROCK_OWNER_ROLE_PREFIX`, `BEDROCK_OWNER_BOUNDARY_ARN`, `BEDROCK_OWNER_BASE_POLICY_ARN`).

The boundary + base-policy ARNs come from the container stack's new public members (added in Task 11). `service-stack` receives `props.container` as a live construct prop, so it can reference `props.container.ownerBedrockBoundary.managedPolicyArn` and `props.container.ownerBedrockBasePolicy.managedPolicyArn` directly.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/service-stack.test.ts` (inside the existing dev describe block, mirroring its `template.hasResourceProperties` style — read the file head first to match the `buildStack`/`template` setup it already uses):

```typescript
  test("backend role can pass per-owner bedrock roles to ECS", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:PassRole",
            Resource: Match.arrayWith([
              Match.objectLike({
                "Fn::Join": Match.arrayWith([
                  Match.arrayWith([
                    Match.stringLikeRegexp("role/isol8-dev-owner-bedrock-\\*"),
                  ]),
                ]),
              }),
            ]),
          }),
        ]),
      }),
    });
  });

  test("backend role can create boundary-bounded per-owner roles", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:CreateRole",
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                "iam:PermissionsBoundary": Match.anyValue(),
              }),
            }),
          }),
        ]),
      }),
    });
  });

  test("backend role can mint Bedrock application inference profiles", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:CreateInferenceProfile"]),
            Resource: "*",
          }),
        ]),
      }),
    });
  });

  test("backend container has the BEDROCK_OWNER_ROLE_PREFIX env var", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "BEDROCK_OWNER_ROLE_PREFIX",
              Value: "isol8-dev-owner-bedrock",
            }),
          ]),
        }),
      ]),
    });
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `apps/infra/`): `npx jest test/service-stack.test.ts -t "per-owner bedrock"`
Expected: FAIL — the new statements/env vars don't exist yet. (If the repo uses `npm test` / `pnpm test`, use that runner; jest is the underlying CDK test runner.)

- [ ] **Step 3: Widen `iam:PassRole`**

In `apps/infra/lib/stacks/service-stack.ts`, change the `IamPassRole` statement (`:251-260`) to:

```typescript
    // IAM PassRole for ECS task roles (shared execution + task roles AND the
    // per-owner Bedrock roles the backend mints at provision time). Without
    // the owner-bedrock-* prefix, RegisterTaskDefinition/CreateService with a
    // per-owner taskRoleArn fails AccessDenied on PassRole.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IamPassRole",
        actions: ["iam:PassRole"],
        resources: [
          props.container.taskExecutionRole.roleArn,
          props.container.taskRole.roleArn,
          `arn:aws:iam::${this.account}:role/isol8-${env}-owner-bedrock-*`,
        ],
        conditions: {
          StringEquals: {
            "iam:PassedToService": "ecs-tasks.amazonaws.com",
          },
        },
      }),
    );
```

- [ ] **Step 4: Add the per-owner role-management + Bedrock-control-plane statements**

Immediately after the `IamPassRole` statement, add:

```typescript
    // Backend mints + scopes + kills per-owner Bedrock roles. CreateRole is
    // boundary-conditioned so a backend that can CreateRole+PutRolePolicy
    // cannot escalate beyond the OwnerBedrockBoundary (Bedrock+EFS+logs+ssm).
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "OwnerBedrockRoleManage",
        actions: [
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:GetRolePolicy",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy",
          "iam:TagRole",
          "iam:GetRole",
        ],
        resources: [
          `arn:aws:iam::${this.account}:role/isol8-${env}-owner-bedrock-*`,
        ],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "OwnerBedrockRoleCreate",
        actions: ["iam:CreateRole"],
        resources: [
          `arn:aws:iam::${this.account}:role/isol8-${env}-owner-bedrock-*`,
        ],
        conditions: {
          StringEquals: {
            "iam:PermissionsBoundary":
              props.container.ownerBedrockBoundary.managedPolicyArn,
          },
        },
      }),
    );
    // Bedrock control plane: AIP ARNs are created at runtime so cannot be
    // pre-scoped; constrain via the runtime owner_id tag on create.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "OwnerBedrockProfileControlPlane",
        actions: [
          "bedrock:CreateInferenceProfile",
          "bedrock:DeleteInferenceProfile",
          "bedrock:GetInferenceProfile",
          "bedrock:ListInferenceProfiles",
          "bedrock:TagResource",
          "bedrock:UntagResource",
          "bedrock:ListTagsForResource",
        ],
        resources: ["*"],
      }),
    );
```

- [ ] **Step 5: Grant the reverse table by name**

After the integration-tables by-name statement (the `DynamoDbIntegrations` block ending `:404`), add a by-name grant for the reverse table (the table is created in Task 12; granting by name avoids the cross-stack export):

```typescript
    // Per-owner AIP reverse index (Plan 2). Granted BY NAME — same
    // cross-stack-export-avoidance posture as DynamoDbIntegrations above.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DynamoDbBedrockAipOwners",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-bedrock-aip-owners`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-bedrock-aip-owners/index/*`,
        ],
      }),
    );
```

- [ ] **Step 6: Add the backend env vars**

In the backend container `environment:` map (opens at `:769`), after the `OAUTH_TOKENS_TABLE: props.database.oauthTokensTable.tableName,` entry (verified at `:879` — re-grep `grep -n "OAUTH_TOKENS_TABLE" lib/stacks/service-stack.ts` to confirm before editing, as line numbers drift), add:

```typescript
        BEDROCK_AIP_OWNERS_TABLE: `isol8-${env}-bedrock-aip-owners`,
        BEDROCK_OWNER_ROLE_PREFIX: `isol8-${env}-owner-bedrock`,
        BEDROCK_OWNER_BOUNDARY_ARN:
          props.container.ownerBedrockBoundary.managedPolicyArn,
        BEDROCK_OWNER_BASE_POLICY_ARN:
          props.container.ownerBedrockBasePolicy.managedPolicyArn,
```

- [ ] **Step 7: Run test to verify it passes**

Run (from `apps/infra/`): `npx jest test/service-stack.test.ts -t "per-owner bedrock"`
Expected: PASS (4 passed). (This requires Task 11's container-stack members to exist; if running Task 10 before 11, `props.container.ownerBedrockBoundary` is undefined → TypeScript compile error. Build Task 11 first, or in the same sitting.)

- [ ] **Step 8: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts apps/infra/test/service-stack.test.ts
git commit -m "feat(infra): backend role mints+passes boundary-scoped per-owner Bedrock roles

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: CDK — shared-role Bedrock removal + base policy + permissions boundary (`container-stack.ts`)

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts`
- Test: `apps/infra/test/container-stack.test.ts`

**Design (read before coding):**
1. Remove the shared `taskRole`'s broad Bedrock grant (`:191-199`) — the per-owner role supersedes it; the base task-def gets NO Bedrock access, forcing every bedrock container onto a per-owner role. (Prod has one founders' container at desired=0, so no live container loses access mid-flight.)
2. Add `OwnerBedrockBasePolicy` — a `ManagedPolicy` carrying the EFS/logs/ssm grants (copied from `:202-238`) that the backend attaches to every per-owner role.
3. Add `OwnerBedrockBoundary` — a `ManagedPolicy` used as the permissions boundary: the UNION of everything a per-owner role may ever hold (scoped Bedrock invoke on inference profiles + foundation models, EFS, logs, ssmmessages) — capping a backend-minted role so it can never grant itself more.
4. Expose both as `public readonly` members so `service-stack` can reference their ARNs.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/container-stack.test.ts` (inside the dev describe block):

```typescript
  test("shared task role no longer carries the broad bedrock:* on Resource '*'", () => {
    // The per-owner role supersedes it; the base task-def must have NO Bedrock
    // access so every bedrock container is forced onto a per-owner role.
    const policies = template.findResources("AWS::IAM::Policy");
    const offending = Object.values(policies).filter((p: any) =>
      (p.Properties.PolicyDocument.Statement as any[]).some(
        (s) =>
          s.Resource === "*" &&
          JSON.stringify(s.Action).includes("bedrock:InvokeModel"),
      ),
    );
    expect(offending).toHaveLength(0);
  });

  test("creates the OwnerBedrockBasePolicy managed policy (EFS/logs/ssm)", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-owner-bedrock-base",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["elasticfilesystem:ClientMount"]),
          }),
        ]),
      }),
    });
  });

  test("creates the OwnerBedrockBoundary permissions-boundary policy", () => {
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "isol8-dev-owner-bedrock-boundary",
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:InvokeModel"]),
          }),
        ]),
      }),
    });
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `apps/infra/`): `npx jest test/container-stack.test.ts -t "owner-bedrock|broad bedrock"`
Expected: FAIL — the shared-role Bedrock grant still exists and the two managed policies don't.

- [ ] **Step 3: Remove the shared-role Bedrock grant**

In `apps/infra/lib/stacks/container-stack.ts`, DELETE the broad Bedrock grant (`:191-199`):

```typescript
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: ["*"],
      }),
    );
```

- [ ] **Step 4: Declare the new public members**

In the `ContainerStack` class member declarations (near `:62-69`, after `public readonly taskRole: iam.Role;`), add:

```typescript
  public readonly ownerBedrockBasePolicy: iam.ManagedPolicy;
  public readonly ownerBedrockBoundary: iam.ManagedPolicy;
```

- [ ] **Step 5: Create the base policy + boundary**

In the constructor, after the existing `this.taskRole` grants (after the ssmmessages block ending `:238`), add. The inline `foundationModelArns` const builds the 9 FM ARNs (3 models × 3 regions) so the boundary's Bedrock cap matches the backend-minted inline policy:

```typescript
    // The 3 fan-out regions x 3 foundation models = 9 FM ARNs. Mirrors
    // core/billing/bedrock_aip.py FM_IDS/FAN_OUT_REGIONS — keep in lockstep.
    const FAN_OUT_REGIONS = ["us-east-1", "us-east-2", "us-west-2"];
    const FM_IDS = [
      "anthropic.claude-opus-4-6-v1",
      "anthropic.claude-sonnet-4-6",
      "anthropic.claude-haiku-4-5-20251001-v1:0",
    ];
    const foundationModelArns = FAN_OUT_REGIONS.flatMap((region) =>
      FM_IDS.map((model) => `arn:aws:bedrock:${region}::foundation-model/${model}`),
    );

    // EFS/logs/ssm grants the backend attaches to every per-owner role. Keeps
    // the non-Bedrock grants in IaC (auditable); only the per-owner-varying
    // scoped-Bedrock inline policy lives in backend code.
    this.ownerBedrockBasePolicy = new iam.ManagedPolicy(this, "OwnerBedrockBasePolicy", {
      managedPolicyName: `isol8-${env}-owner-bedrock-base`,
      statements: [
        new iam.PolicyStatement({
          sid: "EfsClientAccess",
          actions: [
            "elasticfilesystem:ClientMount",
            "elasticfilesystem:ClientWrite",
            "elasticfilesystem:ClientRootAccess",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "CloudWatchLogs",
          actions: [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "SsmExec",
          actions: [
            "ssmmessages:CreateControlChannel",
            "ssmmessages:CreateDataChannel",
            "ssmmessages:OpenControlChannel",
            "ssmmessages:OpenDataChannel",
          ],
          resources: ["*"],
        }),
      ],
    });

    // Permissions boundary capping every backend-minted per-owner role to the
    // UNION of what it may ever hold: scoped Bedrock invoke (on inference
    // profiles + the 9 FM ARNs) + EFS + logs + ssm. A backend that can
    // CreateRole+PutRolePolicy cannot escalate a per-owner role past this.
    this.ownerBedrockBoundary = new iam.ManagedPolicy(this, "OwnerBedrockBoundary", {
      managedPolicyName: `isol8-${env}-owner-bedrock-boundary`,
      statements: [
        new iam.PolicyStatement({
          sid: "BoundaryBedrockInvoke",
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: [
            `arn:aws:bedrock:*:${this.account}:application-inference-profile/*`,
            ...foundationModelArns,
          ],
        }),
        new iam.PolicyStatement({
          sid: "BoundaryEfs",
          actions: [
            "elasticfilesystem:ClientMount",
            "elasticfilesystem:ClientWrite",
            "elasticfilesystem:ClientRootAccess",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "BoundaryLogs",
          actions: [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "BoundarySsm",
          actions: [
            "ssmmessages:CreateControlChannel",
            "ssmmessages:CreateDataChannel",
            "ssmmessages:OpenControlChannel",
            "ssmmessages:OpenDataChannel",
          ],
          resources: ["*"],
        }),
      ],
    });
```

- [ ] **Step 6: Run test to verify it passes**

Run (from `apps/infra/`): `npx jest test/container-stack.test.ts -t "owner-bedrock|broad bedrock"`
Expected: PASS (3 passed)

- [ ] **Step 7: Run the full container-stack suite for regressions**

Run (from `apps/infra/`): `npx jest test/container-stack.test.ts`
Expected: PASS — the base task-def + EFS + healthcheck tests still pass; only the shared-role Bedrock grant is gone.

- [ ] **Step 8: Commit**

```bash
git add apps/infra/lib/stacks/container-stack.ts apps/infra/test/container-stack.test.ts
git commit -m "feat(infra): drop shared-role Bedrock grant; add owner base policy + boundary

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: CDK — the `bedrock-aip-owners` reverse table (`database-stack.ts`)

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts`
- Test: `apps/infra/test/database-stack.test.ts`

**Design (read before coding):** add a single-key DDB table `isol8-${env}-bedrock-aip-owners` (PK `aip_arn` STRING), matching the `creditsTable` pattern (`:213-221`): PAY_PER_REQUEST, `config.removalPolicy`, `pointInTimeRecovery: true`, customer-managed KMS encryption. Expose as a `public readonly` member. The backend grants it BY NAME in `service-stack.ts` (Task 10 Step 5), so no `grantReadWriteData` here.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/database-stack.test.ts` (read the file head first to match its `buildStack`/`template` setup):

```typescript
  test("creates the bedrock-aip-owners reverse table keyed on aip_arn", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "isol8-dev-bedrock-aip-owners",
      KeySchema: [{ AttributeName: "aip_arn", KeyType: "HASH" }],
      AttributeDefinitions: Match.arrayWith([
        { AttributeName: "aip_arn", AttributeType: "S" },
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `apps/infra/`): `npx jest test/database-stack.test.ts -t "bedrock-aip-owners"`
Expected: FAIL — `Template has 0 resources with type AWS::DynamoDB::Table and properties { TableName: "isol8-dev-bedrock-aip-owners" ... }`

- [ ] **Step 3: Declare the public member**

In `database-stack.ts`, find the member declaration block (near the other `public readonly ...Table: dynamodb.Table;` declarations) and add:

```typescript
  public readonly bedrockAipOwnersTable: dynamodb.Table;
```

- [ ] **Step 4: Create the table**

In the constructor, immediately after the `creditTransactionsTable` block (`:226-235`), add:

```typescript
    // Per-owner Bedrock AIP -> owner reverse index (Plan 2). PK aip_arn.
    // Written by ensure_owner_aips; read by resolve_owner_from_aip_arn on the
    // Plan-3 meter hot path (a single bounded GetItem per invocation record).
    this.bedrockAipOwnersTable = new dynamodb.Table(this, "BedrockAipOwnersTable", {
      tableName: `isol8-${env}-bedrock-aip-owners`,
      partitionKey: { name: "aip_arn", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
    });
```

- [ ] **Step 5: Run test to verify it passes**

Run (from `apps/infra/`): `npx jest test/database-stack.test.ts -t "bedrock-aip-owners"`
Expected: PASS (1 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/database-stack.ts apps/infra/test/database-stack.test.ts
git commit -m "feat(infra): add bedrock-aip-owners reverse-index DDB table

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Full backend billing + container suite (incl. the edited existing config tests + the billing-service regression)**

Run (from `apps/backend/`): `uv run pytest tests/unit/billing/test_bedrock_aip.py tests/unit/containers/ tests/unit/services/test_billing_service.py -v`
Expected: PASS — all new tests, every existing container test edited in Task 7 Step 7b (the 8 bedrock-branch files), and the cold-path regression test (Task 7 Step 9) green. Running the whole `tests/unit/containers/` dir (not just the new `*_aip.py` files) is deliberate: the fail-closed builder change in Task 7 touches the existing config tests, so they must be re-run here.

- [ ] **Step 2: Backend lint**

Run (from `apps/backend/`): `uv run ruff check core/billing/bedrock_aip.py core/containers/config.py core/containers/ecs_manager.py core/services/billing_service.py core/config.py`
Expected: no errors.

- [ ] **Step 3: CDK synth + full infra tests**

Run (from `apps/infra/`): `npx cdk synth > /dev/null && echo SYNTH_OK`
Expected: `SYNTH_OK` (no synth errors — the new constructs + cross-stack member references compile and synthesize).

Run (from `apps/infra/`): `npx jest test/service-stack.test.ts test/container-stack.test.ts test/database-stack.test.ts`
Expected: PASS — all three stack test files green.

- [ ] **Step 4: Confirm the SHARED-CONTRACT names are importable for Plans 3 & 4**

Run (from `apps/backend/`):

```bash
uv run python -c "from core.billing.bedrock_aip import resolve_owner_from_aip_arn, resolve_tier_from_aip_arn, resolve_owner_from_identity_arn, revoke_owner_bedrock, restore_owner_bedrock, ensure_owner_aips, ensure_owner_bedrock_role; print('CONTRACTS_OK')"
```
Expected: `CONTRACTS_OK` — Plan 3 imports `resolve_owner_from_aip_arn` / `resolve_tier_from_aip_arn` / `resolve_owner_from_identity_arn`; Plan 4 imports `revoke_owner_bedrock` / `restore_owner_bedrock`.

- [ ] **Step 5: Confirm no stray system-profile id leaks remain in the bedrock provider path**

Run (from `apps/backend/`): `grep -n "us.anthropic.claude-opus-4-6-v1\|us.anthropic.claude-sonnet-4-6\|us.anthropic.claude-haiku-4-5-20251001" core/containers/config.py`
Expected: matches ONLY in (a) the `SYSTEM_PROFILE_ID`-style comments and (b) the `cost`-block comments / `bedrock_pricing` lockstep comment — NOT in any model `id` field or model ref. The actual `id`/`primary`/`fallbacks`/premium/cheap refs now use `aip_arns[...]`. (The `us.` ids legitimately survive as `SYSTEM_PROFILE_ID` sources in `core/billing/bedrock_aip.py` — those are the `copyFrom` sources, correct.)

- [ ] **Step 6: Commit (verification marker)**

```bash
git commit --allow-empty -m "test(bedrock): full Plan 2 verification — AIP+IAM enforcement green

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec / SHARED-CONTRACT coverage:**
- SHARED CONTRACT — persisted per-owner AIP map keyed to owner_id, on the `containers` row (`bedrock_aip_arns`) + dedicated reverse table → Task 2. ✅
- SHARED CONTRACT — `resolve_owner_from_aip_arn(aip_arn) -> str | None` (Plan 3 meter) → Task 3 (Steps 1-5). ✅
- SHARED CONTRACT — `resolve_tier_from_aip_arn(aip_arn) -> str | None` (Plan 3 pricing — `price_from_aip_arn` needs the model tier; reads the `tier` attribute Task 2 already persists on each reverse row) → Task 3 (Steps 6-10). ✅
- SHARED CONTRACT — `resolve_owner_from_identity_arn(identity_arn) -> str | None` (Plan 3 fallback attribution when the AIP-ARN lookup misses; parses the deterministic `isol8-{env}-owner-bedrock-{owner_id}` role-name suffix out of the log's `identity.arn`, accepting both the STS assumed-role and bare IAM-role forms — approach (a), no extra storage) → Task 3b. ✅
- SHARED CONTRACT — `revoke_owner_bedrock(owner_id)` / `restore_owner_bedrock(owner_id)` (Plan 4) → Task 6. ✅
- SHARED CONTRACT — per-owner IAM role scoped via `bedrock:InferenceProfileArn` to the 3 owner AIPs + the 9 FM ARNs (3 models × us-east-1/us-east-2/us-west-2) with explicit `Deny bedrock:CreateInferenceProfile` → Task 1 (pure policy doc) + Task 5 (mint). The Task-1 test asserts exactly this 8/8-sim shape. ✅
- SHARED CONTRACT — container `openclaw.json` model ids become the per-owner AIP ARNs, fail-closed otherwise → Task 7 (builder) + Task 9 (mint-before-start wiring). ✅
- Backend can mint/pass per-owner roles (the day-one `iam:PassRole` blocker at `service-stack.ts:251-260`) → Task 10 (widen PassRole + CreateRole/PutRolePolicy + Bedrock control plane). ✅
- Resize/restart never revert to the shared role (landmine #3) → Task 8 (resize `:785` reads `bedrock_role_arn`; restart `:1384` reads `existing.get("bedrock_role_arn")`). ✅
- Privilege-escalation guard (landmine #7) → Task 11 boundary `ManagedPolicy` + Task 10 `iam:CreateRole` `Condition: iam:PermissionsBoundary`. ✅
- Synchronous mint before service start (landmine #8) → Task 9 mints inside `provision_user_container` before `start_user_service`. ✅
- W53 env-scoped global names (landmine #6) → AIP name `isol8-{env}-{owner_id}-{tier}` (Task 4), role name `isol8-{env}-owner-bedrock-{owner_id}` (Task 5), reverse table `isol8-{env}-bedrock-aip-owners` (Task 12). ✅
- `opus`/`haiku` exact FM suffixes (landmine #10) → `FM_IDS` in Task 1 + Task 1 test asserts the literal strings; CDK `FM_IDS` in Task 11 mirrors them. ✅
- Markup unchanged (no invented `BILLING_MARKUP`) → this plan does not price; pricing is Plan 3 (`raw * 14 // 10`, `markup_multiplier=1.4`). Confirmed not introduced anywhere here. ✅
- **Not in this plan (correct — other plans own them):** the AWS invocation-log Lambda + the `/internal/usage/from-bedrock-log` endpoint + pricing call (Plan 3); the pre-chat gate + kill-switch *invocation* + `desired=0` + legacy-deduct retirement + drift monitor (Plan 4); the `UsageEvent`/`LedgerBackend`/`DdbLedgerBackend` (Plan 1). This plan only DEFINES `resolve_owner_from_aip_arn` / `resolve_tier_from_aip_arn` / `resolve_owner_from_identity_arn` / `revoke_owner_bedrock` / `restore_owner_bedrock` for them to call. ✅
- **§3.6 build-gate #2 (attribution correctness — DEFERRED to Plan 3, but flagged here):** the spec's pre-code build gate asks "does `modelId` log as the AIP id and `identity.arn` as the per-owner role ARN?" This plan has NO live-AWS verification task — it is all unit tests (moto + MagicMock) + `cdk synth`. That is correct for the slice (the AWS log-shape check is Plan 3's job), BUT this plan *produces* the exact AIP ARN + per-owner role ARN whose shape Plan 3 must attribute. **A live mint+invoke smoke test (mint an owner's AIPs+role in dev, run one chat, confirm the invocation log records `modelId` = the AIP ARN and `identity.arn` = `isol8-dev-owner-bedrock-<owner>`) MUST run before prod** — it is the first thing Plan 3's early task does, and it validates Plan 2's output. Do not promote Plan 2 to prod on green unit tests alone. ⚠️ (deferred, but owned-output flagged)

**2. Deprecation-inventory rows folded in:** the dossier deprecation inventory rows that touch THIS plan are infra-only and forward-looking — the `creditsTable`/`creditTransactionsTable` are DROPPED at Phase 3 (not here), and the shared-role broad Bedrock grant (`container-stack.ts:191-199`) is the one item this plan actively REMOVES (Task 11). No `credit_ledger`/`connection_pool`/`usage_service` deletions belong to Plan 2 — those are Plan 4 / Phase 3. Folded as: Task 11 removes the now-superseded shared Bedrock grant; the reverse table + per-owner roles/AIPs are new runtime-created account-global resources, env-named so they sidestep the W53 per-env CFN collision class entirely.

**3. Placeholder scan:** no unfinished-work markers, deferral notes, or "similar to Task N" hand-waves remain. Every code block is complete and directly usable; every command is real with an Expected output. The single-`except ClientError` idempotency note in Tasks 5 & 6 documents a real boto3-vs-MagicMock exception-class subtlety (a `getattr(client.exceptions, ...)` / `except client.exceptions.*` form would raise `TypeError` under a MagicMock client), and is a deliberate correctness note, not unfinished work.

**4. Type / name consistency:** `aip_arns: dict[str, str]` keyed by `("opus","sonnet","haiku")` is identical across `bedrock_aip.py` (`TIERS`, `ensure_owner_aips` return), `config.py` (`build_provider_block`/`premium_model_ref`/`cheap_model_ref`/`build_openclaw_config_dict`/`write_openclaw_config`/`write_user_configs` params), and every test fixture (`_AIPS`/`_aip_arns`/`arns`). The persisted attribute names `bedrock_aip_arns` / `bedrock_role_arn` / `bedrock_revoked` are written in Tasks 2/5/6 and read in Tasks 4/6/8/9 consistently — `bedrock_revoked` is now both written (Task 6 kill-switch) AND read (Task 9 provision-time C6 guard), closing the un-revoke loop. `ensure_owner_bedrock_role`'s `skip_bedrock_grant: bool = False` keyword is defined in Task 5 and supplied by exactly one caller (Task 9's mint). The role-name prefix `isol8-{env}-owner-bedrock-` is identical in `bedrock_aip._owner_role_name` (Task 5), `service-stack` IAM resources + `BEDROCK_OWNER_ROLE_PREFIX` env (Task 10), and the role/PassRole tests. `FM_IDS` / `FAN_OUT_REGIONS` are mirrored between `bedrock_aip.py` (Task 1) and `container-stack.ts` (Task 11) with a lockstep comment in both. The model ref form is `amazon-bedrock/<AIP-ARN>` everywhere it appears. `_hot_swap_provider` (`billing_service.py`) is now scoped OUT of the bedrock target (Task 7 Step 8) so the fail-closed `build_provider_block` raise never reaches it.

**5. Cross-stack safety:** `service-stack` references `props.container.ownerBedrockBoundary` / `props.container.ownerBedrockBasePolicy` as live construct props (same CDK stage), NOT via `CfnOutput`/`Fn::ImportValue` — matching how it already imports `props.container.taskRole` (confirmed: container-stack does not CfnOutput the taskRole). The new reverse table is granted BY NAME (Task 10 Step 5), not via `grantReadWriteData`, so it creates no cross-stack export (the `service-stack.ts:376-383` warning is honored).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-07-bedrock-aip-iam-enforcement.md`. Recommended order is Task 1 → 13 in sequence, committing each. Backend Tasks 1–9 are independent of the CDK Tasks 10–12 except that the runtime env vars/role-prefix/boundary the backend reads are SET by the CDK tasks — so the backend code is correct in isolation (defaults keep unit tests green) and the CDK tasks make it live. Tasks 10 and 11 are coupled (Task 10's `service-stack` references Task 11's `container-stack` members), so build Task 11 before/with Task 10. After Task 13 the SHARED-CONTRACT names (`resolve_owner_from_aip_arn`, `resolve_tier_from_aip_arn`, `resolve_owner_from_identity_arn`, `revoke_owner_bedrock`, `restore_owner_bedrock`, the persisted AIP map) are importable for Plan 3 (meter/pricing/attribution) and Plan 4 (kill-switch).
