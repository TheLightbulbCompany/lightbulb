name: expense-intake
# Plain-language expense/income intake from anyone in the org (originally #expenses Slack).
# Deterministic, gateway-free. Tally classifies, tax-tags, and queues for the finance
# person's REQUIRED approval before anything is treated as a posting candidate.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node) under `command:`.
#  - The ONE genuine LLM step (plain-language → structured fields) CANNOT run in an embedded
#    pipeline (llm-task is 401 here). The agent loop does that extraction and passes the result
#    in as the `extraction` arg. The pipeline stays deterministic from there.
#  - Slack delivery is unreachable here (401). The clarify-request, the submitter confirmation,
#    and the finance-person notification are all written to the `tally/notifications` state queue
#    for the agent loop to deliver.
#  - Durable state via native state.get/set. The original fast-io keys become single-key INDEX
#    ARRAYS (state.* has no prefix-list): tally/approval-queue, tally/notifications, tally/audit-log.
#    Read-only consumer config (vendor-map, chart-of-accounts) is seeded by the agent/user.
#  - Tier 2 (write) requires the finance person's approval — enforced with a real step-level
#    `approval: required` gate placed BEFORE the side effect, per AGENTS.md's two-tier system.
#  - Scripts (tally-categorization-engine.js, tally-tax-tracker.js) exit 0 and carry status in
#    JSON; the workflow branches via condition:. Never gate on .length — gate on an explicit count.

args:
  message:
    required: true          # original plain-language text (kept for audit + agent-side re-extraction)
  submitter:
    required: true
  channel:
    default: "slack"
  extraction:
    required: true          # structured fields the AGENT LOOP extracted from `message`.
                            # Shape: {amount, vendor, category, subcategory, date, description,
                            # type, confidence_score, ambiguity_note}. The pipeline is deterministic
                            # given this; the LLM judgment happened upstream in the agent loop.

