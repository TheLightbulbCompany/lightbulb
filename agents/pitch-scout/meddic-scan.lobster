name: meddic-scan
# Daily pipeline-health scan: read active deals from the CRM, check each deal's
# MEDDIC confirmation state against its pipeline stage, and surface the gaps.
# Runs daily at 8 AM on cron. Fully deterministic (0 LLM).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - CRM is Attio ONLY, reached gateway-free via the baked `mcporter` CLI inside a
#    node step. Its output is YAML-ish TEXT, so the node step REGEX-PARSES stdout
#    (never JSON.parse) into the {deals:[…]} shape pitch-meddic-gap-check.js expects.
#  - MEDDIC fields on Attio are flat confirmation attributes; the node parser maps
#    the ones it can read out of the text. Deals whose MEDDIC sub-structure can't be
#    parsed from text are surfaced to the agent loop (see notes) — the gap check is
#    conservative (missing/unconfirmed → gap), which is the safe default.
#  - Slack is unreachable from an embedded pipeline (401), so the pipeline-health
#    digest is written to a `pitch/notifications` queue for the agent to deliver, and
#    the audit entry to a `pitch/meddic-audit` single-key index array.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

steps:
  # 1. Load active deals from Attio (the connected CRM). mcporter -> text -> parse.
  #    list-records returns the open-pipeline companies; we pull name/domain/stage and
  #    best-effort MEDDIC confirmation flags out of the YAML-ish attribute text.
  - id: load-deals
    command: >
      node -e 'const {execSync}=require("child_process");
        let txt=""; try { txt=execSync("mcporter call attio.list-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",limit:100}))+" --output text",
          {encoding:"utf8",timeout:60000}); } catch(e){ txt=""; }
        const deals=[];
        const meddicKeys=["metrics","economic_buyer","decision_criteria","decision_process","identify_pain","champion"];
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const rid=(p.match(/^([0-9a-f-]{36})/)||[])[1]||"";
          const name=((p.match(/\n\s*name:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const stage=((p.match(/\n\s*(?:stage|dealstage|pipeline_stage):\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          if (!stage) continue;
          const meddic={};
          for (const k of meddicKeys) {
            const re=new RegExp("meddic_"+k+"(?:_confirmed)?:\\s*\"?([^\"\\n]+)\"?","i");
            const m=p.match(re);
            if (m) {
              const v=(m[1]||"").trim().toLowerCase();
              meddic[k]={confirmed: v==="true"||v==="yes"||v==="confirmed", source:"crm"};
            }
          }
          deals.push({record_id:rid, name:name||doms[0]||rid, domain:doms[0]||"", stage, meddic});
        }
        process.stdout.write(JSON.stringify({deals, count:deals.length}));'

  # 2. MEDDIC gap check — deterministic. Feed the loaded deals via stdin.
  #    pitch-meddic-gap-check.js emits {results, total_deals, deals_with_gaps,
  #    deals_clear, alerts}. (It currently exit(1)s when gaps exist — see .js fix note;
  #    the workflow gates on counts, not exit code.)
  - id: scan-gaps
    condition: $load-deals.json.count > 0
    command: node pitch-meddic-gap-check.js
    stdin: $load-deals.stdout

  # 3. Build the decision-forward pipeline-health digest deterministically (no LLM,
  #    no summarize gateway call). Lists deals-with-gaps and their unconfirmed fields.
  - id: digest
    condition: $load-deals.json.count > 0
    command: >
      node -e 'const r=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const lines=(r.alerts||[]);
        const body=lines.length ? lines.map(a=>"- "+a).join("\n") : "All active deals have their required MEDDIC fields confirmed.";
        const text="DAILY PIPELINE HEALTH\n\n"+body
          +"\n\nDeals with gaps: "+(r.deals_with_gaps||0)
          +"\nDeals clear: "+(r.deals_clear||0)
          +"\nTotal active deals: "+(r.total_deals||0);
        process.stdout.write(JSON.stringify({text, deals_with_gaps:r.deals_with_gaps||0}));'
    stdin: $scan-gaps.stdout

  # 4. Queue the digest as a notification for the agent to deliver to #pitch-pipeline
  #    (Slack is unreachable here). Append to the single-key notifications index array.
  - id: notify-load
    condition: $scan-gaps.json.deals_with_gaps > 0
    pipeline: state.get pitch/notifications

  - id: notify-next
    condition: $scan-gaps.json.deals_with_gaps > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const d=JSON.parse(process.env.DIGEST||"{}");
        n.push({kind:"pipeline_health", channel:"#pitch-pipeline",
          text:d.text||"", deals_with_gaps:d.deals_with_gaps||0,
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      DIGEST: $digest.stdout

  - id: notify
    condition: $scan-gaps.json.deals_with_gaps > 0
    pipeline: state.set pitch/notifications
    stdin: $notify-next.stdout

  # 5. Audit log — append the scan result to the single-key audit index array.
  - id: audit-load
    condition: $load-deals.json.count > 0
    pipeline: state.get pitch/meddic-audit

  - id: audit-next
    condition: $load-deals.json.count > 0
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const r=JSON.parse(process.env.SCAN||"{}");
        a.push({action:"meddic_scan",
          total_deals:r.total_deals||0,
          deals_with_gaps:r.deals_with_gaps||0,
          alerts:r.alerts||[],
          authorized_by:"autonomous",
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(a));'
    env:
      AUDIT: $audit-load.stdout
      SCAN: $scan-gaps.stdout

  - id: audit
    condition: $load-deals.json.count > 0
    pipeline: state.set pitch/meddic-audit
    stdin: $audit-next.stdout
