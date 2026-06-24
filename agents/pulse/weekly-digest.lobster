name: weekly-digest
# Monday marketing-intelligence digest — assembles the week's signals into a
# decision-forward briefing. Deterministic and gateway-free; the LLM narrative +
# Slack/Sheets delivery are queued to state for the agent loop (those cannot run
# inside the embedded pipeline — llm-task/slack/openclaw.invoke all 401).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    fast-io is fictional; every former fast-io list/prefix is a SINGLE-KEY INDEX
#    ARRAY, namespaced `pulse/...`. Config the marketer sets (brand voice, GEO
#    queries, competitors, published archive, monitoring) is read-only consumer
#    state the agent loop maintains.
#  - Perplexity `sonar` is a real search API reached via direct fetch (not a gateway
#    tool); the GEO sweep loops in inline node because curl can't iterate. Its answer
#    IS the Share-of-Model signal — deterministic, not an LLM reasoning call.
#  - LLM synthesis (email insight, voice tone, digest narrative) + Slack delivery +
#    Sheets history are written to `pulse/notifications` for the agent loop. No
#    openclaw.invoke / slack / llm-task / gog-sheets here.
#  - biz-reporter / posthog / capability-evolver are fictional tools: we consume
#    whatever the agent loop has cached in state, and emit a "connect analytics"
#    notice when it's empty rather than fabricating numbers.
#  - Deterministic scripts always exit 0 and each tolerates an empty (but non-null)
#    payload, so producers run UNCONDITIONALLY and every downstream ref resolves
#    (no dangling refs). Counts gate the EXTERNAL Perplexity calls only.
#  - Refs are `$id.json.dot.path` (no brackets); array index is a dot (items.0).

args:
  brand_name:
    required: true        # the brand whose Share-of-Model we measure (used by geo-monitor)
  digest_date:
    default: ""           # YYYY-MM-DD stamp; defaults to today in the user's TZ.
                          # The agent loop should pass the marketer's local date — see TZ note.