steps:
  # 1. Load the vendor→category learning map (read-only consumer config; seeded by corrections).
  - id: load-vendor-map
    pipeline: state.get tally/vendor-map

  # 2. Load the chart of accounts (read-only consumer config from connected accounting software).
  - id: load-chart
    pipeline: state.get tally/chart-of-accounts

  # 3. Assemble the engine input ({mode:"check", extraction, vendor_map}). Must precede `validate`.
  - id: validate-input
    command: >
      node -e 'const ex=JSON.parse(process.env.EXTRACTION||"{}");
        const vm=JSON.parse(process.env.VENDOR_MAP||"null")||{};
        process.stdout.write(JSON.stringify({mode:"check", extraction:ex, vendor_map:vm}));'
    env:
      EXTRACTION: $extraction
      VENDOR_MAP: $load-vendor-map.json

  # 3b. Apply vendor mapping + confidence check (Req 9, 10). Deterministic; script always exits 0.
  - id: validate
    command: node tally-categorization-engine.js
    stdin: $validate-input.stdout

  # 4. Route decision. The engine emits confidence_check.route:
  #      ambiguity_flow      -> missing required fields, ask the submitter to clarify
  #      individual_review   -> low confidence / unknown vendor, queue but flag
  #      standard_approval   -> clean, queue for finance approval
  #    Emit an explicit boolean + the route so downstream steps gate cleanly (no .length).
  - id: route
    command: >
      node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const cc=v.confidence_check||{};
        const ambiguous = cc.route==="ambiguity_flow";
        process.stdout.write(JSON.stringify({
          ambiguous,
          proceed: !ambiguous,
          route: cc.route||"standard_approval",
          category: v.category||null,
          subcategory: v.subcategory||null,
          vendor: v.vendor||null,
          amount: v.amount,
          type: v.type||"expense",
          mapping_confidence: v.mapping_confidence||"needs_review"
        }));'
    stdin: $validate.stdout

  # ---- AMBIGUOUS BRANCH: queue a clarification request for the agent to deliver ----

  - id: clarify-load
    condition: $route.json.ambiguous
    pipeline: state.get tally/notifications

  - id: clarify-next
    condition: $route.json.ambiguous
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const ex=JSON.parse(process.env.EXTRACTION||"{}");
        n.push({kind:"clarify_request", channel:process.env.LOBSTER_ARG_CHANNEL||"slack",
          submitter:process.env.LOBSTER_ARG_SUBMITTER||"",
          original_message:process.env.LOBSTER_ARG_MESSAGE||"",
          ambiguity_note: ex.ambiguity_note || "Some required details are missing — please confirm amount, vendor, and category.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $clarify-load.json
      EXTRACTION: $extraction

  - id: clarify
    condition: $route.json.ambiguous
    pipeline: state.set tally/notifications
    stdin: $clarify-next.stdout

  # ---- CLEAR BRANCH: tax-tag, confirm to submitter, gate, queue for finance, notify, audit ----

  # 5. Tax tagging (Req 45). mode=tag; deterministic; exits 0. Build input first, then run.
  - id: tax-input
    condition: $route.json.proceed
    command: >
      node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({mode:"tag", transaction:v}));'
    stdin: $validate.stdout

  - id: tax-tag
    condition: $route.json.proceed
    command: node tally-tax-tracker.js
    stdin: $tax-input.stdout

  # 6. Queue a confirmation-of-interpretation notification back to the submitter (Req 8).
  #    Slack is unreachable here, so it goes to the notifications queue for the agent.
  - id: confirm-load
    condition: $route.json.proceed
    pipeline: state.get tally/notifications

  - id: confirm-next
    condition: $route.json.proceed
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const r=JSON.parse(process.env.ROUTE||"{}");
        n.push({kind:"interpretation_confirm", channel:process.env.LOBSTER_ARG_CHANNEL||"slack",
          submitter:process.env.LOBSTER_ARG_SUBMITTER||"",
          amount:r.amount, category:r.category, subcategory:r.subcategory, vendor:r.vendor,
          text:"Got it — $"+(r.amount==null?"?":r.amount)+" classified as "+(r.category||"uncategorized")
            +(r.subcategory?(" ("+r.subcategory+")"):"")+" from "+(r.vendor||"unknown vendor")
            +". This will go to your finance person for review.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $confirm-load.json
      ROUTE: $route.json
    # Note: $route.json passed whole as an env JSON string; node re-parses it.

  - id: confirm
    condition: $route.json.proceed
    pipeline: state.set tally/notifications
    stdin: $confirm-next.stdout

  # 7. TIER 2 GATE — the finance person must approve before this becomes a posting candidate.
  #    Real step-level approval gate (NOT a fictional await-approval tool). The run halts with a
  #    resumeToken; the side effects below are gated on $approve.approved. Per AGENTS.md, there
  #    are no exceptions to this gate. Anomaly/low-confidence items still flow through it.
  - id: approve
    condition: $route.json.proceed
    approval:
      prompt: "Approve expense from ${submitter} for the finance review queue?"

  # 8. Build the approval-queue entry (only after the gate clears).
  - id: queue-entry
    condition: $approve.approved
    command: >
      node -e 'const entry=JSON.parse(process.env.VALIDATED||"{}");
        const tax=JSON.parse(process.env.TAX||"{}");
        const id=Date.now().toString(36)+"-"+(process.env.LOBSTER_ARG_SUBMITTER||"unknown");
        process.stdout.write(JSON.stringify({
          id,
          entry,
          tax_tag: tax.tax_tag||null,
          cpa_flags: tax.cpa_flags||[],
          submitter: process.env.LOBSTER_ARG_SUBMITTER||"",
          original_message: process.env.LOBSTER_ARG_MESSAGE||"",
          source: "plain_language_input",
          status: "pending_approval",
          queued_at: new Date().toISOString()
        }));'
    env:
      VALIDATED: $validate.stdout
      TAX: $tax-tag.stdout

  # 9. Append the entry to the approval-queue index array (load → edit → set).
  - id: queue-load
    condition: $approve.approved
    pipeline: state.get tally/approval-queue

  - id: queue-next
    condition: $approve.approved
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const e=JSON.parse(require("fs").readFileSync(0,"utf8"));
        q.push(e);
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: $queue-load.json
    stdin: $queue-entry.stdout

  - id: queue
    condition: $approve.approved
    pipeline: state.set tally/approval-queue
    stdin: $queue-next.stdout

  # 10. Notify the finance person that a new item is queued (→ notifications queue for the agent).
  - id: notify-load
    condition: $approve.approved
    pipeline: state.get tally/notifications

  - id: notify-next
    condition: $approve.approved
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const e=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const en=e.entry||{};
        n.push({kind:"finance_review", channel:"slack", target:"#tally-approvals",
          queue_id:e.id, submitter:e.submitter,
          amount:en.amount, category:en.category, vendor:en.vendor,
          description:en.description||"", mapping_confidence:en.mapping_confidence||"needs_review",
          cpa_flag_count:(e.cpa_flags||[]).length,
          text:"New expense from "+e.submitter+": $"+(en.amount==null?"?":en.amount)
            +" — "+(en.category||"uncategorized")+" (vendor: "+(en.vendor||"unknown")+")."
            +" Awaiting your approval.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.json
    stdin: $queue-entry.stdout

  - id: notify
    condition: $approve.approved
    pipeline: state.set tally/notifications
    stdin: $notify-next.stdout

  # 11. Audit trail (Req: every action logged; 7-year retention). Append to the audit index array.
  #     Audited only after the gate clears (the gate decision is itself the recorded action).
  - id: audit-load
    condition: $approve.approved
    pipeline: state.get tally/audit-log

  - id: audit-next
    condition: $approve.approved
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const e=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const en=e.entry||{};
        log.push({action:"expense_intake", queue_id:e.id, submitter:e.submitter,
          amount:en.amount, category:en.category||null,
          confidence: en.confidence_score!=null?en.confidence_score:null,
          source:"plain_language", authorized_by:"finance_approval_gate",
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-load.json
    stdin: $queue-entry.stdout

  - id: audit
    condition: $approve.approved
    pipeline: state.set tally/audit-log
    stdin: $audit-next.stdout
