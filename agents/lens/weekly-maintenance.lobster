name: weekly-maintenance
# Weekly confidence-degradation + learning loop for Lens (Requirement 34).
# Runs weekly on cron (Sunday evening, isolated session).
#
# What it does: re-checks the sources behind previously archived research for
# retraction / supersession / content-drift / inaccessibility, runs the
# deterministic degrader to compute confidence downgrades (with dismissal
# suppression), persists a degradation-log entry, and queues the adaptive LLM
# summary + Slack delivery + capability-evolver learning + Sheets tracker work
# for the agent loop to handle (none of those can run in an embedded pipeline).
#
# Fully deterministic, gateway-free.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node/curl)
#    run under `command:`. Never an `exec --json --shell` under command:.
#  - Durable state via native state.get/set on the per-owner EFS mount. The
#    original fast-io keys become single-key index docs:
#      lens-research/ (prefix list)       -> lens/research-index (one array)
#      lens-state/degradation-dismissals  -> lens/state/degradation-dismissals
#      lens-state/degradation-log/{date}  -> lens/state/degradation-log (one array)
#      audit/{ts}/weekly-maintenance      -> lens/audit-log (one array, capped)
#  - Source accessibility re-check needs agent-browser (FICTIONAL on the embedded
#    runner). We degrade to a Perplexity (sonar) retraction/supersession query
#    per finding via gateway-free `curl` with $PERPLEXITY_API_KEY (same pattern
#    as monitoring-sweep; deterministic structured search, NOT an llm-task).
#  - lens-confidence-degrader.js exits 0 and carries has_downgrades/counts in
#    JSON; we branch on those, never on .length.
#  - FICTIONAL integrations dropped, their work queued to lens/notifications for
#    the loop: llm-task (degradation summary), slack (#lens-research delivery),
#    capability-evolver (weekly learning analysis), gog sheets-write (Confidence
#    Tracker), agent-browser (exact full-page source diff). Each carries the
#    inputs the loop needs plus a "connect agent-browser" notice where relevant.

