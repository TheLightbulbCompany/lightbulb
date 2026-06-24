name: visitor-alert
# Website-visitor identification — the highest-urgency Scout signal (Req 24).
# Triggered immediately by a Leadfeeder / Clearbit Reveal webhook when a company
# researches the user's product. Fast-path enrich -> compliance -> score (with the
# visitor boost) -> dossier -> deposit to Attio + the priority lead-queue, and queue
# an urgent agent-delivery intent. Fully deterministic (0 LLM in-pipeline).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    The priority lead-queue, in-flight Pitch sequences, the daily deposit counter,
#    the agent-delivery inbox, and the audit log are all single-key index arrays/maps
#    (state.* has no prefix-list); a node step reads, edits, and re-writes each.
#  - `fast-io` is fictional — never call it. `openclaw.invoke` (and so slack / apollo /
#    leadfeeder / llm-task as tools) 401s from the embedded runner — never call it.
#  - Apollo / Clearbit contact enrichment is gateway-bound (no creds in the embedded
#    runner), so the agent loop fetches it on the webhook and passes it in via the
#    `enrichment_results` arg; the waterfall merges whatever it is given and flags any
#    unfilled fields rather than guessing.
#  - Attio is the CRM, reached gateway-free via the baked `mcporter` CLI; its output is
#    YAML-ish TEXT, so a node step parses stdout (never JSON.parse).
#  - Slack is unreachable from an embedded pipeline (401), so the urgent alert is written
#    to a `scout/notifications` queue for the agent to deliver. NO openclaw.invoke / slack.
#  - Scripts/inline node always exit 0 and carry status in JSON; steps branch via condition.

args:
  visitor_domain:
    required: true
  page_visited:
    required: true
  visitor_data:
    default: "{}"           # raw webhook payload (company/contact fields the provider gave)
  enrichment_results:
    default: "{}"           # JSON: { database_results: [{source,type,data}], stakeholders: [], target_role }
                            # supplied by the agent loop after it runs Apollo/Clearbit (gateway-bound);
                            # defaults to empty -> the waterfall flags unfilled fields.

