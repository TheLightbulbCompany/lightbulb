name: signal-sweep
# Pitch-mode buying-signal sweep (Reqs 8, 9, 10, 11). Runs every 4 hours on cron.
# Scans signal sources, scores each signal deterministically, threshold-filters, persists
# the qualified set, and queues a notification intent for the agent loop to deliver.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    Activation inputs, the qualified-signal snapshot, and the agent inbox are single-key
#    objects/arrays (state.* has no prefix-list); node steps read, edit, and re-write them.
#  - CRM = Attio ONLY (HubSpot/Salesforce/Pipedrive dropped). Reached gateway-free via the
#    baked `mcporter` CLI; output is YAML-ish TEXT, so a node step regex-parses stdout
#    (never JSON.parse).
#  - Perplexity is a direct deterministic API (curl + node normalize); not an llm-task.
#  - The one model-judgment step (signal-strength scoring) CANNOT run here (llm-task = 401).
#    The deterministic scorer defaults strength to 50; signals needing strength judgment are
#    flagged needs_agent_loop and handed to the agent via the notification intent. NO
#    openclaw.invoke, slack, fast-io, social-intelligence, apollo, hubspot, salesforce.
#  - Scripts/inline node always exit 0 and carry status in JSON; steps branch via condition.

steps:
  # ---------------------------------------------------------------------------
  # 1-4. Activation gate. pitch-activation-check.js needs a single stdin object
  #       {icp_config, bounce_rate, launch_state, canspam_config}. Load each piece
  #       from durable state, assemble it, then run the checker. (Load-before-use.)
  # ---------------------------------------------------------------------------
  - id: icp-load
    pipeline: state.get pitch/icp

  - id: launch-load
    pipeline: state.get pitch/launch-state

  - id: canspam-load
    pipeline: state.get pitch/canspam

  - id: bounce-load
    pipeline: state.get pitch/bounce-rate

  - id: activation-input
    command: >
      node -e 'const icp=JSON.parse(process.env.ICP||"null");
        const launch=JSON.parse(process.env.LAUNCH||"null");
        const canspam=JSON.parse(process.env.CANSPAM||"null");
        const br=JSON.parse(process.env.BOUNCE||"null");
        const bounce_rate=(br&&typeof br.bounce_rate==="number")?br.bounce_rate:(typeof br==="number"?br:null);
        process.stdout.write(JSON.stringify({icp_config:icp, launch_state:launch, canspam_config:canspam, bounce_rate}));'
    env:
      ICP: $icp-load.stdout
      LAUNCH: $launch-load.stdout
      CANSPAM: $canspam-load.stdout
      BOUNCE: $bounce-load.stdout

  # Activation checker (exits 0 always; pass flag carried in JSON). Gate everything below.
  - id: activation-check
    command: node pitch-activation-check.js
    stdin: $activation-input.stdout

  # ---------------------------------------------------------------------------
  # 5. Perplexity buying-signal scan (news, funding, hiring, expansion). Deterministic
  #    direct API: curl -> inline node normalize. ICP keywords come from the loaded
  #    ICP config (industries/tech-stack), passed in via env so nothing is interpolated
  #    into the command line.
  # ---------------------------------------------------------------------------
  - id: perplexity-query
    condition: $activation-check.json.pass
    command: >
      node -e 'const icp=JSON.parse(process.env.ICP||"null")||{};
        const kw=[].concat(icp.industries||[],icp.tech_stack_signals||[]).filter(Boolean).join(" OR ")||"target companies";
        process.stdout.write(JSON.stringify({query:kw+" funding OR hiring OR expansion OR acquisition OR product launch. Return company name, signal type, date, and source URL for each."}));'
    env:
      ICP: $icp-load.stdout

  - id: perplexity-signals
    condition: $activation-check.json.pass
    command: >
      node -e 'const {execSync}=require("child_process");
        const q=JSON.parse(process.env.QUERYIN||"{}").query||"";
        const key=process.env.PERPLEXITY_API_KEY||"";
        let content="", citations=[];
        if (key) { try {
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]});
          const raw=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer \"$PERPLEXITY_API_KEY "
            +"-H \"Content-Type: application/json\" --data-binary @-",
            {input:body, encoding:"utf8", timeout:60000, env:process.env});
          const j=JSON.parse(raw);
          content=(j.choices&&j.choices[0]&&j.choices[0].message&&j.choices[0].message.content)||"";
          citations=j.citations||[];
        } catch(e){ content=""; citations=[]; } }
        process.stdout.write(JSON.stringify({content, citations, total:citations.length, source:"perplexity", available:!!key}));'
    env:
      QUERYIN: $perplexity-query.stdout

  # ---------------------------------------------------------------------------
  # 6. CRM re-engagement (Req 9): closed-lost Attio companies with recent activity.
  #    mcporter -> YAML-ish text -> regex parse (never JSON.parse). Best-effort: if the
  #    CRM isn't connected or returns nothing, this is an empty array and the sweep
  #    continues on the other sources.
  # ---------------------------------------------------------------------------
  - id: crm-reengagement
    condition: $activation-check.json.pass
    command: >
      node -e 'const {execSync}=require("child_process");
        let txt=""; try { txt=execSync("mcporter call attio.list-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",limit:200}))+" --output text",
          {encoding:"utf8",timeout:60000}); } catch(e){ txt=""; }
        const cutoff=Date.now()-30*24*60*60*1000; const signals=[];
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const id=(p.match(/^([0-9a-f-]{36})/)||[])[1]||"";
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const name=((p.match(/\n\s*name:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          const status=((p.match(/\n\s*(?:status|stage|hs_lead_status):\s*"?([^"\n]+)"?/)||[])[1]||"").trim().toLowerCase().replace(/[\s-]+/g,"_");
          const lastAct=((p.match(/\n\s*(?:last_activity_date|notes_last_updated|last_interaction):\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          const isClosedLost=status.includes("closed_lost")||status.includes("lost");
          const actTs=Date.parse(lastAct); const recent=!isNaN(actTs)&&actTs>=cutoff;
          if (isClosedLost && recent && doms[0]) {
            signals.push({company:name||doms[0], domain:doms[0], record_id:id, signal_type:"reengagement", timestamp:lastAct});
          }
        }
        process.stdout.write(JSON.stringify({signals, count:signals.length}));'

  # ---------------------------------------------------------------------------
  # 7. Merge all signal sources into a single normalized array. Perplexity content is a
  #    text blob (one signal of source_type "news"); CRM re-engagement contributes
  #    structured rows. Each row is tagged source_type and given a timestamp so the
  #    deterministic scorer can compute recency.
  # ---------------------------------------------------------------------------
  - id: merge-signals
    condition: $activation-check.json.pass
    command: >
      node -e 'const px=JSON.parse(process.env.PX||"null")||{};
        const crm=(JSON.parse(process.env.CRM||"null")||{}).signals||[];
        const now=new Date().toISOString();
        const merged=[];
        if (px.content) merged.push({company:"", signal_type:"news", source_type:"news",
          content:px.content, citations:px.citations||[], timestamp:now});
        for (const s of crm) merged.push({...s, source_type:"crm_reengagement"});
        process.stdout.write(JSON.stringify({signals:merged, count:merged.length}));'
    env:
      PX: $perplexity-signals.stdout
      CRM: $crm-reengagement.stdout

  # ---------------------------------------------------------------------------
  # 8. Deterministic composite scoring (Req 10) — ICP fit, recency, relationship.
  #    pitch-signal-scorer.js needs {signals, icp_criteria, threshold}. Strength is the
  #    one LLM dimension; the scorer defaults it to 50 and flags needs_agent_loop where
  #    judgment is required. Assemble the scorer input (signals + icp from state).
  # ---------------------------------------------------------------------------
  - id: scorer-input
    condition: $merge-signals.json.count > 0
    command: >
      node -e 'const merged=JSON.parse(process.env.MERGED||"{}").signals||[];
        const icp=JSON.parse(process.env.ICP||"null")||{};
        process.stdout.write(JSON.stringify({signals:merged, icp_criteria:icp, threshold:60}));'
    env:
      MERGED: $merge-signals.stdout
      ICP: $icp-load.stdout

  - id: deterministic-score
    condition: $merge-signals.json.count > 0
    command: node pitch-signal-scorer.js
    stdin: $scorer-input.stdout

  # ---------------------------------------------------------------------------
  # 9. Threshold filter. The scorer already emits `qualified` (composite >= threshold)
  #    and an explicit qualified_count. Re-emit a clean payload with an explicit count to
  #    gate the persist/notify steps on (never gate on .length).
  # ---------------------------------------------------------------------------
  - id: final-filter
    condition: $merge-signals.json.count > 0
    command: >
      node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"))||{};
        const qualified=d.qualified||[];
        const needs_agent=qualified.filter(s=>s.needs_agent_loop);
        process.stdout.write(JSON.stringify({
          qualified, count:qualified.length, threshold:d.threshold||60,
          needs_agent_loop_count:needs_agent.length, total_scanned:d.total||0}));'
    stdin: $deterministic-score.stdout

  # ---------------------------------------------------------------------------
  # 10. Persist the qualified-signal snapshot to durable state (Req 38). Single key,
  #     overwritten each sweep (the agent reads pitch/signals-latest to work the queue).
  # ---------------------------------------------------------------------------
  - id: persist-signals
    condition: $final-filter.json.count > 0
    pipeline: state.set pitch/signals-latest
    stdin: $final-filter.stdout

  # ---------------------------------------------------------------------------
  # 11. Queue a notification intent for the agent loop (Slack is unreachable here, 401).
  #     The intent carries the qualified count + the signals that need model judgment so
  #     the agent can run the one llm-task strength pass and post to #pitch-signals.
  # ---------------------------------------------------------------------------
  - id: notify-load
    condition: $final-filter.json.count > 0
    pipeline: state.get pitch/notifications

  - id: notify-next
    condition: $final-filter.json.count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const f=JSON.parse(process.env.FILTERED||"{}");
        n.push({
          kind:"new_signals",
          needs_llm:(f.needs_agent_loop_count||0)>0,
          deliver_to:"#pitch-signals",
          qualified_count:f.count||0,
          needs_agent_loop_count:f.needs_agent_loop_count||0,
          threshold:f.threshold||60,
          summary:(f.count||0)+" new qualified signals detected. Review queue ready.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      FILTERED: $final-filter.stdout

  - id: notify
    condition: $final-filter.json.count > 0
    pipeline: state.set pitch/notifications
    stdin: $notify-next.stdout

  # ---------------------------------------------------------------------------
  # 12. Audit snapshot (Req 38) — durable, deterministic, no openclaw.invoke. The agent
  #     can read pitch/signal-sweep-latest to answer "what did the last sweep find".
  # ---------------------------------------------------------------------------
  - id: audit-build
    command: >
      node -e 'const merged=JSON.parse(process.env.MERGED||"null")||{};
        const f=JSON.parse(process.env.FILTERED||"null")||{};
        const act=JSON.parse(process.env.ACT||"null")||{};
        process.stdout.write(JSON.stringify({
          action:"signal_sweep",
          activation_pass:!!act.pass,
          signals_scanned:merged.count||0,
          signals_qualified:f.count||0,
          needs_agent_loop_count:f.needs_agent_loop_count||0,
          sources:["perplexity","attio_crm_reengagement"],
          authorized_by:"autonomous",
          timestamp:new Date().toISOString()}));'
    env:
      MERGED: $merge-signals.stdout
      FILTERED: $final-filter.stdout
      ACT: $activation-check.stdout

  - id: audit-save
    pipeline: state.set pitch/signal-sweep-latest
    stdin: $audit-build.stdout
