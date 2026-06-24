name: opt-out-handler
# Requirement 42: Process opt-out requests within 10 business days automatically.
# Triggered by an outbound-platform unsubscribe webhook (Instantly/SmartLead/Mailshake/Lemlist).
# Fully deterministic (0 LLM).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    The opt-out list, audit log, and notification queue are single-key INDEX ARRAYS
#    (state.* has no prefix-list): state.get -> a node step appends -> state.set.
#  - CRM is Attio ONLY, reached gateway-free via the baked `mcporter` CLI inside a node
#    step. Output is YAML-ish TEXT, so a node step REGEX-parses stdout (never JSON.parse).
#    The prospect's company record is looked up by domain, then flagged do-not-contact
#    via a note (the original PATCH-by-domain-as-record-id was invalid — Attio record IDs
#    are UUIDs, not domains).
#  - Slack/cron are unreachable from an embedded pipeline (401), so the notification and
#    the 10-business-day verification reminder are written to `pitch/notifications` for
#    the agent loop to deliver / schedule.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  prospect_domain:
    required: true
  prospect_email:
    required: true
  source:
    default: "unsubscribe_webhook"

steps:
  # 1. Load the durable opt-out list (single-key index array) for idempotent dedup.
  - id: opt-out-list
    pipeline: state.get pitch/opt-outs

  # 2. Append this prospect to the opt-out list (skip if already present). Carry an
  #    explicit `already_present` flag so downstream steps stay idempotent.
  - id: opt-out-next
    command: >
      node -e 'const list=JSON.parse(process.env.LIST||"null")||[];
        const domain=(process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        const email=(process.env.LOBSTER_ARG_PROSPECT_EMAIL||"").toLowerCase();
        const source=process.env.LOBSTER_ARG_SOURCE||"unsubscribe_webhook";
        const now=new Date().toISOString();
        const already=list.some(o=>(o.domain||"").toLowerCase()===domain);
        if(!already){list.push({domain, email, source, opt_out_date:now, processed:true, processing_date:now});}
        process.stdout.write(JSON.stringify({list, already_present:already, domain, email, source, opt_out_date:now}));'
    env:
      LIST: $opt-out-list.json

  # 3. Persist the updated opt-out list.
  - id: persist-opt-out
    pipeline: state.set pitch/opt-outs
    stdin: $opt-out-next.json.list

  # 4. Pause any active sequence for this prospect. Assemble the pauser's stdin
  #    explicitly (it reads prospect_domain + reason from a JSON object).
  - id: pause-input
    command: >
      node -e 'process.stdout.write(JSON.stringify({prospect_domain:process.env.LOBSTER_ARG_PROSPECT_DOMAIN, reason:"opt_out"}));'

  - id: pause-sequence
    command: node pitch-sequence-pauser.js
    stdin: $pause-input.stdout

  # 5. Find the prospect's company record in Attio (CRM dedup source). mcporter -> text -> parse.
  - id: crm-search
    command: >
      node -e 'const {execSync}=require("child_process");
        const d=(process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        let txt=""; try { txt=execSync("mcporter call attio.search-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",query:d,limit:5}))+" --output text",
          {encoding:"utf8",timeout:45000}); } catch (e) { txt=""; }
        const records=[];
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const id=(p.match(/^([0-9a-f-]{36})/)||[])[1]||"";
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const name=((p.match(/\n\s*name:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          records.push({record_id:id, domain:doms[0]||"", domains:doms, name});
        }
        const match=records.find(r=>r.domains.includes(d))||records[0]||null;
        process.stdout.write(JSON.stringify({records, count:records.length, record_id:(match&&match.record_id)||"", matched:!!(match&&match.record_id)}));'

  # 6. Flag the matched company do-not-contact in Attio via a note (gateway-free mcporter).
  #    Only runs when a record was found — otherwise the agent logs the gap (no record to write).
  - id: crm-mark-opt-out
    condition: $crm-search.json.matched
    command: >
      node -e 'const {execSync}=require("child_process");
        const rid=process.env.RECORD_ID; const domain=process.env.LOBSTER_ARG_PROSPECT_DOMAIN;
        const email=process.env.LOBSTER_ARG_PROSPECT_EMAIL; const source=process.env.LOBSTER_ARG_SOURCE||"unsubscribe_webhook";
        const n={parent_object:"companies", parent_record_id:rid,
          title:"DO NOT CONTACT — opt-out processed",
          content:"Prospect "+email+" ("+domain+") opted out via "+source+" on "+new Date().toISOString()
            +". Sequences paused. Status set to do_not_contact. Re-engagement requires rep confirmation and an expired opt-out window."};
        let note="skipped";
        try { execSync("mcporter call attio.create-note --args "+JSON.stringify(JSON.stringify(n))+" --output text",{encoding:"utf8",timeout:45000}); note="created"; }
        catch(e){ note="ERR:"+e.message; }
        process.stdout.write(JSON.stringify({crm_updated: note==="created", record_id:rid, note}));'
    env:
      RECORD_ID: $crm-search.json.record_id

  # 7. Append an audit record to the durable audit log (single-key index array).
  - id: audit-list
    pipeline: state.get pitch/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        log.push({action:"opt_out_processed", domain:process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
          email:process.env.LOBSTER_ARG_PROSPECT_EMAIL, source:process.env.LOBSTER_ARG_SOURCE||"unsubscribe_webhook",
          sequence_paused:true, crm_matched: process.env.CRM_MATCHED==="true",
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-list.json
      CRM_MATCHED: $crm-search.json.matched

  - id: audit
    pipeline: state.set pitch/audit-log
    stdin: $audit-next.stdout

  # 8. Queue a notification + a 10-business-day verification reminder for the agent loop.
  #    (Slack + cron are unreachable from the embedded pipeline — write intents to state.)
  - id: notify-load
    pipeline: state.get pitch/notifications

  - id: notify-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const domain=process.env.LOBSTER_ARG_PROSPECT_DOMAIN;
        const email=process.env.LOBSTER_ARG_PROSPECT_EMAIL;
        const source=process.env.LOBSTER_ARG_SOURCE||"unsubscribe_webhook";
        const now=new Date().toISOString();
        n.push({kind:"opt_out_processed", domain, email, source,
          crm_matched: process.env.CRM_MATCHED==="true",
          summary:"Opt-out processed for "+domain+" ("+email+") via "+source
            +". Sequences paused; Attio "+(process.env.CRM_MATCHED==="true"?"flagged do-not-contact":"record not found — confirm manually")+".",
          created_at:now});
        n.push({kind:"reminder", verify_after:"10 business days", domain,
          summary:"Verify opt-out processing complete for "+domain
            +". Confirm pitch/opt-outs contains "+domain+" and the Attio company status is do_not_contact.",
          created_at:now});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.json
      CRM_MATCHED: $crm-search.json.matched

  - id: notify
    pipeline: state.set pitch/notifications
    stdin: $notify-next.stdout
