name: morning-briefing
# Daily calendar health check + briefing. Runs on cron (7 AM in the user's tz).
# Fully deterministic (0 LLM, 0 gateway). The Ora agent loop renders the narrative
# and delivers to Slack/Teams by draining the ora/notifications queue this run writes.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via native state.get/set on the per-owner EFS mount
#    (LOBSTER_STATE_DIR). fast-io is fictional — gone. Read-only consumer config
#    (connected-calendars, scheduling-rules, user-calendar) is state the settings
#    UI / agent writes; this run only reads it. Notifications + audit are single-key
#    INDEX ARRAYS this run appends to (state.* has no prefix-list).
#  - Calendar reads use the real `gog` CLI (`gog calendar events list --json`),
#    gateway-free. ms365 / caldav / calctl are NOT real CLIs here — if the user has
#    them flagged connected, we queue a single "connect Google" notice instead of
#    pretending to read them.
#  - LLM judgment (briefing narrative) + Slack/Teams delivery CANNOT run in an
#    embedded pipeline (llm-task / slack both 401). The deterministic facts are
#    written to ora/notifications for the agent loop to narrate + deliver.
#  - "Today" is computed in the USER'S timezone (ora-config/user-calendar.timezone),
#    never container UTC — the Berlin-DST class of bug.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.
#    A couple of Ora scripts still process.exit(1) on their normal path (conflict
#    detector exits 1 when conflicts EXIST; connection-health exits 1 when red) —
#    those steps carry on_error: continue so a found-conflict can't kill the run.
#    The .js fix (exit 0 always) is tracked separately.

