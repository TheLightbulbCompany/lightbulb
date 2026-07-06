# AWS Integration Scope Tiers + Self-Account Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `aws_sts` integration offer a three-tier scope choice (ViewOnly/ReadOnly/PowerUser), fix the broken default-path ViewOnly ARN, and let the founder org connect Isol8's own AWS account — gated by a **trust-pin check** (the connected role must trust the connecting owner + their ExternalId), not an owner allowlist — proven live end-to-end.

**Architecture:** The integration delivers scoped AWS credentials into the agent's exec env via a cross-account `AssumeRole` (CloudFormation-created connector role, capped by an AWS managed policy the owner picks). This plan expands the pickable presets, fixes a malformed ARN, and replaces the flat self-account *block* with a *verification*: a role in our account may be connected only if its trust policy pins the connecting owner (principal + their ExternalId) — something only we can create, and which our infra roles never satisfy. The `aws-exec-profile` plugin and credential rail are unchanged.

**Tech Stack:** FastAPI + Pydantic (backend), AWS CDK / CloudFormation (infra), Next.js/React + TypeScript (frontend), pytest + vitest.

## Global Constraints

- **Nothing hardcoded.** No owner ids, account ids, or role ARNs as source literals. The self-account gate needs **no owner-id config** — it verifies the connected role's trust policy. The one place a founder-identity value is unavoidable is the static CDK permission boundary (it can't verify a trust policy at runtime); there we **reuse the existing `PLATFORM_OPS_OWNER_ID`** (same founder org, already wired via GitHub secret → `app.ts` synth env → `service-stack.ts` → `config.py`; also readable from `process.env.PLATFORM_OPS_OWNER_ID_{DEV,PROD}` at synth). Empty ⇒ boundary Deny stays fully broad (fail-closed). Account id uses existing `this.account` (CDK) / `bedrock_aip.OUR_ACCOUNT_ID` (backend).
- **Correct managed-policy ARNs:** `arn:aws:iam::aws:policy/job-function/ViewOnlyAccess`, `arn:aws:iam::aws:policy/ReadOnlyAccess`, `arn:aws:iam::aws:policy/PowerUserAccess`. (`ViewOnlyAccess` is a job-function policy — the `/job-function/` segment is mandatory or IAM returns `NoSuchEntity`.)
- **The connector role name is the fixed literal `isol8-readonly-connector`** (CFN `RoleName`) — do not rename (existing connections reference it).
- **`AdministratorAccess` is never offered.**
- **The `aws-exec-profile` plugin, the `aws_cli` materializer, and the credential chain are NOT modified.**
- **Out of scope:** how the agent is steered/prompted; any per-agent IAM overlay; agent secret-restraint.
- Commit style: end messages with the two trailer lines used across this repo (`Co-Authored-By: Claude Fable 5 …` / `Claude-Session: …`). Never push without approval; work stays on branch `feat/aws-integration-scope-tiers`.
- Verify commands: backend `cd apps/backend && uv run pytest <files> -q` + `uv run ruff format <files>` + `uv run ruff check <files>`; frontend `cd apps/frontend && pnpm exec tsc --noEmit` + targeted `pnpm exec vitest run <file>` (never the full suite); infra `cd apps/infra && npx cdk synth` (regenerates `deploy.yml`).

## File structure (what each touched file owns)

- `apps/backend/core/services/credentials/registry.py` — `AwsStsSpec` (the policy-ARN defaults). **Modify.**
- `apps/backend/core/services/credentials/providers.yaml` — the `aws` provider's `aws_sts` block. **Modify.**
- `apps/infra/aws/aws-readonly-connector.yaml` — the connector CFN template (`AllowedValues`). **Modify.**
- `apps/backend/core/services/credentials/aws_cfn.py` — connect-flow helpers; add `CONNECTOR_ROLE_NAME`. **Modify.**
- `apps/backend/routers/connections.py` — `aws-setup` (tier response) + `aws-complete` (self-account trust-pin gate). **Modify.**
- `apps/backend/core/billing/bedrock_aip.py` — `_build_owner_aws_sts_doc` / `put_owner_aws_sts_policy` (inline grant + self-account deny). **Modify.**
- `apps/infra/lib/stacks/service-stack.ts` — grant the backend `iam:GetRole` on the connector role. **Modify.**
- `apps/infra/lib/stacks/container-stack.ts` — `BoundaryDenyAssumeIntoOurAccount` carve (reuse `PLATFORM_OPS_OWNER_ID`). **Modify.**
- `apps/frontend/src/components/settings/AwsConnectCard.tsx` — scope picker (add PowerUser). **Modify.**
- Tests: `apps/backend/tests/unit/routers/test_connections.py`, `apps/backend/tests/credentials/test_aws_sts_grant.py`. **Modify/add.**

