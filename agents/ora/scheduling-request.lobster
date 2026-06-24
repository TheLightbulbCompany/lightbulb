name: scheduling-request
# Scheduling flow: request -> resolve participants -> merge busy set -> tz math
# -> enforce rules -> rank -> (book ONLY behind a human approval gate) -> verify
# -> roll back on conflict -> queue confirmations for the agent to deliver.
#
# Rewritten for the embedded OpenClaw runner (gateway-free, deterministic):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node, gog)
#    under `command:`. NEVER `exec --json --shell` under command: (exit 127).
#  - There is no fast-io / openclaw.invoke / ms365 / caldav / calctl / taskr on
#    this runner. The ONLY real calendar CLI baked in is `gog` (Google). Other
#    providers are click-to-connect only: if Google isn't connected we write a
#    "connect a calendar" notice to the notifications queue and stop.
#  - gog emits its own JSON envelope, so a node step normalizes `gog calendar
#    events list --json` into the {events:[{start,end,...}]} shape the merger
#    expects (the mcporter-text-parse analog).
#  - LLM judgment (creative alternatives, trade-off narrative, confirmation
#    copy) and Slack delivery cannot run here (llm-task/slack 401). They are
#    written to the `ora/notifications` state-queue for the agent loop.
#  - Booking touches OTHER people's calendars -> Tier-2. A real step-level
#    `approval: required` gate guards the write; the side effect is gated on
#    `$confirm.approved`. (The fictional `lobster await-approval` is gone.)
#  - Timezone safety: every datetime stays ISO-with-offset; the user's own
#    timezone comes from ora-config/user-calendar (NOT container UTC).
#  - Scripts exit 0 and carry status in JSON; steps branch via condition:.
#    Counts are emitted explicitly and gated with `> 0` (never `.length`).

args:
  request_type:
    default: "internal"
  participants:
    required: true          # JSON array of {email, name?, timezone?} OR comma string
  duration_minutes:
    default: 30
  meeting_type:
    default: "general"
  preferred_dates:
    default: ""             # optional "{start_iso}/{end_iso}" range hint
  meeting_title:
    default: "Meeting"

