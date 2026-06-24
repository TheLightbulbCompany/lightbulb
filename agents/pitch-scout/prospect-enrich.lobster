name: prospect-enrich
# Pitch-mode prospect enrichment (Reqs 12-17, 38). Triggered per prospect after a
# signal qualifies them (or after a Scout deposit). Builds a research brief, gates on
# enrichment confidence, ICP-scores deterministically, persists the brief, syncs Attio,
# and queues a quarantine/notify intent + audit record for the agent loop.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    The ICP config, brief store, notifications, and audit log are single-key objects/
#    arrays (state.* has no prefix-list); node steps read, edit, and re-write them.
#  - `fast-io` is fictional — never call it. `openclaw.invoke` (and so slack/apollo/
#    agent-browser/llm-task as tools) 401s from the embedded runner — never call it.
#  - CRM = Attio ONLY (HubSpot/Salesforce/Pipedrive dropped). Reached gateway-free via
#    the baked `mcporter` CLI; output is YAML-ish TEXT, so a node step regex-parses
#    stdout (never JSON.parse).
#  - Perplexity is a real direct-curl API ($PERPLEXITY_API_KEY at the process env) —
#    deterministic structured response, NOT an llm-task call.
#  - Apollo + LinkedIn/web enrichment are gateway-bound (no creds in the embedded
#    runner), so the agent loop fetches those and passes them in via the
#    `enrichment_results` arg. The brief-compilation LLM call is genuine model judgment,
#    so the agent loop compiles the brief (thinking:"low") and passes it in via the
#    optional `brief` arg; when absent this pipeline assembles a deterministic skeleton
#    brief from enrichment_results so confidence/scoring still run (gaps left flagged,
#    never fabricated).
#  - Quarantine alerts (Slack unreachable, 401) are written to a `pitch/notifications`
#    queue for the agent to deliver. Audit records append to a `pitch/audit-log` array.
#  - Scripts/inline node always exit 0 and carry status in JSON; steps branch via condition.

args:
  prospect_domain:
    required: true
  signal_data:
    required: true          # the trigger signal (JSON object)
  enrichment_results:
    required: false         # JSON supplied by the agent loop after it runs Apollo/LinkedIn
                            # (gateway-bound): { apollo:{...}, linkedin:{...}, fields:{...},
                            # verification_flags:[{field,source_tier}] }. Defaults to empty
                            # -> the skeleton brief flags unfilled fields as unverified.
  brief:
    required: false         # JSON research brief pre-compiled by the agent loop
                            # (llm-task, thinking:"low"). When absent, a deterministic
                            # skeleton brief is built from enrichment_results below.

