name: sequence-execute
# Pitch follow-up engine — runs hourly on cron. Finds due touches 2-5 across active
# sequences, applies interrupts (reply / material change / CRM status / final-touch),
# runs deterministic compliance on each prospect, then SPLITS the cleared work into:
#   - autonomous touches  -> pitch/pending-drafts  (agent loop drafts + scans + sends)
#   - C-suite / interrupted -> pitch/notifications  (agent surfaces to the rep)
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node/curl/mcporter)
#    under `command:`. fast-io / openclaw.invoke / slack / llm-task are FICTIONAL or 401
#    from the embedded runner — never called here.
#  - Durable state via state.get/set on the per-owner EFS mount. Active sequences, the
#    draft work-queue, and notifications are SINGLE-KEY INDEX ARRAYS (state.* has no
#    prefix-list): state.get -> a node step edits -> state.set with stdin from that step.
#  - CRM = Attio ONLY, reached gateway-free via the baked `mcporter` CLI. Its output is
#    YAML-ish TEXT, so a node step REGEX-parses stdout (never JSON.parse). HubSpot /
#    Salesforce / Pipedrive CRM paths are dropped.
#  - The outbound email platform (Instantly / SmartLead / Mailshake / Lemlist) is a
#    SEPARATE click-to-connect layer reached by `curl` with its own $*_API_KEY env. Its
#    analytics (bounce rate) + reply feed are deterministic reads kept as curl.
#  - The ONE follow-up DRAFT step is a genuine LLM task (thinking: low). The embedded
#    runner can't call the model, so it is LEFT FOR THE AGENT LOOP: this pipeline writes
#    the cleared touches to pitch/pending-drafts and the agent drafts, content-scans
#    (pitch-content-scanner.js), and sends them in its own loop.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.
#    NEVER gate on .length — producing steps emit an explicit count.

