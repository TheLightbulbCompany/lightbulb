name: draft-and-approve
# First-touch outreach: run the deterministic compliance hard-stops for a qualified
# prospect, then hand the genuine model-judgment work (draft generation, content scan,
# approval gate, send) to the agent loop as a queued intent — keeping a human rep in the
# loop on every first touch. Loads the post-enrichment research brief + voice model +
# connected-platform config, deduces hard blocks, and records the outcome durably.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    The brief store + platform config are single-key maps keyed by domain; the
#    notification queue + audit log are single-key index arrays (state.* has no
#    prefix-list), appended in a node step then re-written.
#  - The first-touch DRAFT is genuine model judgment (the original llm-task call,
#    thinking:"medium") — it cannot run in the embedded pipeline (openclaw.invoke -> 401).
#    So the deterministic compliance gate runs here, and a `draft_and_approve` intent is
#    queued on `pitch/notifications` carrying everything the agent loop needs to: draft
#    the email, run pitch-content-scanner.js on the draft, present it for rep APPROVAL
#    (#pitch-approvals), and send via the connected outbound platform under the rep's name.
#  - Slack/outbound APIs are unreachable from an embedded pipeline (401 / need the live
#    draft + API keys), so the approval gate + send live in the agent loop, NOT here.
#  - Attio is the ONLY CRM, reached gateway-free via the baked `mcporter` CLI; its output
#    is YAML-ish TEXT, so a node step parses stdout (never JSON.parse). The actual CRM
#    activity log is written post-send by the agent loop (it needs the sent draft).
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  prospect_domain:
    required: true

