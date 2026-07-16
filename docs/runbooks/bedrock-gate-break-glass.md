# bedrock-gate — break-glass runbook

Operational recovery for the synchronous Bedrock metering gateway
(`apps/infra/lambda/bedrock-gate/`). Read the design first:
`docs/superpowers/specs/2026-07-15-bedrock-gate-design.md` (§8 failure modes,
§9 rollout). This runbook is the copy-paste layer on top of that.

**Prereqs for every command below:**

```bash
aws sso login --profile isol8-admin
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
cd apps/backend            # the cutover script + inline snippets run from here
```

The single control is the per-owner containers-row attribute
**`bedrock_via_gateway`** (BOOL, default absent/false). True routes the owner's
Bedrock traffic through the gate; false/absent is exactly today's direct-to-
Bedrock behavior. Everything here is either flipping that flag or, when a
reprovision is too slow, hot-swapping the on-disk config directly.

> **Phase note (IAM).** During the transition phases (spec §9, phases 1–3) the
> owner's own `owner-bedrock` IAM policy is **still attached**, so reverting the
> config (flag flip or hot-swap) makes traffic fall back to direct Bedrock
> **immediately** — no IAM action needed. After Phase 4 drops `bedrock:Invoke*`
> from owner roles, a revert additionally requires re-granting IAM with
> `restore_owner_bedrock` (see §3). Know which phase you are in before you
> revert.

---

## 1. EMERGENCY REVERT — gateway is erroring / throttling

Symptom: `gate.error` / `gate.upstream_error` alarms firing, or chat showing
Bedrock 5xx/429 for cut-over owners. Goal: get traffic off the gateway now.

### 1a. Preferred — flip the cohort flag false + reprovision (uses the cutover script)

```bash
# Dry-run first (prints the exact owners that will be reverted, writes nothing):
uv run python -m scripts.bedrock_gate_cutover --cohort trial --env prod --rollback
uv run python -m scripts.bedrock_gate_cutover --cohort paid  --env prod --rollback

# Execute:
uv run python -m scripts.bedrock_gate_cutover --cohort trial --env prod --rollback --apply
uv run python -m scripts.bedrock_gate_cutover --cohort paid  --env prod --rollback --apply
```

`--rollback` sets `bedrock_via_gateway=false` on each Bedrock-path owner and
reprovisions (rewrites `openclaw.json` `baseUrl` back to
`https://bedrock-runtime.<region>.amazonaws.com` **and** drops the gateway
bearer-token task-def env). A single owner: `--owner org_xxx` instead of
`--cohort`. BYOK / ChatGPT-OAuth owners are never touched.

Reprovision is minutes (image pull + running transition). If you cannot wait
for a reprovision, use the hot-swap below — it lands the config change on the
running container via chokidar in seconds.

### 1b. Fastest — direct config hot-swap fallback (`replace_provider_block`)

Repoints the `amazon-bedrock` provider `baseUrl` back to Bedrock in the owner's
on-disk `openclaw.json` in ONE atomic write (no task-def churn, no scale-to-
zero). This is the same `config_patcher.replace_provider_block` path
`billing_service.change_provider` uses for Bedrock↔ChatGPT switches; OpenClaw's
reload-plan classifies the touched keys as hot, so the gateway picks it up
without a process restart.

Because it does not rewrite the task-def, this reverts the **OpenClaw provider**
path immediately. The ACP/Claude-Code path (which reads
`ANTHROPIC_BEDROCK_BASE_URL` from the task-def env) is only reverted by a full
reprovision (§1a) — do the hot-swap to stop the bleeding, then follow with §1a
to revert task-def env too and clear the flag.

Single owner:

```bash
uv run python - <<'PY'
import asyncio, os
os.environ["ENVIRONMENT"] = "prod"
OWNER = "org_xxx"

async def main():
    from core.config import settings
    from core.containers.config import build_provider_block, build_default_model, provider_plugin_entries
    from core.services.config_patcher import replace_provider_block
    from core.services.container_provision import resolve_provider_choice
    from core.billing.bedrock_aip import read_owner_aips

    provider_choice, byo_provider = await resolve_provider_choice(OWNER)
    aip_arns = await read_owner_aips(OWNER)  # per-owner AIP ARNs
    # via_gateway=False forces baseUrl = https://bedrock-runtime.<region>.amazonaws.com
    providers = build_provider_block(
        provider_choice, byo_provider=byo_provider, aip_arns=aip_arns, via_gateway=False,
    )
    await replace_provider_block(
        OWNER,
        providers=providers,
        default_model=build_default_model(provider_choice, byo_provider),
        plugin_entries=provider_plugin_entries(provider_choice, byo_provider),
        ceo_model=None, cheap_model=None,
    )
    print("hot-swapped baseUrl back to direct Bedrock for", OWNER)

asyncio.run(main())
PY
```

