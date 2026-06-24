name: research-execute
# Run a verifiable research request end to end: decompose -> route -> multi-pass
# web/academic search -> corroborate -> freshness -> cross-check -> format ->
# archive. Deterministic + gateway-free; the model judgment (decomposition,
# query refinement, synthesis narrative) and Slack delivery happen in the AGENT
# LOOP, fed from a `lens/notifications` state queue this run writes.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node/curl/gog)
#    under `command:`. Perplexity + Semantic Scholar are plain HTTPS APIs hit with
#    `node` (the scripts/TOOLS.md call them deterministic, NOT llm-task).
#  - Durable state via native state.get/set on the per-owner EFS mount. Config the
#    user sets (verticals, thresholds, source overrides, format preference) is
#    read-only consumer state under `lens/config/*` and `lens/state/*`.
#  - The .js are FLAT in the agent dir (`node lens-*.js`). They `process.exit(1)`
#    on empty/garbage stdin and in their catch handler, so EVERY step that calls
#    one (a) feeds it a fully-assembled non-empty JSON body and (b) sets
#    `on_error: continue` so a script abort never kills the run. See the ".js
#    fixes needed" note in the handoff — the durable fix is to make them exit 0.
#  - LLM judgment + Slack CANNOT run in an embedded pipeline (openclaw.invoke /
#    llm-task / slack all 401). The decomposition + query plan come in via args
#    (the agent loop fills them before invoking); the synthesis narrative + the
#    plan-review + the final delivery are written to `lens/notifications` for the
#    agent loop to compose and post. No openclaw.invoke / slack / await-approval.
#  - Human approval is a real step-level `approval: required` gate placed BEFORE
#    the (cost-bearing) search side effects, NOT a fictional tool call.
#
# Intent preserved from the original: decompose, route per vertical hierarchy,
# multi-pass discovery (Perplexity + Semantic Scholar) calibrated to stakes,
# corroboration + confidence tiers, freshness flags, cross-vertical contradiction
# surfacing, formatted synthesis with inline tiers + gap section + appendix,
# archive to Drive + state. Verification mechanics stay deterministic.

args:
  research_question:
    required: true
  vertical:
    default: ""
  stakes:
    default: "standard"      # casual | standard | high_stakes
  # The agent loop performs the model-side decomposition BEFORE invoking this run
  # (llm-task can't run in-pipeline) and passes the result in. Shape:
  #   [{ "id","question","vertical","target_tier","spans_tiers":[] }, ...]
  sub_questions_json:
    default: "[]"
  # Optional pre-built query plan from the agent loop. If empty, this run derives
  # one search query per sub-question from its `question` text. Shape:
  #   [{ "sub_question_id","query","vertical","target_tier" }, ...]
  query_plan_json:
    default: "[]"
  # Claims assembled by the agent loop from retrieved evidence, ready for the
  # deterministic corroboration/freshness/format scripts. Shape per
  # lens-corroboration-tracker.js: [{ id,text,vertical,sources:[{url,title,tier,
  # date,independent,...}] }, ...]. Empty => synthesis-prep is deferred to the loop.
  claims_json:
    default: "[]"

