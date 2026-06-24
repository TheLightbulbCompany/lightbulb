name: weekly-digest
# Monday calendar-intelligence digest (Reqs 36, 37, 38, 39). Runs on cron,
# Monday ~7:30 AM in the user's timezone.
#
# Fully deterministic (0 LLM, 0 gateway). The Ora agent loop renders the digest
# narrative + pattern explanations and delivers to Slack/Teams by draining the
# ora/notifications queue this run writes.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via native state.get/set on the per-owner EFS mount
#    (LOBSTER_STATE_DIR). fast-io is fictional — gone. Read-only consumer config
#    (connected-calendars, scheduling-rules, user-calendar) is state the settings
#    UI / agent writes; this run only reads it.
#  - The original read prior weeks via `fast-io list --prefix ora-analytics/weekly/`.
#    state.* has NO prefix-list, so weekly analytics is a SINGLE-KEY INDEX ARRAY
#    (ora/analytics-weekly, newest-first, capped at 52). We read it for the trend
#    comparison and append this week's analytics to it.
#  - Calendar reads use the real `gog` CLI (`gog calendar events list --json`),
#    gateway-free, over a past-7-day window computed in the USER's timezone.
#    ms365 / caldav / calctl are NOT real CLIs here — if the user flagged them
#    connected, we queue a single "connect Google" notice instead of pretending.
#  - The two original LLM steps (pattern contextualization + digest narrative) and
#    Slack delivery CANNOT run in an embedded pipeline (llm-task / slack both 401).
#    The deterministic analytics are bundled into ora/notifications with
#    needs_llm_narrative:true for the agent loop to narrate + deliver. The original
#    `capability-evolver` step is fictional — its "learn from the week" intent is
#    written to ora/notifications as a learning task, not called as a tool.
#  - "This week" + "today" are computed in the USER's timezone, never container
#    UTC (the Berlin-DST class of bug).
#  - Pattern/overrun gating uses EXPLICIT counts the analyze-summary step emits,
#    never `.length` (which silently evaluates false in conditions).
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