> The exact helper names (`build_provider_block` / `build_default_model` /
> `provider_plugin_entries` and the `via_gateway` kwarg) land with the Task 7
> config-emission change. Verify them against
> `core/containers/config.py` before running — if the signatures differ, mirror
> whatever `billing_service.change_provider` passes to `replace_provider_block`,
> with the Bedrock (not gateway) `baseUrl`.

Fleet-wide: wrap the body in `for OWNER in await container_repo.scan_all()` and
filter to Bedrock-path owners (`subscription_status == "trial_local"` or
`provider_choice == "bedrock_claude"`) — the same predicate the cutover script's
`_classify_plan` uses. Then still run §1a `--rollback --apply` afterward to
clear the `bedrock_via_gateway` flag and revert the task-def env so the state is
consistent and a later reprovision does not re-enable the gateway.

### 1c. Post-Phase-4 addendum — re-grant IAM

If Phase 4 has already dropped `bedrock:Invoke*` from owner roles, reverting the
config is not enough — the container has no IAM to reach Bedrock directly.
Re-grant per owner:

```bash
uv run python - <<'PY'
import asyncio, os
os.environ["ENVIRONMENT"] = "prod"
async def main():
    from core.billing.bedrock_aip import restore_owner_bedrock
    await restore_owner_bedrock("org_xxx")   # re-attaches owner-bedrock inline policy from persisted AIP map
    print("restored owner IAM")
asyncio.run(main())
PY
```

`restore_owner_bedrock` raises if the owner has no persisted AIP map — that
owner must be fully re-provisioned first.

---

## 2. Reconciliation validation — confirm the gateway is counting before advancing a phase

Do NOT advance to the next cohort/phase until the gateway's own accounting
reconciles against the meter auditor. Two signals:

### 2a. `meter.reconciled_gateway_miss` (the auditor caught a gateway miss)

Emitted by the meter Lambda (`apps/infra/lambda/bedrock-usage-meter/index.py`)
when a **trial** dedup claim SUCCEEDS for an invocation whose IAM identity is the
gateway role — i.e. the gateway streamed the response but never debited it
(crash / client-abort / missing usage event). Near-zero is the goal;
spec §9 requires **≈ 0 over 2 weeks** before Phase 5.

Namespace `Isol8`, dims `{service=isol8-bedrock-meter, env=<env>}`:

```bash
aws cloudwatch get-metric-statistics \
  --namespace Isol8 --metric-name meter.reconciled_gateway_miss \
  --dimensions Name=service,Value=isol8-bedrock-meter Name=env,Value=prod \
  --start-time "$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time   "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum
```

Any sustained non-zero Sum ⇒ the gateway is dropping debits; investigate
`gate.client_abort` / `gate.upstream_error` / `gate.error` (below) before
advancing. The counters still reconcile (the meter backfilled the miss), so
spend is correct — but a high miss rate means the gateway is not doing its job.

### 2b. `gate.*` EMF metrics (the gateway's own view)

Namespace **`Isol8/BedrockGate`**, dimension `env` (some carry an extra `plan`
or `status` property, queryable in Logs Insights but not a metric dimension).
The set the gateway emits: `gate.allowed`, `gate.denied_budget` (per code),
`gate.auth_failed`, `gate.debit_ok`, `gate.debit_skipped_dup`,
`gate.client_abort`, `gate.upstream_error`, `gate.error`, `gate.not_found`,
`gate.hold_release_failed`.

Healthy shape: `gate.debit_ok` ≈ `gate.allowed`; `gate.error` /
`gate.hold_release_failed` flat at zero; `gate.debit_skipped_dup` small
(retries only).

```bash
# debit success rate over the last hour:
for m in gate.allowed gate.debit_ok gate.debit_skipped_dup gate.client_abort gate.upstream_error gate.error; do
  printf '%-24s ' "$m"
  aws cloudwatch get-metric-statistics \
    --namespace Isol8/BedrockGate --metric-name "$m" \
    --dimensions Name=env,Value=prod \
    --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time   "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 3600 --statistics Sum \
    --query 'Datapoints[0].Sum' --output text
done
```

