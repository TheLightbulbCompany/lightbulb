name: content-generate
# Research a topic, run the deterministic content-quality gates, and queue an LLM
# draft-generation INTENT for the agent loop. Triggered per content request.
#
# THE LINE (see AGENTS.md): the intelligence side runs here, deterministically and
# gateway-free. The judgment side (draft generation, tone scoring, repurposing) and
# the delivery side (Slack review package) CANNOT run in an embedded pipeline — every
# openclaw.invoke / llm-task / slack call returns 401. So this workflow:
#   1. Gates on the review-queue size (review fatigue == no approval gate).
#   2. Pulls research via the REAL Perplexity sonar API (curl + node normalizer).
#   3. Compiles a deterministic research brief (no LLM compression).
#   4. Writes a `pulse/draft-intents` state queue describing the draft to generate,
#      the brand voice + brief + platform tones, and which gates to re-run on the body.
#   5. Bumps the queue counter and writes a `pulse/notifications` entry so the agent
#      loop generates the draft, re-runs the body-dependent gates, and posts the
#      review package to #pulse-content.
#
# Contract notes (lobster-authoring SKILL.md):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    Config the marketer set (brand voice, platform tones, cultural calendar, approved
#    claims, queue counter, review history) is READ-ONLY consumer state under pulse-*.
#    The draft-intent + notification queues are single-key INDEX ARRAYS (state.* has
#    no prefix-list): state.get -> a node step appends -> state.set.
#  - Perplexity sonar is a deterministic structured-search API (same query -> same
#    response shape), NOT an LLM reasoning call — so it stays in the pipeline.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.
#    NOTE: pulse-calendar-conflict-checker.js currently exits 1 when conflicts exist
#    (a normal review outcome) — see the .js fix list; until patched, this workflow
#    sets on_error:continue on calendar-check so a conflict never aborts the run.
#  - Body-dependent gates (voice score, GEO check, claim check) need the DRAFT BODY,
#    which only exists after the LLM generates it. They run in the agent loop on the
#    generated body, NOT here — this workflow ships their inputs in the draft intent.

args:
  topic:
    required: true
  content_type:
    default: "blog_post"
  target_platform:
    default: ""
  repurpose:
    default: true

