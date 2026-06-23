name: monitoring-sweep
# Daily autonomous monitoring sweep (Reqs 22-27, 31).
# Brand mentions, competitor activity, subreddit threads, content freshness,
# calendar conflicts, community follow-ups — assembled into a deterministic
# digest the agent loop delivers (Slack is 401 from an embedded pipeline).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - There is NO fast-io and NO agent-browser/summarize/slack via openclaw.invoke —
#    every such call 401s on the embedded runner. Perplexity is reached gateway-free
#    via raw `curl` (real CLI under `command:`), normalized by an inline `node -e`.
#  - Perplexity `sonar` is a deterministic structured-search API (same query -> same
#    response shape), so these are deterministic steps, NOT llm-task reasoning.
#  - Config the marketer sets (competitors, reddit monitors, cultural calendar) is
#    read-only CONSUMER state via state.get. Things that were fast-io PREFIX lists
#    (published/approved content, tracked engagements) are SINGLE-KEY INDEX ARRAYS —
#    state.* has no prefix-list.
#  - LLM judgment (sentiment) + delivery (Slack) can't run here. High-signal alerts
#    and the day's digest are written to `pulse/notifications` + `pulse/monitoring`
#    state queues for the agent loop to score + deliver.
#  - Scripts always exit 0 here (we gate via condition:); the calendar checker's
#    own `process.exit(has_conflicts?1:0)` is neutralized with on_error: continue
#    so a conflict can't abort the run. See the .js fix list in the handoff.
#  - The day's date key is computed in the marketer's timezone (USER.md), never
#    container UTC, so the digest lands on the right calendar day.

args:
  brand_name:
    required: true
  product_name:
    required: true
  brand_category:
    required: true
  user_tz:
    default: "America/New_York"     # from USER.md; overridable per run

