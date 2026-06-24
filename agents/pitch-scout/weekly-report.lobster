name: weekly-report
# Weekly sourcing-intelligence report with conversion tracking (Reqs 37, 44, 45, 46).
# Runs Monday 8 AM on cron. Fully deterministic from the pipeline's side (0 LLM here):
# it cross-references the last 7 days of deposited leads against Attio deal outcomes,
# computes per-signal-type + per-tier conversion rates, flags the threshold issue, and
# hands the analysis to the agent loop (which owns the LLM weight-tuning + Slack delivery).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount. The deposit history,
#    signal weights, and report inbox are single-key arrays/objects (state.* has no
#    prefix-list); a node step reads them, edits, and re-writes.
#  - Deposit history = the durable Scout `scout/deposit-history` audit array (each entry
#    carries {action, domain, score, tier, destination, dedup_result, urgency,
#    dossier_complete, timestamp}) — appended by deposit-lead.lobster. This is the
#    append-only log, not the live `scout/lead-queue` that Pitch drains.
#  - Deal outcomes = Attio `deals`, reached gateway-free via the baked `mcporter` CLI;
#    its output is YAML-ish TEXT, so a node step regex-parses stdout (never JSON.parse).
#  - The capability-evolver weight tuning is genuine model judgment (per AGENTS.md it is
#    the one weekly llm-task). The pipeline CANNOT call the LLM (401), so it writes the
#    finished conversion analysis + a "tune weights / write report / post digest" intent
#    to `scout/notifications` for the agent loop to act on. NO openclaw.invoke, slack, gog.
#  - Scripts/inline node always exit 0 and carry status in JSON; steps branch via condition.

