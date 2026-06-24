name: reply-handler
# Process a prospect reply: detect opt-out, pause the sequence, load Attio MEDDIC
# state + conversation history, and hand the reply off to the rep for a drafted
# response (replies are conversations — never automated).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    Opt-out list, sequence-pause state, audit trail and notification intents are
#    single-key index arrays (state.* has no prefix-list), appended in a node step
#    then re-written.
#  - Attio is the ONLY CRM, reached gateway-free via the baked `mcporter` CLI; its
#    output is YAML-ish TEXT, so a node step parses stdout (never JSON.parse).
#  - Slack/Gmail are unreachable from an embedded pipeline (401), so the reply draft
#    + send is queued as a notification intent on `pitch/notifications` for the agent
#    to deliver. The actual reply DRAFT + MEDDIC extraction are model-judgment tasks,
#    so they are left for the agent loop (see the `reply_review` intent payload).
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  prospect_domain:
    required: true
  reply_content:
    required: true
  reply_subject:
    default: ""

steps:
  # 1. Opt-out detection FIRST (Req 42) — keyword match, deterministic, exits 0.
  - id: opt-out-input
    command: >
      node -e 'process.stdout.write(JSON.stringify({
        reply_text: process.env.REPLY||"",
        prospect_domain: process.env.LOBSTER_ARG_PROSPECT_DOMAIN||""}));'
    env:
      REPLY: $reply_content

  - id: opt-out-check
    command: node pitch-opt-out-detector.js
    stdin: $opt-out-input.stdout

  # 2. Record an opt-out into the durable opt-out list (single-key index array).
  - id: optout-load
    condition: $opt-out-check.json.is_opt_out
    pipeline: state.get pitch/opt-outs

  - id: optout-next
    condition: $opt-out-check.json.is_opt_out
    command: >
      node -e 'const list=JSON.parse(process.env.LIST||"null")||[];
        const domain=process.env.LOBSTER_ARG_PROSPECT_DOMAIN;
        list.push({domain, source:"reply_keyword_detection",
          matched_patterns:(JSON.parse(process.env.DET||"{}").matched_patterns)||[],
          jurisdiction:"pending_lookup", opt_out_date:new Date().toISOString()});
        process.stdout.write(JSON.stringify(list));'
    env:
      LIST: $optout-load.stdout
      DET: $opt-out-check.stdout

  - id: optout-save
    condition: $opt-out-check.json.is_opt_out
    pipeline: state.set pitch/opt-outs
    stdin: $optout-next.stdout

  # 3. Pause the active sequence (always — a reply, opt-out or not, stops automation).
  - id: pause-input
    command: >
      node -e 'process.stdout.write(JSON.stringify({
        prospect_domain: process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
        reason: (JSON.parse(process.env.DET||"{}").is_opt_out) ? "opt_out" : "prospect_reply"}));'
    env:
      DET: $opt-out-check.stdout

  - id: pause-sequence
    command: node pitch-sequence-pauser.js
    stdin: $pause-input.stdout

  # 4. Persist the sequence-pause into durable sequence state (single-key index array).
  - id: seq-load
    pipeline: state.get pitch/sequences

  - id: seq-next
    command: >
      node -e 'const seqs=JSON.parse(process.env.SEQS||"null")||[];
        const p=JSON.parse(process.env.PAUSE||"{}");
        const domain=p.prospect_domain;
        let found=false;
        for (const s of seqs) { if (s.domain===domain) {
          s.paused=true; s.pause_reason=p.pause_reason||p.reason||"prospect_reply";
          s.pause_timestamp=p.pause_timestamp||new Date().toISOString(); found=true; } }
        if (!found) seqs.push({domain, paused:true,
          pause_reason:p.pause_reason||p.reason||"prospect_reply",
          pause_timestamp:p.pause_timestamp||new Date().toISOString()});
        process.stdout.write(JSON.stringify(seqs));'
    env:
      SEQS: $seq-load.stdout
      PAUSE: $pause-sequence.stdout

  - id: seq-save
    pipeline: state.set pitch/sequences
    stdin: $seq-next.stdout

  # 5. NON-OPT-OUT PATH: load Attio MEDDIC state for this domain. mcporter -> text -> parse.
  - id: crm-meddic
    condition: $opt-out-check.json.is_opt_out == false
    command: >
      node -e 'const {execSync}=require("child_process");
        const d=process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"";
        let txt=""; try { txt=execSync("mcporter call attio.search-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",query:d,limit:5}))+" --output text",
          {encoding:"utf8",timeout:45000}); } catch (e) { txt=""; }
        let record_id="", name="", meddic="";
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const id=(p.match(/^([0-9a-f-]{36})/)||[])[1]||"";
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const nm=((p.match(/\n\s*name:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          const md=((p.match(/\n\s*meddic:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          if (!record_id || doms.includes(d.toLowerCase())) { record_id=id; name=nm; meddic=md; }
          if (doms.includes(d.toLowerCase())) break;
        }
        process.stdout.write(JSON.stringify({found:!!record_id, record_id, name, meddic}));'

  # 6. NON-OPT-OUT PATH: load durable conversation/audit history for this domain
  #    (single-key index array, filtered to this prospect by the consumer below).
  - id: history-load
    condition: $opt-out-check.json.is_opt_out == false
    pipeline: state.get pitch/audit-log

  # 7. NON-OPT-OUT PATH: queue a reply-review intent for the agent loop.
  #    The DRAFT reply and MEDDIC extraction are genuine model-judgment tasks
  #    (the original llm-task calls): they cannot run in the embedded pipeline,
  #    so the agent drafts the reply, extracts MEDDIC, and routes it to the rep
  #    for approval before sending from the rep's real inbox via gog.
  - id: notify-load
    condition: $opt-out-check.json.is_opt_out == false
    pipeline: state.get pitch/notifications

  - id: notify-next
    condition: $opt-out-check.json.is_opt_out == false
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const crm=JSON.parse(process.env.CRM||"{}");
        const hist=JSON.parse(process.env.HIST||"null")||[];
        const domain=process.env.LOBSTER_ARG_PROSPECT_DOMAIN;
        const history=hist.filter(h => (h && h.domain)===domain);
        n.push({kind:"reply_review",
          domain,
          reply_content: process.env.REPLY||"",
          reply_subject: process.env.SUBJECT||"",
          meddic_record_id: crm.record_id||"",
          meddic_found: !!crm.found,
          meddic_state: crm.meddic||"",
          conversation_history: history,
          requires: ["draft_reply","meddic_extract","rep_approval","send_via_gog","write_meddic_to_attio"],
          created_at: new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      CRM: $crm-meddic.stdout
      HIST: $history-load.stdout
      REPLY: $reply_content
      SUBJECT: $reply_subject

  - id: notify-save
    condition: $opt-out-check.json.is_opt_out == false
    pipeline: state.set pitch/notifications
    stdin: $notify-next.stdout

  # 8. Audit log (Req 38) — append to the durable audit index array (always runs).
  - id: audit-load
    pipeline: state.get pitch/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const det=JSON.parse(process.env.DET||"{}");
        log.push({action:"reply_handling",
          domain: process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
          prospect_reply: process.env.REPLY||"",
          reply_subject: process.env.SUBJECT||"",
          was_opt_out: !!det.is_opt_out,
          sequence_paused: true,
          authorized_by: det.is_opt_out ? "autonomous" : "rep_pending",
          timestamp: new Date().toISOString()});
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-load.stdout
      DET: $opt-out-check.stdout
      REPLY: $reply_content
      SUBJECT: $reply_subject

  - id: audit-save
    pipeline: state.set pitch/audit-log
    stdin: $audit-next.stdout