steps:
  # ------------------------------------------------------------------
  # 1. Which outbound email platform is connected (drives bounce + reply curls).
  # ------------------------------------------------------------------
  - id: outbound-config
    pipeline: state.get pitch/connected-outbound

  - id: outbound-platform
    command: >
      node -e 'const c=JSON.parse(process.env.CFG||"null")||{};
        const p=(c.platform||"none");
        process.stdout.write(JSON.stringify({platform:p, has_platform: p!=="none"}));'
    env:
      CFG: $outbound-config.stdout

  # ------------------------------------------------------------------
  # 2. Bounce-rate gate (Req 2). Pull aggregate analytics from the connected
  #    outbound platform via curl (real CLI, own API key). Each branch writes raw
  #    JSON to stdout; a normalizer wraps it as {analytics:{...}} for the scorer.
  # ------------------------------------------------------------------
  - id: bounce-instantly
    condition: $outbound-platform.json.platform == "instantly"
    command: >
      curl -s "https://api.instantly.ai/api/v1/analytics/campaign?api_key=$INSTANTLY_API_KEY&metric=bounce_rate"

  - id: bounce-smartlead
    condition: $outbound-platform.json.platform == "smartlead"
    command: >
      curl -s "https://server.smartlead.ai/api/v1/analytics?metric=bounce_rate" -H "Authorization: Bearer $SMARTLEAD_API_KEY"

  - id: bounce-mailshake
    condition: $outbound-platform.json.platform == "mailshake"
    command: >
      curl -s "https://api.mailshake.com/2017-04-01/analytics" -H "Authorization: Basic $MAILSHAKE_API_KEY"

  - id: bounce-lemlist
    condition: $outbound-platform.json.platform == "lemlist"
    command: >
      curl -s "https://api.lemlist.com/api/campaigns/analytics" -H "Authorization: Bearer $LEMLIST_API_KEY"

  # Normalize whichever branch produced raw analytics -> {analytics:{...}}.
  # When no platform is connected there is no send history, so the scorer's
  # "no data" path (pass:false) correctly blocks the run.
  - id: bounce-input
    command: >
      node -e 'const pick=s=>{try{return JSON.parse(s)}catch{return null}};
        const a=pick(process.env.I)||pick(process.env.S)||pick(process.env.M)||pick(process.env.L)||{};
        const an=a.analytics||a.data||a||{};
        process.stdout.write(JSON.stringify({analytics:an}));'
    env:
      I: $bounce-instantly.stdout
      S: $bounce-smartlead.stdout
      M: $bounce-mailshake.stdout
      L: $bounce-lemlist.stdout

  # on_error: continue — the script still emits its {pass,...} JSON to stdout before its
  # non-zero exit on the fail / no-data paths; continue keeps the run alive so the
  # bounce_pass flag flows into scheduler-input. (Proper fix: make the script exit 0 and
  # carry status in JSON — see the .js fix list.)
  - id: bounce-check
    command: node pitch-bounce-rate-check.js
    stdin: $bounce-input.stdout
    on_error: continue

  # ------------------------------------------------------------------
  # 3. Load active sequences (single-key index array) + learned timing weights.
  #    These state reads + the scheduler run UNCONDITIONALLY so that check-due always
  #    produces a real has_due_touches boolean — every downstream gate then references
  #    only always-run steps (no condition ever points at a skipped step). The bounce
  #    gate is enforced by zeroing the scheduler input when bounce-check failed.
  # ------------------------------------------------------------------
  - id: sequences-load
    pipeline: state.get pitch/sequences

  - id: timing-load
    pipeline: state.get pitch/timing-weights

  # Wrap the two arrays into the {sequences, timing_weights} shape the scheduler reads.
  # When the bounce gate failed, feed an EMPTY sequence set so the scheduler finds no
  # due touches and the whole follow-up path no-ops (Req 2 hard block). Emit an explicit
  # count so the (unused but documented) gate never relies on .length.
  - id: scheduler-input
    command: >
      node -e 'const pick=s=>{try{return JSON.parse(s)}catch{return null}};
        const pass=(pick(process.env.BOUNCE)||{}).pass===true;
        const seq=pass?((pick(process.env.SEQ))||[]):[];
        const tw=pick(process.env.TW)||{};
        process.stdout.write(JSON.stringify({sequences:seq, timing_weights:tw, count:seq.length, bounce_pass:pass}));'
    env:
      BOUNCE: $bounce-check.stdout
      SEQ: $sequences-load.stdout
      TW: $timing-load.stdout

  # ------------------------------------------------------------------
  # 4. Compute due touches (Req 24/28/30/31). Scheduler attaches requires_approval
  #    (C-suite) per touch and emits has_due_touches. Runs unconditionally on the
  #    always-present scheduler-input (empty when bounce-blocked => has_due_touches:false).
  # ------------------------------------------------------------------
  - id: check-due
    command: node pitch-sequence-scheduler.js
    stdin: $scheduler-input.stdout

  # ------------------------------------------------------------------
  # 5. Interrupt inputs (Req 29): recent signals, Attio CRM status changes, replies.
  # ------------------------------------------------------------------
  - id: signals-load
    condition: $check-due.json.has_due_touches
    pipeline: state.get pitch/signals-latest

  # CRM status changes — Attio ONLY, via mcporter (text -> regex parse).
  # list-records on companies returns record_id + attributes incl. status/deal_stage.
  # We surface {prospect_domain, old_status, new_status} rows for the interrupt-checker.
  - id: crm-changes
    condition: $check-due.json.has_due_touches
    command: >
      node -e 'const {execSync}=require("child_process");
        let txt=""; try { txt=execSync("mcporter call attio.list-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",limit:100}))+" --output text",
          {encoding:"utf8",timeout:45000}); } catch(e){ txt=""; }
        const changes=[];
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const dom=((p.match(/domains\[\d+\]:\s*([^\s"]+)/)||[])[1]||"").toLowerCase();
          const status=((p.match(/\n\s*status:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          if (dom && status) changes.push({prospect_domain:dom, new_status:status, old_status:""});
        }
        process.stdout.write(JSON.stringify(changes));'

  - id: replies-instantly
    condition: $check-due.json.has_due_touches && $outbound-platform.json.platform == "instantly"
    command: >
      curl -s "https://api.instantly.ai/api/v1/leads/replies?api_key=$INSTANTLY_API_KEY&since=1h"

  - id: replies-smartlead
    condition: $check-due.json.has_due_touches && $outbound-platform.json.platform == "smartlead"
    command: >
      curl -s "https://server.smartlead.ai/api/v1/replies?since=1h" -H "Authorization: Bearer $SMARTLEAD_API_KEY"

  - id: replies-mailshake
    condition: $check-due.json.has_due_touches && $outbound-platform.json.platform == "mailshake"
    command: >
      curl -s "https://api.mailshake.com/2017-04-01/leads/replies?since=1h" -H "Authorization: Basic $MAILSHAKE_API_KEY"

  - id: replies-lemlist
    condition: $check-due.json.has_due_touches && $outbound-platform.json.platform == "lemlist"
    command: >
      curl -s "https://api.lemlist.com/api/activities?type=emailsReplied&since=1h" -H "Authorization: Bearer $LEMLIST_API_KEY"

  # ------------------------------------------------------------------
  # 6. Assemble the interrupt-checker input. The checker reads
  #    {due_touches, recent_signals, crm_changes, replies} — the original fed it only
  #    the scheduler output (missing 3 of 4 keys), so it never fired. Fixed here.
  # ------------------------------------------------------------------
  - id: interrupt-input
    condition: $check-due.json.has_due_touches
    command: >
      node -e 'const pick=s=>{try{return JSON.parse(s)}catch{return null}};
        const due=(pick(process.env.DUE)||{}).due_touches||[];
        const sig=pick(process.env.SIG)||[];
        const crmRaw=pick(process.env.CRM)||[];
        const crm=Array.isArray(crmRaw)?crmRaw:[];
        const rRaw=pick(process.env.RI)||pick(process.env.RS)||pick(process.env.RM)||pick(process.env.RL)||[];
        const replies=Array.isArray(rRaw)?rRaw:(rRaw.replies||rRaw.data||rRaw.activities||[]);
        process.stdout.write(JSON.stringify({due_touches:due, recent_signals:sig, crm_changes:crm, replies:replies}));'
    env:
      DUE: $check-due.stdout
      SIG: $signals-load.stdout
      CRM: $crm-changes.stdout
      RI: $replies-instantly.stdout
      RS: $replies-smartlead.stdout
      RM: $replies-mailshake.stdout
      RL: $replies-lemlist.stdout

  - id: interrupt-check
    condition: $check-due.json.has_due_touches
    command: node pitch-interrupt-checker.js
    stdin: $interrupt-input.stdout

  # ------------------------------------------------------------------
  # 7. Split cleared touches: C-suite (requires_approval) -> approval queue,
  #    autonomous -> compliance -> draft queue. Emit explicit counts for gating.
  #    Gated on the SAME has_due_touches as the notify/audit summary below, so those
  #    unconditional summary steps never reference a skipped step. Tolerates an empty
  #    cleared set internally (when every due touch was interrupted).
  # ------------------------------------------------------------------
  - id: route-csuite
    condition: $check-due.json.has_due_touches
    command: >
      node -e 'const data=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const cleared=data.cleared_touches||[];
        const autonomous=cleared.filter(t=>!t.requires_approval);
        const needsApproval=cleared.filter(t=>t.requires_approval);
        process.stdout.write(JSON.stringify({autonomous, needs_approval:needsApproval,
          autonomous_count:autonomous.length, approval_count:needsApproval.length}));'
    stdin: $interrupt-check.stdout

  # ------------------------------------------------------------------
  # 8. Deterministic compliance on each autonomous touch's PROSPECT (Req 4/6/7/30/41).
  #    pitch-compliance-check.js exports checkCompliance — map over all touches here so
  #    we keep only prospects that pass the hard-stop checks. (The original wired the
  #    content-scanner's flags into compliance, which never had a prospect to check.)
  #    Gated on has_due_touches (not autonomous_count) so it runs whenever route-csuite
  #    ran; an empty autonomous list just yields cleared_count:0.
  # ------------------------------------------------------------------
  - id: compliance-config
    condition: $check-due.json.has_due_touches
    pipeline: state.get pitch/compliance-config

  - id: compliance-filter
    condition: $check-due.json.has_due_touches
    command: >
      node -e 'const {checkCompliance}=require("./pitch-compliance-check.js");
        const data=JSON.parse(process.env.TOUCHES||"{}"); const cfg=JSON.parse(process.env.CFG||"null")||{};
        const touches=data.autonomous||[];
        const cleared=[], blocked=[], approval=[];
        for (const t of touches) {
          const prospect=Object.assign({}, t.prospect||{}, {touch_count:(t.touch_number||1)-1, touch_channel:t.channel, domain:t.domain});
          const r=checkCompliance(prospect, cfg);
          if (!r.pass) { blocked.push({domain:t.domain, blocks:r.blocks}); continue; }
          if (r.requires_approval) { approval.push(Object.assign({}, t, {compliance:r})); continue; }
          cleared.push(t);
        }
        process.stdout.write(JSON.stringify({cleared, blocked, approval,
          cleared_count:cleared.length, blocked_count:blocked.length, approval_count:approval.length}));'
    env:
      TOUCHES: $route-csuite.stdout
      CFG: $compliance-config.stdout

  # ------------------------------------------------------------------
  # 9. Enqueue the compliant autonomous touches for the AGENT LOOP to draft.
  #    The agent reads pitch/pending-drafts, generates one batch of follow-ups
  #    (llm-task, thinking: low — NEW angle per touch, no non-response references),
  #    runs them through pitch-content-scanner.js, then sends via the connected
  #    outbound platform and advances each sequence's touch_count + last_touch_date.
  # ------------------------------------------------------------------
  #    All three steps share the has_due_touches gate so they reference only steps that
  #    ran under that same gate; pushing zero cleared touches is a harmless no-op.
  - id: pending-load
    condition: $check-due.json.has_due_touches
    pipeline: state.get pitch/pending-drafts

  - id: pending-next
    condition: $check-due.json.has_due_touches
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const f=JSON.parse(process.env.FILTER||"{}"); const plat=JSON.parse(process.env.PLAT||"{}").platform||"none";
        const now=new Date().toISOString();
        for (const t of (f.cleared||[])) {
          q.push({kind:"followup_draft", domain:t.domain, touch_number:t.touch_number,
            channel:t.channel, sequence_id:t.sequence_id, prospect:t.prospect||{},
            outbound_platform:plat, needs_llm_draft:true, needs_content_scan:true, queued_at:now});
        }
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: $pending-load.stdout
      FILTER: $compliance-filter.stdout
      PLAT: $outbound-platform.json

  - id: pending-save
    condition: $check-due.json.has_due_touches
    pipeline: state.set pitch/pending-drafts
    stdin: $pending-next.stdout

  # ------------------------------------------------------------------
  # 10. Notifications queue (Slack is unreachable here): C-suite approvals,
  #     interrupts, compliance blocks, and the no-outbound-platform notice.
  #     The agent loop drains pitch/notifications and delivers them. Gated on the
  #     same has_due_touches so route/filter/interrupt refs always resolved.
  # ------------------------------------------------------------------
  - id: notify-load
    condition: $check-due.json.has_due_touches
    pipeline: state.get pitch/notifications

  - id: notify-next
    condition: $check-due.json.has_due_touches
    command: >
      node -e 'const pick=s=>{try{return JSON.parse(s)}catch{return null}};
        const n=pick(process.env.NOTIFS)||[];
        const route=pick(process.env.ROUTE)||{};
        const filt=pick(process.env.FILTER)||{};
        const inter=pick(process.env.INTER)||{};
        const plat=(pick(process.env.PLAT)||{}).platform||"none";
        const now=new Date().toISOString();
        for (const t of (route.needs_approval||[])) {
          n.push({kind:"csuite_approval_required", channel_hint:"#pitch-approvals",
            domain:t.domain, touch_number:t.touch_number,
            reason:t.approval_reason||"C-suite contact — follow-up requires rep approval.", created_at:now});
        }
        for (const t of (filt.approval||[])) {
          n.push({kind:"compliance_approval_required", channel_hint:"#pitch-approvals",
            domain:t.domain, touch_number:t.touch_number, compliance:t.compliance, created_at:now});
        }
        for (const b of (filt.blocked||[])) {
          n.push({kind:"compliance_blocked", channel_hint:"#pitch-pipeline",
            domain:b.domain, blocks:b.blocks, created_at:now});
        }
        for (const i of (inter.interrupts||[])) {
          n.push({kind:"sequence_interrupt", channel_hint:"#pitch-pipeline",
            domain:i.domain, touch_number:i.touch_number, interrupt_type:i.interrupt_type,
            reason:i.reason, created_at:now});
        }
        if (plat==="none" && ((route.autonomous_count||0)>0 || (filt.cleared_count||0)>0)) {
          n.push({kind:"no_outbound_platform", channel_hint:"#pitch-approvals",
            reason:"Due follow-ups are ready but no outbound email platform is connected. To send outreach, connect your email platform in settings. Supported: Instantly, SmartLead, Mailshake, Lemlist.", created_at:now});
        }
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      ROUTE: $route-csuite.stdout
      FILTER: $compliance-filter.stdout
      INTER: $interrupt-check.stdout
      PLAT: $outbound-platform.json

  - id: notify-save
    condition: $check-due.json.has_due_touches
    pipeline: state.set pitch/notifications
    stdin: $notify-next.stdout

  # ------------------------------------------------------------------
  # 11. Audit row (Req 38) appended to a single-key audit-log index array.
  #     Same has_due_touches gate keeps every step ref resolved.
  # ------------------------------------------------------------------
  - id: audit-load
    condition: $check-due.json.has_due_touches
    pipeline: state.get pitch/audit-log

  - id: audit-next
    condition: $check-due.json.has_due_touches
    command: >
      node -e 'const pick=s=>{try{return JSON.parse(s)}catch{return null}};
        const log=pick(process.env.LOG)||[];
        const bounce=pick(process.env.BOUNCE)||{};
        const inter=pick(process.env.INTER)||{};
        const route=pick(process.env.ROUTE)||{};
        const filt=pick(process.env.FILTER)||{};
        log.push({action:"sequence_execute", at:new Date().toISOString(),
          authorized_by:"autonomous",
          bounce_rate: bounce.bounce_rate!==undefined?bounce.bounce_rate:null,
          due_cleared: inter.cleared_count||0,
          interrupted: inter.interrupted_count||0,
          csuite_routed: route.approval_count||0,
          drafts_queued: filt.cleared_count||0,
          compliance_blocked: filt.blocked_count||0,
          compliance_approval: filt.approval_count||0});
        if (log.length>500) log.splice(0, log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-load.stdout
      BOUNCE: $bounce-check.stdout
      INTER: $interrupt-check.stdout
      ROUTE: $route-csuite.stdout
      FILTER: $compliance-filter.stdout

  - id: audit-save
    condition: $check-due.json.has_due_touches
    pipeline: state.set pitch/audit-log
    stdin: $audit-next.stdout
