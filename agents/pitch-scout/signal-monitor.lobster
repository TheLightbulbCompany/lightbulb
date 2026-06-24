name: signal-monitor
# Continuous buying-signal monitoring for a single active brief (Reqs 19-28, 47).
# Runs every 4h on cron; the agent loop (or daily-source) invokes it once PER active
# brief with that brief's id, so the embedded runner stays loop-free + deterministic.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount. Briefs, the per-brief
#    signal snapshots, the signal history (for source-health), and the agent-delivery
#    inbox are all single-key index arrays/objects (state.* has no prefix-list); a node
#    step reads, edits, and re-writes each.
#  - Perplexity is the deterministic primary search engine, called via plain `curl` with
#    $PERPLEXITY_API_KEY; the optional enterprise sources (Apollo funding, BuiltWith,
#    Bombora, 6sense) are click-to-connect — each external call is wrapped in a node
#    try/catch so a missing key / unconnected source is a no-op (empty shaped result,
#    exit 0), never a hard failure.
#  - Job-posting classification is the deterministic scout-job-signal-classifier.js; the
#    ~30% it can't classify are handed to the agent loop via a `scout/notifications`
#    intent (the embedded pipeline CANNOT call llm-task — 401).
#  - Slack is unreachable from an embedded pipeline (401), so source-health alerts are
#    written to `scout/notifications` for the agent to deliver. NO openclaw.invoke / slack.
#  - Scripts/inline node always exit 0 and carry status in JSON; steps branch via condition.

args:
  brief_id:
    required: true        # which active brief to monitor signals for

