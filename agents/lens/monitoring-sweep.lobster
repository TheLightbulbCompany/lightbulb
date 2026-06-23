name: monitoring-sweep
# Daily source-change sweep for Lens (Requirements 32-34).
# Monitors the user's configured verticals (financial / academic / tech / legal /
# competitive) for changes, diffs against the stored baseline, and queues any
# meaningful change as a Slack-bound notification for the agent loop to deliver.
#
# Fully deterministic, gateway-free. Runs daily on cron (isolated session).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via native state.get/set on the per-owner EFS mount. The original
#    fast-io keys (lens-config/monitoring, lens-monitoring/previous-state) become
#    single-key index docs read with state.get / written with state.set.
#  - Perplexity (sonar) is reached gateway-free via `curl` with $PERPLEXITY_API_KEY
#    from the process env — it is a deterministic structured-search API, NOT an LLM
#    step (see TOOLS.md). Every curl is piped to an inline node normalizer.
#  - FICTIONAL integrations dropped: agent-browser (tech-monitor full-page fetch),
#    openclaw.invoke/fast-io (state), llm-task (semantic change summary), slack
#    (alert delivery) — all 401 on the embedded runner. The tech vertical degrades
#    to a Perplexity changelog query + a "connect agent-browser" notice; the LLM
#    summary + Slack alert are written to a `lens/notifications` queue for the loop.
#  - lens-change-describer.js exits 0 and carries status in JSON; steps branch via
#    condition: (never on .length — an explicit count is emitted and gated).