steps:
  # ---------------------------------------------------------------------------
  # 1. Recent-news signal via Perplexity (real direct-curl API; deterministic). Best
  #    effort — on_error:continue so a Perplexity outage never kills enrichment. The
  #    node wrapper carries any failure as empty content (exit 0).
  # ---------------------------------------------------------------------------
  - id: recent-news
    on_error: continue
    command: >
      node -e 'const {execSync}=require("child_process");
        const d=process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"";
        const key=process.env.PERPLEXITY_API_KEY||"";
        let content="", citations=[];
        if (key) { try {
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",
            content:"Recent news, funding, leadership changes, and hiring for "+d+" in the last 30 days. Cite sources."}]});
          const raw=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer \"$PERPLEXITY_API_KEY "
            +"-H \"Content-Type: application/json\" --data-binary @-",
            {input:body, encoding:"utf8", timeout:60000, env:process.env});
          const j=JSON.parse(raw);
          content=(j.choices&&j.choices[0]&&j.choices[0].message&&j.choices[0].message.content)||"";
          citations=j.citations||[];
        } catch(e){ content=""; citations=[]; } }
        process.stdout.write(JSON.stringify({content, citations, total:citations.length, available:!!key}));'

  # ---------------------------------------------------------------------------
  # 2. CRM history check (Req 13) — Attio company + recent notes for this domain.
  #    mcporter -> YAML-ish TEXT -> regex parse (never JSON.parse). Best-effort: if the
  #    CRM isn't connected or returns nothing, crm_history is an empty array and the
  #    enrichment continues.
  # ---------------------------------------------------------------------------
  - id: crm-history
    command: >
      node -e 'const {execSync}=require("child_process");
        const d=process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"";
        let txt=""; try { txt=execSync("mcporter call attio.search-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",query:d,limit:5}))+" --output text",
          {encoding:"utf8",timeout:45000}); } catch(e){ txt=""; }
        let record_id="", name="", history=[];
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const id=(p.match(/^([0-9a-f-]{36})/)||[])[1]||"";
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const nm=((p.match(/\n\s*name:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          const stage=((p.match(/\n\s*(?:status|stage|hs_lead_status):\s*"?([^"\n]+)"?/)||[])[1]||"").trim().toLowerCase().replace(/[\s-]+/g,"_");
          const lastAct=((p.match(/\n\s*(?:last_activity_date|notes_last_updated|last_interaction):\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          if (doms.includes(d.toLowerCase()) || (!record_id && doms[0])) {
            record_id=id; name=nm;
            if (lastAct||stage) history.push({date:lastAct||"", type:"crm_record", outcome:stage||"contact", contact:nm});
          }
        }
        process.stdout.write(JSON.stringify({record_id, name, crm_history:history, found:!!record_id}));'

  # ---------------------------------------------------------------------------
  # 3. Compile the research brief. The brief-compilation LLM call is genuine model
  #    judgment, so the agent loop supplies it via the optional `brief` arg. When absent
  #    this builds a DETERMINISTIC SKELETON brief from the agent-supplied enrichment_results
  #    + Perplexity news + Attio CRM history, populating verification_flags from which
  #    source filled each field. Missing fields stay null/unverified — never fabricated.
  # ---------------------------------------------------------------------------
  - id: compile-brief
    command: >
      node -e 'const supplied=JSON.parse(process.env.BRIEF||"null");
        const enr=JSON.parse(process.env.ENRICH||"null")||{};
        const sig=JSON.parse(process.env.SIGNAL||"{}");
        const news=JSON.parse(process.env.NEWS||"{}");
        const crm=JSON.parse(process.env.CRM||"{}");
        const domain=(process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        if (supplied && typeof supplied==="object") {
          const b={...supplied}; b.domain=b.domain||domain;
          if (!b.crm_history && (crm.crm_history||[]).length) b.crm_history=crm.crm_history;
          if (!Array.isArray(b.verification_flags)) b.verification_flags=[];
          process.stdout.write(JSON.stringify({brief:b, mode:"agent_compiled",
            verification_flags:b.verification_flags}));
          return;
        }
        const f=enr.fields||{}; const vf=enr.verification_flags||[];
        const recent=news.content?[{title:"Recent activity", summary:String(news.content).slice(0,600),
          date:new Date().toISOString().slice(0,10)}]:null;
        const brief={
          domain,
          company_size:(typeof f.company_size==="number")?f.company_size:null,
          funding_stage:f.funding_stage||null,
          estimated_revenue:f.estimated_revenue||null,
          tech_stack:Array.isArray(f.tech_stack)?f.tech_stack:null,
          trigger_signal:{type:sig.type||sig.signal_type||"unknown",
            context:sig.context||sig.summary||"", qualification_reason:sig.qualification_reason||""},
          prospect_role:f.prospect_role||f.title||null,
          prospect_tenure:f.prospect_tenure||null,
          recent_public_content:recent,
          crm_history:(crm.crm_history||[]).length?crm.crm_history:null,
          competitive_context:f.competitive_context||null,
          verification_flags:vf};
        process.stdout.write(JSON.stringify({brief, mode:"skeleton", verification_flags:vf}));'
    env:
      BRIEF: $brief
      ENRICH: $enrichment_results
      SIGNAL: $signal_data
      NEWS: $recent-news.stdout
      CRM: $crm-history.stdout

  # ---------------------------------------------------------------------------
  # 4. Enrichment confidence check (Req 15). pitch-enrichment-confidence.js needs a single
  #    stdin object {brief, verification_flags, threshold, domain}. It exits 0 always and
  #    carries the pass flag + confidence + remediation in JSON; gate the steps below on it.
  # ---------------------------------------------------------------------------
  - id: confidence-input
    command: >
      node -e 'const c=JSON.parse(require("fs").readFileSync(0,"utf8"))||{};
        const b=c.brief||{};
        process.stdout.write(JSON.stringify({brief:b, verification_flags:c.verification_flags||b.verification_flags||[],
          domain:b.domain||process.env.LOBSTER_ARG_PROSPECT_DOMAIN}));'
    stdin: $compile-brief.stdout

  - id: confidence-check
    command: node pitch-enrichment-confidence.js
    stdin: $confidence-input.stdout

  # ---------------------------------------------------------------------------
  # 5. ICP scoring (Req 16). pitch-icp-router.js needs {prospect, icp_criteria,
  #    enrichment_data, threshold} and exits 0 (it scores + routes). Build that input
  #    from the brief fields + the ICP config in durable state. The scorer runs
  #    UNCONDITIONALLY so the always-run brief-store/audit steps never reference a skipped
  #    producer; for a confidence-failed prospect the input carries scored:false so the
  #    router scores 0 and routes to archived (the quarantine branch is what actually
  #    routes it). icp-input embeds the confidence pass flag for the agent's read.
  # ---------------------------------------------------------------------------
  - id: icp-load
    pipeline: state.get pitch/icp

  - id: icp-input
    command: >
      node -e 'const c=JSON.parse(process.env.COMPILED||"{}"); const b=c.brief||{};
        const icp=JSON.parse(process.env.ICP||"null")||{};
        const conf=JSON.parse(process.env.CONF||"{}");
        const domain=(b.domain||process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        const enrichment_data={company_size:b.company_size, funding_stage:b.funding_stage,
          geography:b.geography||null, industry:b.industry||null, tech_stack:b.tech_stack||[]};
        process.stdout.write(JSON.stringify({prospect:{domain}, icp_criteria:icp,
          enrichment_data, threshold:(typeof icp.threshold==="number"?icp.threshold:60),
          confidence_passed:conf.pass!==false}));'
    env:
      COMPILED: $compile-brief.stdout
      ICP: $icp-load.stdout
      CONF: $confidence-check.stdout

  - id: icp-score
    command: node pitch-icp-router.js
    stdin: $icp-input.stdout

  # ---------------------------------------------------------------------------
  # 6. Persist the brief (Req 38). The brief store is a single-key map keyed by domain
  #    (state.* can't slash-namespace). The router's `destination` (briefs/active vs
  #    briefs/archived) and the confidence-check's destination (briefs/quarantine) are
  #    recorded on the entry so the agent can route reads. Load-before-use, then re-write.
  # ---------------------------------------------------------------------------
  - id: brief-store
    pipeline: state.get pitch/briefs

  - id: brief-store-next
    command: >
      node -e 'const store=JSON.parse(process.env.STORE||"null")||{};
        const c=JSON.parse(process.env.COMPILED||"{}"); const b=c.brief||{};
        const conf=JSON.parse(process.env.CONF||"{}");
        const icp=JSON.parse(process.env.ICP||"null");
        const domain=(b.domain||process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        const tier=!conf.pass?"quarantine":((icp&&icp.qualified)?"active":"archived");
        store[domain]={brief:b,
          icp_score:icp?icp.score:null, qualified:icp?!!icp.qualified:false,
          breakdown:icp?icp.breakdown:null, enrichment_confidence:conf.confidence,
          tier, destination:!conf.pass?conf.destination:(icp?icp.destination:null),
          enriched_at:new Date().toISOString()};
        process.stdout.write(JSON.stringify(store));'
    env:
      STORE: $brief-store.stdout
      COMPILED: $compile-brief.stdout
      CONF: $confidence-check.stdout
      ICP: $icp-score.stdout

  - id: persist-brief
    pipeline: state.set pitch/briefs
    stdin: $brief-store-next.stdout

  # ---------------------------------------------------------------------------
  # 7. CRM sync (Req 17). Upsert the Attio company by domain with the ICP score + signal
  #    type as a note. Runs UNCONDITIONALLY (so the always-run audit step never references
  #    a skipped producer) but NO-OPS internally for confidence-failed prospects — only
  #    confidence-cleared prospects get written to the CRM (quarantine never touches it).
  #    mcporter, gateway-free, YAML-ish text parse.
  # ---------------------------------------------------------------------------
  - id: crm-sync
    command: >
      node -e 'const {execSync}=require("child_process");
        const c=JSON.parse(process.env.COMPILED||"{}"); const b=c.brief||{};
        const icp=JSON.parse(process.env.ICP||"{}"); const conf=JSON.parse(process.env.CONF||"{}");
        if (conf.pass===false) { process.stdout.write(JSON.stringify({synced:false, skipped:"low_confidence"})); return; }
        const domain=(b.domain||process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        const sig=(b.trigger_signal||{});
        const up={object:"companies", matching_attribute:"domains",
          values:{domains:[domain], name:b.company_name||domain}};
        let rec=""; try { rec=execSync("mcporter call attio.upsert-record --args "
          +JSON.stringify(JSON.stringify(up))+" --output text",{encoding:"utf8",timeout:45000}); } catch(e){ rec="ERR:"+e.message; }
        const rid=(rec.match(/record_id:\s*([0-9a-f-]{36})/)||[])[1]||"";
        let note="skipped";
        if (rid) { const n={parent_object:"companies", parent_record_id:rid,
            title:"Pitch enrichment: "+(sig.type||"signal"),
            content:"ICP score "+(icp.score!=null?icp.score:"n/a")+" (qualified="+(!!icp.qualified)
              +", confidence="+(conf.confidence!=null?conf.confidence:"n/a")+"). "+(sig.context||"")};
          try { execSync("mcporter call attio.create-note --args "+JSON.stringify(JSON.stringify(n))+" --output text",{encoding:"utf8",timeout:45000}); note="created"; } catch(e){ note="ERR:"+e.message; } }
        process.stdout.write(JSON.stringify({synced:!!rid, record_id:rid, note}));'
    env:
      COMPILED: $compile-brief.stdout
      ICP: $icp-score.stdout
      CONF: $confidence-check.stdout

  # ---------------------------------------------------------------------------
  # 8. Quarantine notification (Req 15). Confidence below threshold -> queue an intent for
  #    the agent loop to deliver to #pitch-quarantine (Slack is unreachable here, 401).
  #    Load-before-use on the notifications array, append, re-write.
  # ---------------------------------------------------------------------------
  - id: quarantine-load
    condition: $confidence-check.json.pass == false
    pipeline: state.get pitch/notifications

  - id: quarantine-next
    condition: $confidence-check.json.pass == false
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const conf=JSON.parse(process.env.CONF||"{}");
        const rem=conf.remediation||{};
        n.push({kind:"quarantine", urgent:false, deliver_to:"#pitch-quarantine",
          domain:process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
          confidence:conf.confidence, threshold:conf.threshold,
          missing_fields:rem.missing_fields||[], unverified_fields:rem.unverified_fields||[],
          summary:"Prospect "+process.env.LOBSTER_ARG_PROSPECT_DOMAIN+" quarantined (confidence "
            +(conf.confidence!=null?conf.confidence:"n/a")+" < "+(conf.threshold!=null?conf.threshold:"0.6")+").",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $quarantine-load.stdout
      CONF: $confidence-check.stdout

  - id: quarantine-notify
    condition: $confidence-check.json.pass == false
    pipeline: state.set pitch/notifications
    stdin: $quarantine-next.stdout

  # ---------------------------------------------------------------------------
  # 9. Audit log (Req 38) — append a deterministic record to the single-key audit array.
  #    Always runs (qualified, archived, or quarantined). Load-before-use, append, cap, write.
  # ---------------------------------------------------------------------------
  - id: audit-load
    pipeline: state.get pitch/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const conf=JSON.parse(process.env.CONF||"{}");
        const icp=JSON.parse(process.env.ICP||"null");
        const crm=JSON.parse(process.env.CRMSYNC||"null");
        log.push({action:"prospect_enrichment",
          domain:process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
          icp_score:icp?icp.score:null, qualified:icp?!!icp.qualified:false,
          enrichment_confidence:conf.confidence, quarantined:conf.pass===false,
          crm_synced:crm?!!crm.synced:false,
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      CONF: $confidence-check.stdout
      ICP: $icp-score.stdout
      CRMSYNC: $crm-sync.stdout

  - id: audit-log
    pipeline: state.set pitch/audit-log
    stdin: $audit-next.stdout