steps:
  # ---------------------------------------------------------------------------
  # 1. Load the post-enrichment research brief. prospect-enrich.lobster persists briefs
  #    into a single-key map `pitch/briefs` keyed by domain (state.* can't slash-namespace),
  #    each entry being {brief, icp_score, qualified, enrichment_confidence, tier, ...}.
  # ---------------------------------------------------------------------------
  - id: briefs-load
    pipeline: state.get pitch/briefs

  # ---------------------------------------------------------------------------
  # 2. Load the voice model from Google Drive (gog, real CLI, gateway-free). Best-effort:
  #    if it isn't present the agent loop drafts from the brief alone. Exits 0 always.
  # ---------------------------------------------------------------------------
  - id: voice-load
    command: >
      gog drive read --file pitch-voice-model.json --json 2>/dev/null || echo '{}'

  # ---------------------------------------------------------------------------
  # 3. Load the connected outbound + CRM platform config (single-key maps in durable
  #    state). The agent loop reads platform names to pick the send path / CRM activity
  #    write; "no platform connected" is surfaced to the rep as a click-to-connect prompt.
  # ---------------------------------------------------------------------------
  - id: outbound-load
    pipeline: state.get pitch/connected-outbound

  - id: crm-load
    pipeline: state.get pitch/connected-crm

  # ---------------------------------------------------------------------------
  # 4. Pull this prospect's brief entry out of the map and shape the compliance input.
  #    pitch-compliance-check.js reads a single stdin object {prospect, config} and runs
  #    six independent hard-stop checks (verification score, GDPR/TCPA consent, 5-touch
  #    max, opt-out, explicit decline, current customer). The prospect fields come off the
  #    brief; the config carries the verification + opt-out-window thresholds. It exits 0
  #    always (carrying pass/blocks in JSON) once the .js fix below lands.
  # ---------------------------------------------------------------------------
  - id: compliance-input
    command: >
      node -e 'const store=JSON.parse(process.env.BRIEFS||"null")||{};
        const domain=(process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        const entry=store[domain]||{}; const b=entry.brief||{};
        const sig=b.trigger_signal||{};
        const prospect={
          domain,
          title:b.prospect_role||b.title||"",
          email:b.prospect_email||"",
          verification_score:(typeof b.verification_score==="number")?b.verification_score:
            (typeof b.enrichment_confidence==="number")?b.enrichment_confidence:
            (typeof entry.enrichment_confidence==="number")?entry.enrichment_confidence:undefined,
          jurisdiction:b.jurisdiction||"",
          gdpr_consent:b.gdpr_consent||null,
          tcpa_opt_in:b.tcpa_opt_in||null,
          touch_channel:"email",
          touch_count:(typeof b.touch_count==="number")?b.touch_count:0,
          opted_out:!!b.opted_out, opt_out_date:b.opt_out_date||null,
          explicitly_declined:!!b.explicitly_declined,
          is_current_customer:!!b.is_current_customer,
          customer_outreach_authorized:!!b.customer_outreach_authorized};
        const config={min_verification_score:0.7, opt_out_window_days:30};
        process.stdout.write(JSON.stringify({prospect, config, has_brief:!!store[domain],
          icp_score:entry.icp_score!=null?entry.icp_score:null,
          enrichment_confidence:entry.enrichment_confidence!=null?entry.enrichment_confidence:null,
          signal_context:sig.context||sig.summary||""}));'
    env:
      BRIEFS: $briefs-load.stdout

  # 4b. Surface has_brief / score explicitly so downstream steps can gate on a real count
  #     (never gate on .length). 1 when a brief exists for this domain, else 0.
  - id: brief-gate
    command: >
      node -e 'const c=JSON.parse(require("fs").readFileSync(0,"utf8"))||{};
        process.stdout.write(JSON.stringify({has_brief:c.has_brief?1:0,
          icp_score:c.icp_score, enrichment_confidence:c.enrichment_confidence,
          signal_context:c.signal_context||""}));'
    stdin: $compliance-input.stdout

  # ---------------------------------------------------------------------------
  # 5. Compliance hard-stop check (Req 4, 6, 7, 26, 30, 41). Deterministic, zero LLM.
  #    Runs UNCONDITIONALLY so the always-run audit step never references a skipped
  #    producer (a skipped-step ref is unsafe — see reference.lobster). For a
  #    missing-brief domain the prospect is mostly-empty, which the script handles
  #    cleanly; the no-brief branch below is what actually short-circuits the draft.
  #    Exits 0 always (post-fix) and carries {pass, blocks, requires_approval,
  #    approval_needed, is_csuite} in JSON.
  # ---------------------------------------------------------------------------
  - id: compliance-check
    command: node pitch-compliance-check.js
    stdin: $compliance-input.stdout

  # ---------------------------------------------------------------------------
  # 6. PASS PATH — queue a `draft_and_approve` intent for the agent loop. The draft
  #    generation (thinking:"medium"), the pitch-content-scanner.js pass, the rep
  #    approval gate (#pitch-approvals), and the send via the connected outbound platform
  #    are all genuine model-judgment / live-credential / human-in-the-loop work that the
  #    embedded pipeline cannot do — so they are handed off here. C-suite first touches
  #    (is_csuite) and competitor/pricing/commitment flags route to mandatory rep approval
  #    per AGENTS.md autonomy boundaries; the intent carries that signal.
  #    Load-before-use on the notifications array, append, re-write.
  # ---------------------------------------------------------------------------
  - id: draft-notify-load
    condition: $brief-gate.json.has_brief > 0 && $compliance-check.json.pass
    pipeline: state.get pitch/notifications

  - id: draft-notify-next
    condition: $brief-gate.json.has_brief > 0 && $compliance-check.json.pass
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const store=JSON.parse(process.env.BRIEFS||"null")||{};
        const domain=(process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        const entry=store[domain]||{}; const b=entry.brief||{};
        const comp=JSON.parse(process.env.COMP||"{}");
        const voice=JSON.parse(process.env.VOICE||"{}");
        const outbound=JSON.parse(process.env.OUTBOUND||"null")||{};
        const crm=JSON.parse(process.env.CRMCFG||"null")||{};
        const sig=b.trigger_signal||{};
        n.push({kind:"draft_and_approve", deliver_to:"#pitch-approvals",
          urgent: comp.is_csuite===true || (entry.icp_score!=null && entry.icp_score>=90),
          domain,
          channel:"email",
          brief:b,
          icp_score:entry.icp_score!=null?entry.icp_score:null,
          enrichment_confidence:entry.enrichment_confidence!=null?entry.enrichment_confidence:null,
          signal_context:sig.context||sig.summary||"",
          voice_model_present: voice && Object.keys(voice).length>0,
          is_csuite: comp.is_csuite===true,
          requires_approval: comp.is_csuite===true,
          compliance_approval_needed: comp.requires_approval===true,
          compliance_approval_reasons:(comp.approval_needed||[]).map(a=>a.reason),
          outbound_platform: outbound.platform||null,
          crm_platform: crm.platform||null,
          requires:["draft_first_touch","content_scan","rep_approval","send_via_outbound","crm_activity_log","audit_send"],
          note:"DRAFT for rep review — first touch is never sent autonomously. Run pitch-content-scanner.js on the draft (with competitor_names + signal_terms config); reject_and_regenerate on missing signal grounding or non-response language; route_to_approval on competitor/pricing/commitment/timeline flags. Send under the rep name only after approval.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $draft-notify-load.stdout
      BRIEFS: $briefs-load.stdout
      COMP: $compliance-check.stdout
      VOICE: $voice-load.stdout
      OUTBOUND: $outbound-load.stdout
      CRMCFG: $crm-load.stdout

  - id: draft-notify-save
    condition: $brief-gate.json.has_brief > 0 && $compliance-check.json.pass
    pipeline: state.set pitch/notifications
    stdin: $draft-notify-next.stdout

  # ---------------------------------------------------------------------------
  # 7. BLOCK PATH — compliance hard-stopped this prospect (opt-out, decline, 5-touch max,
  #    current customer, missing consent/verification). Queue a `compliance_block` intent
  #    so the rep can see why no draft was produced. No outreach, no draft. Slack 401 ->
  #    state queue. Load-before-use, append, re-write.
  # ---------------------------------------------------------------------------
  - id: block-notify-load
    condition: "$brief-gate.json.has_brief > 0 && !$compliance-check.json.pass"
    pipeline: state.get pitch/notifications

  - id: block-notify-next
    condition: "$brief-gate.json.has_brief > 0 && !$compliance-check.json.pass"
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const comp=JSON.parse(process.env.COMP||"{}");
        const domain=process.env.LOBSTER_ARG_PROSPECT_DOMAIN;
        const blocks=(comp.blocks||[]).map(b=>({check:b.check, reason:b.reason, details:b.details}));
        n.push({kind:"compliance_block", deliver_to:"#pitch-approvals", urgent:false,
          domain, blocks,
          summary:"First touch to "+domain+" blocked by compliance: "
            +blocks.map(b=>b.reason).join(", ")+". No draft produced.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $block-notify-load.stdout
      COMP: $compliance-check.stdout

  - id: block-notify-save
    condition: "$brief-gate.json.has_brief > 0 && !$compliance-check.json.pass"
    pipeline: state.set pitch/notifications
    stdin: $block-notify-next.stdout

  # ---------------------------------------------------------------------------
  # 8. MISSING-BRIEF PATH — no post-enrichment brief exists for this domain. Run
  #    prospect-enrich.lobster first. Queue a notice for the rep. Load-before-use.
  # ---------------------------------------------------------------------------
  - id: nobrief-load
    condition: $brief-gate.json.has_brief == 0
    pipeline: state.get pitch/notifications

  - id: nobrief-next
    condition: $brief-gate.json.has_brief == 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const domain=process.env.LOBSTER_ARG_PROSPECT_DOMAIN;
        n.push({kind:"no_brief", deliver_to:"#pitch-approvals", urgent:false, domain,
          summary:"No research brief found for "+domain+". Run prospect-enrich first.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $nobrief-load.stdout

  - id: nobrief-save
    condition: $brief-gate.json.has_brief == 0
    pipeline: state.set pitch/notifications
    stdin: $nobrief-next.stdout

  # ---------------------------------------------------------------------------
  # 9. Audit log (Req 38) — append a deterministic record to the single-key audit array.
  #    Always runs (drafted, blocked, or no-brief). Load-before-use, append, cap, write.
  #    The post-send draft/CRM-activity record is written separately by the agent loop.
  # ---------------------------------------------------------------------------
  - id: audit-load
    pipeline: state.get pitch/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const gate=JSON.parse(process.env.GATE||"{}");
        const comp=JSON.parse(process.env.COMP||"null");
        const has=gate.has_brief>0;
        const outcome=!has?"no_brief":(comp&&comp.pass?"queued_for_draft":"compliance_blocked");
        log.push({action:"first_touch_draft",
          domain:process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
          outcome,
          icp_score:gate.icp_score!=null?gate.icp_score:null,
          enrichment_confidence:gate.enrichment_confidence!=null?gate.enrichment_confidence:null,
          compliance_pass: comp?!!comp.pass:null,
          compliance_blocks:(comp&&comp.blocks||[]).map(b=>b.reason),
          is_csuite: comp?comp.is_csuite===true:null,
          approved:false, authorized_by:"rep_pending",
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      GATE: $brief-gate.stdout
      COMP: $compliance-check.stdout

  - id: audit-save
    pipeline: state.set pitch/audit-log
    stdin: $audit-next.stdout