---

### Task 1: Fix the broken ViewOnly ARN (ship-blocker, lands first)

`arn:aws:iam::aws:policy/ViewOnlyAccess` returns `NoSuchEntity`; the correct ARN is `arn:aws:iam::aws:policy/job-function/ViewOnlyAccess`. It is wrong in three sites, and ViewOnly is the default scope, so the default connect path fails role creation today. The test only asserts a suffix, so it passes on the broken value.

**Files:**
- Modify: `apps/backend/core/services/credentials/registry.py:174`
- Modify: `apps/backend/core/services/credentials/providers.yaml:453`
- Modify: `apps/infra/aws/aws-readonly-connector.yaml:20,26`
- Test: `apps/backend/tests/unit/routers/test_connections.py:1279-1280`

**Interfaces:**
- Produces: `AwsStsSpec.view_only_policy_arn == "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"` (consumed by Task 2's `aws-setup` response and the frontend).

- [ ] **Step 1: Harden the failing test to an exact-ARN assert**

In `apps/backend/tests/unit/routers/test_connections.py`, replace the two suffix assertions (currently lines 1279-1280):

```python
        assert body1["view_only"]["policy_arn"] == "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
        assert body1["read_only"]["policy_arn"] == "arn:aws:iam::aws:policy/ReadOnlyAccess"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py::AwsConnectFlowTests::test_aws_setup_mints_then_reuses_external_id -q`
Expected: FAIL — `view_only.policy_arn` is currently `arn:aws:iam::aws:policy/ViewOnlyAccess` (missing `/job-function/`).

- [ ] **Step 3: Fix the ARN in `registry.py:174`**

```python
    view_only_policy_arn: str = "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
```

- [ ] **Step 4: Fix the ARN in `providers.yaml:453`**

```yaml
    view_only_policy_arn: "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
```

- [ ] **Step 5: Fix the ARN in the CFN template `aws-readonly-connector.yaml`**

Line 26 (inside `AllowedValues`):

```yaml
      - "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
```

And the human example on line 20 (Description), change `arn:aws:iam::aws:policy/ViewOnlyAccess` to `arn:aws:iam::aws:policy/job-function/ViewOnlyAccess`.

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py::AwsConnectFlowTests::test_aws_setup_mints_then_reuses_external_id -q`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
cd apps/backend && uv run ruff format core/services/credentials/registry.py tests/unit/routers/test_connections.py && uv run ruff check core/services/credentials/registry.py
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/aws-scope-tiers
git add apps/backend/core/services/credentials/registry.py apps/backend/core/services/credentials/providers.yaml apps/infra/aws/aws-readonly-connector.yaml apps/backend/tests/unit/routers/test_connections.py
git commit -m "fix(aws): correct ViewOnly managed-policy ARN (job-function path)"
```

---

### Task 2: Add the PowerUser tier — backend

Add `power_user` (`arn:aws:iam::aws:policy/PowerUserAccess`) to the spec, provider config, `aws-setup` response, and CFN `AllowedValues`.

**Files:**
- Modify: `apps/backend/core/services/credentials/registry.py:175-176`
- Modify: `apps/backend/core/services/credentials/providers.yaml:454-455`
- Modify: `apps/backend/routers/connections.py:61-68`
- Modify: `apps/infra/aws/aws-readonly-connector.yaml:25-27`
- Test: `apps/backend/tests/unit/routers/test_connections.py`

**Interfaces:**
- Consumes: `AwsStsSpec` (Task 1).
- Produces: `AwsStsSpec.power_user_policy_arn: str`; `aws-setup` response gains a `power_user: {policy_arn, quick_create_url}` block (consumed by Task 3 frontend).

- [ ] **Step 1: Write the failing test**

Add to `test_aws_setup_mints_then_reuses_external_id` (after the read_only assertions, ~line 1280):

```python
        assert body1["power_user"]["policy_arn"] == "arn:aws:iam::aws:policy/PowerUserAccess"
        assert body1["power_user"]["quick_create_url"] is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py::AwsConnectFlowTests::test_aws_setup_mints_then_reuses_external_id -q`
Expected: FAIL with `KeyError: 'power_user'`.

- [ ] **Step 3: Add the field to `AwsStsSpec` (`registry.py`, after `read_only_policy_arn` at line 175)**

```python
    read_only_policy_arn: str = "arn:aws:iam::aws:policy/ReadOnlyAccess"
    power_user_policy_arn: str = "arn:aws:iam::aws:policy/PowerUserAccess"
```

- [ ] **Step 4: Add it to `providers.yaml` (after `read_only_policy_arn` at line 454)**

```yaml
    read_only_policy_arn: "arn:aws:iam::aws:policy/ReadOnlyAccess"
    power_user_policy_arn: "arn:aws:iam::aws:policy/PowerUserAccess"
```

- [ ] **Step 5: Add the tier to the `aws-setup` response (`connections.py`, in the returned dict after the `read_only` block ~line 68)**

```python
        "read_only": {
            "policy_arn": p.aws_sts.read_only_policy_arn,
            "quick_create_url": _quick_create(p.aws_sts.read_only_policy_arn),
        },
        "power_user": {
            "policy_arn": p.aws_sts.power_user_policy_arn,
            "quick_create_url": _quick_create(p.aws_sts.power_user_policy_arn),
        },
```

- [ ] **Step 6: Add the ARN to CFN `AllowedValues` (`aws-readonly-connector.yaml`, lines 25-27)**

```yaml
    AllowedValues:
      - "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
      - "arn:aws:iam::aws:policy/ReadOnlyAccess"
      - "arn:aws:iam::aws:policy/PowerUserAccess"
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -q`
Expected: PASS.

- [ ] **Step 8: Lint + commit**

```bash
cd apps/backend && uv run ruff format core/services/credentials/registry.py routers/connections.py && uv run ruff check core/services/credentials/registry.py routers/connections.py
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/aws-scope-tiers
git add apps/backend/core/services/credentials/registry.py apps/backend/core/services/credentials/providers.yaml apps/backend/routers/connections.py apps/infra/aws/aws-readonly-connector.yaml apps/backend/tests/unit/routers/test_connections.py
git commit -m "feat(aws): add PowerUser scope tier to the connector integration"
```

---

### Task 3: Add the PowerUser tier — frontend picker

**Files:**
- Modify: `apps/frontend/src/components/settings/AwsConnectCard.tsx:36-40, 208-234`

**Interfaces:**
- Consumes: `aws-setup` `power_user` block (Task 2).

- [ ] **Step 1: Extend the response interface + scope type (lines 36-40)**

```tsx
  view_only: AwsScopeOption;
  read_only: AwsScopeOption;
  power_user: AwsScopeOption;
}

type AwsScope = "view_only" | "read_only" | "power_user";
```

- [ ] **Step 2: Add the PowerUser button + hint to the picker (in the scope `<div className="flex gap-2">`, after the ReadOnly button ~line 220)**

```tsx
          <Button
            size="sm"
            variant={scope === "power_user" ? "default" : "outline"}
            disabled={!canManage}
            onClick={() => setScope("power_user")}
            aria-pressed={scope === "power_user"}
          >
            PowerUser
          </Button>
        </div>
        {scope === "read_only" && (
          <span className="text-[11px] text-[#8a8578]">broader — includes data-plane reads</span>
        )}
        {scope === "power_user" && (
          <span className="text-[11px] text-[#8a8578]">read + write; excludes IAM / Organizations</span>
        )}
```

(Ensure the selected scope's `quick_create_url` / `policy_arn` is read from `setup.power_user` wherever `setup.view_only` / `setup.read_only` are consumed for the chosen `scope` — mirror the existing `scope === "read_only" ? setup.read_only : setup.view_only` selection so the third branch resolves `setup.power_user`. `tsc` will flag any exhaustive branch that misses `power_user`.)

- [ ] **Step 3: Typecheck**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: clean (no output).

- [ ] **Step 4: Lint the file**

Run: `cd apps/frontend && pnpm exec eslint src/components/settings/AwsConnectCard.tsx`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/aws-scope-tiers
git add apps/frontend/src/components/settings/AwsConnectCard.tsx
git commit -m "feat(aws): PowerUser tier in the AWS connect picker"
```

---

### Task 4: Grant the backend `iam:GetRole` on the connector role

The self-account trust-pin gate (Task 5) inspects a pasted our-account role's trust policy via `iam:GetRole`. Grant the backend that read, scoped to the fixed connector role.

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts` (the backend task role's IAM policy — grep for the statement granting the backend `iam:PutRolePolicy` on per-owner roles, used by `bedrock_aip`; add the read alongside it)

**Interfaces:**
- Produces: the backend task role may call `iam:GetRole` on `arn:aws:iam::{account}:role/isol8-readonly-connector`.

- [ ] **Step 1: Locate the backend's IAM grant**

Run: `cd apps/infra && grep -n "PutRolePolicy\|iam:\|owner-bedrock" lib/stacks/service-stack.ts | head`
Expected: find the `iam.PolicyStatement` where the backend is granted IAM permissions on per-owner roles.

- [ ] **Step 2: Add a scoped `iam:GetRole` statement to the backend task role**

Add (adjacent to the existing backend IAM statement):

```typescript
new iam.PolicyStatement({
  sid: "ReadConnectorRoleForTrustCheck",
  actions: ["iam:GetRole"],
  resources: [`arn:aws:iam::${this.account}:role/isol8-readonly-connector`],
}),
```

- [ ] **Step 3: Validate synth**

Run: `cd apps/infra && npx cdk synth >/dev/null 2>synth.err; echo "exit=$?"; grep -i error synth.err | head; rm -f synth.err`
Expected: `exit=0`, no errors. `.github/workflows/deploy.yml` unchanged (no pipeline-structure change).

- [ ] **Step 4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/aws-scope-tiers
git status --short   # no cdk.out / node_modules
git add apps/infra/lib/stacks/service-stack.ts
git commit -m "feat(aws): grant backend iam:GetRole on the connector role for trust checks"
```

---

### Task 5: Self-account gate — trust-pin verification (replaces the flat block)

A role in our account may be connected **only if its trust policy pins the connecting owner** — i.e. it trusts that owner's per-owner principal (`aws_cfn.owner_task_role_arn(owner_id)`) AND requires that owner's ExternalId. Verified via `iam:GetRole`. This blocks every tenant (our infra roles aren't pinned to them; they can't create roles in our account) and even stops us from connecting a non-connector role. No owner-id allowlist.

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py` (add `CONNECTOR_ROLE_NAME` next to `OUR_ACCOUNT_ID` — single source, base module)
- Modify: `apps/backend/core/services/credentials/aws_cfn.py:13` (point `_STACK_NAME` at it)
- Modify: `apps/backend/routers/connections.py:87-99` (the helper + the account gate)
- Test: `apps/backend/tests/unit/routers/test_connections.py`

**Interfaces:**
- Consumes: `bedrock_aip.OUR_ACCOUNT_ID`, `bedrock_aip._iam_client()`, `aws_cfn.owner_task_role_arn`, the pending record's `external_id`.
- Produces: `_self_account_role_is_pinned(role_arn, owner_id, external_id) -> bool`; `bedrock_aip.CONNECTOR_ROLE_NAME` (used by Task 6's exact-ARN check).

- [ ] **Step 1: Add the connector-role constant to `bedrock_aip.py`** (next to `OUR_ACCOUNT_ID`)

`bedrock_aip` is the base module both `connections.py` and `bedrock_aip` itself can import; `aws_cfn` already imports `bedrock_aip`, so the constant can't live in `aws_cfn` without a cycle. The gate itself doesn't name-match (the scoped `iam:GetRole` from Task 4 already restricts verification to this role) — the constant exists for Task 6's exact-ARN deny-drop.

```python
OUR_ACCOUNT_ID: str = "877352799272"
CONNECTOR_ROLE_NAME = "isol8-readonly-connector"
```

Then point `aws_cfn._STACK_NAME` at it to avoid a duplicate literal (`aws_cfn` already imports `bedrock_aip`):

```python
_STACK_NAME = bedrock_aip.CONNECTOR_ROLE_NAME
```

- [ ] **Step 2: Write the failing tests (`test_connections.py`)**

Mirror the existing async_client + mocked-store pattern. Patch `routers.connections.bedrock_aip._iam_client` to a `MagicMock`, and `routers.connections.aws_cfn.owner_task_role_arn` to return the owner's principal. Cover, as four `@pytest.mark.asyncio` tests on `POST /api/v1/connections/aws/aws-complete`:

```python
# (a) our-account CONNECTOR arn whose GetRole trust pins THIS owner + their
#     ExternalId  -> passes the gate (status != 400; reaches grant).
# (b) TENANT-CONTAINMENT: our-account arn whose trust does NOT pin the owner
#     (e.g. trusts account-root, like a CDK role) -> 400. This is the guarantee
#     that no tenant can ever connect one of our infra roles — their trust would
#     never name them, and they can't create a role in our account.
# (c) our-account CONNECTOR arn but GetRole ExternalId != the pending ExternalId
#     -> 400.
# (d) external-account arn -> unchanged (no GetRole; proceeds).
```

For the mocked `get_role`, return `{"Role": {"AssumeRolePolicyDocument": {...trust...}}}` where the Allow statement's `Principal.AWS` is the owner principal and `Condition.StringEquals["sts:ExternalId"]` is the pending ExternalId (for the pass case), and a mismatching principal/ExternalId for the fail cases.

- [ ] **Step 3: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -k pinned -q`
Expected: FAIL — `_self_account_role_is_pinned` does not exist and our-account ARNs currently hard-400.

- [ ] **Step 4: Add the trust-pin helper (`connections.py`, near the other module helpers)**

```python
async def _self_account_role_is_pinned(role_arn: str, owner_id: str, external_id: str) -> bool:
    """True iff a role IN OUR account is a legitimate connector for THIS owner:
    its trust policy allows the owner's per-owner principal AND requires the
    owner's ExternalId. Only we can create such a role, and our infra roles
    (which trust account-root, not a specific owner) never satisfy it — so this
    blocks connecting an arbitrary our-account role (account-takeover) without an
    owner-id allowlist. Any failure (GetRole error, shape mismatch) -> False."""
    from core.dynamodb import run_in_thread

    role_name = role_arn.split(":role/", 1)[1]
    try:
        resp = await run_in_thread(bedrock_aip._iam_client().get_role, RoleName=role_name)
        trust = resp["Role"]["AssumeRolePolicyDocument"]  # boto3 returns a decoded dict
    except Exception:
        return False
    expected_principal = aws_cfn.owner_task_role_arn(owner_id)
    for stmt in trust.get("Statement", []):
        if stmt.get("Effect") != "Allow":
            continue
        principal = stmt.get("Principal", {}).get("AWS")
        principals = principal if isinstance(principal, list) else [principal]
        if expected_principal not in principals:
            continue
        cond = stmt.get("Condition", {}).get("StringEquals", {})
        if cond.get("sts:ExternalId") == external_id:
            return True
    return False
```

- [ ] **Step 5: Replace the flat account gate (`connections.py`, lines 95-99)**

```python
    role_arn = body.role_arn.strip()
    match = _ROLE_ARN_RE.fullmatch(role_arn)
    if not match:
        raise HTTPException(status_code=400, detail="invalid role ARN")
    if match.group(1) == bedrock_aip.OUR_ACCOUNT_ID:
        # Our own account: allowed ONLY if the pasted role is trust-pinned to this
        # owner (trusts their principal + requires their ExternalId) — verified
        # against the live role. Blocks connecting an arbitrary our-account role
        # (e.g. account-root-trusting CDK roles = account takeover). No allowlist.
        rec_ext = ((await integration_secret_store.get(owner_id, provider)) or {}).get("external_id")
        if not rec_ext or not await _self_account_role_is_pinned(role_arn, owner_id, rec_ext):
            raise HTTPException(status_code=400, detail="role must be in your own AWS account, not Isol8's")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -q`
Expected: PASS (all, including the pre-existing cases).

- [ ] **Step 7: Lint + commit**

```bash
cd apps/backend && uv run ruff format routers/connections.py core/services/credentials/aws_cfn.py tests/unit/routers/test_connections.py && uv run ruff check routers/connections.py core/services/credentials/aws_cfn.py
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/aws-scope-tiers
git add apps/backend/routers/connections.py apps/backend/core/services/credentials/aws_cfn.py apps/backend/tests/unit/routers/test_connections.py
git commit -m "feat(aws): trust-pin gate for connecting a role in our own account"
```

---

### Task 6: Inline policy — omit the self-account Deny when the connected role is in our account

The per-owner inline `owner-aws-sts` policy carries a `Deny sts:AssumeRole` into our account; an explicit Deny beats the exact-ARN Allow, so when the connected role is in our account that Deny must be omitted (the exact-ARN Allow becomes the sole grant; deny-by-default blocks all else). Data-driven from the connected ARN — no owner-id config.

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py:576-644`
- Test: `apps/backend/tests/credentials/test_aws_sts_grant.py`

**Interfaces:**
- Produces: `_build_owner_aws_sts_doc(role_arns, *, include_self_account_deny: bool = True)`; `put_owner_aws_sts_policy` omits the Deny when any connected ARN is in our account.

- [ ] **Step 1: Write the failing tests (`test_aws_sts_grant.py`)**

```python
def test_self_account_deny_omitted_when_flag_false():
    doc = bedrock_aip._build_owner_aws_sts_doc(
        ["arn:aws:iam::877352799272:role/isol8-readonly-connector"],
        include_self_account_deny=False,
    )
    assert all(s["Effect"] != "Deny" for s in doc["Statement"])
    allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow")
    assert allow["Resource"] == ["arn:aws:iam::877352799272:role/isol8-readonly-connector"]


def test_self_account_deny_present_by_default():
    doc = bedrock_aip._build_owner_aws_sts_doc(["arn:aws:iam::111122223333:role/ro"])
    assert any(s["Effect"] == "Deny" for s in doc["Statement"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/credentials/test_aws_sts_grant.py -k self_account -q`
Expected: FAIL — `_build_owner_aws_sts_doc` has no `include_self_account_deny` kwarg.

- [ ] **Step 3: Add the flag to `_build_owner_aws_sts_doc` (`bedrock_aip.py:576`)**

```python
def _build_owner_aws_sts_doc(role_arns: list[str], *, include_self_account_deny: bool = True) -> dict:
```

And build the statement list conditionally (replace the `return {...}` body):

```python
    statements: list[dict] = [
        {
            "Sid": "OwnerScopedAssumeReadonly",
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": list(role_arns),
        },
    ]
    if include_self_account_deny:
        statements.append(
            {
                "Sid": "DenyAssumeIntoOurAccount",
                "Effect": "Deny",
                "Action": "sts:AssumeRole",
                "Resource": f"arn:aws:iam::{OUR_ACCOUNT_ID}:role/*",
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}
```

- [ ] **Step 4: Drive the flag from the connected ARNs in `put_owner_aws_sts_policy` (`bedrock_aip.py:625`)**

After the empty-guard, drop the Deny **only** for the EXACT connector ARN — not "any our-account role". This is the security hardening: the deny-drop is load-bearing on the Task 5 gate being the only path here, so if a bug or ungated caller ever passes a *different* our-account ARN, the Deny stays and the assume is still blocked. `OUR_ACCOUNT_ID` and `CONNECTOR_ROLE_NAME` are module constants already in scope — no new import:

```python
    connector_arn = f"arn:aws:iam::{OUR_ACCOUNT_ID}:role/{CONNECTOR_ROLE_NAME}"
    # Drop the self-account Deny ONLY for the exact connector role (never for any
    # other our-account ARN) — keeps the tenant-protection Deny intact even if a
    # bad ARN somehow reaches here.
    has_connector = any(a == connector_arn for a in role_arns)
    ...
        PolicyDocument=json.dumps(
            _build_owner_aws_sts_doc(role_arns, include_self_account_deny=not has_connector)
        ),
```

- [ ] **Step 5: Add `put`-level tests (mirror `test_put_uses_owner_aws_sts_policy_name_on_owner_role`)** — the deny-drop for the connector AND the hardening test proving a non-connector our-account ARN keeps the Deny:

```python
@pytest.mark.asyncio
async def test_put_omits_self_account_deny_for_connector_arn(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(bedrock_aip, "_iam_client", lambda: fake)
    monkeypatch.setattr(bedrock_aip.settings, "ENVIRONMENT", "dev", raising=False)

    await bedrock_aip.put_owner_aws_sts_policy(
        "org_self", ["arn:aws:iam::877352799272:role/isol8-readonly-connector"]
    )
    doc = json.loads(fake.put_role_policy.call_args.kwargs["PolicyDocument"])
    assert all(s["Effect"] != "Deny" for s in doc["Statement"])


@pytest.mark.asyncio
async def test_put_keeps_deny_for_non_connector_our_account_arn(monkeypatch):
    # HARDENING: only the EXACT connector ARN drops the deny. Any OTHER
    # our-account ARN (e.g. a bug or ungated caller) MUST keep the deny so the
    # assume stays blocked — the tenant-protection guarantee.
    fake = MagicMock()
    monkeypatch.setattr(bedrock_aip, "_iam_client", lambda: fake)
    monkeypatch.setattr(bedrock_aip.settings, "ENVIRONMENT", "dev", raising=False)

    await bedrock_aip.put_owner_aws_sts_policy(
        "org_x", ["arn:aws:iam::877352799272:role/isol8-prod-service-x"]
    )
    doc = json.loads(fake.put_role_policy.call_args.kwargs["PolicyDocument"])
    assert any(s["Effect"] == "Deny" for s in doc["Statement"])
```

- [ ] **Step 6: Run all grant tests to verify pass**

Run: `cd apps/backend && uv run pytest tests/credentials/test_aws_sts_grant.py -q`
Expected: PASS (including the pre-existing default-Deny tests).

- [ ] **Step 7: Lint + commit**

```bash
cd apps/backend && uv run ruff format core/billing/bedrock_aip.py tests/credentials/test_aws_sts_grant.py && uv run ruff check core/billing/bedrock_aip.py
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/aws-scope-tiers
git add apps/backend/core/billing/bedrock_aip.py apps/backend/tests/credentials/test_aws_sts_grant.py
git commit -m "feat(aws): omit self-account Deny in the inline grant for our-account connections"
```

---

### Task 7: Boundary carve — exempt the founder role (reuse `PLATFORM_OPS_OWNER_ID`)

The shared boundary's `BoundaryDenyAssumeIntoOurAccount` denies every per-owner role from assuming any role in our account — including the founder's connector. IAM boundaries are static (can't verify a trust policy), so this is the one place a founder-identity value is needed. Exempt **only** the founder's per-owner role via an `aws:PrincipalArn` condition; reuse the existing `PLATFORM_OPS_OWNER_ID` (same founder org). Unset ⇒ no condition (Deny stays fully broad, fail-closed).

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts:285, 340-345`

**Interfaces:**
- Consumes: `process.env.PLATFORM_OPS_OWNER_ID_{DEV,PROD}` (already exported to the synth env by `app.ts`).
- Produces: boundary Deny that exempts exactly the founder per-owner role ARN.

- [ ] **Step 1: Compose the founder role ARN before the boundary definition (before `this.ownerBedrockBoundary = …` at line 285)**

```typescript
    // Self-account carve-out (config-driven, nothing hardcoded): the founder
    // org, reusing PLATFORM_OPS_OWNER_ID (same owner). Empty ⇒ no carve (Deny
    // stays fully broad; fail-closed). Per-owner role name mirrors the Bedrock
    // rail: isol8-<env>-owner-bedrock-<owner>.
    const selfAccountOwnerId =
      props.environment === "prod"
        ? (process.env.PLATFORM_OPS_OWNER_ID_PROD ?? "")
        : (process.env.PLATFORM_OPS_OWNER_ID_DEV ?? "");
    const selfAccountRoleArn = selfAccountOwnerId
      ? `arn:aws:iam::${this.account}:role/isol8-${props.environment}-owner-bedrock-${selfAccountOwnerId}`
      : undefined;
```

- [ ] **Step 2: Add the exempting condition to the boundary Deny (replace the `BoundaryDenyAssumeIntoOurAccount` statement at lines 340-345)**

```typescript
        // Defense in depth: never let a per-owner role assume into OUR account,
        // even if a misconfigured inline policy tried to. An explicit Deny always
        // wins. The founder's per-owner role is exempted by aws:PrincipalArn so it
        // (and only it) can assume its own trust-pinned connector role; every other
        // owner stays fully denied.
        new iam.PolicyStatement({
          sid: "BoundaryDenyAssumeIntoOurAccount",
          effect: iam.Effect.DENY,
          actions: ["sts:AssumeRole"],
          resources: [`arn:aws:iam::${this.account}:role/*`],
          conditions: selfAccountRoleArn
            ? { StringNotEquals: { "aws:PrincipalArn": selfAccountRoleArn } }
            : undefined,
        }),
```

- [ ] **Step 3: Validate synth (unset and set)**

Run: `cd apps/infra && npx cdk synth >/dev/null 2>synth.err; echo "exit=$?"; grep -i error synth.err | head; rm -f synth.err`
Expected: `exit=0`. With `PLATFORM_OPS_OWNER_ID_*` unset in the shell, the Deny has no condition (still present, fully broad). `.github/workflows/deploy.yml` unchanged.

- [ ] **Step 4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/aws-scope-tiers
git status --short   # no cdk.out staged
git add apps/infra/lib/stacks/container-stack.ts
git commit -m "feat(aws): boundary carve-out for the founder role's connector assume"
```

---

### Task 8: Live end-to-end validation (Approach A) + open questions

Not a code task — the acceptance gate. Performed after Tasks 1-7 are deployed to **dev** first, then repeated for prod. `PLATFORM_OPS_OWNER_ID` is already set for prod (`org_3DBS…`); set the dev value if the dev founder org should have it.

- [ ] **Step 1: Deploy**

Merge/deploy the branch. Confirm the dev backend + a CDK deploy applied (the boundary carve + the `iam:GetRole` grant need the CDK deploy). Ensure `PLATFORM_OPS_OWNER_ID_DEV` is set to the dev founder-org id if validating in dev.

- [ ] **Step 2: Connect via the real flow (Approach A)**

As the founder org, open the AWS connect card → pick a tier → deploy the CFN quick-create stack in our account → paste the connector role ARN back. Confirm `aws-complete` returns `{"status": "connected"}` (validates: the ViewOnly ARN fix, the trust-pin gate accepting the legit connector, and the whole rail end-to-end).

- [ ] **Step 3: Assert the credential-routing invariant from a gateway agent exec**

From the agent's exec (chat/agent exec path, NOT a root ECS-exec shell):
- `aws sts get-caller-identity` → the returned `Arn` is the **assumed connector role** (`.../isol8-readonly-connector/...`), not the per-owner task role.
- A write (`aws s3 mb …`) is **denied** under ViewOnly/ReadOnly and **permitted** under PowerUser (re-connect with PowerUser to spot-check).
- Confirm the node process has **no** `AWS_PROFILE` and Bedrock inference still works (chat a message; no AccessDenied).

- [ ] **Step 4: Assert isolation holds**

- A **non-founder** owner still gets 400 from `aws-complete` when connecting any our-account ARN (their trust wouldn't pin them; and they can't create a role in our account).
- The founder cannot connect a **non-connector** our-account role (paste a CDK/service role ARN → trust-pin fails → 400).
- The founder's agent cannot assume any non-connector role in our account (e.g. `aws sts assume-role --role-arn arn:aws:iam::877352799272:role/isol8-dev-service-…` from the agent exec is denied).

- [ ] **Step 5: Resolve the three open questions**

- **Per-owner role exists for the founder org:** confirm `isol8-{env}-owner-bedrock-{owner}` exists for the founder org (the CFN trust pins it; the boundary carve composes it). If the founder org's provider type never minted a per-owner role, close that gap first.
- **Reprovision rehydration:** reprovision the dev container and confirm `~/.aws/config` is re-materialized (or add a rehydration path if it silently drops).
- **Session refresh:** run a long agent exec spanning >1h and confirm the 3600s connector-role session refreshes cleanly (no mid-run expiry).

- [ ] **Step 6: Promote to prod**

Repeat Steps 1-5 against the prod account with the founder org (`PLATFORM_OPS_OWNER_ID_PROD = org_3DBS8G83oPrMm49yN2b70EjoFpN`, already set), once dev is green.