Cross-check the actual spend counters reconcile: for a canary trial owner,
`billing-accounts.trial_daily_spend_microcents.<YYYY-MM-DD>` should track the
sum of that owner's priced invocations (the meter is the source of truth). A
large positive `reserved_microcents.<date>` that never drains back toward zero
means holds are leaking (they TTL-expire in ~20 min, so a persistent non-zero
is a red flag).

---

## 3. Deny-flag stuck — owner denied after budget should allow

The kill switch writes `denied=true` on the owner's `bedrock-gate-tokens` row
(fired by the meter cap-kill and `enforce_on_low_balance`); the gateway 403s
while it is set. It is meant to clear itself on the next allow, via the same
restore paths as the IAM revoke today:

- **Trial, day rolled over:** `provision_gate.maybe_restore_trial_bedrock`
  clears it on the next `/container/status` poll once the daily counter is
  under cap. Force a poll (open the app) or wait a tick.
- **Paid, balance now positive:** `metronome_gate` clears it after ONE
  Metronome-positive refresh. A stale fast-gate row refreshes lazily when >60s
  old.
- **Manual clear (admin):** clear both the deny flag and the IAM revoke stamp:

```bash
uv run python - <<'PY'
import asyncio, os
os.environ["ENVIRONMENT"] = "prod"
async def main():
    from core.billing.bedrock_aip import restore_owner_bedrock
    from core.billing.bedrock_gate_tokens import deny_gate_token   # lands with Task 7
    await restore_owner_bedrock("org_xxx")        # clears bedrock_revoked + re-attaches IAM
    await deny_gate_token("org_xxx", False)       # clears denied on the gate-tokens row
    print("cleared deny flag + restored IAM for org_xxx")
asyncio.run(main())
PY
```

If `deny_gate_token` is not yet available in the deployed backend, clear the
row directly: `GetItem`/`UpdateItem` on `isol8-<env>-bedrock-gate-tokens` keyed
by the owner's `bedrock_gate_token_hash` (from the containers row), setting
`denied = false`.

---

## 4. acpx image-bump caveat — RE-VERIFY after every Claude Code bump

The ACP / Claude-Code path routes through the gateway using THREE env vars on
the per-user task-def:

- `ANTHROPIC_BEDROCK_BASE_URL` = the gate Function URL
- `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`
- `ANTHROPIC_AUTH_TOKEN` = the same per-owner bearer token

These were verified against the pinned Claude Code binary **2.1.156** (grepped,
not just doc'd; spec §5.5 / §10 V1). The CLI version **floats with acpx image
bumps**, and the mechanism is version-sensitive in two ways:

1. The three env-var names above must still be honored (AWS-SDK endpoint vars
   like `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` do NOT work on 2.1.156 — 0 hits in
   the binary).
2. 2.1.156 predates the `CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD` escape
   hatch and **hard-fails** (`Truncated event message received`) if the proxy
   reframes or drops the `application/vnd.amazon.eventstream` content-type — so
   the gateway's verbatim byte-passthrough is load-bearing for ACP, not
   optional.

**After ANY acpx / Claude Code image bump**, before rolling it fleet-wide:
grep the new binary for the three env-var names, run one ACP turn through the
gateway on a dev canary, and confirm streaming completes (no `Truncated event
message received`). If the env-var contract changed, hold the bump and update
`ecs_manager`'s ACP env emission + this runbook before proceeding.

---

## Quick reference

| Situation | Action |
|---|---|
| Gateway erroring, need off now | §1a `--rollback --apply` (cohort or `--owner`); §1b hot-swap if reprovision too slow |
| Single owner misbehaving | `--owner org_xxx --env prod --rollback --apply` |
| Post-Phase-4 revert | §1a/§1b **plus** §1c `restore_owner_bedrock` |
| Advancing a phase | §2 — `meter.reconciled_gateway_miss` ≈ 0 + `gate.error` flat + counters reconcile |
| Owner stuck denied | §3 — poll to auto-clear, or admin `restore_owner_bedrock` + `deny_gate_token(..., False)` |
| After acpx bump | §4 — re-verify the three ANTHROPIC/CLAUDE env vars + content-type passthrough |
