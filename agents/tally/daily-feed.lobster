name: daily-feed
# Tally's daily bank-feed pipeline — deterministic, gateway-free, Tier-1 read-only.
# Runs on cron (daily) + on-demand. It NEVER posts to the ledger: it pulls
# transactions, categorizes against the vendor map, flags anomalies / duplicates,
# tags tax, batches what is approvable, and queues everything that needs a human
# decision (the Tier-2 writes) onto a notifications state-queue for the agent loop.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via native state.get/set on the per-owner EFS mount. fast-io is
#    fictional. Every former `tally-config/*` / `tally-learning/*` key is now a
#    native state key under the `tally/` namespace (read-only consumer config the
#    user sets in settings, plus the agent's own learned/queue keys).
#  - LLM categorization (unknown vendors), Slack/Teams delivery, and ledger writes
#    CANNOT happen in an embedded pipeline (all 401). The pipeline stays deterministic
#    and writes those intents to `tally/notifications` for the agent loop to handle.
#  - Plaid / Stripe / Perplexity are reached gateway-free via `curl` with their API
#    keys from process.env. Stripe is read-only. Bank/Stripe feeds degrade to empty
#    (not failure) when the credential is absent — the activation gate already reports it.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.
#    tally-activation-check.js carries the gate decision in result.pass (exit 0 even on a
#    failed gate). The `activation` wrapper below normalizes its output to guaranteed JSON.
#  - Dates are computed in the USER's timezone (tally/timezone), never container UTC.

args:
  trigger:
    required: false
    default: cron        # "cron" | "webhook" — informational; both run the same path.

