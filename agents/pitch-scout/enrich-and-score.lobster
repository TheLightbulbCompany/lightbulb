name: enrich-and-score
# Waterfall enrichment, compliance, ICP scoring, dossier assembly for one lead.
# Deterministic; produces {dossier, scoring} that the Scout agent then chains into
# deposit-lead. Triggered per lead after signal monitoring qualifies them.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    The enrichment cache, briefs, notifications, and audit log are single-key index
#    arrays/objects (state.* has no prefix-list), edited in a node step then re-written.
#  - `fast-io` is fictional — never call it. `openclaw.invoke` (and so slack/apollo/
#    zoominfo/llm-task as tools) 401s from the embedded runner — never call it.
#  - Perplexity is a real direct-curl API ($PERPLEXITY_API_KEY at the process env).
#  - Apollo / ZoomInfo contact enrichment is gateway-bound (no creds in the embedded
#    runner), so the agent loop fetches those and passes them in via the
#    `enrichment_results` arg; this pipeline merges whatever it is given and the
#    waterfall flags any unfilled fields rather than guessing.
#  - The LLM outreach-angle (only ~30% of leads) is left for the agent loop; the
#    dossier-builder emits `outreach_angle_needs_llm` for it to act on.
#  - Match-rate / source-health warnings can't post to Slack here, so they are written
#    to a `scout/notifications` queue for the agent to deliver.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  lead_domain:
    required: true
  signal_data:
    required: true          # the trigger signal (JSON object)
  brief_id:
    required: true          # which active brief / ICP this lead is sourced against
  enrichment_results:
    required: false         # JSON: { database_results: [{source,type,data}], stakeholders: [], target_role }
                            # supplied by the agent loop after it runs Apollo/ZoomInfo (gateway-bound);
                            # defaults to empty -> the waterfall flags unfilled fields.