steps:
  # 1. Quick dedup — already in the priority queue or an in-flight Pitch sequence?
  #    Load both single-key index arrays; a node step decides by domain (carry the flag
  #    in JSON, exit 0 — never gate on .length).
  - id: queue
    pipeline: state.get scout/lead-queue

  # Reads Pitch's producer key (Scout + Pitch share one per-owner state store).
  - id: sequences
    pipeline: state.get pitch/sequences

  - id: dedup
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const seq=JSON.parse(process.env.SEQ||"null")||[];
        const d=(process.env.LOBSTER_ARG_VISITOR_DOMAIN||"").toLowerCase();
        const inQueue=q.some(x=>String((x.domain||x.company_domain||"")).toLowerCase()===d);
        const inSeq=seq.some(x=>String((x.domain||x.prospect_domain||"")).toLowerCase()===d);
        const exists=inQueue||inSeq;
        process.stdout.write(JSON.stringify({exists, in_queue:inQueue, in_sequence:inSeq,
          action: exists?"append_visitor_signal":"process", domain:d}));'
    env:
      QUEUE: $queue.stdout
      SEQ: $sequences.stdout

  # 2. Build the visitor signal. Deterministic page -> signal-type map; the source is
  #    `visitor_identification` so the ICP scorer applies the visitor boost (Req 24).
  - id: build-signal
    condition: $dedup.json.exists == false
    command: >
      node -e 'const pageMap={pricing:"website_visit_pricing",demo:"website_visit_demo",
          comparison:"website_visit_comparison","case-study":"website_visit_casestudy",
          contact:"website_visit_contact"};
        const page=process.env.LOBSTER_ARG_PAGE_VISITED||"unknown";
        const hit=Object.entries(pageMap).find(([k])=>page.toLowerCase().includes(k));
        const signalType=hit?hit[1]:"website_visit";
        const ts=new Date().toISOString();
        process.stdout.write(JSON.stringify({type:signalType, signal_type:signalType,
          source:"visitor_identification", signal_source:"visitor_identification",
          domain:(process.env.LOBSTER_ARG_VISITOR_DOMAIN||"").toLowerCase(),
          page_visited:page, timestamp:ts, date:ts.slice(0,10),
          summary:"Company visited the "+page+" page — highest-urgency self-selection signal."}));'

  # 3. Assemble the waterfall input from the agent-supplied enrichment (Apollo/Clearbit,
  #    gateway-bound) plus whatever firmographic fields the webhook payload carried. The
  #    waterfall merges with field-level source tracking and flags any unfilled fields.
  - id: waterfall-input
    condition: $dedup.json.exists == false
    command: >
      node -e 'const enr=JSON.parse(process.env.ENRICH||"null")||{};
        const vd=JSON.parse(process.env.VDATA||"null")||{};
        const domain=(process.env.LOBSTER_ARG_VISITOR_DOMAIN||"").toLowerCase();
        const dbResults=(enr.database_results||[]).slice();
        const webhookFields={};
        for (const k of ["company_name","company_size","industry","revenue_range",
          "funding_stage","title","seniority","linkedin_url","email","name","geography","country"]) {
          if (vd[k]!==undefined && vd[k]!==null && vd[k]!=="") webhookFields[k]=vd[k];
        }
        if (Object.keys(webhookFields).length>0)
          dbResults.push({source:"visitor_webhook",type:"webhook",data:webhookFields});
        process.stdout.write(JSON.stringify({
          lead:{domain},
          database_stack:[],
          target_role:enr.target_role||"",
          cached_profile:null,
          database_results:dbResults}));'
    env:
      ENRICH: $enrichment_results
      VDATA: $visitor_data

  - id: merge-enrichment
    condition: $dedup.json.exists == false
    command: node scout-enrichment-waterfall.js
    stdin: $waterfall-input.stdout

  # 4. Compliance — business email only, high-restriction region flagging.
  - id: compliance-input
    condition: $dedup.json.exists == false
    command: >
      node -e 'const m=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const p=m.enriched_profile||{};
        process.stdout.write(JSON.stringify({lead:{email:p.email||"",
          geography:p.geography||p.country||"",
          domain:p.company_domain||process.env.LOBSTER_ARG_VISITOR_DOMAIN}}));'
    stdin: $merge-enrichment.stdout

  - id: compliance-check
    condition: $dedup.json.exists == false
    command: node scout-compliance-check.js
    stdin: $compliance-input.stdout

  # 5. ICP scoring — deterministic weighted math. signal_source drives the visitor boost.
  - id: score-input
    condition: $dedup.json.exists == false
    command: >
      node -e 'const m=JSON.parse(process.env.MERGED||"{}");
        const sig=JSON.parse(process.env.SIGNAL||"{}");
        const p=m.enriched_profile||{};
        const lead={domain:(process.env.LOBSTER_ARG_VISITOR_DOMAIN||p.company_domain||"").toLowerCase(),
          company_size:p.company_size,funding_stage:p.funding_stage,
          geography:p.geography||p.country,industry:p.industry,
          tech_stack:p.tech_stack||[],title:p.title,
          signal_timestamp:sig.timestamp||sig.date,
          signal_source:"visitor_identification"};
        process.stdout.write(JSON.stringify({lead,icp:{},config:{}}));'
    env:
      MERGED: $merge-enrichment.stdout
      SIGNAL: $build-signal.stdout

  - id: icp-score
    condition: $dedup.json.exists == false
    command: node scout-icp-scorer.js
    stdin: $score-input.stdout

  # 6. Build the dossier from {lead, enrichment, scoring, signal, stakeholders}.
  - id: dossier-input
    condition: $dedup.json.exists == false
    command: >
      node -e 'const m=JSON.parse(process.env.MERGED||"{}");
        const sc=JSON.parse(process.env.SCORED||"{}");
        const sig=JSON.parse(process.env.SIGNAL||"{}");
        const enr=JSON.parse(process.env.ENRICH||"null")||{};
        const scored=(sc.scored||[])[0]||{};
        const p=m.enriched_profile||{};
        const lead={domain:(process.env.LOBSTER_ARG_VISITOR_DOMAIN||p.company_domain||"").toLowerCase(),
          name:p.name||null,company_name:p.company_name||null};
        const signal={type:sig.type,source:sig.source,date:sig.date||sig.timestamp,
          timestamp:sig.timestamp,summary:sig.summary,context:sig.summary};
        process.stdout.write(JSON.stringify({lead,enrichment:p,scoring:scored,signal,
          stakeholders:enr.stakeholders||[]}));'
    env:
      MERGED: $merge-enrichment.stdout
      SCORED: $icp-score.stdout
      SIGNAL: $build-signal.stdout
      ENRICH: $enrichment_results

  - id: build-dossier
    condition: $dedup.json.exists == false
    command: node scout-dossier-builder.js
    stdin: $dossier-input.stdout

  # NOTE: visitor signals always classify deterministically (website_visit_* are in the
  # dossier-builder's SIGNAL_ANGLE_MAP), so outreach_angle_needs_llm is false here — no
  # agent-loop LLM step is required for the visitor fast-path.

  # 7. Volume check — today's deposit count + the daily limiter (Req 54/64). Wrap the
  #    single visitor as leads[] for the limiter; emit deposited_count to gate on.
  - id: today
    condition: $dedup.json.exists == false
    pipeline: state.get scout/today-deposits

  - id: volume-input
    condition: $dedup.json.exists == false
    command: >
      node -e 'const today=(JSON.parse(process.env.TODAY||"null")||{}).count||0;
        const sc=JSON.parse(process.env.SCORED||"{}"); const scored=(sc.scored||[])[0]||{};
        process.stdout.write(JSON.stringify({leads:[{score:scored.score||0,
          domain:process.env.LOBSTER_ARG_VISITOR_DOMAIN}], today_deposits:today, daily_limit:50}));'
    env:
      TODAY: $today.stdout
      SCORED: $icp-score.stdout

  - id: volume
    condition: $dedup.json.exists == false
    command: node scout-volume-limiter.js
    stdin: $volume-input.stdout

  # 8. Deposit to Attio: upsert the company by domain, attach the visitor signal as a note.
  - id: deposit-attio
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const {execSync}=require("child_process");
        const m=JSON.parse(process.env.MERGED||"{}"); const p=m.enriched_profile||{};
        const sc=JSON.parse(process.env.SCORED||"{}"); const scored=(sc.scored||[])[0]||{};
        const sig=JSON.parse(process.env.SIGNAL||"{}");
        const domain=process.env.LOBSTER_ARG_VISITOR_DOMAIN;
        const up={object:"companies", matching_attribute:"domains",
          values:{domains:[domain], name:p.company_name||domain}};
        let rec=""; try { rec=execSync("mcporter call attio.upsert-record --args "
          +JSON.stringify(JSON.stringify(up))+" --output text",{encoding:"utf8",timeout:45000}); } catch(e){ rec="ERR:"+e.message; }
        const rid=(rec.match(/record_id:\s*([0-9a-f-]{36})/)||[])[1]||"";
        let note="skipped";
        if (rid) { const n={parent_object:"companies", parent_record_id:rid,
            title:"Website visitor: "+(sig.page_visited||"site"),
            content:"Visited "+(sig.page_visited||"the site")+" — highest-urgency signal. ICP score "
              +(scored.score||0)+" ("+(scored.tier||"")+", visitor boost "+(scored.visitor_boost||0)+")."};
          try { execSync("mcporter call attio.create-note --args "+JSON.stringify(JSON.stringify(n))+" --output text",{encoding:"utf8",timeout:45000}); note="created"; } catch(e){ note="ERR:"+e.message; } }
        process.stdout.write(JSON.stringify({deposited:!!rid, record_id:rid, note}));'
    env:
      MERGED: $merge-enrichment.stdout
      SCORED: $icp-score.stdout
      SIGNAL: $build-signal.stdout

  # 9. Enqueue the visitor into the priority lead-queue (append to the single-key array).
  - id: queue-next
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const dos=JSON.parse(process.env.DOSSIER||"{}");
        const sc=JSON.parse(process.env.SCORED||"{}"); const scored=(sc.scored||[])[0]||{};
        q.push({domain:(process.env.LOBSTER_ARG_VISITOR_DOMAIN||"").toLowerCase(),
          score:scored.score||0, tier:scored.tier||"priority", priority:true,
          signal:"visitor_identification", page_visited:process.env.LOBSTER_ARG_PAGE_VISITED,
          dossier:dos, queued_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: $queue.stdout
      DOSSIER: $build-dossier.stdout
      SCORED: $icp-score.stdout

  - id: enqueue
    condition: $volume.json.deposited_count > 0
    pipeline: state.set scout/lead-queue
    stdin: $queue-next.stdout

  # 10. Update the daily deposit counter (single-key map {count,date}).
  - id: counter-next
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({count:v.today_total||0,
          date:new Date().toISOString().slice(0,10)}));'
    stdin: $volume.stdout

  - id: counter
    condition: $volume.json.deposited_count > 0
    pipeline: state.set scout/today-deposits
    stdin: $counter-next.stdout

  # 11. Queue the urgent alert for the agent to deliver (Slack is unreachable here).
  - id: notify-load
    condition: $volume.json.deposited_count > 0
    pipeline: state.get scout/notifications

  - id: notify-next
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const m=JSON.parse(process.env.MERGED||"{}"); const p=m.enriched_profile||{};
        const sc=JSON.parse(process.env.SCORED||"{}"); const scored=(sc.scored||[])[0]||{};
        const da=JSON.parse(process.env.DEPOSIT||"{}");
        n.push({kind:"website_visitor", urgent:true, deliver_to:"#scout-alerts",
          domain:(process.env.LOBSTER_ARG_VISITOR_DOMAIN||"").toLowerCase(),
          page_visited:process.env.LOBSTER_ARG_PAGE_VISITED,
          score:scored.score||0, tier:scored.tier||"priority", visitor_boost:scored.visitor_boost||0,
          company:p.company_name||"", contact:(p.title||"")+(p.name?(" — "+p.name):""),
          attio_record_id:da.record_id||"",
          summary:"Website visitor self-selected — work immediately.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      MERGED: $merge-enrichment.stdout
      SCORED: $icp-score.stdout
      DEPOSIT: $deposit-attio.stdout

  - id: notify
    condition: $volume.json.deposited_count > 0
    pipeline: state.set scout/notifications
    stdin: $notify-next.stdout

  # 12. Audit log — append the run record to the durable audit index (single key).
  - id: audit-load
    condition: $dedup.json.exists == false
    pipeline: state.get scout/audit-log

  - id: audit-next
    condition: $dedup.json.exists == false
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const sc=JSON.parse(process.env.SCORED||"{}"); const scored=(sc.scored||[])[0]||{};
        const vol=JSON.parse(process.env.VOL||"{}");
        const da=JSON.parse(process.env.DEPOSIT||"null");
        log.push({action:"visitor_alert",
          domain:(process.env.LOBSTER_ARG_VISITOR_DOMAIN||"").toLowerCase(),
          page:process.env.LOBSTER_ARG_PAGE_VISITED,
          score:scored.score||0, tier:scored.tier||"",
          deposited:(vol.deposited_count||0)>0,
          attio_record_id:(da&&da.record_id)||"",
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log.slice(-1000)));'
    env:
      LOG: $audit-load.stdout
      SCORED: $icp-score.stdout
      VOL: $volume.stdout
      DEPOSIT: $deposit-attio.stdout

  - id: audit-log
    condition: $dedup.json.exists == false
    pipeline: state.set scout/audit-log
    stdin: $audit-next.stdout