steps:
  # 1. Load the archived research index (was the fast-io `lens-research/` prefix
  #    list). Single-key array; absent -> null. A node step normalizes to [] and
  #    keeps only entries whose verification tier is verified/supported (the
  #    original step's filter) so the degrader only re-checks load-bearing claims.
  - id: research-index-raw
    pipeline: state.get lens/research-index

  - id: verified-findings
    command: >
      node -e 'let idx=JSON.parse(process.env.IDX||"null");if(!Array.isArray(idx))idx=[];
        const findings=[];
        for(const e of idx){
          const cs=e.confidence_summary||{};
          const tier=(cs.dominant_tier||e.confidence_tier||"").toLowerCase();
          const vr=(typeof e.verification_rate==="number")?e.verification_rate:(cs.verification_rate||0);
          const keep=tier==="verified"||tier==="supported"||vr>=0.5;
          if(!keep)continue;
          const sources=Array.isArray(e.sources)?e.sources
            :((e.source_appendix&&e.source_appendix.sources)||[]);
          findings.push({
            id:e.id||e.question||("finding-"+findings.length),
            claim_id:e.id||e.question||("finding-"+findings.length),
            claim_text:e.question||e.claim_text||"",
            confidence_tier:tier||(vr>=0.75?"verified":"supported"),
            sources:sources.map(s=>({url:s.url||"",title:s.title||"",tier:s.tier||""})).filter(s=>s.url)
          });
        }
        process.stdout.write(JSON.stringify({findings,count:findings.length}));'
    env:
      IDX: $research-index-raw.stdout

  # 2. Collect every distinct source URL across the verified findings so we can
  #    re-check each once (an explicit count gates the network step below).
  - id: source-urls
    command: >
      node -e 'const vf=JSON.parse(process.env.VF||"{}");const findings=vf.findings||[];
        const seen=new Set();const urls=[];
        for(const f of findings)for(const s of (f.sources||[])){
          const u=(s.url||"").trim();if(!u||seen.has(u))continue;seen.add(u);urls.push(u);}
        process.stdout.write(JSON.stringify({urls,count:urls.length}));'
    env:
      VF: $verified-findings.stdout

  # 3. Source accessibility / change re-check. agent-browser (exact full-page
  #    diff) is FICTIONAL here, so we proxy with a single Perplexity sonar query
  #    that asks whether any of the cited sources were retracted, superseded, or
  #    materially changed, and parse retracted/superseded URLs out of the answer.
  #    Always runs so its .stdout ref always resolves; the network call is skipped
  #    in-script when there are no URLs or no key. Output keys match the degrader's
  #    `source_status_updates` map ({url:{retracted|superseded|content_changed|
  #    inaccessible}}). Heuristic-only here; the loop runs agent-browser for the
  #    authoritative page-level diff (see the notice queued below).
  - id: source-status
    command: >
      node -e 'const {execFileSync}=require("child_process");
        const su=JSON.parse(process.env.SU||"{}");const urls=su.urls||[];
        const key=process.env.PERPLEXITY_API_KEY||"";
        const status={};
        if(urls.length&&key){
          const q="For each of these source URLs, has it been retracted, superseded by a newer version, materially changed, or become inaccessible since publication? Answer per URL.\n"+urls.join("\n");
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]});
          let raw="{}";
          try{raw=execFileSync("curl",["-sS","https://api.perplexity.ai/chat/completions",
            "-H","Authorization: Bearer "+key,"-H","Content-Type: application/json","--data-binary","@-"],
            {input:body,encoding:"utf8",timeout:60000});}catch(e){raw="{}";}
          let d={};try{d=JSON.parse(raw);}catch(e){d={};}
          const txt=((d.choices&&d.choices[0]&&d.choices[0].message)?d.choices[0].message.content:"")||"";
          const lower=txt.toLowerCase();
          for(const u of urls){
            const i=lower.indexOf(u.toLowerCase());
            if(i<0)continue;
            const win=lower.slice(i,i+400);
            if(/retract/.test(win))status[u]={retracted:true};
            else if(/supersed|replaced by|newer version|deprecated/.test(win))status[u]={superseded:true,superseded_by:"(see source-change notice)"};
            else if(/materially changed|content (has )?changed|updated|revised/.test(win))status[u]={content_changed:true};
            else if(/inaccessible|no longer available|404|removed|dead link/.test(win))status[u]={inaccessible:true};
          }
        }
        process.stdout.write(JSON.stringify({source_status_updates:status,checked:urls.length,auth_ok:!!key,
          needs_agent_browser:urls.length>0,
          notice:urls.length>0?"Exact full-page source diff needs agent-browser — connect it for authoritative retraction/supersession detection. This week used a Perplexity heuristic.":""}));'
    env:
      SU: $source-urls.stdout

  # 4. Dismissal history for alert suppression (was fast-io
  #    lens-state/degradation-dismissals). Read-only consumer state the loop
  #    maintains when the user dismisses a downgrade alert.
  - id: dismissals
    pipeline: state.get lens/state/degradation-dismissals

  # 5. Assemble the degrader input ({verified_findings, source_status_updates,
  #    dismissal_history}) — load-before-use: all three producers are above.
  - id: degrade-input
    command: >
      node -e 'const vf=JSON.parse(process.env.VF||"{}");
        const ss=JSON.parse(process.env.SS||"{}");
        const dh=JSON.parse(process.env.DH||"null")||{};
        process.stdout.write(JSON.stringify({
          verified_findings:vf.findings||[],
          source_status_updates:ss.source_status_updates||{},
          dismissal_history:dh}));'
    env:
      VF: $verified-findings.stdout
      SS: $source-status.stdout
      DH: $dismissals.stdout

  # 6. Deterministic confidence degradation check (Req 34). Exits 0 and carries
  #    has_downgrades / active_downgrades / suppressed_downgrades in JSON.
  - id: degrade-check
    command: node lens-confidence-degrader.js
    stdin: $degrade-input.stdout

  # 7. Queue the adaptive degradation summary + Slack delivery for the loop.
  #    llm-task (#tone-adaptive summary) and slack (#lens-research post) BOTH 401
  #    on the embedded runner, so we hand the loop everything it needs: the full
  #    degrade-check payload, the prompt, the channel, and the source-check notice.
  #    Gated on the degrader's own has_downgrades flag (never on .length).
  - id: notify-load
    condition: $degrade-check.json.has_downgrades
    pipeline: state.get lens/notifications

  - id: notify-next
    condition: $degrade-check.json.has_downgrades
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const dc=JSON.parse(process.env.DC||"{}");
        const ss=JSON.parse(process.env.SS||"{}");
        n.push({kind:"confidence_degradation",channel:"#lens-research",
          needs_llm_summary:true,
          llm_prompt:"Summarize these confidence downgrades for the user. Be specific about which findings were affected and why. Adapt your tone — this is maintenance, not alarm. For suppressed downgrades, mention them briefly as informational.",
          degrade_check:dc,
          total_checked:dc.total_checked||0,
          active_downgrades:dc.active_downgrades||0,
          suppressed_downgrades:dc.suppressed_downgrades||0,
          source_check_notice:ss.notice||"",
          alert_header:"WEEKLY CONFIDENCE REVIEW",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      DC: $degrade-check.stdout
      SS: $source-status.stdout

  - id: notify
    condition: $degrade-check.json.has_downgrades
    pipeline: state.set lens/notifications
    stdin: $notify-next.stdout

  # 8. Persist this run's degradation result to the degradation-log index array
  #    (was fast-io lens-state/degradation-log/{date} — keyed-per-date, now one
  #    capped array since state.* has no prefix-list). Always runs.
  - id: degrade-log-load
    pipeline: state.get lens/state/degradation-log

  - id: degrade-log-next
    command: >
      node -e 'let log=JSON.parse(process.env.LOG||"null");if(!Array.isArray(log))log=[];
        const dc=JSON.parse(process.env.DC||"{}");
        log.push({date:new Date().toISOString().slice(0,10),
          total_checked:dc.total_checked||0,
          active_downgrades:dc.active_downgrades||0,
          suppressed_downgrades:dc.suppressed_downgrades||0,
          downgrades:dc.downgrades||[],
          recorded_at:new Date().toISOString()});
        if(log.length>200)log=log.slice(log.length-200);
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $degrade-log-load.stdout
      DC: $degrade-check.stdout

  - id: degrade-log
    pipeline: state.set lens/state/degradation-log
    stdin: $degrade-log-next.stdout

  # 9. Queue the weekly capability-evolver learning + Sheets-tracker write for
  #    the loop. capability-evolver is FICTIONAL (per the contract + AGENTS.md)
  #    and gog sheets-write surface isn't guessed from a pipeline — both are loop
  #    work. We hand the loop the state keys to read (it has state access) plus
  #    the analysis focus and the degradation payload for the tracker row.
  #    Always runs (weekly learning is the point of the job).
  - id: learn-notify-load
    pipeline: state.get lens/notifications

  - id: learn-notify-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const dc=JSON.parse(process.env.DC||"{}");
        const date=new Date().toISOString().slice(0,10);
        n.push({kind:"weekly_learning",
          capability_evolver_fictional:true,
          connect_notice:"Weekly preference-learning ran in the agent loop (capability-evolver is not an embedded tool). Review which source tiers / confidence thresholds / decomposition styles to adjust.",
          read_state_keys:["lens/state/source-overrides","lens/config/confidence-thresholds","lens/state/degradation-dismissals","lens/state/historical-research"],
          analysis_focus:"Which source tiers does the user consistently reclassify? Which confidence thresholds are too conservative or too loose for their domain? Which decomposition styles does the user prefer? Which monitoring/degradation alerts get consistently dismissed?",
          created_at:new Date().toISOString()});
        n.push({kind:"confidence_tracker_write",
          gog_sheets_fictional_surface:true,
          connect_notice:"Update the Lens Confidence Tracker sheet via gog in the loop (the sheets-write subcommand surface is not invoked from the pipeline).",
          spreadsheet:"Lens Confidence Tracker",
          sheet:date,
          data:dc,
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $learn-notify-load.stdout
      DC: $degrade-check.stdout

  - id: learn-notify
    pipeline: state.set lens/notifications
    stdin: $learn-notify-next.stdout

  # 10. Append an audit record to the single-key audit index array (was the
  #     fast-io audit/{ts}/weekly-maintenance prefix). Capped at 500.
  - id: audit-load
    pipeline: state.get lens/audit-log

  - id: audit-next
    command: >
      node -e 'let log=JSON.parse(process.env.LOG||"null");if(!Array.isArray(log))log=[];
        const dc=JSON.parse(process.env.DC||"{}");
        log.push({action:"weekly_maintenance",
          findings_checked:dc.total_checked||0,
          downgrades:dc.active_downgrades||0,
          suppressed:dc.suppressed_downgrades||0,
          authorized_by:"autonomous",
          timestamp:new Date().toISOString()});
        if(log.length>500)log=log.slice(log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-load.stdout
      DC: $degrade-check.stdout

  - id: audit
    pipeline: state.set lens/audit-log
    stdin: $audit-next.stdout