steps:
  # 1. Load the active briefs (single-key index array) and select this one for ICP/vertical.
  - id: briefs
    pipeline: state.get scout/briefs

  - id: brief
    command: >
      node -e 'const all=JSON.parse(process.env.BRIEFS||"null")||[];
        const id=process.env.LOBSTER_ARG_BRIEF_ID||"";
        const b=all.find(x=>String(x.id||x.brief_id)===id)||{};
        const icp=b.icp||{}; const vertical=b.vertical||icp.vertical||"technology";
        process.stdout.write(JSON.stringify({found:!!(b&&(b.id||b.brief_id)), brief:b, icp,
          vertical, target_roles:icp.target_roles||b.target_roles||[],
          configured_databases:b.configured_databases||icp.configured_databases||[]}));'
    env:
      BRIEFS: $briefs.stdout

  # 2. Vertical router -> database stack for this brief's vertical.
  - id: router-input
    command: >
      node -e 'const b=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({vertical:b.vertical||"technology",
          configured_databases:b.configured_databases||[]}));'
    stdin: $brief.stdout

  - id: get-stack
    command: node scout-vertical-router.js
    stdin: $router-input.stdout

  # 3. Enrichment cache (single-key map keyed by domain). state.* can't slash-namespace,
  #    so the whole cache lives under one key as { "<domain>": <profile> }.
  - id: cache-map
    pipeline: state.get scout/enrichment-cache

  - id: cache-check
    command: >
      node -e 'const {execSync}=require("child_process");
        const map=JSON.parse(process.env.CACHEMAP||"null")||{};
        const domain=process.env.LOBSTER_ARG_LEAD_DOMAIN||"";
        const cached=map[domain]||null;
        let out=JSON.stringify({cache_hit:false,domain,action:"full_enrichment"});
        try{ out=execSync("node scout-enrichment-cache.js",{encoding:"utf8",
          input:JSON.stringify({domain,cached_profile:cached})}); }catch(e){}
        process.stdout.write(out);'
    env:
      CACHEMAP: $cache-map.stdout

  # 4. Recent news via Perplexity (real direct-curl API; deterministic structured response).
  - id: recent-news
    on_error: continue
    command: >
      curl -sS https://api.perplexity.ai/chat/completions
      -H "Authorization: Bearer $PERPLEXITY_API_KEY"
      -H "Content-Type: application/json"
      -d "{\"model\":\"sonar\",\"messages\":[{\"role\":\"user\",\"content\":\"Recent news about $LOBSTER_ARG_LEAD_DOMAIN in the last 30 days\"}]}"
      | node -e 'let raw=""; try{raw=require("fs").readFileSync(0,"utf8");}catch(e){raw="";}
        let d={}; try{d=JSON.parse(raw);}catch(e){d={};}
        const c=((d.choices||[])[0]||{}).message&&d.choices[0].message.content||"";
        const urls=d.citations||[];
        process.stdout.write(JSON.stringify({results:urls.map(u=>({url:u,content:c,source:"perplexity"})),total:urls.length}));'

  # 5. Assemble the waterfall input. Merge the agent-supplied database_results
  #    (Apollo/ZoomInfo, gateway-bound) with the cached profile + Perplexity news.
  - id: waterfall-input
    command: >
      node -e 'const enr=JSON.parse(process.env.ENRICH||"null")||{};
        const cacheC=JSON.parse(process.env.CACHECHECK||"{}");
        const news=JSON.parse(process.env.NEWS||"{}");
        const brief=JSON.parse(process.env.BRIEF||"{}");
        const stack=JSON.parse(process.env.STACK||"{}");
        const domain=(process.env.LOBSTER_ARG_LEAD_DOMAIN||"").toLowerCase();
        const dbResults=(enr.database_results||[]).slice();
        if (news&&news.total>0) dbResults.push({source:"perplexity",type:"direct_api",
          data:{recent_news:(news.results||[]).map(r=>r.content).filter(Boolean).slice(0,1)[0]||null}});
        const targetRole=(enr.target_role)||(brief.target_roles||[])[0]||"";
        const dbNames=((stack.available_databases)||stack.databases||[]).map(d=>d.name);
        process.stdout.write(JSON.stringify({
          lead:{domain},
          database_stack:dbNames,
          target_role:targetRole,
          cached_profile:cacheC.cache_hit?cacheC.cached_profile:null,
          database_results:dbResults}));'
    env:
      ENRICH: $enrichment_results
      CACHECHECK: $cache-check.stdout
      NEWS: $recent-news.stdout
      BRIEF: $brief.stdout
      STACK: $get-stack.stdout

  # 6. Waterfall merge with field-level source tracking.
  - id: merge-enrichment
    command: node scout-enrichment-waterfall.js
    stdin: $waterfall-input.stdout

  # 7. Compliance — business email only, high-restriction region flagging.
  - id: compliance-input
    command: >
      node -e 'const m=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const p=m.enriched_profile||{};
        process.stdout.write(JSON.stringify({lead:{email:p.email||"",
          geography:p.geography||p.country||"",domain:p.company_domain||process.env.LOBSTER_ARG_LEAD_DOMAIN}}));'
    stdin: $merge-enrichment.stdout

  - id: compliance-check
    command: node scout-compliance-check.js
    stdin: $compliance-input.stdout

  # 8. ICP scoring — deterministic weighted math. Build {lead, icp, config} for the scorer.
  - id: score-input
    command: >
      node -e 'const m=JSON.parse(process.env.MERGED||"{}");
        const brief=JSON.parse(process.env.BRIEF||"{}");
        const sig=JSON.parse(process.env.SIGNAL||"{}");
        const p=m.enriched_profile||{};
        const lead={domain:(process.env.LOBSTER_ARG_LEAD_DOMAIN||p.company_domain||"").toLowerCase(),
          company_size:p.company_size,funding_stage:p.funding_stage,geography:p.geography||p.country,
          industry:p.industry,tech_stack:p.tech_stack||[],title:p.title,
          signal_strength:sig.signal_strength||sig.strength,
          bombora_surge_score:sig.bombora_surge_score,sixsense_buying_stage:sig.sixsense_buying_stage,
          signal_timestamp:sig.timestamp||sig.date,signal_source:sig.source||sig.signal_source};
        process.stdout.write(JSON.stringify({lead,icp:brief.icp||{},config:brief.scoring_config||{}}));'
    env:
      MERGED: $merge-enrichment.stdout
      BRIEF: $brief.stdout
      SIGNAL: $signal_data

  - id: icp-score
    command: node scout-icp-scorer.js
    stdin: $score-input.stdout

  # 9. Match-rate monitor (per-cycle email coverage). Single lead here -> emit a 1-lead stat.
  - id: match-input
    command: >
      node -e 'const m=JSON.parse(process.env.MERGED||"{}");
        const stack=JSON.parse(process.env.STACK||"{}");
        const brief=JSON.parse(process.env.BRIEF||"{}");
        const p=m.enriched_profile||{}; const srcs=m.data_sources||{};
        const found=p.email?1:0; const verified=(found&&srcs.email)?1:0;
        process.stdout.write(JSON.stringify({enrichment_stats:{total_enriched:1,
          emails_found:found,emails_verified:verified,vertical:brief.vertical||"unknown",
          database_stack:((stack.available_databases)||stack.databases||[]).map(d=>d.name)}}));'
    env:
      MERGED: $merge-enrichment.stdout
      STACK: $get-stack.stdout
      BRIEF: $brief.stdout

  - id: match-rate
    command: node scout-match-rate-monitor.js
    stdin: $match-input.stdout

  # 9b. Match-rate warning -> notifications queue (Slack is unreachable from the pipeline).
  - id: match-notify-load
    condition: $match-rate.json.pass == false
    pipeline: state.get scout/notifications

  - id: match-notify-next
    condition: $match-rate.json.pass == false
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const mr=JSON.parse(process.env.MR||"{}");
        n.push({kind:"match_rate_alert",urgent:false,
          domain:process.env.LOBSTER_ARG_LEAD_DOMAIN,match_rate:mr.match_rate,
          diagnosis:(mr.diagnosis&&mr.diagnosis.message)||"Email match rate below threshold",
          recommendation:(mr.diagnosis&&mr.diagnosis.recommendation)||"",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $match-notify-load.stdout
      MR: $match-rate.stdout

  - id: match-notify
    condition: $match-rate.json.pass == false
    pipeline: state.set scout/notifications
    stdin: $match-notify-next.stdout

  # 10. Build the dossier from {lead, enrichment, scoring, signal, stakeholders}.
  - id: dossier-input
    command: >
      node -e 'const m=JSON.parse(process.env.MERGED||"{}");
        const sc=JSON.parse(process.env.SCORED||"{}");
        const sig=JSON.parse(process.env.SIGNAL||"{}");
        const enr=JSON.parse(process.env.ENRICH||"null")||{};
        const scored=(sc.scored||[])[0]||{};
        const p=m.enriched_profile||{};
        const lead={domain:(process.env.LOBSTER_ARG_LEAD_DOMAIN||p.company_domain||"").toLowerCase(),
          name:p.name||null,company_name:p.company_name||null,brief_id:process.env.LOBSTER_ARG_BRIEF_ID};
        const signal={type:sig.type||sig.signal_type,source:sig.source||sig.signal_source,
          date:sig.date||sig.timestamp,timestamp:sig.timestamp,summary:sig.summary,context:sig.context,raw:sig.raw};
        process.stdout.write(JSON.stringify({lead,enrichment:p,scoring:scored,signal,
          stakeholders:enr.stakeholders||[]}));'
    env:
      MERGED: $merge-enrichment.stdout
      SCORED: $icp-score.stdout
      SIGNAL: $signal_data
      ENRICH: $enrichment_results

  - id: build-dossier
    command: node scout-dossier-builder.js
    stdin: $dossier-input.stdout

  # NOTE: the LLM outreach-angle for unclassified signals (~30% of leads) is intentionally
  # left for the agent loop. When $build-dossier.json.outreach_angle_needs_llm is true the
  # agent should generate the one-sentence angle (thinking:"low") and stitch it into the
  # dossier before chaining deposit-lead. Pipeline stays deterministic.

  # 11. Refresh the enrichment cache (single-key map keyed by domain).
  - id: cache-write-next
    command: >
      node -e 'const map=JSON.parse(process.env.CACHEMAP||"null")||{};
        const m=JSON.parse(process.env.MERGED||"{}");
        const domain=process.env.LOBSTER_ARG_LEAD_DOMAIN;
        map[domain]={enriched_profile:m.enriched_profile||{},data_sources:m.data_sources||{},
          cached_at:new Date().toISOString()};
        process.stdout.write(JSON.stringify(map));'
    env:
      CACHEMAP: $cache-map.stdout
      MERGED: $merge-enrichment.stdout

  - id: cache-write
    pipeline: state.set scout/enrichment-cache
    stdin: $cache-write-next.stdout

  # 12. Append an audit record (single-key index array).
  - id: audit-load
    pipeline: state.get scout/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const sc=JSON.parse(process.env.SCORED||"{}"); const scored=(sc.scored||[])[0]||{};
        const mr=JSON.parse(process.env.MR||"{}");
        const comp=JSON.parse(process.env.COMP||"{}");
        const dos=JSON.parse(process.env.DOSSIER||"{}");
        const cc=JSON.parse(process.env.CACHECHECK||"{}");
        log.push({action:"enrichment_and_scoring",domain:process.env.LOBSTER_ARG_LEAD_DOMAIN,
          brief_id:process.env.LOBSTER_ARG_BRIEF_ID,icp_score:scored.score,tier:scored.tier,
          match_rate:mr.match_rate,compliance_flags:(comp.results||[]).flatMap(r=>r.flags||[]),
          dossier_complete:dos.complete,cache_hit:!!cc.cache_hit,
          authorized_by:"autonomous",timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      SCORED: $icp-score.stdout
      MR: $match-rate.stdout
      COMP: $compliance-check.stdout
      DOSSIER: $build-dossier.stdout
      CACHECHECK: $cache-check.stdout

  - id: audit-log
    pipeline: state.set scout/audit-log
    stdin: $audit-next.stdout

  # 13. Final handoff payload: {dossier, scoring} for the agent to chain into deposit-lead.
  - id: result
    command: >
      node -e 'const dos=JSON.parse(process.env.DOSSIER||"{}");
        const sc=JSON.parse(process.env.SCORED||"{}"); const scored=(sc.scored||[])[0]||{};
        const comp=JSON.parse(process.env.COMP||"{}");
        process.stdout.write(JSON.stringify({dossier:dos,
          scoring:{score:scored.score||0,tier:scored.tier||"archive"},
          compliance:(comp.results||[])[0]||{pass:true},
          lead_domain:process.env.LOBSTER_ARG_LEAD_DOMAIN}));'
    env:
      DOSSIER: $build-dossier.stdout
      SCORED: $icp-score.stdout
      COMP: $compliance-check.stdout