steps:
  # ---- Load consumer config FIRST (load-before-use). All read-only. ----------
  - id: load-verticals
    pipeline: state.get lens/config/verticals

  - id: load-thresholds
    pipeline: state.get lens/config/confidence-thresholds

  - id: load-freshness
    pipeline: state.get lens/config/freshness-thresholds

  - id: load-overrides
    pipeline: state.get lens/state/source-overrides

  - id: load-history
    pipeline: state.get lens/state/historical-research

  - id: load-format-pref
    pipeline: state.get lens/state/user-format-preference

  # ---- 1. Normalize the decomposition supplied by the agent loop. ------------
  # If sub_questions_json is empty we emit a single whole-question sub-question so
  # the run is still deterministic; we also flag that the loop should decompose.
  - id: decompose
    command: >
      node -e 'const q=process.env.LOBSTER_ARG_RESEARCH_QUESTION||"";
        const v=process.env.LOBSTER_ARG_VERTICAL||"";
        let subs=[]; try{subs=JSON.parse(process.env.SUBS||"[]")||[]}catch(e){subs=[]}
        let needs_loop=false;
        if(!Array.isArray(subs)||subs.length===0){
          subs=[{id:"sq1",question:q,vertical:v,target_tier:"primary",spans_tiers:[]}];
          needs_loop=true;
        }
        subs=subs.map((s,i)=>({id:s.id||("sq"+(i+1)),question:s.question||q,
          vertical:s.vertical||v,target_tier:s.target_tier||"primary",
          spans_tiers:Array.isArray(s.spans_tiers)?s.spans_tiers:[]}));
        process.stdout.write(JSON.stringify({sub_questions:subs,count:subs.length,
          needs_agent_loop_decompose:needs_loop}));'
    env:
      SUBS: $sub_questions_json

  # ---- 2. Route sub-questions to source tiers per the vertical hierarchy. -----
  - id: route-input
    command: >
      node -e 'const sq=JSON.parse(process.env.SQ||"{}").sub_questions||[];
        const vc=JSON.parse(process.env.VC||"null")||{};
        const ov=JSON.parse(process.env.OV||"null")||{};
        process.stdout.write(JSON.stringify({sub_questions:sq,vertical_config:vc,user_overrides:ov}));'
    env:
      SQ: $decompose.stdout
      VC: $load-verticals.stdout
      OV: $load-overrides.stdout

  - id: route
    command: node lens-source-router.js
    stdin: $route-input.stdout
    on_error: continue

  # ---- 3. Confidence estimate per sub-question (pre-search expectation). ------
  - id: estimate-input
    command: >
      node -e 'const sq=JSON.parse(process.env.SQ||"{}").sub_questions||[];
        const vc=JSON.parse(process.env.VC||"null")||{};
        const hist=JSON.parse(process.env.HIST||"null")||{};
        process.stdout.write(JSON.stringify({sub_questions:sq,vertical_config:vc,historical_research:hist}));'
    env:
      SQ: $decompose.stdout
      VC: $load-verticals.stdout
      HIST: $load-history.stdout

  - id: estimate
    command: node lens-confidence-estimator.js
    stdin: $estimate-input.stdout
    on_error: continue

  # ---- 4. Queue a PLAN-REVIEW notification for the agent loop to deliver. -----
  # Slack is 401 in-pipeline; the loop renders + posts the plan (and the per-stakes
  # pass budget) and collects approval. We still hard-gate the run below.
  - id: plan-notify-load
    pipeline: state.get lens/notifications

  - id: plan-notify-next
    command: >
      node -e 'let n=JSON.parse(process.env.NOTIFS||"null");if(!Array.isArray(n))n=[];
        const sub=JSON.parse(process.env.SUBS||"{}");
        const est=JSON.parse(process.env.EST||"null")||{};
        const stakes=process.env.LOBSTER_ARG_STAKES||"standard";
        const passes=stakes==="casual"?2:stakes==="high_stakes"?5:3;
        n.push({kind:"research_plan",channel:"#lens-research",
          question:process.env.LOBSTER_ARG_RESEARCH_QUESTION||"",
          vertical:process.env.LOBSTER_ARG_VERTICAL||"",
          stakes,planned_passes:passes,
          sub_questions:sub.sub_questions||[],
          thin_areas:est.thin_areas||[],
          needs_decompose:!!sub.needs_agent_loop_decompose,
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $plan-notify-load.stdout
      SUBS: $decompose.stdout
      EST: $estimate.stdout

  - id: plan-notify
    pipeline: state.set lens/notifications
    stdin: $plan-notify-next.stdout

  # ---- 5. HUMAN APPROVAL GATE — before any cost-bearing search. --------------
  # Real step-level gate (not a fictional await-approval tool). The run halts with
  # a resumeToken; the user approves; $approve-plan.approved unblocks the search.
  - id: approve-plan
    approval:
      prompt: "Approve this research plan before Lens spends search passes? Review the plan posted to #lens-research."

  # ---- 6. Build the query plan (one query per sub-question if loop gave none). -
  # Runs UNCONDITIONALLY so $query-plan.json.count always resolves for the many
  # downstream `$query-plan.json.count > 0` gates (referencing a skipped step's
  # .json is a dangling ref). The approval is enforced INSIDE: unless APPROVED is
  # true the step emits count:0, which keeps every cost-bearing search step skipped.
  - id: query-plan
    command: >
      node -e 'const approved=process.env.APPROVED==="true";
        let qp=[];try{qp=JSON.parse(process.env.QP||"[]")||[]}catch(e){qp=[]}
        const subs=(JSON.parse(process.env.SUBS||"{}").sub_questions)||[];
        if(!Array.isArray(qp)||qp.length===0){
          qp=subs.map(s=>({sub_question_id:s.id,query:s.question,
            vertical:s.vertical,target_tier:s.target_tier}));
        }
        const queries=qp.map(x=>(typeof x==="string"?x:(x.query||x.question||""))).filter(Boolean);
        process.stdout.write(JSON.stringify({query_plan:approved?qp:[],
          queries:approved?queries:[],count:approved?queries.length:0,approved}));'
    env:
      APPROVED: $approve-plan.approved
      QP: $query_plan_json
      SUBS: $decompose.stdout

  # ---- 7. PASS 1 — Perplexity structured web search (deterministic HTTPS). ----
  # No API key => emit empty result + key_missing flag (exit 0); the loop surfaces
  # the "connect PERPLEXITY_API_KEY" notice instead of bricking the run.
  - id: pass1-web
    condition: $query-plan.json.count > 0
    command: >
      node -e 'const https=require("https");
        const input=JSON.parse(require("fs").readFileSync(0,"utf8")||"{}");
        const queries=(input.queries||[]).slice(0,12);
        const key=process.env.PERPLEXITY_API_KEY||"";
        if(!key){process.stdout.write(JSON.stringify({results:[],high_value_urls:[],total:0,key_missing:true}));return;}
        if(!queries.length){process.stdout.write(JSON.stringify({results:[],high_value_urls:[],total:0}));return;}
        const results=[];let done=0;
        const finish=()=>{const urls=[...new Set(results.flatMap(r=>(r&&r.citations)||[]))];
          process.stdout.write(JSON.stringify({results,high_value_urls:urls.slice(0,20),total:urls.length}));};
        queries.forEach((q,i)=>{
          const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:String(q)}]});
          const req=https.request({hostname:"api.perplexity.ai",path:"/chat/completions",method:"POST",
            headers:{Authorization:"Bearer "+key,"Content-Type":"application/json","Content-Length":Buffer.byteLength(body)}},
            res=>{let d="";res.on("data",c=>d+=c);res.on("end",()=>{
              try{const r=JSON.parse(d);results[i]={content:(r.choices&&r.choices[0]&&r.choices[0].message.content)||"",citations:r.citations||[]};}
              catch(e){results[i]={content:"",citations:[]};}
              if(++done===queries.length)finish();});});
          req.on("error",()=>{results[i]={content:"",citations:[]};if(++done===queries.length)finish();});
          req.write(body);req.end();});'
    stdin: $query-plan.stdout
    on_error: continue

  # ---- 8. PASS 1 — Semantic Scholar academic search (free, no key). ----------
  - id: pass1-academic
    condition: $query-plan.json.count > 0
    command: >
      node -e 'const https=require("https");
        const input=JSON.parse(require("fs").readFileSync(0,"utf8")||"{}");
        const queries=(input.queries||[]).slice(0,12);
        if(!queries.length){process.stdout.write(JSON.stringify({results:[],papers:[]}));return;}
        const results=[];let done=0;
        const finish=()=>process.stdout.write(JSON.stringify({results,papers:results.flatMap(r=>(r&&r.data)||[])}));
        queries.forEach((q,i)=>{
          const path="/graph/v1/paper/search?query="+encodeURIComponent(String(q))+
            "&limit=10&fields=title,abstract,year,citationCount,authors,url,externalIds";
          const req=https.request({hostname:"api.semanticscholar.org",path,method:"GET"},
            res=>{let d="";res.on("data",c=>d+=c);res.on("end",()=>{
              try{results[i]=JSON.parse(d);}catch(e){results[i]={data:[]};}
              if(++done===queries.length)finish();});});
          req.on("error",()=>{results[i]={data:[]};if(++done===queries.length)finish();});
          req.end();});'
    stdin: $query-plan.stdout
    on_error: continue

  # ---- 9. Emit a search-result summary (explicit count; NEVER gate on .length).
  - id: pass1-summary
    condition: $query-plan.json.count > 0
    command: >
      node -e 'const web=JSON.parse(process.env.WEB||"null")||{};
        const aca=JSON.parse(process.env.ACA||"null")||{};
        const urls=web.high_value_urls||[];
        const papers=aca.papers||[];
        process.stdout.write(JSON.stringify({
          high_value_urls:urls,url_count:urls.length,
          paper_count:papers.length,
          total_evidence:urls.length+papers.length,
          key_missing:!!web.key_missing}));'
    env:
      WEB: $pass1-web.stdout
      ACA: $pass1-academic.stdout

  # ---- 10. Queue a RETRIEVAL/refine notification for the agent loop. ----------
  # Full-page retrieval of high-value URLs + any pass-2 query refinement is model/
  # browser work (agent-browser + llm-task are unreachable here). Hand the loop the
  # citation URLs + paper set + the per-stakes pass budget; it reads + refines.
  - id: retrieve-notify-load
    condition: $query-plan.json.count > 0
    pipeline: state.get lens/notifications

  - id: retrieve-notify-next
    condition: $query-plan.json.count > 0
    command: >
      node -e 'let n=JSON.parse(process.env.NOTIFS||"null");if(!Array.isArray(n))n=[];
        const sum=JSON.parse(process.env.SUM||"null")||{};
        const web=JSON.parse(process.env.WEB||"null")||{};
        const aca=JSON.parse(process.env.ACA||"null")||{};
        const stakes=process.env.LOBSTER_ARG_STAKES||"standard";
        n.push({kind:"research_retrieval",
          question:process.env.LOBSTER_ARG_RESEARCH_QUESTION||"",
          vertical:process.env.LOBSTER_ARG_VERTICAL||"",
          stakes,min_passes:stakes==="casual"?2:stakes==="high_stakes"?5:3,
          high_value_urls:sum.high_value_urls||[],
          web_results:web.results||[],
          academic_papers:aca.papers||[],
          key_missing:!!sum.key_missing,
          instruction:"Deep-read high_value_urls, assemble claims with tiered sources, then re-invoke research-execute with claims_json filled (and refined queries if gaps remain).",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $retrieve-notify-load.stdout
      SUM: $pass1-summary.stdout
      WEB: $pass1-web.stdout
      ACA: $pass1-academic.stdout

  - id: retrieve-notify
    condition: $query-plan.json.count > 0
    pipeline: state.set lens/notifications
    stdin: $retrieve-notify-next.stdout

  # ---- 11. Count assembled claims (gate the deterministic synthesis chain). ---
  - id: claims-count
    command: >
      node -e 'let c=[];try{c=JSON.parse(process.env.CLAIMS||"[]")||[]}catch(e){c=[]}
        if(!Array.isArray(c))c=[];
        process.stdout.write(JSON.stringify({count:c.length}));'
    env:
      CLAIMS: $claims_json

  # ---- 12. Corroboration + confidence tiers (deterministic; needs claims). ----
  - id: corroborate-input
    condition: $claims-count.json.count > 0
    command: >
      node -e 'const claims=JSON.parse(process.env.CLAIMS||"[]")||[];
        const th=JSON.parse(process.env.TH||"null")||{};
        process.stdout.write(JSON.stringify({claims,confidence_thresholds:th}));'
    env:
      CLAIMS: $claims_json
      TH: $load-thresholds.stdout

  - id: corroborate
    condition: $claims-count.json.count > 0
    command: node lens-corroboration-tracker.js
    stdin: $corroborate-input.stdout
    on_error: continue

  # ---- 13. Freshness check across every source on every claim. ----------------
  - id: freshness-input
    condition: $claims-count.json.count > 0
    command: >
      node -e 'const claims=JSON.parse(process.env.CLAIMS||"[]")||[];
        const seen=new Set();const sources=[];
        for(const c of claims){for(const s of (c.sources||[])){
          if(!s||!s.url||seen.has(s.url))continue;seen.add(s.url);sources.push(s);}}
        const fr=JSON.parse(process.env.FR||"null")||{};
        process.stdout.write(JSON.stringify({sources,
          vertical:process.env.LOBSTER_ARG_VERTICAL||"technology",
          freshness_thresholds:fr}));'
    env:
      CLAIMS: $claims_json
      FR: $load-freshness.stdout

  - id: freshness
    condition: $claims-count.json.count > 0
    command: node lens-freshness-checker.js
    stdin: $freshness-input.stdout
    on_error: continue

  # ---- 14. Cross-vertical contradiction surfacing (deterministic numerical). --
  # Semantic contradictions are flagged needs_agent_loop in the output for the loop.
  - id: cross-vertical-input
    condition: $claims-count.json.count > 0
    command: >
      node -e 'const claims=JSON.parse(process.env.CLAIMS||"[]")||[];
        const findings=claims.map(c=>({entity:c.entity||c.company||c.topic||null,
          vertical:c.vertical,claim_type:c.claim_type||null,claim_text:c.text||"",
          extracted_value:c.extracted_value,source_url:(c.sources&&c.sources[0]&&c.sources[0].url)||null}));
        process.stdout.write(JSON.stringify({mode:"cross_vertical",findings}));'
    env:
      CLAIMS: $claims_json

  - id: cross-vertical
    condition: $claims-count.json.count > 0
    command: node lens-cross-checker.js
    stdin: $cross-vertical-input.stdout
    on_error: continue

  # ---- 15. Format synthesis: inline tiers + confidence summary + gap + appendix.
  - id: format-input
    condition: $claims-count.json.count > 0
    command: >
      node -e 'const tracked=(JSON.parse(process.env.CORR||"null")||{}).claims||[];
        const pref=JSON.parse(process.env.PREF||"null");
        process.stdout.write(JSON.stringify({tracked_claims:tracked,
          vertical:process.env.LOBSTER_ARG_VERTICAL||"technology",
          user_format_preference:pref||null}));'
    env:
      CORR: $corroborate.stdout
      PREF: $load-format-pref.stdout

  - id: format-synthesis
    condition: $claims-count.json.count > 0
    command: node lens-synthesis-formatter.js
    stdin: $format-input.stdout
    on_error: continue

  # ---- 16. Queue the SYNTHESIS-narrative + DELIVERY notification for the loop. -
  # Narrative writing (llm-task) + Slack post are unreachable here. We hand the loop
  # the fully formatted, deterministic scaffold; it writes prose preserving every
  # inline tier and posts to #lens-research. Gaps stay gaps — never filled here.
  - id: deliver-notify-load
    condition: $claims-count.json.count > 0
    pipeline: state.get lens/notifications

  - id: deliver-notify-next
    condition: $claims-count.json.count > 0
    command: >
      node -e 'let n=JSON.parse(process.env.NOTIFS||"null");if(!Array.isArray(n))n=[];
        const f=JSON.parse(process.env.FMT||"null")||{};
        const fr=JSON.parse(process.env.FRESH||"null")||{};
        const cv=JSON.parse(process.env.CV||"null")||{};
        n.push({kind:"research_complete",channel:"#lens-research",
          question:process.env.LOBSTER_ARG_RESEARCH_QUESTION||"",
          vertical:process.env.LOBSTER_ARG_VERTICAL||"",
          stakes:process.env.LOBSTER_ARG_STAKES||"standard",
          confidence_summary:f.confidence_summary||null,
          formatted_claims:f.formatted_claims||[],
          gap_section:f.gap_section||null,
          source_appendix:f.source_appendix||null,
          freshness_flags:fr.checked?fr.checked.filter(s=>s.is_stale):[],
          cross_vertical_issues:cv.cross_vertical||[],
          instruction:"Write the synthesis narrative preserving every inline [TIER] tag and citation exactly; do NOT fill gaps; then post to #lens-research.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $deliver-notify-load.stdout
      FMT: $format-synthesis.stdout
      FRESH: $freshness.stdout
      CV: $cross-vertical.stdout

  - id: deliver-notify
    condition: $claims-count.json.count > 0
    pipeline: state.set lens/notifications
    stdin: $deliver-notify-next.stdout

  # ---- 17. Archive the deterministic deliverable to durable state (index array).
  # Persist the FULL research content (claims, source_appendix, gaps) so an archived
  # entry is retrievable in full — not just summary counts. The summary stats stay
  # alongside the content for cheap index scans. Entry count is capped to keep the
  # single-key index array bounded.
  - id: archive-load
    condition: $claims-count.json.count > 0
    pipeline: state.get lens/research-index

  - id: archive-next
    condition: $claims-count.json.count > 0
    command: >
      node -e 'let idx=JSON.parse(process.env.IDX||"null");if(!Array.isArray(idx))idx=[];
        const f=JSON.parse(process.env.FMT||"null")||{};
        const corr=JSON.parse(process.env.CORR||"null")||{};
        const claims=corr.claims||[];
        idx.push({question:process.env.LOBSTER_ARG_RESEARCH_QUESTION||"",
          vertical:process.env.LOBSTER_ARG_VERTICAL||"",
          stakes:process.env.LOBSTER_ARG_STAKES||"standard",
          claims:claims,
          confidence_summary:f.confidence_summary||null,
          source_appendix:f.source_appendix||null,
          gaps:f.gap_section||null,
          verification_rate:(f.confidence_summary&&f.confidence_summary.verification_rate)||0,
          claim_count:claims.length,
          gap_count:(f.gap_section&&f.gap_section.count)||0,
          source_count:(f.source_appendix&&f.source_appendix.total)||0,
          archived_at:new Date().toISOString()});
        if(idx.length>200)idx=idx.slice(idx.length-200);
        process.stdout.write(JSON.stringify(idx));'
    env:
      IDX: $archive-load.stdout
      FMT: $format-synthesis.stdout
      CORR: $corroborate.stdout

  - id: archive
    condition: $claims-count.json.count > 0
    pipeline: state.set lens/research-index
    stdin: $archive-next.stdout

  # ---- 18. Build the human-readable markdown deliverable + hand it to the loop.
  # We render the markdown deterministically here, but the Drive WRITE itself is a
  # `gog` skill call whose exact write-subcommand surface we won't guess from a
  # pipeline — the agent loop owns `gog` and persists this to "Lens Research
  # Archive". The durable machine-readable archive already landed in state above.
  - id: render-deliverable
    condition: $claims-count.json.count > 0
    command: >
      node -e 'const f=JSON.parse(process.env.FMT||"null")||{};
        const q=(process.env.LOBSTER_ARG_RESEARCH_QUESTION||"research").replace(/[^a-zA-Z0-9 _-]/g,"").slice(0,80);
        const date=new Date().toISOString().slice(0,10);
        const lines=[];
        lines.push("# "+q);
        if(f.confidence_summary)lines.push("",f.confidence_summary.text||"");
        lines.push("","## Findings");
        for(const c of (f.formatted_claims||[]))lines.push("",c.formatted||"");
        if(f.gap_section){lines.push("","## "+(f.gap_section.title||"GAPS"));
          for(const g of (f.gap_section.gaps||[]))lines.push("- "+(g.question||""));}
        if(f.source_appendix){lines.push("","## "+(f.source_appendix.title||"SOURCES"));
          for(const s of (f.source_appendix.sources||[]))lines.push("- "+[s.title,s.url].filter(Boolean).join(" — "));}
        process.stdout.write(JSON.stringify({filename:date+"-"+q+".md",
          folder:"Lens Research Archive",markdown:lines.join("\n")}));'
    env:
      FMT: $format-synthesis.stdout

  - id: drive-notify-load
    condition: $claims-count.json.count > 0
    pipeline: state.get lens/notifications

  - id: drive-notify-next
    condition: $claims-count.json.count > 0
    command: >
      node -e 'let n=JSON.parse(process.env.NOTIFS||"null");if(!Array.isArray(n))n=[];
        const d=JSON.parse(process.env.DELIV||"null")||{};
        n.push({kind:"drive_archive",
          folder:d.folder||"Lens Research Archive",
          filename:d.filename||"research.md",
          markdown:d.markdown||"",
          instruction:"Write this markdown to the named file in the Drive folder via the gog skill.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $drive-notify-load.stdout
      DELIV: $render-deliverable.stdout

  - id: drive-notify
    condition: $claims-count.json.count > 0
    pipeline: state.set lens/notifications
    stdin: $drive-notify-next.stdout