steps:
  # 1. Load the active-briefs index array (single key; written by brief management).
  - id: briefs-load
    pipeline: state.get scout/briefs

  # 2. Select this run's brief by id and derive every query term the sweeps need.
  #    Gate on `found` (not .length — silently false in conditions).
  - id: brief
    command: >
      node -e 'const all=JSON.parse(process.env.BRIEFS||"null")||[];
        const id=process.env.LOBSTER_ARG_BRIEF_ID||"";
        const b=all.find(x=>x&&(x.brief_id===id||x.id===id))||null;
        const ok=!!b && (b.status==="active") && (b.confirmed_by_user!==false);
        const j=(a)=>Array.isArray(a)?a.join(" "):(a||"");
        process.stdout.write(JSON.stringify({
          found: ok,
          brief_id: id,
          vertical: (b&&b.vertical)||"",
          configured_databases: (b&&b.configured_databases)||[],
          icp_keywords: (b&&j(b.icp_keywords))||"",
          icp_domains: (b&&j(b.icp_domains))||"",
          icp_topics: (b&&j(b.icp_topics))||"",
          pain_keywords: (b&&(j(b.icp_pain_keywords)||j(b.pain_keywords)))||"",
          competitor_names: (b&&j(b.competitor_names))||""
        }));'
    env:
      BRIEFS: $briefs-load.stdout

  # 3. Vertical routing — which database/signal stack applies (deterministic lookup).
  #    The router reads {vertical, configured_databases} from stdin.
  - id: route-input
    condition: $brief.json.found
    command: >
      node -e 'const b=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({vertical:b.vertical||"technology",
          configured_databases:b.configured_databases||[]}));'
    stdin: $brief.stdout

  - id: route-verticals
    condition: $brief.json.found
    command: node scout-vertical-router.js
    stdin: $route-input.stdout

  # 4. Funding signals — Apollo recent-funding (optional/click-to-connect). Wrapped so a
  #    missing APOLLO_API_KEY or unconnected source returns []  (exit 0, never aborts).
  - id: funding-signals
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const kw=process.env.KW||""; const key=process.env.APOLLO_API_KEY||"";
        let out={signals:[],source:"apollo_funding",connected:!!key};
        if (key) { try {
          const body=JSON.stringify({q_keywords:kw, organization_latest_funding_stage_cd:["seed","series_a","series_b"], per_page:20});
          const raw=execSync("curl -sS https://api.apollo.io/v1/mixed_companies/search "
            +"-H \"Content-Type: application/json\" -H \"Cache-Control: no-cache\" "
            +"-H \"X-Api-Key: "+key+"\" -d "+JSON.stringify(body),
            {encoding:"utf8",timeout:45000});
          const d=JSON.parse(raw); const orgs=(d.organizations||d.accounts||[]);
          out.signals=orgs.map(o=>({company:o.name||"", domain:(o.primary_domain||o.website_url||"").replace(/^https?:\/\//,"").replace(/\/.*$/,"").toLowerCase(),
            funding_stage:o.latest_funding_stage||"", funding:o.total_funding||null}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      KW: $brief.json.icp_keywords

  # 5. Regulatory filings — SEC EDGAR full-text search (free public API, no key).
  - id: sec-signals
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const kw=encodeURIComponent(process.env.KW||"");
        let out={signals:[],source:"sec_edgar"};
        if (kw) { try {
          const url="https://efts.sec.gov/LATEST/search-index?q="+kw+"&forms=8-K,S-1,13F";
          const raw=execSync("curl -sS -A \"isol8-scout signal-monitor\" "+JSON.stringify(url),
            {encoding:"utf8",timeout:45000});
          const d=JSON.parse(raw); const hits=((d.hits||{}).hits)||[];
          out.signals=hits.map(h=>{const s=h._source||{};
            return {company:(s.display_names||[])[0]||"", form:s.root_forms?s.root_forms[0]:(s.form||""),
              filed:s.file_date||"", accession:h._id||""};});
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      KW: $brief.json.icp_keywords

  # 6. Funding news sweep — Perplexity (deterministic; supplements Apollo + SEC).
  - id: perplexity-funding
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const kw=process.env.KW||""; const key=process.env.PERPLEXITY_API_KEY||"";
        let out={signals:[],source:"funding_news"};
        if (key && kw) { try {
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",
            content:"Recent funding rounds and investments for companies matching: "+kw+". Last 24 hours."}]});
          const raw=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer "+key+"\" -H \"Content-Type: application/json\" -d "+JSON.stringify(body),
            {encoding:"utf8",timeout:60000});
          const d=JSON.parse(raw); const c=(((d.choices||[])[0]||{}).message||{}).content||"";
          const urls=d.citations||[];
          out.signals=urls.map(u=>({url:u,content:c}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      KW: $brief.json.icp_keywords

  # 7. News / PR signals — Perplexity (acquisitions, leadership changes, launches, regulatory).
  - id: news-signals
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const kw=process.env.KW||""; const key=process.env.PERPLEXITY_API_KEY||"";
        let out={signals:[],source:"news_trigger"};
        if (key && kw) { try {
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",
            content:kw+" acquisition OR leadership change OR product launch OR regulatory. Last 24 hours news."}]});
          const raw=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer "+key+"\" -H \"Content-Type: application/json\" -d "+JSON.stringify(body),
            {encoding:"utf8",timeout:60000});
          const d=JSON.parse(raw); const c=(((d.choices||[])[0]||{}).message||{}).content||"";
          const urls=d.citations||[];
          out.signals=urls.map(u=>({url:u,content:c}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      KW: $brief.json.icp_keywords

  # 8. Job-posting signals — Perplexity discovery, then deterministic classification.
  - id: job-postings
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const kw=process.env.KW||""; const key=process.env.PERPLEXITY_API_KEY||"";
        let postings=[];
        if (key && kw) { try {
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",
            content:kw+" hiring OR job posting OR careers. Recent postings with company name and role title."}]});
          const raw=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer "+key+"\" -H \"Content-Type: application/json\" -d "+JSON.stringify(body),
            {encoding:"utf8",timeout:60000});
          const d=JSON.parse(raw); const c=(((d.choices||[])[0]||{}).message||{}).content||"";
          const urls=d.citations||[];
          postings=urls.map(u=>{ let host=""; try{host=new URL(u).hostname.replace(/^www\./,"");}catch(e){}
            return {title:c.slice(0,200), description:c, company:host, domain:host, url:u}; });
        } catch(e){} }
        process.stdout.write(JSON.stringify({postings, count:postings.length}));'
    env:
      KW: $brief.json.icp_keywords

  # 8b. Deterministic keyword classification (~70%). Carries needs_llm[] for the agent loop.
  - id: classify-jobs
    condition: $brief.json.found
    command: node scout-job-signal-classifier.js
    stdin: $job-postings.stdout

  # 9. Technographic signals — BuiltWith (optional/click-to-connect; wrapped no-op).
  - id: tech-signals
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const dom=(process.env.DOMAINS||"").split(/\s+/).filter(Boolean)[0]||"";
        const key=process.env.BUILTWITH_API_KEY||"";
        let out={signals:[],source:"tech_stack_change",connected:!!key};
        if (key && dom) { try {
          const url="https://api.builtwith.com/v21/api.json?KEY="+key+"&LOOKUP="+encodeURIComponent(dom);
          const raw=execSync("curl -sS "+JSON.stringify(url),{encoding:"utf8",timeout:45000});
          const d=JSON.parse(raw); const results=(d.Results||[]);
          out.signals=results.map(r=>({domain:dom,
            technologies:((((r.Result||{}).Paths||[])[0]||{}).Technologies||[]).map(t=>t.Name).slice(0,25)}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      DOMAINS: $brief.json.icp_domains

  # 10. Intent data — Bombora surge (optional/click-to-connect; wrapped no-op).
  - id: bombora-intent
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const topics=encodeURIComponent(process.env.TOPICS||""); const key=process.env.BOMBORA_API_KEY||"";
        let out={signals:[],source:"bombora_intent",connected:!!key};
        if (key && topics) { try {
          const url="https://api.bombora.com/v3/surge?topics="+topics+"&limit=20";
          const raw=execSync("curl -sS "+JSON.stringify(url)+" -H \"Authorization: Bearer "+key+"\"",
            {encoding:"utf8",timeout:45000});
          const d=JSON.parse(raw); const items=(d.data||d.results||d.surge||[]);
          out.signals=(Array.isArray(items)?items:[]).map(s=>({company:s.company||s.domain||"",
            domain:(s.domain||"").toLowerCase(), topic:s.topic||"", surge_score:s.surge_score||s.score||null}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      TOPICS: $brief.json.icp_topics

  # 11. Intent data — 6sense (optional/click-to-connect; wrapped no-op).
  - id: sixsense-intent
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const kw=encodeURIComponent(process.env.KW||""); const key=process.env.SIXSENSE_API_KEY||"";
        let out={signals:[],source:"6sense_intent",connected:!!key};
        if (key && kw) { try {
          const url="https://api.6sense.com/v3/company/insights?keywords="+kw+"&limit=20";
          const raw=execSync("curl -sS "+JSON.stringify(url)+" -H \"Authorization: Bearer "+key+"\"",
            {encoding:"utf8",timeout:45000});
          const d=JSON.parse(raw); const items=(d.companies||d.data||d.results||[]);
          out.signals=(Array.isArray(items)?items:[]).map(s=>({company:s.name||s.company||"",
            domain:(s.domain||"").toLowerCase(), buying_stage:s.buying_stage||s.stage||""}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      KW: $brief.json.icp_keywords

  # 12. Community / pain signals — Reddit + HN via a date-windowed Perplexity sweep
  #     (last30days is fictional; a windowed search is the contract-approved substitute).
  - id: community-signals
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const pain=process.env.PAIN||""; const key=process.env.PERPLEXITY_API_KEY||"";
        let out={signals:[],source:"reddit_pain"};
        if (key && pain) { try {
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",
            content:pain+" site:reddit.com OR site:news.ycombinator.com. Last 24 hours discussions and complaints."}]});
          const raw=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer "+key+"\" -H \"Content-Type: application/json\" -d "+JSON.stringify(body),
            {encoding:"utf8",timeout:60000});
          const d=JSON.parse(raw); const c=(((d.choices||[])[0]||{}).message||{}).content||"";
          out.signals=(d.citations||[]).map(u=>({url:u,content:c}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      PAIN: $brief.json.pain_keywords

  # 13. Competitor-displacement / review-site signals — Perplexity over G2/Capterra/TrustRadius.
  - id: review-signals
    condition: $brief.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const comp=process.env.COMP||""; const key=process.env.PERPLEXITY_API_KEY||"";
        let out={signals:[],source:"competitor_displacement"};
        if (key && comp) { try {
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",
            content:comp+" review frustrated switching alternative site:g2.com OR site:capterra.com OR site:trustradius.com"}]});
          const raw=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer "+key+"\" -H \"Content-Type: application/json\" -d "+JSON.stringify(body),
            {encoding:"utf8",timeout:60000});
          const d=JSON.parse(raw); const c=(((d.choices||[])[0]||{}).message||{}).content||"";
          out.signals=(d.citations||[]).map(u=>({url:u,content:c}));
        } catch(e){ out.error=String(e.message||e); } }
        process.stdout.write(JSON.stringify(out));'
    env:
      COMP: $brief.json.competitor_names

  # 14. Merge every source's signals with a source tag + timestamp; emit an explicit count.
  - id: merge-signals
    condition: $brief.json.found
    command: >
      node -e 'const ts=new Date().toISOString();
        const pull=(env,type)=>{ const o=JSON.parse(process.env[env]||"null")||{};
          return (o.signals||[]).map(s=>Object.assign({},s,{signal_source:s.signal_source||type,timestamp:s.timestamp||ts})); };
        const cls=JSON.parse(process.env.JOBS||"null")||{};
        const jobsClassified=(cls.classified||[]).map(s=>Object.assign({},s,{signal_source:"job_posting_intent",timestamp:ts}));
        const signals=[].concat(
          pull("FUNDING","funding_signal"),
          pull("SEC","regulatory_filing"),
          pull("PXFUND","funding_news"),
          pull("NEWS","news_trigger"),
          jobsClassified,
          pull("TECH","tech_stack_change"),
          pull("BOMBORA","bombora_intent"),
          pull("SIXSENSE","6sense_intent"),
          pull("COMMUNITY","reddit_pain"),
          pull("REVIEWS","competitor_displacement"));
        process.stdout.write(JSON.stringify({
          brief_id:process.env.LOBSTER_ARG_BRIEF_ID||"", signals, count:signals.length,
          jobs_needs_llm:(cls.needs_llm||[]), jobs_needs_llm_count:cls.needs_llm_count||0,
          date:ts.slice(0,10), timestamp:ts}));'
    env:
      FUNDING: $funding-signals.stdout
      SEC: $sec-signals.stdout
      PXFUND: $perplexity-funding.stdout
      NEWS: $news-signals.stdout
      JOBS: $classify-jobs.stdout
      TECH: $tech-signals.stdout
      BOMBORA: $bombora-intent.stdout
      SIXSENSE: $sixsense-intent.stdout
      COMMUNITY: $community-signals.stdout
      REVIEWS: $review-signals.stdout

  # 15. Persist this brief's signal snapshot into the per-brief signals index (single key).
  #     Replace any prior same-day entry for this brief, keep the last 60 snapshots.
  - id: signals-load
    condition: $brief.json.found
    pipeline: state.get scout/signals

  - id: signals-next
    condition: $brief.json.found
    command: >
      node -e 'const arr=JSON.parse(process.env.PRIOR||"null")||[];
        const m=JSON.parse(process.env.MERGED||"{}");
        const key=(m.brief_id||"")+"|"+(m.date||"");
        const kept=arr.filter(e=>((e.brief_id||"")+"|"+(e.date||""))!==key);
        kept.push({brief_id:m.brief_id, date:m.date, count:m.count, signals:m.signals, timestamp:m.timestamp});
        process.stdout.write(JSON.stringify(kept.slice(-60)));'
    env:
      PRIOR: $signals-load.stdout
      MERGED: $merge-signals.stdout

  - id: signals-save
    condition: $brief.json.found
    pipeline: state.set scout/signals
    stdin: $signals-next.stdout

  # 16. Append today's detected signals to the rolling signal-history (for source health).
  - id: history-load
    condition: $brief.json.found
    pipeline: state.get scout/signal-history

  - id: history-next
    condition: $brief.json.found
    command: >
      node -e 'const hist=JSON.parse(process.env.HIST||"null")||[];
        const m=JSON.parse(process.env.MERGED||"{}");
        for (const s of (m.signals||[])) hist.push({source:s.signal_source, timestamp:s.timestamp});
        const cutoff=Date.now()-30*24*60*60*1000;
        const pruned=hist.filter(h=>{const t=Date.parse(h.timestamp||"");return isNaN(t)?true:t>=cutoff;});
        process.stdout.write(JSON.stringify(pruned.slice(-2000)));'
    env:
      HIST: $history-load.stdout
      MERGED: $merge-signals.stdout

  - id: history-save
    condition: $brief.json.found
    pipeline: state.set scout/signal-history
    stdin: $history-next.stdout

  # 17. Source-health check (Req 47) — flag any configured source silent >=14 days.
  #     The configured sources come from the vertical router's signal_sources map.
  - id: health-input
    condition: $brief.json.found
    command: >
      node -e 'const route=JSON.parse(process.env.ROUTE||"{}");
        const hist=JSON.parse(process.env.HIST||"null")||[];
        const ss=route.signal_sources||{}; const set={};
        for (const k of Object.keys(ss)) for (const s of (ss[k]||[])) set[s]=true;
        process.stdout.write(JSON.stringify({signal_history:hist,
          configured_sources:Object.keys(set)}));'
    env:
      ROUTE: $route-verticals.stdout
      HIST: $history-next.stdout

  - id: source-health
    condition: $brief.json.found
    command: node scout-source-health-check.js
    stdin: $health-input.stdout

  # 18. If any source is silent, queue a Slack-delivery intent for the agent loop
  #     (the embedded pipeline can't reach Slack). Gate on the script's explicit flag.
  - id: alert-load
    condition: $source-health.json.has_alerts
    pipeline: state.get scout/notifications

  - id: alert-next
    condition: $source-health.json.has_alerts
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const h=JSON.parse(process.env.HEALTH||"{}");
        n.push({kind:"source_health_alert", deliver_to:"#scout-alerts",
          brief_id:process.env.LOBSTER_ARG_BRIEF_ID||"",
          silent_sources:h.silent_sources||0, alerts:h.alerts||[],
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $alert-load.stdout
      HEALTH: $source-health.stdout

  - id: alert-save
    condition: $source-health.json.has_alerts
    pipeline: state.set scout/notifications
    stdin: $alert-next.stdout

  # 19. If any job postings need model judgment, queue a classification intent for the
  #     agent loop (llm-task is unreachable here). Gate on the explicit count.
  - id: jobs-llm-load
    condition: $merge-signals.json.jobs_needs_llm_count > 0
    pipeline: state.get scout/notifications

  - id: jobs-llm-next
    condition: $merge-signals.json.jobs_needs_llm_count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const m=JSON.parse(process.env.MERGED||"{}");
        n.push({kind:"classify_job_postings", needs_llm:true,
          brief_id:m.brief_id||"", postings:m.jobs_needs_llm||[],
          count:m.jobs_needs_llm_count||0, created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $jobs-llm-load.stdout
      MERGED: $merge-signals.stdout

  - id: jobs-llm-save
    condition: $merge-signals.json.jobs_needs_llm_count > 0
    pipeline: state.set scout/notifications
    stdin: $jobs-llm-next.stdout

  # 20. Audit log — append a run record to the durable audit index (single key).
  - id: audit-load
    condition: $brief.json.found
    pipeline: state.get scout/audit-log

  - id: audit-next
    condition: $brief.json.found
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const m=JSON.parse(process.env.MERGED||"{}"); const h=JSON.parse(process.env.HEALTH||"{}");
        log.push({action:"signal_monitor", brief_id:m.brief_id||"",
          signals_detected:m.count||0,
          sources_queried:["apollo_funding","sec_edgar","perplexity","builtwith","bombora","6sense","reddit_hn"],
          silent_sources:h.silent_sources||0,
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log.slice(-1000)));'
    env:
      LOG: $audit-load.stdout
      MERGED: $merge-signals.stdout
      HEALTH: $source-health.stdout

  - id: audit-save
    condition: $brief.json.found
    pipeline: state.set scout/audit-log
    stdin: $audit-next.stdout