steps:
  # ── Consumer config (read-only state the user/UI sets) ──────────────
  # 1. Connected-calendars map + the user's primary calendar (tz lives here).
  - id: load-connected
    pipeline: state.get ora-config/connected-calendars

  - id: load-user-calendar
    pipeline: state.get ora-config/user-calendar

  - id: load-rules
    pipeline: state.get ora-config/scheduling-rules

  - id: load-exceptions
    pipeline: state.get ora-state/exception-history

  # Cached participant timezone profiles (single-key index array, not a prefix
  # list). Shape: [{email, name, timezone, working_hours}, ...].
  - id: load-tz-profiles
    pipeline: state.get ora-contacts/profiles

  # ── 2. Gate on a connected calendar. Only Google (gog) is real here. ──
  - id: gate
    command: >
      node -e 'const c=JSON.parse(process.env.CONNECTED||"null")||{};
        const gog=!!(c.gog&&c.gog.connected);
        const anyOther=!!((c.ms365&&c.ms365.connected)||(c.caldav&&c.caldav.connected)||(c.calctl&&c.calctl.connected));
        process.stdout.write(JSON.stringify({gog_connected:gog, only_other:(!gog&&anyOther), none:(!gog&&!anyOther)}));'
    env:
      CONNECTED: $load-connected.stdout

  # If Google is not connected, queue a click-to-connect notice and stop here.
  - id: connect-notice-load
    condition: "!$gate.json.gog_connected"
    pipeline: state.get ora/notifications

  - id: connect-notice-next
    condition: "!$gate.json.gog_connected"
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const only=process.env.ONLY_OTHER==="true";
        n.push({kind:"connect_calendar", channel:"#ora-calendar",
          text: only
            ? "I can only act on Google Calendar from automated flows right now. To schedule this, connect Google Calendar in your settings."
            : "To schedule meetings, connect a calendar in your settings. Supported: Google Calendar, Microsoft 365 / Outlook, Apple Calendar, or any CalDAV provider (iCloud, Fastmail, Nextcloud).",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $connect-notice-load.stdout
      ONLY_OTHER: $gate.json.only_other

  - id: connect-notice
    condition: "!$gate.json.gog_connected"
    pipeline: state.set ora/notifications
    stdin: $connect-notice-next.stdout

  # ── 3. Resolve participants against cached tz profiles (deterministic). ──
  # The original called gog/ms365 contacts-search via the gateway; here we
  # resolve from the user-maintained tz-profile cache and the args. Unknowns
  # are kept with a null timezone so the ranker/agent can flag "tz unknown".
  - id: resolve-contacts
    condition: $gate.json.gog_connected
    command: >
      node -e 'function arr(x){try{const v=JSON.parse(x);return Array.isArray(v)?v:null;}catch(e){return null;}}
        const raw=process.env.PARTICIPANTS||"";
        let reqs=arr(raw);
        if(!reqs){reqs=raw.split(",").map(s=>s.trim()).filter(Boolean).map(s=>({email:s.includes("@")?s:"",name:s.includes("@")?"":s}));}
        const profiles=arr(process.env.PROFILES)||[];
        const byEmail={}; const byName={};
        for(const p of profiles){if(p.email)byEmail[String(p.email).toLowerCase()]=p; if(p.name)byName[String(p.name).toLowerCase()]=p;}
        const contacts=reqs.map(r=>{
          const em=String(r.email||"").toLowerCase();
          const nm=String(r.name||"").toLowerCase();
          const hit=(em&&byEmail[em])||(nm&&byName[nm])||{};
          return {email:r.email||hit.email||"", name:r.name||hit.name||"",
            timezone:r.timezone||hit.timezone||null,
            working_hours:r.working_hours||hit.working_hours||null,
            tz_known:!!(r.timezone||hit.timezone)};
        });
        const unresolved=contacts.filter(c=>!c.tz_known).length;
        process.stdout.write(JSON.stringify({contacts, count:contacts.length, unresolved_tz:unresolved}));'
    env:
      PARTICIPANTS: $participants
      PROFILES: $load-tz-profiles.stdout

  # ── 4. Compute the search window (user-tz aware, not container UTC). ──
  # preferred_dates may be "{startISO}/{endISO}"; otherwise default to the
  # next 14 days. The user's own timezone is read from user-calendar config.
  - id: window
    condition: $gate.json.gog_connected
    command: >
      node -e 'const uc=JSON.parse(process.env.USERCAL||"null")||{};
        const tz=uc.timezone||"UTC"; const pd=(process.env.PREFERRED||"").trim();
        let start,end;
        if(pd.includes("/")){const [s,e]=pd.split("/");start=new Date(s);end=new Date(e);}
        if(!start||isNaN(start.getTime())){start=new Date();}
        if(!end||isNaN(end.getTime())){end=new Date(start.getTime()+14*24*60*60*1000);}
        process.stdout.write(JSON.stringify({range_start:start.toISOString(), range_end:end.toISOString(), timezone:tz}));'
    env:
      USERCAL: $load-user-calendar.stdout
      PREFERRED: $preferred_dates

  # ── 5. Fetch Google Calendar events for the window (real CLI). ──
  - id: fetch-gog
    condition: $gate.json.gog_connected
    command: >
      gog calendar events list primary --from "$WIN_FROM" --to "$WIN_TO" --max 250 --json
    env:
      WIN_FROM: $window.json.range_start
      WIN_TO: $window.json.range_end
    on_error: continue

  # Normalize gog's JSON envelope into {events:[{start,end,title,event_id,...}]}.
  # gog wraps results and uses {start:{dateTime|date}, end:{...}} — map to flat
  # ISO strings the merger expects. Always exit 0 with a (possibly empty) list.
  - id: normalize-events
    condition: $gate.json.gog_connected
    command: >
      node -e 'function parse(s){try{return JSON.parse(s);}catch(e){return null;}}
        const root=parse(process.env.RAW); let items=[];
        if(root){ items = root.items||root.events||root.result||(Array.isArray(root)?root:[]) ||[]; }
        if(!Array.isArray(items)) items=[];
        const events=items.map(e=>{
          const s=e.start&&(e.start.dateTime||e.start.date)||e.start||null;
          const en=e.end&&(e.end.dateTime||e.end.date)||e.end||null;
          const allDay=!!(e.start&&e.start.date&&!e.start.dateTime);
          return {start:s, end:en, title:e.summary||e.title||"(untitled)",
            event_id:e.id||e.event_id||null, calendar_id:e.organizer&&e.organizer.email||"primary",
            is_all_day:allDay, is_focus_block:/focus|deep work|do not schedule/i.test(e.summary||"")};
        }).filter(e=>e.start&&e.end);
        process.stdout.write(JSON.stringify({events, count:events.length}));'
    env:
      RAW: $fetch-gog.stdout

  # ── 6. Merge into a unified non-overlapping busy set. ──
  - id: merge-events
    condition: $gate.json.gog_connected
    command: node ora-availability-merger.js
    stdin: '{"mode":"merge_busy","events_by_source":{"gog":$normalize-events.json.events}}'

  # ── 7. Find free slots against the merged busy set. ──
  - id: find-free-slots
    condition: $gate.json.gog_connected
    command: node ora-availability-merger.js
    stdin: '{"mode":"find_free_slots","busy_intervals":$merge-events.json.busy_intervals,"duration_minutes":${duration_minutes},"working_hours":$load-rules.json.working_hours,"buffer_minutes":$load-rules.json.buffer_minutes,"timezone":$window.json.timezone,"no_meeting_days":$load-rules.json.no_meeting_days,"range_start":$window.json.range_start,"range_end":$window.json.range_end,"max_results":10}'

  # Gate the rest of the flow on having at least one free slot (explicit count).
  - id: have-slots
    condition: $gate.json.gog_connected
    command: >
      node -e 'const f=JSON.parse(process.env.FREE||"null")||{};
        process.stdout.write(JSON.stringify({count:Number(f.count||0)}));'
    env:
      FREE: $find-free-slots.stdout

  # ── 8. Per-participant timezone projection for each slot (DST per date). ──
  - id: resolve-timezones
    condition: $have-slots.json.count > 0
    command: node ora-datetime-engine.js
    stdin: '{"mode":"batch_convert","slots":$find-free-slots.json.free_slots,"participants":$resolve-contacts.json.contacts}'

  # ── 9. Rule enforcement on each candidate slot. ──
  - id: enforce-rules
    condition: $have-slots.json.count > 0
    command: node ora-rule-enforcer.js
    stdin: '{"slots":$resolve-timezones.json.slots,"rules":$load-rules.json,"exception_history":$load-exceptions.json,"today_meetings":$merge-events.json.busy_intervals}'

  # ── 10. Rank surviving slots (top 3 + scenario tag). ──
  - id: rank-slots
    condition: $have-slots.json.count > 0
    command: node ora-slot-ranker.js
    stdin: '{"slots":$enforce-rules.json.passing_slots,"participants":$resolve-contacts.json.contacts,"user_preferences":$load-rules.json}'

  # ── 11. Queue an "options to present" notification for the agent loop. ──
  # The agent loop owns: creative-alternatives when needs_agent_loop, the
  # human-readable trade-off narrative, and Slack delivery (all LLM/gateway).
  # We hand it the deterministic ranked slots + scenario; it writes the copy.
  - id: present-load
    condition: $have-slots.json.count > 0
    pipeline: state.get ora/notifications

  - id: present-next
    condition: $have-slots.json.count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const rank=JSON.parse(process.env.RANK||"null")||{};
        const contacts=(JSON.parse(process.env.CONTACTS||"null")||{}).contacts||[];
        n.push({kind:"present_options", channel:"#ora-calendar",
          needs_creative_alternatives: !!rank.needs_agent_loop,
          scenario: rank.scenario||"unknown",
          ranked_slots: rank.ranked||[],
          participants: contacts,
          meeting_type: process.env.MEETING_TYPE,
          meeting_title: process.env.MEETING_TITLE,
          request_type: process.env.REQUEST_TYPE,
          unresolved_tz: (JSON.parse(process.env.CONTACTS||"null")||{}).unresolved_tz||0,
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $present-load.stdout
      RANK: $rank-slots.stdout
      CONTACTS: $resolve-contacts.stdout
      MEETING_TYPE: $meeting_type
      MEETING_TITLE: $meeting_title
      REQUEST_TYPE: $request_type

  - id: present
    condition: $have-slots.json.count > 0
    pipeline: state.set ora/notifications
    stdin: $present-next.stdout

  # ── 12. Pick the top-ranked slot as the booking candidate. ──
  # The agent loop presents options + records the user's pick; in the
  # deterministic flow we propose the #1 ranked slot for the approval gate.
  - id: candidate
    condition: $have-slots.json.count > 0
    command: >
      node -e 'const r=JSON.parse(process.env.RANK||"null")||{};
        const top=(r.ranked||[])[0]||null;
        process.stdout.write(JSON.stringify({has_candidate:!!top, slot:top, scenario:r.scenario||"unknown"}));'
    env:
      RANK: $rank-slots.stdout

  # ── 13. TIER-2 HUMAN APPROVAL — booking touches other people's calendars. ──
  # Real step-level gate. The run halts with a resumeToken until approved.
  # Nothing below this writes to a calendar unless $confirm.approved is true.
  - id: confirm
    condition: $candidate.json.has_candidate
    approval:
      required: true
      prompt: "Book the proposed slot? This sends an invite to the participants."

  # ── 14. Build the provider booking payload (deterministic). ──
  - id: prepare-booking
    condition: $confirm.approved
    command: node ora-booking-engine.js
    stdin: '{"mode":"prepare_booking","slot":$candidate.json.slot,"participants":$resolve-contacts.json.contacts,"title":${meeting_title},"meeting_type":${meeting_type},"user_calendar":$load-user-calendar.json,"add_video_link":true}'

  # ── 15. Book via Google Calendar (the only real write path here). ──
  # gog returns the created event JSON; capture stdout for the normalizer.
  - id: book-gog
    condition: $confirm.approved && $prepare-booking.json.provider == "gog"
    command: >
      node -e 'const {execSync}=require("child_process");
        const a=JSON.parse(process.env.ARGS||"{}");
        const attEmails=(a.attendees||[]).map(x=>x.email).filter(Boolean);
        // gog write surface is `gog calendar create <calendarId>`:
        //   --summary (not --title), --attendees as ONE csv, --with-meet for video.
        const argv=["calendar","create",a.calendar_id||"primary",
          "--summary",a.title||"Meeting","--from",a.start,"--to",a.end,"--json"];
        if(a.description) argv.push("--description",a.description);
        if(attEmails.length) argv.push("--attendees",attEmails.join(","));
        if(a.conference_data_version) argv.push("--with-meet");
        if(a.send_updates) argv.push("--send-updates",a.send_updates);
        const q=s=>"\x27"+String(s).replace(/\x27/g,"\x27\\\x27\x27")+"\x27";
        let out="";
        try{out=execSync("gog "+argv.map(q).join(" "),{encoding:"utf8",timeout:45000});}
        catch(e){process.stdout.write(JSON.stringify({error:String(e.message||e).slice(0,300)}));process.exit(0);}
        let parsed=null; try{parsed=JSON.parse(out);}catch(e){}
        const ev=(parsed&&(parsed.result||parsed.event||parsed))||{};
        process.stdout.write(JSON.stringify(ev));'
    env:
      ARGS: $prepare-booking.json.args
    on_error: continue

  # ── 16. Normalize the booking result (provider-agnostic downstream). ──
  - id: normalize-booking
    condition: $confirm.approved
    command: node ora-booking-normalizer.js
    stdin: '{"gog":$book-gog.json,"ms365":null,"caldav":null,"calctl":null}'

  # ── 17. Post-hoc conflict verification — reload + re-merge fresh state. ──
  - id: post-reload-gog
    condition: $normalize-booking.json.ok
    command: >
      gog calendar events list primary --from "$SLOT_START" --to "$SLOT_END" --max 250 --json
    env:
      SLOT_START: $candidate.json.slot.start
      SLOT_END: $candidate.json.slot.end
    on_error: continue

  - id: post-normalize-events
    condition: $normalize-booking.json.ok
    command: >
      node -e 'function parse(s){try{return JSON.parse(s);}catch(e){return null;}}
        const root=parse(process.env.RAW); let items=[];
        if(root){ items=root.items||root.events||root.result||(Array.isArray(root)?root:[])||[]; }
        if(!Array.isArray(items)) items=[];
        const events=items.map(e=>{const s=e.start&&(e.start.dateTime||e.start.date)||e.start||null;
          const en=e.end&&(e.end.dateTime||e.end.date)||e.end||null;
          return {start:s,end:en,title:e.summary||"(untitled)",event_id:e.id||null};
        }).filter(e=>e.start&&e.end);
        process.stdout.write(JSON.stringify({events, count:events.length}));'
    env:
      RAW: $post-reload-gog.stdout

  - id: post-merge
    condition: $normalize-booking.json.ok
    command: node ora-availability-merger.js
    stdin: '{"mode":"merge_busy","events_by_source":{"gog":$post-normalize-events.json.events}}'

  - id: verify-booking
    condition: $normalize-booking.json.ok
    command: node ora-booking-engine.js
    stdin: '{"mode":"verify_booking","event_id":$normalize-booking.json.event_id,"slot":$candidate.json.slot,"fresh_busy_intervals":$post-merge.json.busy_intervals}'

  # ── 18. Roll back via Google if a conflict appeared in the verify window. ──
  - id: rollback-gog
    condition: $verify-booking.json.needs_rollback && $normalize-booking.json.provider == "gog"
    command: >
      gog calendar delete primary "$EVENT_ID" --send-updates none --force
    env:
      EVENT_ID: $normalize-booking.json.event_id
    on_error: continue

  - id: rollback-alert-load
    condition: $verify-booking.json.needs_rollback
    pipeline: state.get ora/notifications

  - id: rollback-alert-next
    condition: $verify-booking.json.needs_rollback
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"booking_rolled_back", channel:"#ora-alerts", urgent:true,
          text:"Booking rolled back — a conflict appeared in the verify window. The slot is no longer available. Want me to find another time?",
          event_id:process.env.EVENT_ID||"", created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $rollback-alert-load.stdout
      EVENT_ID: $normalize-booking.json.event_id
    on_error: continue

  - id: rollback-alert
    condition: $verify-booking.json.needs_rollback
    pipeline: state.set ora/notifications
    stdin: $rollback-alert-next.stdout

  # ── 19. Queue confirmation messages for the agent loop (LLM copy). ──
  # Confirmed bookings only. The agent loop generates per-participant copy
  # (timezone, video link, prep) and delivers it; we hand it the facts.
  - id: confirm-load
    condition: $verify-booking.json.confirmed
    pipeline: state.get ora/notifications

  - id: confirm-next
    condition: $verify-booking.json.confirmed
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const nb=JSON.parse(process.env.BOOKING||"null")||{};
        const contacts=(JSON.parse(process.env.CONTACTS||"null")||{}).contacts||[];
        n.push({kind:"send_confirmation", channel:"#ora-calendar",
          event_id:nb.event_id, provider:nb.provider, video_link:nb.video_link||null,
          slot:JSON.parse(process.env.SLOT||"null"),
          participants:contacts, meeting_type:process.env.MEETING_TYPE,
          meeting_title:process.env.MEETING_TITLE, created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $confirm-load.stdout
      BOOKING: $normalize-booking.stdout
      CONTACTS: $resolve-contacts.stdout
      SLOT: $candidate.json.slot
      MEETING_TYPE: $meeting_type
      MEETING_TITLE: $meeting_title

  - id: confirm-queue
    condition: $verify-booking.json.confirmed
    pipeline: state.set ora/notifications
    stdin: $confirm-next.stdout

  # ── 20. Schedule reminders via a durable state queue (taskr is fictional). ──
  # Append to a single-key reminders index array; the cron/agent loop drains it.
  - id: reminders-load
    condition: $verify-booking.json.confirmed
    pipeline: state.get ora/reminders

  - id: reminders-next
    condition: $verify-booking.json.confirmed
    command: >
      node -e 'const r=JSON.parse(process.env.REMINDERS||"null")||[];
        const nb=JSON.parse(process.env.BOOKING||"null")||{};
        const contacts=(JSON.parse(process.env.CONTACTS||"null")||{}).contacts||[];
        const slot=JSON.parse(process.env.SLOT||"null")||{};
        r.push({event_id:nb.event_id, start:slot.start, video_link:nb.video_link||null,
          participants:contacts, remind_at_24h:true, remind_at_1h:true,
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(r));'
    env:
      REMINDERS: $reminders-load.stdout
      BOOKING: $normalize-booking.stdout
      CONTACTS: $resolve-contacts.stdout
      SLOT: $candidate.json.slot

  - id: reminders
    condition: $verify-booking.json.confirmed
    pipeline: state.set ora/reminders
    stdin: $reminders-next.stdout

  # ── 21. Audit log — append to a single-key audit index array. ──
  - id: audit-load
    condition: $verify-booking.json.confirmed
    pipeline: state.get ora/audit-scheduling

  - id: audit-next
    condition: $verify-booking.json.confirmed
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const nb=JSON.parse(process.env.BOOKING||"null")||{};
        const vb=JSON.parse(process.env.VERIFY||"null")||{};
        a.push({action:"meeting_scheduled", request_type:process.env.REQUEST_TYPE,
          meeting_type:process.env.MEETING_TYPE, meeting_title:process.env.MEETING_TITLE,
          slot:JSON.parse(process.env.SLOT||"null"), provider:nb.provider,
          event_id:nb.event_id, verify_confirmed:!!vb.confirmed,
          rolled_back:!!vb.needs_rollback, authorized_by:"user_confirmed",
          timestamp:new Date().toISOString()});
        if(a.length>500) a.splice(0, a.length-500);
        process.stdout.write(JSON.stringify(a));'
    env:
      AUDIT: $audit-load.stdout
      BOOKING: $normalize-booking.stdout
      VERIFY: $verify-booking.stdout
      SLOT: $candidate.json.slot
      REQUEST_TYPE: $request_type
      MEETING_TYPE: $meeting_type
      MEETING_TITLE: $meeting_title

  - id: audit
    condition: $verify-booking.json.confirmed
    pipeline: state.set ora/audit-scheduling
    stdin: $audit-next.stdout