steps:
  # 1. Load activation config (connections the user wired in settings). Read-only
  #    consumer state. Single object key, shaped for tally-activation-check.js.
  - id: activation-config
    pipeline: state.get tally/activation

  # 2. Activation gate. tally-activation-check.js carries the gate decision in result.pass
  #    (exit 0 either way). The wrapper normalizes its output to guaranteed-parseable JSON
  #    and supplies a fallback object if it ever emits nothing. Branch on $activation.json.pass.
  - id: activation
    command: >
      node -e 'const {execSync}=require("child_process");
        let out="{}";
        try { out=execSync("node tally-activation-check.js",
          {input:process.env.CFG||"null", encoding:"utf8"}); }
        catch(e){ out=(e.stdout&&e.stdout.toString())||"{}"; }
        let j; try { j=JSON.parse(out); } catch(_) { j={pass:false,blockers:[{check:"parse",reason:"activation check produced no JSON"}]}; }
        process.stdout.write(JSON.stringify(j));'
    env:
      CFG: $activation-config.stdout

  # 3. Compute today / yesterday in the USER's timezone (NOT container UTC).
  #    tally/timezone is a read-only config key (IANA name, e.g. "America/New_York");
  #    default to UTC if unset. Emits ISO date strings the feeds use as a window.
  - id: dates
    condition: $activation.json.pass
    command: >
      node -e 'const cfg=JSON.parse(process.env.TZCFG||"null")||{};
        const tz=(cfg&&cfg.timezone)||"UTC";
        function ymd(d){ const p=new Intl.DateTimeFormat("en-CA",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(d);
          const g=t=>p.find(x=>x.type===t).value; return g("year")+"-"+g("month")+"-"+g("day"); }
        const now=new Date(); const y=new Date(now.getTime()-86400000);
        process.stdout.write(JSON.stringify({today:ymd(now), yesterday:ymd(y), tz}));'
    env:
      TZCFG: $activation-config.stdout

  # 4. Pull the Plaid bank feed (read-only). curl-only; degrades to an empty feed
  #    when PLAID_* env is absent so the rest of the pipeline still runs. Normalizes
  #    Plaid's {transactions:[...]} into a flat array the engine scripts expect.
  - id: bank-feed
    condition: $activation.json.pass
    command: >
      node -e 'const {execSync}=require("child_process");
        const id=process.env.PLAID_CLIENT_ID, sec=process.env.PLAID_SECRET, tok=process.env.PLAID_ACCESS_TOKEN;
        const start=process.env.START, end=process.env.END;
        if(!id||!sec||!tok){ process.stdout.write(JSON.stringify({transactions:[],count:0,source:"plaid",connected:false})); process.exit(0); }
        let raw="{}";
        try { raw=execSync("curl -sS -X POST https://production.plaid.com/transactions/get "
          +"-H \"Content-Type: application/json\" -d @-",
          {input:JSON.stringify({client_id:id,secret:sec,access_token:tok,start_date:start,end_date:end,options:{count:500}}),
           encoding:"utf8",timeout:60000}); } catch(e){ raw="{}"; }
        let j={}; try { j=JSON.parse(raw); } catch(_){ j={}; }
        const txns=(j.transactions||[]).map(t=>({
          id:t.transaction_id, transaction_id:t.transaction_id,
          vendor:t.merchant_name||t.name||"", amount:Math.abs(t.amount||0),
          date:t.date, source:"plaid", account_id:t.account_id, category:""}));
        process.stdout.write(JSON.stringify({transactions:txns,count:txns.length,source:"plaid",connected:true,error:j.error_message||null}));'
    env:
      START: $dates.json.yesterday
      END: $dates.json.today

  # 5. Pull Stripe charges (read-only revenue feed). curl with STRIPE_API_KEY.
  #    Degrades to empty when unset. Emits a normalized {charges,count} for the
  #    dashboard/metrics step and for duplicate cross-source detection.
  - id: stripe-feed
    condition: $activation.json.pass
    command: >
      node -e 'const {execSync}=require("child_process");
        const key=process.env.STRIPE_API_KEY;
        if(!key){ process.stdout.write(JSON.stringify({charges:[],count:0,source:"stripe",connected:false})); process.exit(0); }
        const since=process.env.SINCE; const created=Math.floor(new Date(since+"T00:00:00Z").getTime()/1000)||0;
        let raw="{}";
        try { raw=execSync("curl -sS -G https://api.stripe.com/v1/charges "
          +"-u "+key+": -d limit=100 -d created[gte]="+created,
          {encoding:"utf8",timeout:60000}); } catch(e){ raw="{}"; }
        let j={}; try { j=JSON.parse(raw); } catch(_){ j={}; }
        const charges=(j.data||[]).map(c=>({
          id:c.id, transaction_id:c.id, vendor:(c.billing_details&&c.billing_details.name)||c.description||"stripe",
          amount:(c.amount||0)/100, date:new Date((c.created||0)*1000).toISOString().slice(0,10),
          source:"stripe", invoice_number:c.invoice||null}));
        process.stdout.write(JSON.stringify({charges,count:charges.length,source:"stripe",connected:true}));'
    env:
      SINCE: $dates.json.yesterday

  # 6. Load the learned vendor map + chart of accounts (read-only consumer state).
  - id: vendor-map
    condition: $activation.json.pass
    pipeline: state.get tally/vendor-map

  - id: chart-of-accounts
    condition: $activation.json.pass
    pipeline: state.get tally/chart-of-accounts

  - id: vendor-history
    condition: $activation.json.pass
    pipeline: state.get tally/vendor-history

  - id: dismissal-history
    condition: $activation.json.pass
    pipeline: state.get tally/dismissal-history

  - id: approval-prefs
    condition: $activation.json.pass
    pipeline: state.get tally/approval-preferences

  # 7. Categorize each bank transaction deterministically (vendor map → category).
  #    tally-categorization-engine.js runs per-extraction in "check" mode, so we map
  #    over the feed inline and call it once per txn, collecting the routes. Anything
  #    that comes back route != "standard_approval" is queued for LLM/agent-loop review
  #    (unknown-vendor categorization is the only LLM call in the original; it cannot
  #    run in-pipeline, so it becomes a notification intent).
  - id: categorize
    condition: $bank-feed.json.count > 0
    command: >
      node -e 'const {execSync}=require("child_process");
        const feed=JSON.parse(process.env.FEED||"{}").transactions||[];
        const vmap=JSON.parse(process.env.VMAP||"null")||{};
        const out=[]; let needsReview=0;
        for(const tx of feed){
          const inp={mode:"check", vendor_map:vmap, extraction:{
            amount:tx.amount, vendor:tx.vendor||"unknown", category:"", confidence_score:undefined}};
          let r="{}";
          try { r=execSync("node tally-categorization-engine.js",{input:JSON.stringify(inp),encoding:"utf8"}); } catch(e){ r=(e.stdout&&e.stdout.toString())||"{}"; }
          let j; try { j=JSON.parse(r); } catch(_){ j={confidence_check:{route:"individual_review"}}; }
          const route=(j.confidence_check&&j.confidence_check.route)||"individual_review";
          if(route!=="standard_approval") needsReview++;
          out.push({transaction_id:tx.transaction_id, vendor:tx.vendor, amount:tx.amount, date:tx.date,
            category:j.category||null, subcategory:j.subcategory||null,
            mapping_source:j.mapping_source||"llm_extraction", route, confidence_check:j.confidence_check||{}});
        }
        process.stdout.write(JSON.stringify({categorized:out, count:out.length, needs_review_count:needsReview}));'
    env:
      FEED: $bank-feed.stdout
      VMAP: $vendor-map.stdout

  # 8. Anomaly scan over the whole feed (6 deterministic signal types).
  - id: anomaly-input
    condition: $bank-feed.json.count > 0
    command: >
      node -e 'const feed=JSON.parse(process.env.FEED||"{}").transactions||[];
        process.stdout.write(JSON.stringify({transactions:feed,
          vendor_history:JSON.parse(process.env.VH||"null")||{},
          chart_of_accounts:JSON.parse(process.env.COA||"null")||[],
          dismissal_history:JSON.parse(process.env.DH||"null")||{}}));'
    env:
      FEED: $bank-feed.stdout
      VH: $vendor-history.stdout
      COA: $chart-of-accounts.stdout
      DH: $dismissal-history.stdout

  - id: anomaly-scan
    condition: $bank-feed.json.count > 0
    command: node tally-anomaly-detector.js
    stdin: $anomaly-input.stdout

  # 9. Duplicate detection (bank feed self-dupes + bank/stripe cross-source dupes).
  - id: recon-input
    condition: $bank-feed.json.count > 0
    command: >
      node -e 'const bank=JSON.parse(process.env.FEED||"{}").transactions||[];
        const stripe=(JSON.parse(process.env.STRIPE||"{}").charges)||[];
        process.stdout.write(JSON.stringify({ledger_entries:[], bank_entries:bank, all_entries:bank.concat(stripe)}));'
    env:
      FEED: $bank-feed.stdout
      STRIPE: $stripe-feed.stdout

  - id: duplicate-check
    condition: $bank-feed.json.count > 0
    command: node tally-reconciliation.js
    stdin: $recon-input.stdout

  # 10. Tax tagging — tag each transaction by deduction category + collect CPA flags.
  #     tally-tax-tracker.js "tag" mode is per-transaction, so map over the categorized
  #     set inline and aggregate the cpa_flags.
  - id: tax-tag
    condition: $categorize.json.count > 0
    command: >
      node -e 'const {execSync}=require("child_process");
        const cats=JSON.parse(process.env.CATS||"{}").categorized||[];
        const ytd=JSON.parse(process.env.YTD||"null")||{};
        const tagged=[]; const cpaFlags=[];
        for(const tx of cats){
          const inp={mode:"tag", transaction:{vendor:tx.vendor, amount:tx.amount, category:tx.category||""}, ytd_by_vendor:ytd};
          let r="{}";
          try { r=execSync("node tally-tax-tracker.js",{input:JSON.stringify(inp),encoding:"utf8"}); } catch(e){ r=(e.stdout&&e.stdout.toString())||"{}"; }
          let j; try { j=JSON.parse(r); } catch(_){ j={}; }
          tagged.push({transaction_id:tx.transaction_id, tax_tag:j.tax_tag||null});
          for(const f of (j.cpa_flags||[])) cpaFlags.push({transaction_id:tx.transaction_id, vendor:tx.vendor, amount:tx.amount, ...f});
        }
        process.stdout.write(JSON.stringify({tagged, tagged_count:tagged.length, cpa_flags:cpaFlags, cpa_flag_count:cpaFlags.length}));'
    env:
      CATS: $categorize.stdout
      YTD: $vendor-history.stdout

  # 11. Batch approvals — split the categorized+flagged set into auto-batchable vs
  #     individual-review. Attach anomaly flags so the batcher routes flagged txns
  #     to individual review. This produces the Tier-2 *request*, NOT a write.
  - id: batch-input
    condition: $categorize.json.count > 0
    command: >
      node -e 'const cats=JSON.parse(process.env.CATS||"{}").categorized||[];
        const anom=(JSON.parse(process.env.ANOM||"{}").results)||[];
        const flagsById={}; for(const r of anom) flagsById[r.transaction_id]=r.anomaly_flags||[];
        const entries=cats.map(t=>({transaction_id:t.transaction_id, vendor:t.vendor, amount:t.amount,
          date:t.date, category:t.category, anomaly_flags:flagsById[t.transaction_id]||[]}));
        process.stdout.write(JSON.stringify({entries,
          vendor_map:JSON.parse(process.env.VMAP||"null")||{},
          approval_preferences:JSON.parse(process.env.PREFS||"null")||{}}));'
    env:
      CATS: $categorize.stdout
      ANOM: $anomaly-scan.stdout
      VMAP: $vendor-map.stdout
      PREFS: $approval-prefs.stdout

  - id: batch-approvals
    condition: $categorize.json.count > 0
    command: node tally-approval-batcher.js
    stdin: $batch-input.stdout

  # 12. Quarterly tax-deadline check (read-only). Reads the user's YTD income/deduction
  #     state; the script computes the 30-day window. Date passed in user-tz.
  - id: tax-state
    condition: $activation.json.pass
    pipeline: state.get tally/tax-state

  - id: deadline-input
    condition: $activation.json.pass
    command: >
      node -e 'const ts=JSON.parse(process.env.TS||"null")||{};
        process.stdout.write(JSON.stringify({mode:"deadline", current_date:process.env.TODAY,
          tax_rate:ts.tax_rate||0.25, ytd_income:ts.ytd_income||0, ytd_deductions:ts.ytd_deductions||0}));'
    env:
      TS: $tax-state.stdout
      TODAY: $dates.json.today

  - id: tax-deadline
    condition: $activation.json.pass
    command: node tally-tax-tracker.js
    stdin: $deadline-input.stdout

  # 13. Dashboard metrics (read-only math). Feeds the live financial state +
  #     stripe MRR into tally-metrics-calculator.js. Writing the Google Sheet is a
  #     side effect routed below to the notifications queue (gog sheets-write needs
  #     the agent loop / a connected sheet, not an in-pipeline gateway call).
  - id: financial-state
    condition: $activation.json.pass
    pipeline: state.get tally/financial-data

  - id: metrics-input
    condition: $activation.json.pass
    command: >
      node -e 'const fd=JSON.parse(process.env.FD||"null")||{};
        const stripeCount=JSON.parse(process.env.STRIPE||"{}").count||0;
        fd.stripe=fd.stripe||{}; fd.stripe.stripe_charges_today=stripeCount;
        process.stdout.write(JSON.stringify(fd));'
    env:
      FD: $financial-state.stdout
      STRIPE: $stripe-feed.stdout

  - id: metrics
    condition: $activation.json.pass
    command: node tally-metrics-calculator.js
    stdin: $metrics-input.stdout

  # 14. Emit explicit counts so the notification queue can gate without using .length.
  - id: tally-counts
    condition: $activation.json.pass
    command: >
      node -e 'const j=x=>{try{return JSON.parse(x||"null")||{};}catch(_){return {};}};
        const feed=j(process.env.FEED), cat=j(process.env.CAT), anom=j(process.env.ANOM),
          dup=j(process.env.DUP), tax=j(process.env.TAX), batch=j(process.env.BATCH),
          dl=j(process.env.DL), met=j(process.env.MET);
        const metricAlerts=(met.alerts&&met.alerts.length)||0;
        process.stdout.write(JSON.stringify({
          transactions:feed.count||0,
          categorized:cat.count||0,
          needs_review:cat.needs_review_count||0,
          anomalies_flagged:anom.flagged_count||0,
          anomalies_warning:anom.warning_count||0,
          duplicates:(dup.summary&&dup.summary.duplicates_found)||0,
          cpa_flags:tax.cpa_flag_count||0,
          batch_count:(batch.batch&&batch.batch.count)||0,
          individual_count:(batch.individual&&batch.individual.count)||0,
          deadline_upcoming:dl.has_upcoming?1:0,
          metric_alerts:metricAlerts,
          notify:1}));'
    env:
      FEED: $bank-feed.stdout
      CAT: $categorize.stdout
      ANOM: $anomaly-scan.stdout
      DUP: $duplicate-check.stdout
      TAX: $tax-tag.stdout
      BATCH: $batch-approvals.stdout
      DL: $tax-deadline.stdout
      MET: $metrics.stdout

  # 15. Append a single delivery intent onto tally/notifications for the agent loop.
  #     This is where Slack delivery (approvals digest, anomaly alerts, tax reminder,
  #     metric alerts) + the gog Sheets dashboard write + unknown-vendor LLM review
  #     all get handed off — none of them can run inside an embedded pipeline.
  - id: notify-load
    condition: $tally-counts.json.notify > 0
    pipeline: state.get tally/notifications

  - id: notify-next
    condition: $tally-counts.json.notify > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const c=JSON.parse(process.env.COUNTS||"{}");
        const batch=JSON.parse(process.env.BATCH||"null")||{};
        const anom=JSON.parse(process.env.ANOM||"null")||{};
        const dup=JSON.parse(process.env.DUP||"null")||{};
        const tax=JSON.parse(process.env.TAX||"null")||{};
        const dl=JSON.parse(process.env.DL||"null")||{};
        const met=JSON.parse(process.env.MET||"null")||{};
        const cat=JSON.parse(process.env.CAT||"null")||{};
        n.push({kind:"daily_feed_review",
          created_at:new Date().toISOString(),
          date:process.env.TODAY,
          summary:c,
          deliver:{
            approvals:{channel:"#tally-approvals", batch:batch.batch||null, individual:batch.individual||null},
            anomalies:{channel:"#tally-alerts", warning_count:c.anomalies_warning||0, results:anom.results||[]},
            tax_deadline:{channel:"#tally-alerts", has_upcoming:!!(dl.has_upcoming), alerts:dl.alerts||[]},
            metrics:{channel:"#tally-digest", dashboard:"Tally Dashboard/Live", metrics:met.metrics||{}, alerts:met.alerts||[], sheet_write_pending:true},
            cpa_flags:tax.cpa_flags||[],
            duplicates:dup.duplicates||[],
            unknown_vendor_review:(cat.categorized||[]).filter(t=>t.route!=="standard_approval")
          }});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      COUNTS: $tally-counts.stdout
      BATCH: $batch-approvals.stdout
      ANOM: $anomaly-scan.stdout
      DUP: $duplicate-check.stdout
      TAX: $tax-tag.stdout
      DL: $tax-deadline.stdout
      MET: $metrics.stdout
      CAT: $categorize.stdout
      TODAY: $dates.json.today

  - id: notify
    condition: $tally-counts.json.notify > 0
    pipeline: state.set tally/notifications
    stdin: $notify-next.stdout

  # 16. Append an audit-trail entry (7-year retention requirement) to the single-key
  #     audit index array. Was fast-io `audit/{{timestamp}}/...`; now an index array.
  - id: audit-load
    condition: $activation.json.pass
    pipeline: state.get tally/audit-log

  - id: audit-next
    condition: $activation.json.pass
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const c=JSON.parse(process.env.COUNTS||"{}");
        a.push({action:"daily_feed", date:process.env.TODAY, timestamp:new Date().toISOString(),
          transactions_processed:c.transactions||0, categorized:c.categorized||0,
          anomalies_flagged:c.anomalies_flagged||0, duplicates_found:c.duplicates||0,
          cpa_flags:c.cpa_flags||0, authorized_by:"autonomous"});
        if(a.length>5000) a.splice(0, a.length-5000);
        process.stdout.write(JSON.stringify(a));'
    env:
      AUDIT: $audit-load.stdout
      COUNTS: $tally-counts.stdout
      TODAY: $dates.json.today

  - id: audit
    condition: $activation.json.pass
    pipeline: state.set tally/audit-log
    stdin: $audit-next.stdout