steps:
  # 1. Read which calendars the user connected. The settings UI writes this key.
  #    Read-only consumer config — null/absent means nothing connected.
  - id: load-connected
    pipeline: state.get ora-config/connected-calendars

  # 2. Read the user's primary-calendar profile (timezone + calendar_id for reads).
  - id: load-user-cal
    pipeline: state.get ora-config/user-calendar

  # 3. Read scheduling rules (buffer, working hours, focus block, no-meeting days).
  - id: load-rules
    pipeline: state.get ora-config/scheduling-rules

  # 4. Normalize config + compute today's date IN THE USER'S TIMEZONE.
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
        const today=new Intl.DateTimeFormat("en-CA",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
        const any=gog||ms365||caldav||calctl;
        const unreachable=[]; if(ms365)unreachable.push("ms365"); if(caldav)unreachable.push("caldav"); if(calctl)unreachable.push("calctl");
        process.stdout.write(JSON.stringify({timezone:tz, calendar_id:calId, today,
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
          message:"To run morning briefings, connect a calendar in your settings. Supported: Google Calendar, Microsoft 365 / Outlook, Apple Calendar, or any CalDAV provider (iCloud, Fastmail, Nextcloud).",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load-none.stdout

  - id: notify-none
    condition: $prep.json.none_connected
    pipeline: state.set ora/notifications
    stdin: $notify-none-build.stdout

  # 6. Pull today's events from Google Calendar (the only real CLI provider).
  #    gog emits JSON on stdout; we tolerate failure (auth-expired etc.) so the
  #    run continues and surfaces a health notice rather than aborting.
  - id: load-gog-raw
    condition: $prep.json.gog_connected
    on_error: continue
    command: >
      gog calendar events list --calendar "${LOBSTER_PREP_CAL}" --date "${LOBSTER_PREP_TODAY}" --json 2>/dev/null || echo '{"events":[]}'
    env:
      LOBSTER_PREP_CAL: $prep.json.calendar_id
      LOBSTER_PREP_TODAY: $prep.json.today

  # 7. Normalize gog's calendar payload into the merger's events_by_source shape.
  #    Handles both {events:[…]} and bare [...] gog output; maps start/end/title,
  #    flags focus blocks by title heuristic, carries attendees + video link +
  #    description through for downstream quality/video-link checks. Exits 0 always.
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
          const atts=(e.attendees||[]).map(a=>({email:a.email||"",external:!!a.external,response:a.responseStatus||""}));
          const video=e.hangoutLink||e.video_link||((e.conferenceData&&e.conferenceData.entryPoints&&e.conferenceData.entryPoints[0]&&e.conferenceData.entryPoints[0].uri)||"")||"";
          events.push({start,end,title,
            is_all_day:isAllDay, is_focus_block:FOCUS.test(title),
            event_id:e.id||e.event_id||null, calendar_id:e.calendar_id||process.env.LOBSTER_PREP_CAL||"primary",
            calendar_source:"gog", attendees:atts, video_link:video,
            description:e.description||"", notes:e.notes||"",
            recurring_id:e.recurringEventId||e.recurring_id||null});
        }
        process.stdout.write(JSON.stringify({ok:true, count:events.length, events}));'
    stdin: $load-gog-raw.stdout
    env:
      LOBSTER_PREP_CAL: $prep.json.calendar_id

  # 8. Merge every connected source into a unified non-overlapping busy set.
  #    Only gog is wired (the others are unreachable CLIs); the merger handles a
  #    single source fine. block_all_day:false drops all-day events from busy.
  #    Build the merger input FIRST (it consumes normalize-events), then merge.
  - id: merge-input
    condition: $prep.json.gog_connected
    command: >
      node -e 'const g=JSON.parse(process.env.GOG||"null")||{};
        process.stdout.write(JSON.stringify({mode:"merge_busy",
          events_by_source:{gog:(g.events||[])}, block_all_day:false}));'
    env:
      GOG: $normalize-events.stdout

  - id: merge-events
    condition: $prep.json.gog_connected
    command: node ora-availability-merger.js
    stdin: $merge-input.stdout

  # 9. Conflict detection across the merged set (Req 10).
  #    Detector currently exits 1 when conflicts exist (self-DoS) → on_error:continue
  #    so a found conflict can't abort the run. Pass busy_intervals as `events`.
  - id: conflict-input
    condition: $prep.json.gog_connected
    command: >
      node -e 'const m=JSON.parse(process.env.MERGE||"null")||{};
        process.stdout.write(JSON.stringify({events:(m.busy_intervals||[])}));'
    env:
      MERGE: $merge-events.stdout

  - id: conflict-check
    condition: $prep.json.gog_connected
    on_error: continue
    command: node ora-conflict-detector.js
    stdin: $conflict-input.stdout

  # 10. Buffer-violation check (Req 11). Exits 0 cleanly.
  - id: buffer-input
    condition: $prep.json.gog_connected
    command: >
      node -e 'const m=JSON.parse(process.env.MERGE||"null")||{};
        const r=JSON.parse(process.env.RULES||"null")||{};
        process.stdout.write(JSON.stringify({events:(m.busy_intervals||[]),
          buffer_minutes:(r.buffer_minutes||15)}));'
    env:
      MERGE: $merge-events.stdout
      RULES: $load-rules.stdout

  - id: buffer-check
    condition: $prep.json.gog_connected
    command: node ora-buffer-checker.js
    stdin: $buffer-input.stdout

  # 11. Focus-block presence + explicit count (Req 8). Inline node, exits 0.
  - id: focus-check
    condition: $prep.json.gog_connected
    command: >
      node -e 'const iv=(JSON.parse(require("fs").readFileSync(0,"utf8")).busy_intervals)||[];
        const fb=iv.filter(x=>x.is_focus_block);
        process.stdout.write(JSON.stringify({has_focus_block:fb.length>0,
          focus_blocks:fb.length, needs_suggestion:fb.length===0}));'
    stdin: $merge-events.stdout

  # 12. If no focus block, find free slots to suggest one (deterministic).
  #     Timezone comes from the USER profile, not the container. Gated on the
  #     explicit needs_suggestion flag (never on .length).
  - id: suggest-input
    condition: $prep.json.gog_connected && $focus-check.json.needs_suggestion
    command: >
      node -e 'const m=JSON.parse(process.env.MERGE||"null")||{};
        const r=JSON.parse(process.env.RULES||"null")||{};
        const fb=(r.focus_block||{});
        process.stdout.write(JSON.stringify({mode:"find_free_slots",
          busy_intervals:(m.busy_intervals||[]),
          duration_minutes:(fb.duration_minutes||90),
          working_hours:(r.working_hours||{start:"09:00",end:"18:00"}),
          timezone:process.env.TZ_USER||"UTC",
          range_start:process.env.TODAY_START||undefined,
          max_results:3, no_meeting_days:(r.no_meeting_days||[])}));'
    env:
      MERGE: $merge-events.stdout
      RULES: $load-rules.stdout
      TZ_USER: $prep.json.timezone
      TODAY_START: $prep.json.today

  - id: suggest-focus
    condition: $prep.json.gog_connected && $focus-check.json.needs_suggestion
    command: node ora-availability-merger.js
    stdin: $suggest-input.stdout

  # 13. Meeting-quality / no-agenda check (Req 34). Reads dismissal history (a
  #     read-only state key the agent maintains). Exits 0.
  - id: load-dismissals
    condition: $prep.json.gog_connected
    pipeline: state.get ora-state/agenda-dismissals

  - id: quality-input
    condition: $prep.json.gog_connected
    command: >
      node -e 'const m=JSON.parse(process.env.MERGE||"null")||{};
        const d=JSON.parse(process.env.DISMISS||"null")||{};
        process.stdout.write(JSON.stringify({meetings:(m.busy_intervals||[]),
          dismissal_history:d}));'
    env:
      MERGE: $merge-events.stdout
      DISMISS: $load-dismissals.stdout

  - id: quality-check
    condition: $prep.json.gog_connected
    command: node ora-meeting-quality-checker.js
    stdin: $quality-input.stdout

  # 14. Flag meetings missing a video link (deterministic filter, exits 0).
  - id: missing-video
    condition: $prep.json.gog_connected
    command: >
      node -e 'const iv=(JSON.parse(require("fs").readFileSync(0,"utf8")).busy_intervals)||[];
        const need=iv.filter(x=>!x.is_focus_block && (x.attendees||[]).length>0 && !x.video_link);
        process.stdout.write(JSON.stringify({needs_link:need, count:need.length}));'
    stdin: $merge-events.stdout

  # 15. Connection health (Req 46). Build the per-provider shape it expects, then
  #     run it. Health-check exits 1 when overall=red (self-DoS) → on_error:continue.
  - id: health-input
    condition: $prep.json.gog_connected
    command: >
      node -e 'const cc=JSON.parse(process.env.CONNECTED||"null")||{};
        const connections=[];
        for(const [k,v] of Object.entries(cc)){ if(v&&v.connected){
          connections.push({platform:k, type:k,
            api_test:v.api_test||null, webhook_status:v.webhook_status||null,
            token_status:v.token_status||null});
        }}
        process.stdout.write(JSON.stringify({connections}));'
    env:
      CONNECTED: $load-connected.stdout

  - id: health-check
    condition: $prep.json.gog_connected
    on_error: continue
    command: node ora-connection-health.js
    stdin: $health-input.stdout

  # 16. Assemble the deterministic briefing payload for the agent loop to narrate.
  #     (No llm-task / slack here — both 401 in the embedded runner.) Everything
  #     the narrative + delivery needs is bundled into one notification object.
  - id: brief-payload
    condition: $prep.json.gog_connected
    command: >
      node -e 'function p(e,f){try{return JSON.parse(process.env[e]||"null")||f}catch(_){return f}}
        const m=p("MERGE",{}), conf=p("CONF",{}), buf=p("BUF",{}),
          foc=p("FOC",{}), sug=p("SUG",null), qual=p("QUAL",{}), vid=p("VID",{}),
          health=p("HEALTH",{});
        const unreachable=p("UNREACH",[]);
        const has_issues=!!(conf.has_conflicts||buf.has_violations||
          (foc.needs_suggestion===true)||((qual.flagged_count||0)>0)||((vid.count||0)>0));
        process.stdout.write(JSON.stringify({
          todays_events:(m.busy_intervals||[]),
          event_count:(m.total_intervals||0),
          conflicts:conf, buffer_violations:buf,
          focus_status:foc, focus_suggestion:sug,
          no_agenda_meetings:qual, missing_video_links:(vid.count||0),
          connection_health:(health.overall_status||"unknown"),
          connection_alert:(health.alert===true),
          unreachable_providers:unreachable,
          has_issues}));'
    env:
      MERGE: $merge-events.stdout
      CONF: $conflict-check.stdout
      BUF: $buffer-check.stdout
      FOC: $focus-check.stdout
      SUG: $suggest-focus.stdout
      QUAL: $quality-check.stdout
      VID: $missing-video.stdout
      HEALTH: $health-check.stdout
      UNREACH: $prep.json.unreachable_providers

  # 17. Queue the briefing for the agent loop to render (LLM) + deliver (Slack).
  - id: notify-load
    condition: $prep.json.gog_connected
    pipeline: state.get ora/notifications

  - id: notify-build
    condition: $prep.json.gog_connected
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const b=JSON.parse(process.env.BRIEF||"{}");
        n.push({kind:"morning_briefing", channel:"#ora-calendar",
          needs_llm_narrative:true, briefing_input:b, created_at:new Date().toISOString()});
        if(b.connection_alert){ n.push({kind:"connection_alert", channel:"#ora-alerts",
          overall_status:b.connection_health, created_at:new Date().toISOString()}); }
        if((b.unreachable_providers||[]).length>0){ n.push({kind:"provider_unreachable",
          channel:"#ora-alerts", providers:b.unreachable_providers,
          message:"These calendars are toggled on but have no working integration in this build; only Google Calendar reads are wired. Reconnect Google Calendar in settings or contact support.",
          created_at:new Date().toISOString()}); }
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      BRIEF: $brief-payload.stdout

  - id: notify
    condition: $prep.json.gog_connected
    pipeline: state.set ora/notifications
    stdin: $notify-build.stdout

  # 18. Append a run record to the audit index array (single-key, append-then-set).
  - id: audit-load
    pipeline: state.get ora/audit-log

  - id: audit-build
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const b=JSON.parse(process.env.BRIEF||"null")||{};
        const prep=JSON.parse(process.env.PREP||"{}");
        a.push({action:"morning_briefing", date:prep.today||null,
          gog_connected:!!prep.gog_connected, events_today:(b.event_count||0),
          conflicts:!!(b.conflicts&&b.conflicts.has_conflicts),
          buffer_violations:!!(b.buffer_violations&&b.buffer_violations.has_violations),
          focus_block_present:!!(b.focus_status&&b.focus_status.has_focus_block),
          no_agenda_flagged:(b.no_agenda_meetings&&b.no_agenda_meetings.flagged_count)||0,
          missing_video_links:(b.missing_video_links||0),
          connection_health:(b.connection_health||"unknown"),
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        if(a.length>500)a.splice(0,a.length-500);
        process.stdout.write(JSON.stringify(a));'
    env:
      AUDIT: $audit-load.stdout
      BRIEF: $brief-payload.stdout
      PREP: $prep.json

  - id: audit
    pipeline: state.set ora/audit-log
    stdin: $audit-build.stdout