steps:
  # 0. Today's date key in the marketer's timezone (not container UTC).
  - id: today-key
    command: >
      node -e 'const tz=process.env.USER_TZ||"America/New_York";
        const ymd=new Intl.DateTimeFormat("en-CA",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
        process.stdout.write(JSON.stringify({date:ymd, iso:new Date().toISOString()}));'
    env:
      USER_TZ: ${user_tz}

  # ── Step 1: Brand mention monitoring (Req 22) ──────────────────────────────
  # Perplexity structured search -> citations. Deterministic shape. Emits explicit
  # counts so downstream gates use $x.json.count (never .length).
  - id: mention-scan
    command: >
      node -e 'const {execSync}=require("child_process");
        const brand=process.env.BRAND_NAME||"", product=process.env.PRODUCT_NAME||"";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"Find recent mentions of \""+brand+"\" or \""+product+"\" across web, news, social platforms, and forums. Return URLs and the context of each mention."}]});
        let out="{}"; try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out="{}"; }
        let r={}; try { r=JSON.parse(out); } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({content, citations, total:citations.length,
          top_urls:citations.slice(0,20), high_signal_urls:citations.slice(0,10),
          high_signal_count:Math.min(citations.length,10), urgent_count:citations.length}));'
    env:
      BRAND_NAME: ${brand_name}
      PRODUCT_NAME: ${product_name}

  # Deep page-read (agent-browser) is FICTIONAL here. Carry the high-signal URLs +
  # the search context forward so the agent loop can deep-read + sentiment-score.
  - id: mention-pack
    command: >
      node -e 'const m=JSON.parse(process.env.MENTIONS||"{}");
        process.stdout.write(JSON.stringify({content:m.content||"", high_signal_urls:m.high_signal_urls||[],
          high_signal_count:m.high_signal_count||0, total:m.total||0}));'
    env:
      MENTIONS: $mention-scan.stdout

  # ── Step 2: Competitor monitoring (Req 23) ─────────────────────────────────
  # Marketer-configured competitor list (consumer config). null if unset.
  - id: competitors-cfg
    pipeline: state.get pulse/config-competitors

  - id: competitor-content
    command: >
      node -e '(function(){ const {execSync}=require("child_process");
        const cfg=JSON.parse(process.env.CFG||"null")||{};
        const names=(cfg.names||cfg.competitors||[]).join(", ");
        if(!names){ process.stdout.write(JSON.stringify({content:"",citations:[],total:0,top_urls:[],configured:false})); return; }
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"Find recent blog posts, announcements, and content published by these competitors: "+names+". Return URLs and the topics covered."}]});
        let out="{}"; try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out="{}"; }
        let r={}; try { r=JSON.parse(out); } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({content, citations, total:citations.length, top_urls:citations.slice(0,15), configured:true}));})();'
    env:
      CFG: $competitors-cfg.stdout

  # ── Step 3: Subreddit monitoring (Req 26) ──────────────────────────────────
  - id: reddit-cfg
    pipeline: state.get pulse/config-reddit-monitors

  - id: subreddit-scan
    command: >
      node -e '(function(){ const {execSync}=require("child_process");
        const cfg=JSON.parse(process.env.CFG||"null")||{};
        const queries=(cfg.queries||[]).join("; ");
        if(!queries){ process.stdout.write(JSON.stringify({content:"",citations:[],total:0,top_urls:[],configured:false})); return; }
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"Search Reddit (site:reddit.com) for recent threads matching these queries: "+queries+". Return thread URLs and the questions being asked."}]});
        let out="{}"; try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out="{}"; }
        let r={}; try { r=JSON.parse(out); } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({content, citations, total:citations.length, top_urls:citations.slice(0,15), configured:true}));})();'
    env:
      CFG: $reddit-cfg.stdout

  # ── Step 5: Content freshness check (Req 25) ───────────────────────────────
  # Published archive was a fast-io PREFIX list -> now a single-key index array
  # the agent loop appends to on publish. null -> empty archive (no flags).
  - id: published-archive
    pipeline: state.get pulse/content-published

  - id: freshness-input
    command: >
      node -e 'const arr=JSON.parse(process.env.PUBLISHED||"null")||[];
        process.stdout.write(JSON.stringify({content_archive:arr, citation_data:{}}));'
    env:
      PUBLISHED: $published-archive.stdout

  # freshness-tracker.js reads {content_archive, citation_data} -> {flagged_count,...}.
  # on_error: continue so an unexpected non-zero (e.g. empty input) never aborts the sweep.
  - id: freshness-check
    command: node pulse-freshness-tracker.js
    stdin: $freshness-input.stdout
    on_error: continue

  # ── Step 6: Content calendar conflict check (Req 27) ───────────────────────
  # Scheduled/approved content (was a fast-io prefix list) -> single-key index array.
  - id: scheduled-content
    pipeline: state.get pulse/content-approved

  # Marketer-grown cultural calendar (consumer config; the .js seeds defaults).
  - id: cultural-cfg
    pipeline: state.get pulse/config-cultural-calendar

  # Active news-cycle scan (Perplexity). Emits a {topic, relevant_to_content}[] array
  # shaped exactly for the conflict checker's news_conflicts input.
  - id: news-check
    command: >
      node -e 'const {execSync}=require("child_process");
        const cat=process.env.BRAND_CATEGORY||"";
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"What breaking news cycles are active today related to "+cat+"? Return each active news topic on its own line, no bullets or numbering, just the topic names."}]});
        let out="{}"; try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out="{}"; }
        let r={}; try { r=JSON.parse(out); } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const lines=content.split(/\n+/).map(s=>s.trim()).filter(s=>s.length>0);
        const conflicts=lines.slice(0,5).map(topic=>({topic, relevant_to_content:true}));
        process.stdout.write(JSON.stringify({content, citations:r.citations||[], total:(r.citations||[]).length, conflicts}));'
    env:
      BRAND_CATEGORY: ${brand_category}

  - id: conflict-input
    command: >
      node -e 'const sched=JSON.parse(process.env.SCHED||"null")||[];
        const cal=JSON.parse(process.env.CAL||"null")||[];
        const news=JSON.parse(process.env.NEWS||"{}").conflicts||[];
        process.stdout.write(JSON.stringify({scheduled_content:sched, cultural_calendar:cal, news_conflicts:news}));'
    env:
      SCHED: $scheduled-content.stdout
      CAL: $cultural-cfg.stdout
      NEWS: $news-check.stdout

  # calendar-conflict-checker.js exits 1 when conflicts exist (a self-DoS under
  # on_error: stop) — on_error: continue neutralizes it until the .js is fixed to
  # always exit 0. We branch on $x.json.has_conflicts, never on the exit code.
  - id: calendar-conflict
    command: node pulse-calendar-conflict-checker.js
    stdin: $conflict-input.stdout
    on_error: continue

  # ── Step 9: Community follow-up tracking (Req 31) ──────────────────────────
  # Tracked engagements (was a fast-io prefix list) -> single-key index array of
  # {thread_url, ...}. Emit explicit thread_urls + count for the gate.
  - id: engagements
    pipeline: state.get pulse/community-engagements

  - id: followup-input
    command: >
      node -e 'const arr=JSON.parse(process.env.ENG||"null")||[];
        const urls=arr.map(e=>e.thread_url||e.url).filter(Boolean);
        process.stdout.write(JSON.stringify({thread_urls:urls, count:urls.length}));'
    env:
      ENG: $engagements.stdout

  # Unconditional so digest-build's $followup-scan.stdout always resolves; the
  # zero-engagement short-circuit lives inside the node (skips the API, not the step).
  - id: followup-scan
    command: >
      node -e '(function(){ const {execSync}=require("child_process");
        const urls=JSON.parse(process.env.URLS||"{}").thread_urls||[];
        if(urls.length===0){ process.stdout.write(JSON.stringify({content:"",citations:[],total:0,new_activity_count:0})); return; }
        const body=JSON.stringify({model:"sonar",messages:[{role:"user",
          content:"Check for new comments or activity on these community thread URLs: "+urls.join(", ")+". Summarize any new replies or updates since the last check."}]});
        let out="{}"; try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",
          {input:body,encoding:"utf8",timeout:60000}); } catch(e){ out="{}"; }
        let r={}; try { r=JSON.parse(out); } catch(e){ r={}; }
        const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
        const citations=r.citations||[];
        process.stdout.write(JSON.stringify({content, citations, total:citations.length, new_activity_count:citations.length}));})();'
    env:
      URLS: $followup-input.stdout

  # ── Step 7+8: Assemble the day's digest + alert queue (delivery is agent-loop) ──
  # Slack/summarize are 401 here, so we build a deterministic digest object and a
  # flagged-alerts list. The agent loop sentiment-scores ambiguous mentions, runs
  # `summarize`, and posts to #pulse-content.
  - id: digest-build
    command: >
      node -e 'const mention=JSON.parse(process.env.MENTION||"{}");
        const comp=JSON.parse(process.env.COMP||"{}");
        const reddit=JSON.parse(process.env.REDDIT||"{}");
        const fresh=JSON.parse(process.env.FRESH||"null")||{};
        const cal=JSON.parse(process.env.CAL||"null")||{};
        const followup=JSON.parse(process.env.FOLLOWUP||"null")||{};
        const tk=JSON.parse(process.env.TK||"{}");
        const alerts=[];
        if((mention.total||0)>0) alerts.push({kind:"brand_mention", urgent:(mention.high_signal_count||0)>0,
          count:mention.total, high_signal_urls:mention.high_signal_urls||[],
          needs_sentiment:true, context:(mention.content||"").slice(0,2000)});
        if(cal.has_conflicts) alerts.push({kind:"calendar_conflict", urgent:(cal.high_severity||0)>0,
          conflicts:cal.conflicts||[]});
        if((followup.new_activity_count||0)>0) alerts.push({kind:"community_followup", urgent:false,
          summary:(followup.content||"").slice(0,2000)});
        const digest={date:tk.date, generated_at:tk.iso,
          mentions:{total:mention.total||0, high_signal_count:mention.high_signal_count||0,
            high_signal_urls:mention.high_signal_urls||[], context:(mention.content||"").slice(0,4000), needs_deep_read:true, needs_sentiment:true},
          competitors:{configured:comp.configured!==false, total:comp.total||0,
            top_urls:comp.top_urls||[], context:(comp.content||"").slice(0,4000), needs_summarize:true},
          community:{configured:reddit.configured!==false, total:reddit.total||0,
            top_urls:reddit.top_urls||[], context:(reddit.content||"").slice(0,4000), needs_summarize:true},
          freshness:{flagged_count:fresh.flagged_count||0, high_priority:fresh.high_priority||0,
            stale_stats:fresh.stale_stats||0, refresh_queue:fresh.refresh_queue||[]},
          calendar:{has_conflicts:!!cal.has_conflicts, high_severity:cal.high_severity||0, conflicts:cal.conflicts||[]},
          followups:{new_activity_count:followup.new_activity_count||0, summary:(followup.content||"").slice(0,2000)}};
        process.stdout.write(JSON.stringify({digest, alerts, alert_count:alerts.length}));'
    env:
      MENTION: $mention-scan.stdout
      COMP: $competitor-content.stdout
      REDDIT: $subreddit-scan.stdout
      FRESH: $freshness-check.stdout
      CAL: $calendar-conflict.stdout
      FOLLOWUP: $followup-scan.stdout
      TK: $today-key.stdout

  # Persist the day's monitoring digest under the TZ-correct date key (index array,
  # newest first, capped) so the weekly digest can read a fixed-key history.
  - id: monitoring-load
    pipeline: state.get pulse/monitoring

  - id: monitoring-next
    command: >
      node -e 'const hist=JSON.parse(process.env.HIST||"null")||[];
        const d=JSON.parse(process.env.BUILD||"{}").digest||{};
        const next=[d, ...hist.filter(x=>x&&x.date!==d.date)].slice(0,60);
        process.stdout.write(JSON.stringify(next));'
    env:
      HIST: $monitoring-load.stdout
      BUILD: $digest-build.stdout

  - id: monitoring-store
    pipeline: state.set pulse/monitoring
    stdin: $monitoring-next.stdout

  # Enqueue flagged alerts for the agent loop to sentiment-score + deliver to Slack.
  # Gated on an explicit count (never .length).
  - id: notify-load
    condition: $digest-build.json.alert_count > 0
    pipeline: state.get pulse/notifications

  - id: notify-next
    condition: $digest-build.json.alert_count > 0
    command: >
      node -e 'const q=JSON.parse(process.env.NOTIFS||"null")||[];
        const b=JSON.parse(process.env.BUILD||"{}");
        const tk=JSON.parse(process.env.TK||"{}");
        for(const a of (b.alerts||[])) q.push(Object.assign({}, a, {source:"monitoring-sweep", date:tk.date, created_at:tk.iso}));
        process.stdout.write(JSON.stringify(q));'
    env:
      NOTIFS: $notify-load.stdout
      BUILD: $digest-build.stdout
      TK: $today-key.stdout

  - id: notify-store
    condition: $digest-build.json.alert_count > 0
    pipeline: state.set pulse/notifications
    stdin: $notify-next.stdout

  # ── Step 10: Audit log (index array, capped) ───────────────────────────────
  - id: audit-load
    pipeline: state.get pulse/audit-monitoring

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const b=JSON.parse(process.env.BUILD||"{}").digest||{};
        const tk=JSON.parse(process.env.TK||"{}");
        log.unshift({action:"daily_monitoring", date:tk.date,
          mentions_found:b.mentions?b.mentions.total:0,
          competitor_updates:b.competitors?b.competitors.total:0,
          subreddit_threads:b.community?b.community.total:0,
          stale_content:b.freshness?b.freshness.flagged_count:0,
          calendar_conflicts:b.calendar?b.calendar.has_conflicts:false,
          followup_activity:b.followups?b.followups.new_activity_count:0,
          authorized_by:"autonomous", timestamp:tk.iso});
        process.stdout.write(JSON.stringify(log.slice(0,365)));'
    env:
      LOG: $audit-load.stdout
      BUILD: $digest-build.stdout
      TK: $today-key.stdout

  - id: audit-store
    pipeline: state.set pulse/audit-monitoring
    stdin: $audit-next.stdout