steps:
  # 1. Load monitoring configuration (read-only consumer state the user sets in UI).
  #    Absent key -> null; a node step normalizes to the per-vertical shape so every
  #    downstream `enabled` flag resolves instead of throwing on a missing path.
  - id: config-raw
    pipeline: state.get lens-config/monitoring

  - id: config
    command: >
      node -e 'const c=JSON.parse(process.env.CFG||"null")||{};
        const v=(k)=>{const o=c[k]||{};return {enabled:!!o.enabled,
          tracked_companies:o.tracked_companies||"", topics:o.topics||"",
          tracked_urls:o.tracked_urls||[], tracked_regulations:o.tracked_regulations||""};};
        const out={financial:v("financial"),academic:v("academic"),
          technology:v("technology"),legal:v("legal"),competitive:v("competitive")};
        out.any_enabled = out.financial.enabled||out.academic.enabled||out.technology.enabled||out.legal.enabled||out.competitive.enabled;
        out.tech_needs_browser = out.technology.enabled && (out.technology.tracked_urls||[]).length>0;
        process.stdout.write(JSON.stringify(out));'
    env:
      CFG: $config-raw.stdout

  # 2. Financial — Perplexity sonar for new SEC filings on tracked companies.
  #    Always runs (so its .stdout ref always resolves for the unconditional
  #    assembly steps); the network call is skipped in-script when the vertical
  #    is disabled — emitting an empty result the baseline/diff treat as "no data".
  - id: financial-monitor
    command: >
      node -e 'const {execFileSync}=require("child_process");
        const cfg=JSON.parse(process.env.CFG||"{}");
        const key=process.env.PERPLEXITY_API_KEY||"";
        const enabled=!!cfg.enabled;
        const q=(cfg.tracked_companies||"")+" new SEC filing 8-K 10-Q last 24 hours";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]});
        let raw="{}";
        if(enabled&&key){try{raw=execFileSync("curl",["-sS","https://api.perplexity.ai/chat/completions",
          "-H","Authorization: Bearer "+key,"-H","Content-Type: application/json","--data-binary","@-"],
          {input:body,encoding:"utf8",timeout:60000});}catch(e){raw="{}";}}
        let d={};try{d=JSON.parse(raw);}catch(e){d={};}
        const c=(d.choices&&d.choices[0]&&d.choices[0].message)?d.choices[0].message.content:"";
        const urls=d.citations||[];
        process.stdout.write(JSON.stringify({vertical:"financial",enabled,content:c,citations:urls,high_value_urls:urls.slice(0,10),total:urls.length,auth_ok:!!key}));'
    env:
      CFG: $config.json.financial

  # 3. Academic — new papers in configured topics. Always runs; skips in-script when disabled.
  - id: academic-monitor
    command: >
      node -e 'const {execFileSync}=require("child_process");
        const cfg=JSON.parse(process.env.CFG||"{}");
        const key=process.env.PERPLEXITY_API_KEY||"";
        const enabled=!!cfg.enabled;
        const q=(cfg.topics||"")+" new paper arxiv pubmed";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]});
        let raw="{}";
        if(enabled&&key){try{raw=execFileSync("curl",["-sS","https://api.perplexity.ai/chat/completions",
          "-H","Authorization: Bearer "+key,"-H","Content-Type: application/json","--data-binary","@-"],
          {input:body,encoding:"utf8",timeout:60000});}catch(e){raw="{}";}}
        let d={};try{d=JSON.parse(raw);}catch(e){d={};}
        const c=(d.choices&&d.choices[0]&&d.choices[0].message)?d.choices[0].message.content:"";
        const urls=d.citations||[];
        process.stdout.write(JSON.stringify({vertical:"academic",enabled,content:c,citations:urls,high_value_urls:urls.slice(0,10),total:urls.length,auth_ok:!!key}));'
    env:
      CFG: $config.json.academic

  # 4. Tech — Perplexity changelog/docs query. Always runs; skips in-script when
  #    disabled. Full-page fetch of tracked_urls needed agent-browser (FICTIONAL
  #    here); we emit a Perplexity proxy result and a `connect` notice so the tech
  #    vertical still moves. The agent loop runs agent-browser for deep page reads
  #    when it drains the notification queue.
  - id: tech-monitor
    command: >
      node -e 'const {execFileSync}=require("child_process");
        const cfg=JSON.parse(process.env.CFG||"{}");
        const key=process.env.PERPLEXITY_API_KEY||"";
        const enabled=!!cfg.enabled;
        const urls=cfg.tracked_urls||[];
        const q=(urls.join(" "))+" changelog release notes new version documentation update";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]});
        let raw="{}";
        if(enabled&&key){try{raw=execFileSync("curl",["-sS","https://api.perplexity.ai/chat/completions",
          "-H","Authorization: Bearer "+key,"-H","Content-Type: application/json","--data-binary","@-"],
          {input:body,encoding:"utf8",timeout:60000});}catch(e){raw="{}";}}
        let d={};try{d=JSON.parse(raw);}catch(e){d={};}
        const c=(d.choices&&d.choices[0]&&d.choices[0].message)?d.choices[0].message.content:"";
        const cit=d.citations||[];
        process.stdout.write(JSON.stringify({vertical:"technology",enabled,content:c,citations:cit,high_value_urls:cit.slice(0,10),total:cit.length,auth_ok:!!key,
          tracked_urls:urls,needs_agent_browser:enabled&&urls.length>0,
          notice:(enabled&&urls.length>0)?"Full-page diff of tracked_urls needs agent-browser — connect it for exact page-level change detection.":""}));'
    env:
      CFG: $config.json.technology

  # 5. Legal/regulatory — tracked regulation updates. Always runs; skips in-script when disabled.
  - id: legal-monitor
    command: >
      node -e 'const {execFileSync}=require("child_process");
        const cfg=JSON.parse(process.env.CFG||"{}");
        const key=process.env.PERPLEXITY_API_KEY||"";
        const enabled=!!cfg.enabled;
        const q=(cfg.tracked_regulations||"")+" update revision amendment Federal Register";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]});
        let raw="{}";
        if(enabled&&key){try{raw=execFileSync("curl",["-sS","https://api.perplexity.ai/chat/completions",
          "-H","Authorization: Bearer "+key,"-H","Content-Type: application/json","--data-binary","@-"],
          {input:body,encoding:"utf8",timeout:60000});}catch(e){raw="{}";}}
        let d={};try{d=JSON.parse(raw);}catch(e){d={};}
        const c=(d.choices&&d.choices[0]&&d.choices[0].message)?d.choices[0].message.content:"";
        const urls=d.citations||[];
        process.stdout.write(JSON.stringify({vertical:"legal",enabled,content:c,citations:urls,high_value_urls:urls.slice(0,10),total:urls.length,auth_ok:!!key}));'
    env:
      CFG: $config.json.legal

  # 6. Competitive — tracked-company announcements / launches. Always runs; skips in-script when disabled.
  - id: competitive-monitor
    command: >
      node -e 'const {execFileSync}=require("child_process");
        const cfg=JSON.parse(process.env.CFG||"{}");
        const key=process.env.PERPLEXITY_API_KEY||"";
        const enabled=!!cfg.enabled;
        const q=(cfg.tracked_companies||"")+" announcement launch release funding partnership";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]});
        let raw="{}";
        if(enabled&&key){try{raw=execFileSync("curl",["-sS","https://api.perplexity.ai/chat/completions",
          "-H","Authorization: Bearer "+key,"-H","Content-Type: application/json","--data-binary","@-"],
          {input:body,encoding:"utf8",timeout:60000});}catch(e){raw="{}";}}
        let d={};try{d=JSON.parse(raw);}catch(e){d={};}
        const c=(d.choices&&d.choices[0]&&d.choices[0].message)?d.choices[0].message.content:"";
        const urls=d.citations||[];
        process.stdout.write(JSON.stringify({vertical:"competitive",enabled,content:c,citations:urls,high_value_urls:urls.slice(0,10),total:urls.length,auth_ok:!!key}));'
    env:
      CFG: $config.json.competitive

  # 7. Load the stored baseline (previous sweep snapshot).
  - id: load-previous
    pipeline: state.get lens-monitoring/previous-state

  # 8. Assemble the describer input. The current sweep's text-per-vertical is
  #    concatenated content; the baseline's text is pulled from the same shape.
  #    source_type stays "unstructured" so the describer runs its word-level diff
  #    (it sets needs_llm_summary for the semantic pass we queue, not run, below).
  - id: describe-input
    command: >
      node -e 'const cur={
          financial:JSON.parse(process.env.FIN||"null"),
          academic:JSON.parse(process.env.ACA||"null"),
          technology:JSON.parse(process.env.TECH||"null"),
          legal:JSON.parse(process.env.LEGAL||"null"),
          competitive:JSON.parse(process.env.COMP||"null")};
        const prev=JSON.parse(process.env.PREV||"null")||{};
        const txt=(o)=>o&&o.content?o.content:"";
        const verts=["financial","academic","technology","legal","competitive"];
        const cur_text=verts.map(v=>txt(cur[v])).filter(Boolean).join("\n");
        const prev_text=verts.map(v=>txt((prev[v])||null)).filter(Boolean).join("\n");
        process.stdout.write(JSON.stringify({source_type:"unstructured",source_url:"lens-monitoring-sweep",
          previous_text:prev_text,current_text:cur_text}));'
    env:
      FIN: $financial-monitor.stdout
      ACA: $academic-monitor.stdout
      TECH: $tech-monitor.stdout
      LEGAL: $legal-monitor.stdout
      COMP: $competitive-monitor.stdout
      PREV: $load-previous.stdout

  # 9. Deterministic change diff. Script exits 0 and carries has_changes /
  #    needs_llm_summary in JSON; we branch on those, never on .length.
  - id: describe-changes
    command: node lens-change-describer.js
    stdin: $describe-input.stdout

  # 10. Queue the meaningful change as a notification for the agent loop.
  #     The semantic LLM summary (llm-task, 401 here) and the Slack post
  #     (#lens-research, 401 here) BOTH happen in the loop when it drains this
  #     queue. We hand it everything: the diff stats + the raw current/prev text.
  - id: notify-load
    condition: $describe-changes.json.has_changes
    pipeline: state.get lens/notifications

  - id: notify-next
    condition: $describe-changes.json.has_changes
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const diff=JSON.parse(process.env.DIFF||"{}");
        const din=JSON.parse(process.env.DESC_IN||"{}");
        const tech=JSON.parse(process.env.TECH||"null")||{};
        n.push({kind:"source_change",channel:"#lens-research",
          needs_llm_summary:!!diff.needs_llm_summary,
          llm_prompt:"Describe specifically what changed in these monitored sources. Name the section, the number, the policy. Focus on changes that affect the accuracy of existing Lens research findings.",
          diff_stats:diff.diff_stats||null,
          current_text:din.current_text||"",
          previous_text:din.previous_text||"",
          tech_connect_notice:tech.notice||"",
          alert_text:"SOURCE CHANGE DETECTED — monitored sources moved. This may affect previously verified findings. Review recommended.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      DIFF: $describe-changes.stdout
      DESC_IN: $describe-input.stdout
      TECH: $tech-monitor.stdout

  - id: notify
    condition: $describe-changes.json.has_changes
    pipeline: state.set lens/notifications
    stdin: $notify-next.stdout

  # 11. Store the current sweep as the new baseline (next run diffs against it).
  - id: baseline-next
    command: >
      node -e 'const out={
          financial:JSON.parse(process.env.FIN||"null"),
          academic:JSON.parse(process.env.ACA||"null"),
          technology:JSON.parse(process.env.TECH||"null"),
          legal:JSON.parse(process.env.LEGAL||"null"),
          competitive:JSON.parse(process.env.COMP||"null"),
          captured_at:new Date().toISOString()};
        process.stdout.write(JSON.stringify(out));'
    env:
      FIN: $financial-monitor.stdout
      ACA: $academic-monitor.stdout
      TECH: $tech-monitor.stdout
      LEGAL: $legal-monitor.stdout
      COMP: $competitive-monitor.stdout

  - id: store-current
    pipeline: state.set lens-monitoring/previous-state
    stdin: $baseline-next.stdout

  # 12. Append an audit record to the single-key audit index array.
  - id: audit-load
    pipeline: state.get lens/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const diff=JSON.parse(process.env.DIFF||"{}");
        const cfg=JSON.parse(process.env.CFG||"{}");
        const enabled=Object.keys(cfg).filter(k=>cfg[k]&&cfg[k].enabled);
        log.push({action:"monitoring_sweep",changes_detected:!!diff.has_changes,
          verticals_monitored:enabled,authorized_by:"autonomous",
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log.slice(-500)));'
    env:
      LOG: $audit-load.stdout
      DIFF: $describe-changes.stdout
      CFG: $config.stdout

  - id: audit
    pipeline: state.set lens/audit-log
    stdin: $audit-next.stdout