steps:
  # ---------------------------------------------------------------------------
  # 1. Review-queue size gate (Req 48). Read the marketer's queue counter +
  #    review history (consumer config the agent loop maintains) and run the
  #    deterministic limiter. Pause generation when the queue is full or the
  #    marketer is rubber-stamping.
  # ---------------------------------------------------------------------------
  - id: queue-size
    pipeline: state.get pulse-state/queue-size

  - id: review-history
    pipeline: state.get pulse-state/review-history

  - id: queue-input
    command: >-
      node -e 'const q=JSON.parse(process.env.QSIZE||"null")||{};
        const hist=JSON.parse(process.env.HIST||"null")||[];
        process.stdout.write(JSON.stringify({
          current_queue_size: (typeof q==="number"?q:(q.count||q.size||0)),
          max_queue_size: q.max_queue_size||10,
          review_history: Array.isArray(hist)?hist:(hist.items||[])
        }));'
    env:
      QSIZE: "$queue-size.json"
      HIST: "$review-history.json"

  - id: queue-check
    command: node pulse-queue-limiter.js
    stdin: $queue-input.stdout

  # Explicit boolean for the proceed gate (NOT a .length test). When at_limit or
  # rubber-stamping, every downstream step is skipped and a pause notice is queued.
  - id: gate
    command: >-
      node -e 'const c=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const blocked=!!c.at_limit||!!c.rubber_stamping_detected;
        process.stdout.write(JSON.stringify({proceed:blocked?0:1, blocked:blocked?1:0,
          at_limit:!!c.at_limit, rubber_stamping:!!c.rubber_stamping_detected,
          message:c.message||""}));'
    stdin: $queue-check.stdout

  # If blocked, queue a pause notification for the agent to deliver to #pulse-content.
  - id: pause-notify-load
    condition: $gate.json.blocked > 0
    pipeline: state.get pulse/notifications

  - id: pause-notify-next
    condition: $gate.json.blocked > 0
    command: >-
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const g=JSON.parse(process.env.GATE||"{}");
        n.push({kind:"queue_paused", channel:"#pulse-content",
          topic:process.env.LOBSTER_ARG_TOPIC||"",
          at_limit:g.at_limit, rubber_stamping:g.rubber_stamping,
          message:g.message||"Content generation paused — review queue is full.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: "$pause-notify-load.json"
      GATE: "$gate.stdout"

  - id: pause-notify
    condition: $gate.json.blocked > 0
    pipeline: state.set pulse/notifications
    stdin: $pause-notify-next.stdout

  # ---------------------------------------------------------------------------
  # 2. Load brand voice + platform tones (Req 1). Consumer config the marketer
  #    set; the agent loop scores the generated draft against these.
  # ---------------------------------------------------------------------------
  - id: load-voice
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse-config/brand-voice

  - id: load-platform-config
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse-config/platform-tones

  # ---------------------------------------------------------------------------
  # 3. Research pull (Req 8, 9). Perplexity sonar via REAL curl. Four structured
  #    searches: competitor content, community questions, search trends/data, and
  #    industry analysis. The query is passed through env (LOBSTER_ARG_TOPIC) into
  #    the node body builder, never interpolated into the shell command line.
  # ---------------------------------------------------------------------------
  - id: research-competitors
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const t=process.env.LOBSTER_ARG_TOPIC||"";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"Find recent competitor content and blog posts about "+t+". Return key URLs, headlines, and the competitor brands covering this topic."}]});
        const {execSync}=require("child_process");
        let out="";
        try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out=""; }
        let r={}; try { r=JSON.parse(out)||{}; } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({lane:"competitors", content, citations,
          total:citations.length, top_urls:citations.slice(0,10), ok:!!content}));'

  - id: research-community
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const t=process.env.LOBSTER_ARG_TOPIC||"";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"Find Reddit discussions, Hacker News threads, and community questions about "+t+". Return thread URLs and the core questions being asked."}]});
        const {execSync}=require("child_process");
        let out="";
        try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out=""; }
        let r={}; try { r=JSON.parse(out)||{}; } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({lane:"community", content, citations,
          total:citations.length, top_urls:citations.slice(0,10), ok:!!content}));'

  - id: research-search
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const t=process.env.LOBSTER_ARG_TOPIC||"";
        const yr=new Date().getFullYear();
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"Find recent search trends, industry data, and concrete statistics about "+t+" for "+yr+". Return URLs and specific data points."}]});
        const {execSync}=require("child_process");
        let out="";
        try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out=""; }
        let r={}; try { r=JSON.parse(out)||{}; } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({lane:"search", content, citations,
          total:citations.length, top_urls:citations.slice(0,10), ok:!!content}));'

  - id: research-industry
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const t=process.env.LOBSTER_ARG_TOPIC||"";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:t+" industry analysis report"}]});
        const {execSync}=require("child_process");
        let out="";
        try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out=""; }
        let r={}; try { r=JSON.parse(out)||{}; } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({lane:"industry", content, citations,
          total:citations.length, top_urls:citations.slice(0,10), ok:!!content}));'

  # Internal prior-art lookup. The original used fast-io list with a prefix filter
  # (fictional). We read the agent's own published-content INDEX array and match by
  # topic deterministically — no prefix-list needed.
  - id: research-internal
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse/content-published

  - id: research-internal-match
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const idx=JSON.parse(process.env.IDX||"null")||[];
        const list=Array.isArray(idx)?idx:(idx.items||[]);
        const t=(process.env.LOBSTER_ARG_TOPIC||"").toLowerCase();
        const matches=list.filter(p=>((p.topic||p.title||"")+"").toLowerCase().includes(t)).slice(0,10);
        process.stdout.write(JSON.stringify({matches, count:matches.length}));'
    env:
      IDX: "$research-internal.json"

  # ---------------------------------------------------------------------------
  # 4. Compile a deterministic research brief (Req — replaces the fictional
  #    summarize/llm compression). No model judgment: we collate the lane
  #    contents + dedup the citation URLs into one structured object the agent
  #    loop hands to the draft-generation model.
  # ---------------------------------------------------------------------------
  - id: compile-brief
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const j=k=>{try{return JSON.parse(process.env[k]||"{}")||{};}catch(e){return {};}};
        const comp=j("COMP"), comm=j("COMM"), srch=j("SRCH"), ind=j("IND");
        const internal=(j("INT").matches)||[];
        const lanes={competitors:comp, community:comm, search:srch, industry:ind};
        const allUrls=[...new Set([].concat(comp.top_urls||[],comm.top_urls||[],srch.top_urls||[],ind.top_urls||[]))];
        const brief={
          topic:process.env.LOBSTER_ARG_TOPIC||"",
          content_type:process.env.LOBSTER_ARG_CONTENT_TYPE||"blog_post",
          target_platform:process.env.LOBSTER_ARG_TARGET_PLATFORM||"",
          lanes,
          internal_prior_art:internal,
          all_citation_urls:allUrls,
          citation_count:allUrls.length,
          lanes_ok:[comp.ok,comm.ok,srch.ok,ind.ok].filter(Boolean).length,
          compiled_at:new Date().toISOString()
        };
        process.stdout.write(JSON.stringify({brief, count:brief.lanes_ok}));'
    env:
      COMP: "$research-competitors.stdout"
      COMM: "$research-community.stdout"
      SRCH: "$research-search.stdout"
      IND: "$research-industry.stdout"
      INT: "$research-internal-match.stdout"

  # ---------------------------------------------------------------------------
  # 5. Calendar conflict check (Req 17). This is the ONE quality gate that does
  #    NOT need the draft body — it runs on the schedule metadata. So it runs
  #    here. (Body-dependent gates — voice score, GEO, claim check — run in the
  #    agent loop on the generated draft body; their config is shipped in the
  #    intent below.)
  #    News-cycle input is empty here; the daily monitoring sweep owns news checks.
  #    on_error:continue defends against the script's current exit(1)-on-conflict.
  # ---------------------------------------------------------------------------
  - id: load-cultural-calendar
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse-config/cultural-calendar

  - id: calendar-input
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const cal=JSON.parse(process.env.CAL||"null")||[];
        const list=Array.isArray(cal)?cal:(cal.items||[]);
        const now=new Date();
        const stamp=now.toISOString().slice(0,19).replace(/[-:T]/g,"");
        process.stdout.write(JSON.stringify({
          scheduled_content:[{id:"draft-"+stamp,
            title:process.env.LOBSTER_ARG_TOPIC||"",
            scheduled_date:now.toISOString().slice(0,10)}],
          cultural_calendar:list,
          news_conflicts:[]
        }));'
    env:
      CAL: "$load-cultural-calendar.json"

  - id: calendar-check
    condition: $gate.json.proceed > 0
    on_error: continue
    command: node pulse-calendar-conflict-checker.js
    stdin: $calendar-input.stdout

  # ---------------------------------------------------------------------------
  # 6. Build the DRAFT-GENERATION INTENT. The model work (draft body, tone score,
  #    repurposing) happens in the agent loop. We assemble everything the loop
  #    needs: brief, brand voice, platform tones, content params, the calendar
  #    result already computed, and the marketer's approved-claims map (for the
  #    body claim check the loop will run). Append to the pulse/draft-intents
  #    single-key index array.
  # ---------------------------------------------------------------------------
  - id: approved-claims
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse-state/approved-claims

  - id: intents-load
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse/draft-intents

  - id: intents-next
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const j=k=>{try{return JSON.parse(process.env[k]||"null");}catch(e){return null;}};
        const intents=j("INTENTS")||[];
        const brief=(j("BRIEF")||{}).brief||{};
        const voice=j("VOICE")||{};
        const tones=j("TONES")||{};
        const cal=j("CAL")||{};
        const claims=j("CLAIMS")||{};
        const now=new Date();
        const stamp=now.toISOString().slice(0,19).replace(/[-:T]/g,"");
        intents.push({
          id:"draft-"+stamp,
          status:"awaiting_generation",
          topic:process.env.LOBSTER_ARG_TOPIC||"",
          content_type:process.env.LOBSTER_ARG_CONTENT_TYPE||"blog_post",
          target_platform:process.env.LOBSTER_ARG_TARGET_PLATFORM||"",
          repurpose:(process.env.LOBSTER_ARG_REPURPOSE||"true")!=="false",
          research_brief:brief,
          brand_voice:voice,
          platform_tones:tones,
          approved_claims:claims&&typeof claims==="object"?claims:{},
          calendar_check:cal,
          gates_to_run_in_loop:["voice_score_deterministic","voice_score_tone","geo_check","claim_check"],
          read_aloud_prompt:"Read this aloud. If any sentence sounds like it came from a committee, flag it before approving.",
          created_at:now.toISOString()
        });
        process.stdout.write(JSON.stringify(intents));'
    env:
      INTENTS: "$intents-load.json"
      BRIEF: "$compile-brief.stdout"
      VOICE: "$load-voice.json"
      TONES: "$load-platform-config.json"
      CAL: "$calendar-check.stdout"
      CLAIMS: "$approved-claims.json"

  - id: intents
    condition: $gate.json.proceed > 0
    pipeline: state.set pulse/draft-intents
    stdin: $intents-next.stdout

  # ---------------------------------------------------------------------------
  # 7. Bump the review-queue counter (Req 48). The agent loop decrements it when
  #    the marketer clears a draft. Read-modify-write the single counter key.
  # ---------------------------------------------------------------------------
  - id: queue-bump-next
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const q=JSON.parse(process.env.QSIZE||"null")||{};
        const cur=(typeof q==="number"?q:(q.count||q.size||0));
        const max=(typeof q==="object"&&q)?(q.max_queue_size||10):10;
        process.stdout.write(JSON.stringify({count:cur+1, max_queue_size:max,
          updated_at:new Date().toISOString()}));'
    env:
      QSIZE: "$queue-size.json"

  - id: queue-bump
    condition: $gate.json.proceed > 0
    pipeline: state.set pulse-state/queue-size
    stdin: $queue-bump-next.stdout

  # ---------------------------------------------------------------------------
  # 8. Notify the marketer (Req 16). Slack is unreachable from the embedded
  #    runner, so we queue a `draft_ready` notification for the agent loop to
  #    deliver to #pulse-content AFTER it generates the body and runs the
  #    body-dependent gates (voice/GEO/claim). The loop fills in those scores.
  # ---------------------------------------------------------------------------
  - id: notify-load
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse/notifications

  - id: notify-next
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const cal=JSON.parse(process.env.CAL||"{}")||{};
        const brief=(JSON.parse(process.env.BRIEF||"{}")||{}).brief||{};
        n.push({kind:"draft_ready", channel:"#pulse-content",
          topic:process.env.LOBSTER_ARG_TOPIC||"",
          content_type:process.env.LOBSTER_ARG_CONTENT_TYPE||"blog_post",
          target_platform:process.env.LOBSTER_ARG_TARGET_PLATFORM||"",
          research_citations:brief.citation_count||0,
          calendar_conflicts:!!cal.has_conflicts,
          calendar_high_severity:cal.high_severity||0,
          read_aloud_prompt:"Read this aloud. If any sentence sounds like it came from a committee, flag it before approving.",
          requires_agent_loop:["generate_draft","voice_score_tone","repurpose","post_review_package"],
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: "$notify-load.json"
      CAL: "$calendar-check.stdout"
      BRIEF: "$compile-brief.stdout"

  - id: notify
    condition: $gate.json.proceed > 0
    pipeline: state.set pulse/notifications
    stdin: $notify-next.stdout

  # ---------------------------------------------------------------------------
  # 9. Audit log (Req 20). Append to the pulse/audit-log index array (fast-io
  #    write was fictional). Deterministic; no LLM scores yet — the agent loop
  #    appends the post-generation audit with voice/GEO scores once the body
  #    exists.
  # ---------------------------------------------------------------------------
  - id: audit-load
    condition: $gate.json.proceed > 0
    pipeline: state.get pulse/audit-log

  - id: audit-next
    condition: $gate.json.proceed > 0
    command: >-
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const cal=JSON.parse(process.env.CAL||"{}")||{};
        log.push({action:"content_intent_queued",
          topic:process.env.LOBSTER_ARG_TOPIC||"",
          content_type:process.env.LOBSTER_ARG_CONTENT_TYPE||"blog_post",
          target_platform:process.env.LOBSTER_ARG_TARGET_PLATFORM||"",
          repurpose:(process.env.LOBSTER_ARG_REPURPOSE||"true")!=="false",
          calendar_conflicts:!!cal.has_conflicts,
          authorized_by:"autonomous_intelligence_layer",
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: "$audit-load.json"
      CAL: "$calendar-check.stdout"

  - id: audit
    condition: $gate.json.proceed > 0
    pipeline: state.set pulse/audit-log
    stdin: $audit-next.stdout