steps:
  # ---- Resolve the digest date deterministically (user TZ, not container UTC) ----
  # The agent loop should pass --digest_date so the stamp matches the marketer's
  # Monday. If absent we fall back to the process TZ env (set per-owner) rather than
  # raw UTC getDate()/getHours(), which would roll the date a day early for western
  # timezones running the 07:00-local cron.
  - id: stamp
    command: >-
      node -e 'const a=process.env.LOBSTER_ARG_DIGEST_DATE||"";
        const d=a||new Date().toLocaleDateString("en-CA",{timeZone:process.env.TZ||"UTC"});
        process.stdout.write(JSON.stringify({date:d}));'

  # ============================================================================
  # SECTION 1 — Content performance: connect published content to business outcomes
  # ============================================================================
  # Former biz-reporter/posthog/Stripe pulls are fictional tools. The agent loop
  # caches whatever analytics the marketer has connected into these read-only state
  # arrays; we consume them. Each is a single-key index array (no prefix-list).
  - id: load-published
    pipeline: state.get pulse/content-published

  - id: load-traffic
    pipeline: state.get pulse/analytics-traffic

  - id: load-conversions
    pipeline: state.get pulse/analytics-conversions

  - id: load-revenue
    pipeline: state.get pulse/analytics-revenue

  # Assemble the connector payload from cached analytics + the published archive.
  # analytics_connected lets the notify step add a "connect analytics" notice.
  - id: perf-input
    command: >-
      node -e 'const content=JSON.parse(process.env.CONTENT||"null")||[];
        const traffic=JSON.parse(process.env.TRAFFIC||"null")||[];
        const conv=JSON.parse(process.env.CONV||"null")||[];
        const rev=JSON.parse(process.env.REV||"null")||[];
        const connected=(traffic.length+conv.length+rev.length)>0;
        process.stdout.write(JSON.stringify({analytics_connected:connected,
          content, traffic_data:traffic, conversion_data:conv, revenue_data:rev}));'
    env:
      CONTENT: "$load-published.json"
      TRAFFIC: "$load-traffic.json"
      CONV: "$load-conversions.json"
      REV: "$load-revenue.json"

  # Deterministic content->outcome join. Empty archive -> empty result (script-safe),
  # so this runs unconditionally and connect-outcomes.stdout always resolves.
  - id: connect-outcomes
    command: node pulse-performance-connector.js
    stdin: $perf-input.stdout

  # ============================================================================
  # SECTION 2 — GEO Share of Model (Perplexity sonar; one call per configured query)
  # ============================================================================
  - id: load-geo-queries
    pipeline: state.get pulse/geo-queries

  - id: load-competitors
    pipeline: state.get pulse/competitors

  - id: load-prior-geo
    pipeline: state.get pulse/geo-share-history

  # Normalize the configured query set + emit an explicit count to gate the API loop.
  - id: geo-count
    command: >-
      node -e 'const q=JSON.parse(process.env.Q||"null")||[];
        const list=Array.isArray(q)?q:(q.queries||[]);
        process.stdout.write(JSON.stringify({count:list.length, queries:list}));'
    env:
      Q: "$load-geo-queries.json"

  # Real search API. Loop in node (curl cannot iterate). One sonar call per query ->
  # {query, response, sources_cited}. Unconditional so geo-analysis-input's
  # $geo-sweep.stdout always resolves; the zero-query short-circuit lives inside the
  # node (skips the API and the PERPLEXITY_API_KEY requirement, not the step). Always
  # exits 0; per-query errors are captured in-band so one failure never kills the sweep.
  - id: geo-sweep
    command: >-
      node -e 'const input=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const queries=input.queries||[];
        if(queries.length===0){ process.stdout.write(JSON.stringify({query_results:[], count:0})); }
        else {(async()=>{ const out=[];
          for (const q of queries) {
            try {
              const res=await fetch("https://api.perplexity.ai/chat/completions",{
                method:"POST",
                headers:{"Authorization":"Bearer "+process.env.PERPLEXITY_API_KEY,"Content-Type":"application/json"},
                body:JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]})});
              const j=await res.json();
              const content=(j.choices&&j.choices[0]&&j.choices[0].message&&j.choices[0].message.content)||"";
              out.push({query:q, platform:"perplexity", response:content, sources_cited:j.citations||[]});
            } catch(e){ out.push({query:q, platform:"perplexity", response:"", sources_cited:[], error:e.message}); }
          }
          process.stdout.write(JSON.stringify({query_results:out, count:out.length}));
        })();}'
    stdin: $geo-count.json

  # Assemble the monitor input. Reads the sweep when it ran (count>0), else falls
  # back to empty query_results — so geo-analysis runs unconditionally and its
  # .stdout always resolves. brand_name comes from the arg.
  - id: geo-analysis-input
    command: >-
      node -e 'const cnt=JSON.parse(process.env.CNT||"{}").count||0;
        const sweep=cnt>0?(JSON.parse(process.env.SWEEP||"{}")):{query_results:[]};
        const comp=JSON.parse(process.env.COMP||"null")||[];
        const prior=JSON.parse(process.env.PRIOR||"null")||[];
        const names=Array.isArray(comp)?comp:(comp.names||[]);
        process.stdout.write(JSON.stringify({query_results:sweep.query_results||[],
          brand_name:process.env.LOBSTER_ARG_BRAND_NAME||"",
          competitors:names, prior_weeks:prior}));'
    env:
      CNT: "$geo-count.json"
      SWEEP: "$geo-sweep.stdout"
      COMP: "$load-competitors.json"
      PRIOR: "$load-prior-geo.json"

  - id: geo-analysis
    command: node pulse-geo-monitor.js
    stdin: $geo-analysis-input.stdout

  # Append this week's Share-of-Model to the history index array (newest first),
  # capped to the last 26 weeks. Only persists when we actually swept (count>0) so a
  # zero-query week doesn't write a bogus 0% entry that corrupts the trend.
  - id: geo-history-next
    condition: $geo-count.json.count > 0
    command: >-
      node -e 'const hist=JSON.parse(process.env.HIST||"null")||[];
        const cur=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const entry={date:process.env.DATE, share_of_model:cur.share_of_model,
          cited_count:cur.cited_count, total_queries:cur.total_queries, trend_vs_prior:cur.trend_vs_prior};
        process.stdout.write(JSON.stringify([entry, ...hist].slice(0,26)));'
    env:
      HIST: "$load-prior-geo.json"
      DATE: "$stamp.json.date"
    stdin: $geo-analysis.stdout

  - id: geo-history
    condition: $geo-count.json.count > 0
    pipeline: state.set pulse/geo-share-history
    stdin: $geo-history-next.stdout

  # ============================================================================
  # SECTION 3 — Email performance (deterministic metrics; LLM synthesis deferred)
  # ============================================================================
  # Mailchimp/Resend/Postmark are click-to-connect; the agent loop caches sends.
  - id: load-email-sends
    pipeline: state.get pulse/email-sends

  # The analyzer handles an empty list itself (returns {patterns:[], note:...}).
  - id: email-input
    command: >-
      node -e 'const sends=JSON.parse(process.env.SENDS||"null")||[];
        process.stdout.write(JSON.stringify({email_sends:sends}));'
    env:
      SENDS: "$load-email-sends.json"

  - id: email-data
    command: node pulse-email-analyzer.js
    stdin: $email-input.stdout

  # ============================================================================
  # SECTION 4 — Voice-drift scan on the week's published content (deterministic)
  # ============================================================================
  - id: load-voice-doc
    pipeline: state.get pulse/brand-voice

  # batch_drift on empty pieces -> empty scores (script-safe), so unconditional.
  - id: voice-input
    command: >-
      node -e 'const pieces=JSON.parse(process.env.PIECES||"null")||[];
        const voice=JSON.parse(process.env.VOICE||"null")||{};
        process.stdout.write(JSON.stringify({mode:"batch_drift", pieces:pieces, voice_doc:voice}));'
    env:
      PIECES: "$load-published.json"
      VOICE: "$load-voice-doc.json"

  - id: voice-drift-scan
    command: node pulse-voice-scorer.js
    stdin: $voice-input.stdout

  # ============================================================================
  # SECTION 5 — Monitoring rollup from the week (read-only; produced by the sweep)
  # ============================================================================
  - id: load-monitoring
    pipeline: state.get pulse/monitoring

  # ============================================================================
  # SECTION 6 — Freshness refresh queue (deterministic date math)
  # ============================================================================
  # Empty archive -> empty queue (script-safe), so unconditional.
  - id: freshness-input
    command: >-
      node -e 'const archive=JSON.parse(process.env.ARCHIVE||"null")||[];
        process.stdout.write(JSON.stringify({content_archive:archive, citation_data:{}}));'
    env:
      ARCHIVE: "$load-published.json"

  - id: freshness-queue
    command: node pulse-freshness-tracker.js
    stdin: $freshness-input.stdout

  # ============================================================================
  # SECTION 7 — Queue the digest narrative + delivery for the agent loop
  # ============================================================================
  # The LLM narrative (digest prose + 3 action items), the email-pattern synthesis,
  # the voice tone judgment, the Slack post to #pulse-content, and the Google Sheet
  # history row ALL require the gateway/LLM and cannot run here. We assemble the full
  # deterministic digest payload and append ONE intent to pulse/notifications; the
  # agent loop picks it up, runs llm-task (thinking:low) for the narrative, and
  # delivers to Slack + Sheets. Every input below comes from an UNCONDITIONAL
  # producer, so the refs always resolve.
  - id: notify-load
    pipeline: state.get pulse/notifications

  - id: notify-next
    command: >-
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const perf=JSON.parse(process.env.PERF||"null")||{};
        const geo=JSON.parse(process.env.GEO||"null")||{};
        const email=JSON.parse(process.env.EMAIL||"null")||{};
        const voice=JSON.parse(process.env.VOICE||"null")||{};
        const monitoring=JSON.parse(process.env.MON||"null")||[];
        const fresh=JSON.parse(process.env.FRESH||"null")||{};
        const connected=(JSON.parse(process.env.PERFMETA||"{}").analytics_connected)===true;
        n.push({kind:"weekly_digest", date:process.env.DATE,
          needs_llm:true, deliver_to:"slack:#pulse-content", also_write_sheet:"Pulse Weekly Reports",
          analytics_connected:connected,
          notices: connected ? [] :
            ["No analytics connected — content-to-outcome attribution is empty. To show which content drove revenue, connect GA4/Stripe (biz-reporter) and PostHog in settings."],
          data:{
            content_performance: perf.top_3_by_outcome||[],
            total_attributed_revenue: perf.total_attributed_revenue||0,
            email, geo, voice_drift:voice, monitoring,
            freshness: fresh.refresh_queue||[]
          },
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: "$notify-load.json"
      PERF: "$connect-outcomes.stdout"
      PERFMETA: "$perf-input.stdout"
      GEO: "$geo-analysis.stdout"
      EMAIL: "$email-data.stdout"
      VOICE: "$voice-drift-scan.stdout"
      MON: "$load-monitoring.json"
      FRESH: "$freshness-queue.stdout"
      DATE: "$stamp.json.date"

  - id: notify
    pipeline: state.set pulse/notifications
    stdin: $notify-next.stdout

  # ============================================================================
  # SECTION 8 — Audit log (append-only index array; deterministic)
  # ============================================================================
  - id: audit-load
    pipeline: state.get pulse/audit-log

  - id: audit-next
    command: >-
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const geo=JSON.parse(process.env.GEO||"null")||{};
        const voice=JSON.parse(process.env.VOICE||"null")||{};
        const perf=JSON.parse(process.env.PERF||"null")||{};
        const fresh=JSON.parse(process.env.FRESH||"null")||{};
        log.push({action:"weekly_digest", date:process.env.DATE,
          share_of_model: geo.share_of_model!==undefined?geo.share_of_model:null,
          voice_drift_flagged: voice.flagged_pieces!==undefined?voice.flagged_pieces:null,
          top_content: perf.top_3_by_outcome||[],
          refresh_queue_size: fresh.flagged_count||0,
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log.slice(-500)));'
    env:
      LOG: "$audit-load.json"
      GEO: "$geo-analysis.stdout"
      VOICE: "$voice-drift-scan.stdout"
      PERF: "$connect-outcomes.stdout"
      FRESH: "$freshness-queue.stdout"
      DATE: "$stamp.json.date"

  - id: audit-log
    pipeline: state.set pulse/audit-log
    stdin: $audit-next.stdout