steps:
  # 1. Load the durable Scout deposit history (append-only audit array). Written by
  #     deposit-lead. This is the reliable deposit log, not the live lead-queue Pitch drains.
  - id: deposits-load
    pipeline: state.get scout/deposit-history

  # 2. Load current capability-evolver-tuned signal weights (object, may be null on first run).
  - id: weights-load
    pipeline: state.get scout/signal-weights

  # 3. Pull recent deal outcomes from Attio (CRM). mcporter -> YAML-ish text -> regex parse.
  #    list-records on the `deals` object; we read domain + stage + close_date per record.
  - id: crm-outcomes
    command: >
      node -e 'const {execSync}=require("child_process");
        let txt=""; try { txt=execSync("mcporter call attio.list-records --args "
          +JSON.stringify(JSON.stringify({object:"deals",limit:200}))+" --output text",
          {encoding:"utf8",timeout:60000}); } catch (e) { txt=""; }
        const outcomes=[];
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const assoc=[...p.matchAll(/(?:associated_company|company|company_domain)\[?\d*\]?:\s*"?([a-z0-9.-]+\.[a-z]{2,})"?/gi)].map(m=>m[1].toLowerCase());
          const stage=((p.match(/\n\s*stage:\s*"?([^"\n]+)"?/)||[])[1]||"").trim().toLowerCase().replace(/[\s-]+/g,"_");
          const close=((p.match(/\n\s*close_date:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          const value=parseFloat(((p.match(/\n\s*(?:deal_value|value|amount):\s*"?([0-9.]+)"?/)||[])[1]||"0"))||0;
          const domain=doms[0]||assoc[0]||"";
          if (domain) outcomes.push({domain, stage, close_date:close, deal_value:value});
        }
        process.stdout.write(JSON.stringify({outcomes, count:outcomes.length}));'

  # 4. Conversion analysis (Reqs 37, 44, 45). Cross-reference deposits in the last 7 days
  #    against closed_won outcomes; compute per-signal-type + per-tier rates; flag the
  #    threshold issue (priority vs standard converting at similar rates). Deterministic.
  - id: conversion-analysis
    command: >
      node -e 'const q=JSON.parse(process.env.DEPOSITS||"null")||[];
        const outs=(JSON.parse(process.env.OUTCOMES||"null")||{}).outcomes||[];
        const weekMs=7*24*60*60*1000; const cutoff=Date.now()-weekMs;
        const deposits=q.filter(d=>{const t=Date.parse(d.timestamp||"");return isNaN(t)?true:t>=cutoff;});
        const wonByDomain={};
        for (const o of outs) { if (o.stage==="closed_won"||o.stage==="won") wonByDomain[o.domain]=true; }
        const signalPerf={}; const scorePerf={priority:{total:0,converted:0},standard:{total:0,converted:0}};
        for (const dep of deposits) {
          const domain=(dep.domain||"").toLowerCase();
          const sigType=(dep.dossier&&dep.dossier.trigger_signal&&dep.dossier.trigger_signal.type)||dep.signal_type||"unknown";
          const tier=dep.tier||"standard";
          if (!signalPerf[sigType]) signalPerf[sigType]={total:0,converted:0};
          signalPerf[sigType].total++;
          if (scorePerf[tier]) scorePerf[tier].total++;
          if (wonByDomain[domain]) { signalPerf[sigType].converted++; if (scorePerf[tier]) scorePerf[tier].converted++; }
        }
        for (const k of Object.keys(signalPerf)) { const s=signalPerf[k]; s.rate=s.total>0?+(s.converted/s.total).toFixed(3):0; }
        const pr=scorePerf.priority.total>0?scorePerf.priority.converted/scorePerf.priority.total:0;
        const sr=scorePerf.standard.total>0?scorePerf.standard.converted/scorePerf.standard.total:0;
        const thresholdIssue=Math.abs(pr-sr)<0.10 && scorePerf.priority.total>5 && scorePerf.standard.total>5;
        process.stdout.write(JSON.stringify({
          deposits_analyzed:deposits.length,
          outcomes_seen:outs.length,
          signal_performance:signalPerf,
          score_performance:scorePerf,
          priority_rate:+pr.toFixed(3), standard_rate:+sr.toFixed(3),
          threshold_issue:thresholdIssue,
          threshold_recommendation: thresholdIssue
            ? "Priority and standard tiers are converting at similar rates. Consider lowering the priority threshold."
            : null
        }));'
    env:
      DEPOSITS: $deposits-load.stdout
      OUTCOMES: $crm-outcomes.stdout

  # 5. Did we analyze anything? Gate the rest of the run on a real deposit count.
  #    (.length is silently false in conditions — gate on the explicit count instead.)
  - id: has-data
    command: >
      node -e 'const a=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({count:a.deposits_analyzed||0}));'
    stdin: $conversion-analysis.stdout

  # 6. Assemble the report payload (deterministic digest body + the weight-tuning intent).
  #    The agent loop owns the LLM pass (signal-weight tuning), the Google-Doc write, and
  #    the Slack post — all unreachable from the embedded pipeline.
  - id: report-build
    condition: $has-data.json.count > 0
    command: >
      node -e 'const a=JSON.parse(process.env.ANALYSIS||"{}");
        const w=JSON.parse(process.env.WEIGHTS||"null")||{};
        const date=new Date().toISOString().slice(0,10);
        const sp=a.signal_performance||{};
        const lines=Object.keys(sp).sort((x,y)=>sp[y].total-sp[x].total).map(k=>
          "- "+k+": "+sp[k].converted+"/"+sp[k].total+" converted ("+Math.round((sp[k].rate||0)*100)+"%)");
        const body="WEEKLY SOURCING INTELLIGENCE - "+date+"\n\n"+
          "Deposits analyzed (7d): "+(a.deposits_analyzed||0)+"\n"+
          "Deal outcomes scanned: "+(a.outcomes_seen||0)+"\n\n"+
          "Conversion by signal type:\n"+(lines.join("\n")||"- (none)")+"\n\n"+
          "Tier conversion: priority "+Math.round((a.priority_rate||0)*100)+"%, "+
          "standard "+Math.round((a.standard_rate||0)*100)+"%\n"+
          (a.threshold_recommendation?("\n[ACTION] "+a.threshold_recommendation+"\n"):"");
        process.stdout.write(JSON.stringify({
          date, digest:body, conversion_analysis:a, current_weights:w
        }));'
    env:
      ANALYSIS: $conversion-analysis.stdout
      WEIGHTS: $weights-load.stdout

  # 7. Load the agent-delivery queue (single-key array) so we can append the report intent.
  - id: notify-load
    condition: $has-data.json.count > 0
    pipeline: state.get scout/notifications

  # 8. Append a `weekly_report` intent for the agent loop. It carries the deterministic
  #    digest + the full conversion analysis so the agent can: run the one weekly llm-task
  #    to tune signal weights (writing scout/signal-weights), write the Google Doc, and
  #    post the digest to #scout-digest. Slack/gog/LLM are all unreachable here.
  - id: notify-next
    condition: $has-data.json.count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const r=JSON.parse(process.env.REPORT||"{}");
        n.push({
          kind:"weekly_report",
          needs_llm:true,
          deliver_to:"#scout-digest",
          date:r.date,
          digest:r.digest,
          conversion_analysis:r.conversion_analysis,
          current_weights:r.current_weights,
          threshold_issue:(r.conversion_analysis||{}).threshold_issue||false,
          created_at:new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      REPORT: $report-build.stdout

  - id: notify
    condition: $has-data.json.count > 0
    pipeline: state.set scout/notifications
    stdin: $notify-next.stdout

  # 9. Persist this week's report snapshot to durable state (history / audit; the agent
  #    can read scout/weekly-report-latest to answer "what did last week look like").
  - id: report-save
    condition: $has-data.json.count > 0
    pipeline: state.set scout/weekly-report-latest
    stdin: $report-build.stdout