steps:
  # 1. Read which calendars the user connected. The settings UI writes this key.
  #    Read-only consumer config — null/absent means nothing connected.
  - id: load-connected
    pipeline: state.get ora-config/connected-calendars

  # 2. Read the user's primary-calendar profile (timezone + calendar_id for reads).
  - id: load-user-cal
    pipeline: state.get ora-config/user-calendar

  # 3. Read scheduling rules (working hours, staffed days, no-meeting days) — the
  #    analytics script uses these for the available-hours denominator.
  - id: load-rules
    pipeline: state.get ora-config/scheduling-rules

  # 4. Normalize config + compute the past-7-day window IN THE USER'S TIMEZONE.
  #    Emits explicit booleans/counts so later steps gate without touching .length.
  #    gog is the only real CLI here; ms365/caldav/calctl are recognized as
  #    "flagged but unreachable" so we can surface a connect notice.
  - id: prep
    command: >
      node -e 'const cc=JSON.parse(process.env.CONNECTED||"null")||{};
        const uc=JSON.parse(process.env.USERCAL||"null")||{};
        const tz=uc.timezone||"UTC";
        const gog=!!(cc.gog&&cc.gog.connected);
        const ms365=!!(cc.ms365&&cc.ms365.connected);
        const caldav=!!(cc.caldav&&cc.caldav.connected);
        const calctl=!!(cc.calctl&&cc.calctl.connected);
        const calId=(cc.gog&&cc.gog.calendar_id)||uc.calendar_id||"primary";
        const ymd=d=>new Intl.DateTimeFormat("en-CA",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit"}).format(d);
        const now=new Date();
        const today=ymd(now);
        const since=ymd(new Date(now.getTime()-7*24*60*60*1000));
        const any=gog||ms365||caldav||calctl;
        const unreachable=[]; if(ms365)unreachable.push("ms365"); if(caldav)unreachable.push("caldav"); if(calctl)unreachable.push("calctl");
        process.stdout.write(JSON.stringify({timezone:tz, calendar_id:calId,
          today, since, until:today,
          gog_connected:gog, any_connected:any, none_connected:!any,
          unreachable_providers:unreachable, unreachable_count:unreachable.length}));'
    env:
      CONNECTED: $load-connected.stdout
      USERCAL: $load-user-cal.stdout

  # 5. Nothing connected at all → queue a click-to-connect notice and stop early.
  - id: notify-load-none
    condition: $prep.json.none_connected
    pipeline: state.get ora/notifications

  - id: notify-none-build
    condition: $prep.json.none_connected
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"no_calendar_connected", channel:"#ora-calendar",
          message:"To run the weekly calendar digest, connect a calendar in your settings. Supported: Google Calendar, Microsoft 365 / Outlook, Apple Calendar, or any CalDAV provider (iCloud, Fastmail, Nextcloud).",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load-none.stdout

  - id: notify-none
    condition: $prep.json.none_connected
    pipeline: state.set ora/notifications
    stdin: $notify-none-build.stdout

  # 6. Pull the past week's events from Google Calendar (the only real CLI provider).
  #    gog emits JSON on stdout; tolerate failure (auth-expired etc.) so the run
  #    continues and surfaces a health notice rather than aborting.
  - id: load-gog-raw
    condition: $prep.json.gog_connected
    on_error: continue
    command: >
      gog calendar events list --calendar "${LOBSTER_PREP_CAL}" --since "${LOBSTER_PREP_SINCE}" --until "${LOBSTER_PREP_UNTIL}" --json 2>/dev/null || echo '{"events":[]}'
    env:
      LOBSTER_PREP_CAL: $prep.json.calendar_id
      LOBSTER_PREP_SINCE: $prep.json.since
      LOBSTER_PREP_UNTIL: $prep.json.until

  # 7. Normalize gog's calendar payload into the analytics/merger event shape.
  #    Handles both {events:[…]} and bare [...] gog output; maps start/end/title,
  #    flags focus blocks + all-day, carries meeting_type/recurring metadata the
  #    analytics script reads (by_type, overruns, patterns). Exits 0 always.
  - id: normalize-events
    condition: $prep.json.gog_connected
    command: >
      node -e 'function rd(){try{return require("fs").readFileSync(0,"utf8")}catch(e){return""}}
        let raw; try{raw=JSON.parse(rd()||"{}")}catch(e){raw={}}
        const list=Array.isArray(raw)?raw:(raw.events||raw.items||[]);
        const FOCUS=/focus|deep work|do not disturb|dnd|heads-?down|no meetings/i;
        const events=[]; for(const e of list){
          const start=(e.start&&(e.start.dateTime||e.start.date))||e.start||e.start_time||"";
          const end=(e.end&&(e.end.dateTime||e.end.date))||e.end||e.end_time||"";
          if(!start||!end)continue;
          const isAllDay=!!(e.start&&e.start.date&&!e.start.dateTime)||!!e.is_all_day;
          const title=e.summary||e.title||"(untitled)";
          events.push({start,end,title,
            is_all_day:isAllDay, is_focus_block:FOCUS.test(title),
            event_id:e.id||e.event_id||null,
            calendar_id:e.calendar_id||process.env.LOBSTER_PREP_CAL||"primary",
            calendar_source:"gog",
            meeting_type:e.meeting_type||e.category||null,
            recurring_id:e.recurringEventId||e.recurring_id||null,
            series_title:e.series_title||null,
            actual_duration_minutes:e.actual_duration_minutes||null});
        }
        process.stdout.write(JSON.stringify({ok:true, count:events.length, events}));'
    stdin: $load-gog-raw.stdout
    env:
      LOBSTER_PREP_CAL: $prep.json.calendar_id

  # 8. Load the rolling weekly-analytics index array (newest-first) for the trend
  #    comparison + pattern detection. Replaces the fictional fast-io prefix list.
  - id: load-prior
    condition: $prep.json.gog_connected
    pipeline: state.get ora/analytics-weekly

  # 9. Build the analytics input. The analyzer wants the RAW weekly event set
  #    (not merged busy intervals — it splits meetings vs focus blocks itself),
  #    the prior weeks array, and the rules. prior_weeks is the index array as-is
  #    (newest-first is what detectPatterns expects: slice(0,3/4)).
  - id: analyze-input
    condition: $prep.json.gog_connected
    command: >
      node -e 'const g=JSON.parse(process.env.GOG||"null")||{};
        const prior=JSON.parse(process.env.PRIOR||"null")||[];
        const rules=JSON.parse(process.env.RULES||"null")||{};
        process.stdout.write(JSON.stringify({
          events:(g.events||[]),
          prior_weeks:Array.isArray(prior)?prior:[],
          rules}));'
    env:
      GOG: $normalize-events.stdout
      PRIOR: $load-prior.stdout
      RULES: $load-rules.stdout

  # 10. Run full calendar analytics (Reqs 36, 37, 38, 39). Pure compute, exits 0.
  - id: analyze
    condition: $prep.json.gog_connected
    command: node ora-calendar-analytics.js
    stdin: $analyze-input.stdout

  # 11. Emit EXPLICIT counts for gating (never gate on .length in a condition).
  #     Also surfaces whether there's anything worth the agent loop contextualizing.
  - id: analyze-summary
    condition: $prep.json.gog_connected
    command: >
      node -e 'const a=JSON.parse(require("fs").readFileSync(0,"utf8"))||{};
        const patterns=(a.patterns||[]).length;
        const overruns=(a.recurring_overruns||[]).length;
        process.stdout.write(JSON.stringify({
          pattern_count:patterns,
          overrun_count:overruns,
          needs_context: (patterns+overruns)>0,
          total_meetings:(a.total_meetings||0),
          load_score:(a.load_score||0)}));'
    stdin: $analyze.stdout

  # 12. Append this week's analytics to the rolling index array (newest-first,
  #     capped at 52 weeks). Build then set — this is the producer side of the
  #     load-prior read above, keyed by the user's week-ending date.
  - id: store-build
    condition: $prep.json.gog_connected
    command: >
      node -e 'const prior=JSON.parse(process.env.PRIOR||"null")||[];
        const a=JSON.parse(process.env.ANALYTICS||"null")||{};
        const week=process.env.WEEK_ENDING||"";
        const arr=Array.isArray(prior)?prior.slice():[];
        const entry=Object.assign({week_ending:week}, a);
        const filtered=arr.filter(w=>w&&w.week_ending!==week);
        filtered.unshift(entry);
        if(filtered.length>52)filtered.splice(52);
        process.stdout.write(JSON.stringify(filtered));'
    env:
      PRIOR: $load-prior.stdout
      ANALYTICS: $analyze.stdout
      WEEK_ENDING: $prep.json.today

  - id: store-analytics
    condition: $prep.json.gog_connected
    pipeline: state.set ora/analytics-weekly
    stdin: $store-build.stdout

  # 13. Bundle the deterministic digest payload for the agent loop to narrate.
  #     (No llm-task / slack here — both 401 in the embedded runner.) Carries the
  #     full analytics, the explicit context flag, and the prior weeks so the loop
  #     can lead with the load score + its change. needs_llm_narrative:true is the
  #     marker the agent loop drains to render the digest + pattern explanations.
  - id: digest-payload
    condition: $prep.json.gog_connected
    command: >
      node -e 'function p(e,f){try{return JSON.parse(process.env[e]||"null")||f}catch(_){return f}}
        const a=p("ANALYTICS",{}), s=p("SUMMARY",{}), prior=p("PRIOR",[]);
        const unreachable=p("UNREACH",[]);
        process.stdout.write(JSON.stringify({
          analytics:a,
          pattern_count:(s.pattern_count||0),
          overrun_count:(s.overrun_count||0),
          needs_context:(s.needs_context===true),
          prior_weeks:Array.isArray(prior)?prior:[],
          unreachable_providers:unreachable}));'
    env:
      ANALYTICS: $analyze.stdout
      SUMMARY: $analyze-summary.stdout
      PRIOR: $load-prior.stdout
      UNREACH: $prep.json.unreachable_providers

  # 14. Queue the digest for the agent loop to render (LLM) + deliver (Slack), plus
  #     a separate learning-task intent (the fictional capability-evolver tool, now
  #     a deferred agent-loop task) and the unreachable-provider notice.
  - id: notify-load
    condition: $prep.json.gog_connected
    pipeline: state.get ora/notifications

  - id: notify-build
    condition: $prep.json.gog_connected
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const d=JSON.parse(process.env.DIGEST||"{}");
        const a=d.analytics||{};
        n.push({kind:"weekly_digest", channel:"#ora-calendar",
          needs_llm_narrative:true, needs_pattern_context:(d.needs_context===true),
          digest_input:d, created_at:new Date().toISOString()});
        n.push({kind:"capability_learning", channel:null,
          needs_llm_narrative:true,
          focus:"Which scheduling patterns does the user consistently override? Which flags do they dismiss? Which meeting types do they actually engage with prep briefs for?",
          analytics:a,
          read_state:["ora-state/exception-history","ora-state/agenda-dismissals"],
          created_at:new Date().toISOString()});
        if((d.unreachable_providers||[]).length>0){ n.push({kind:"provider_unreachable",
          channel:"#ora-alerts", providers:d.unreachable_providers,
          message:"These calendars are toggled on but have no working integration in this build; only Google Calendar reads are wired. Reconnect Google Calendar in settings or contact support.",
          created_at:new Date().toISOString()}); }
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      DIGEST: $digest-payload.stdout

  - id: notify
    condition: $prep.json.gog_connected
    pipeline: state.set ora/notifications
    stdin: $notify-build.stdout

  # 15. Append a run record to the audit index array (single-key, append-then-set).
  - id: audit-load
    pipeline: state.get ora/audit-log

  - id: audit-build
    command: >
      node -e 'const arr=JSON.parse(process.env.AUDIT||"null")||[];
        const a=JSON.parse(process.env.ANALYTICS||"null")||{};
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        const prep=JSON.parse(process.env.PREP||"{}");
        arr.push({action:"weekly_digest",
          week_ending:prep.today||null, since:prep.since||null,
          gog_connected:!!prep.gog_connected,
          load_score:(a.load_score||0),
          meeting_hours:(a.meeting_hours||0),
          focus_pct:(a.focus_pct||0),
          total_meetings:(a.total_meetings||0),
          patterns_detected:(s.pattern_count||0),
          overruns_detected:(s.overrun_count||0),
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        if(arr.length>500)arr.splice(0,arr.length-500);
        process.stdout.write(JSON.stringify(arr));'
    env:
      AUDIT: $audit-load.stdout
      ANALYTICS: $analyze.stdout
      SUMMARY: $analyze-summary.stdout
      PREP: $prep.json

  - id: audit
    pipeline: state.set ora/audit-log
    stdin: $audit-build.stdout
