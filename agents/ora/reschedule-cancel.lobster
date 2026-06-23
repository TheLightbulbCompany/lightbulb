name: reschedule-cancel
# Reschedule or cancel a meeting — Tier 2 (touches other people's calendars, so it
# halts on a real approval gate before any write). Fully deterministic; the only
# judgment (message drafting, adaptive tone) is handed to the agent loop via an
# ora/notifications state-queue intent instead of being attempted in-pipeline.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node, gog) under `command:`.
#  - There is NO fast-io / openclaw.invoke / slack / llm-task / await-approval on the embedded
#    runner (all 401 or fictional). Config the user set (connected-calendars, user-calendar,
#    scheduling-rules, contacts) is read-only consumer state under native state.get keys.
#  - Calendar reads/writes go through the baked `gog` CLI (Google only here — gog covers
#    Gmail/Calendar/Drive). ms365/caldav/calctl are NOT reachable gateway-free, so a
#    non-google primary calendar routes to a "connect/handle in agent loop" notice instead
#    of a broken provider call.
#  - Every script exits 0 and carries status in JSON; steps branch via `condition:`.
#  - Notifications (the drafted reschedule/cancel message + dependency surfacing) are written
#    to the ora/notifications queue for the agent loop to deliver — Slack is unreachable here.
#  - The destructive calendar write is gated on a step-level `approval: required` (the real
#    Tier-2 confirmation gate), NOT a fictional await-approval tool.

args:
  action_type:
    required: true          # "reschedule" | "cancel"
  event_id:
    required: true
  new_date_preference:
    default: ""             # ISO range "YYYY-MM-DD/YYYY-MM-DD" for reschedule slot search

steps:
  # 1. Read connected-calendar config + the primary-calendar pointer (consumer state the
  #    settings UI wrote). Both are single-key reads; null means "nothing connected".
  - id: load-connected
    pipeline: state.get ora-config/connected-calendars

  - id: load-user-calendar
    pipeline: state.get ora-config/user-calendar

  # 2. Connection gate. We can only act gateway-free on a Google primary calendar.
  #    Anything else (or nothing connected) → notice; the rest of the run no-ops.
  #    The gate also normalizes action_type into JSON so downstream steps can gate
  #    on $gate.json.action_type — a bare $action_type arg ref is rejected by the
  #    condition grammar (it must reference a step's .json.field).
  - id: gate
    command: >
      node -e 'const conn=JSON.parse(process.env.CONN||"null")||{};
        const uc=JSON.parse(process.env.UC||"null")||{};
        const platform=(uc.platform||"").toLowerCase();
        const googleConnected = !!(conn.gog && conn.gog.connected) || platform==="google" || platform==="gcal" || platform==="gmail";
        const anyConnected = ["gog","ms365","caldav","calctl"].some(k=>conn[k]&&conn[k].connected);
        const supported = googleConnected;
        let reason="";
        if(!anyConnected) reason="no_calendar";
        else if(!supported) reason="unsupported_provider";
        const action=(process.env.ACTION_TYPE||"").toLowerCase()==="cancel"?"cancel":"reschedule";
        process.stdout.write(JSON.stringify({
          proceed: supported,
          reason,
          platform: platform||"google",
          calendar_id: uc.calendar_id||"primary",
          action_type: action
        }));'
    env:
      CONN: $load-connected.json
      UC: $load-user-calendar.json
      ACTION_TYPE: $action_type

  # 3. Load the target event from Google Calendar (real gog CLI; tolerate failure → exit 0).
  - id: load-event
    condition: $gate.json.proceed
    command: >
      node -e 'const {execSync}=require("child_process");
        const id=process.env.LOBSTER_ARG_EVENT_ID||"";
        let out="";
        try { out=execSync("gog calendar events get --event-id "+JSON.stringify(id)+" --json",
          {encoding:"utf8",timeout:45000}); } catch(e){ out=""; }
        let ev=null; try { ev=JSON.parse(out); } catch(_) { ev=null; }
        if(ev&&ev.event) ev=ev.event;
        const found=!!(ev&&(ev.event_id||ev.id||ev.start));
        if(ev&&!ev.event_id&&ev.id) ev.event_id=ev.id;
        process.stdout.write(JSON.stringify({found, event:ev||{}}));'

  # 4. Same-day events (for dependency scan). gog list is fallible → wrap + default {}.
  #    Filter to the target event's calendar day via the start date when present.
  - id: load-same-day
    condition: $load-event.json.found
    command: >
      node -e 'const {execSync}=require("child_process");
        const ev=JSON.parse(process.env.EVENT||"{}");
        const start=ev.start&&(ev.start.dateTime||ev.start.date||ev.start)||"";
        const day=(String(start).slice(0,10))||"";
        const cal=process.env.CAL_ID||"primary";
        let args="gog calendar events list --calendar "+JSON.stringify(cal)+" --json";
        if(day){ args+=" --from "+JSON.stringify(day)+" --to "+JSON.stringify(day); }
        let out="";
        try { out=execSync(args,{encoding:"utf8",timeout:45000}); } catch(e){ out=""; }
        let parsed=null; try { parsed=JSON.parse(out); } catch(_) { parsed=null; }
        const events=(parsed&&(parsed.events||parsed.items))||(Array.isArray(parsed)?parsed:[]);
        process.stdout.write(JSON.stringify({events, count:events.length}));'
    env:
      EVENT: $load-event.json.event
      CAL_ID: $gate.json.calendar_id

  # 5. Merge same-day events into a unified busy set (pure compute).
  - id: merge-same-day
    condition: $load-event.json.found
    command: node ora-availability-merger.js
    stdin: '{"mode":"merge_busy","events_by_source":{"gog":$load-same-day.json.events}}'

  # 6. Dependency scan (prep/follow-up/travel/recurring). Deterministic, exits 0.
  - id: check-dependencies
    condition: $load-event.json.found
    command: node ora-dependency-checker.js
    stdin: '{"event":$load-event.json.event,"all_events":$merge-same-day.json.busy_intervals}'

  # ── RESCHEDULE-ONLY: find + rank candidate new slots ─────────────────────────────

  # 7. Load scheduling rules (working hours, buffer, no-meeting days) — consumer state.
  - id: load-rules
    condition: $gate.json.proceed && $gate.json.action_type == "reschedule"
    pipeline: state.get ora-config/scheduling-rules

  # 8. Pull events across the target date range for the new-slot search.
  - id: load-range
    condition: $load-event.json.found && $gate.json.action_type == "reschedule"
    command: >
      node -e 'const {execSync}=require("child_process");
        const range=process.env.LOBSTER_ARG_NEW_DATE_PREFERENCE||"";
        const cal=process.env.CAL_ID||"primary";
        const parts=String(range).split("/");
        const from=parts[0]||"", to=parts[1]||from;
        let args="gog calendar events list --calendar "+JSON.stringify(cal)+" --json";
        if(from){ args+=" --from "+JSON.stringify(from); }
        if(to){ args+=" --to "+JSON.stringify(to); }
        let out="";
        try { out=execSync(args,{encoding:"utf8",timeout:45000}); } catch(e){ out=""; }
        let parsed=null; try { parsed=JSON.parse(out); } catch(_) { parsed=null; }
        const events=(parsed&&(parsed.events||parsed.items))||(Array.isArray(parsed)?parsed:[]);
        process.stdout.write(JSON.stringify({events, count:events.length, range_from:from, range_to:to}));'
    env:
      CAL_ID: $gate.json.calendar_id

  - id: merge-range
    condition: $load-event.json.found && $gate.json.action_type == "reschedule"
    command: node ora-availability-merger.js
    stdin: '{"mode":"merge_busy","events_by_source":{"gog":$load-range.json.events}}'

  # 9. Build the find_free_slots input (durable rules + the event's duration + range bounds).
  #    Timezone comes from the user-calendar config (NOT container UTC) — DST-safe per the
  #    merger's Intl math. Defaults baked in so a sparse rules row still resolves.
  - id: slots-input
    condition: $load-event.json.found && $gate.json.action_type == "reschedule"
    command: >
      node -e 'const rules=JSON.parse(process.env.RULES||"null")||{};
        const ev=JSON.parse(process.env.EVENT||"{}");
        const uc=JSON.parse(process.env.UC||"null")||{};
        const range=JSON.parse(process.env.RANGE||"{}");
        const startMs=ev.start&&Date.parse(ev.start.dateTime||ev.start.date||ev.start);
        const endMs=ev.end&&Date.parse(ev.end.dateTime||ev.end.date||ev.end);
        let dur=ev.duration_minutes;
        if(!dur && startMs && endMs && endMs>startMs) dur=Math.round((endMs-startMs)/60000);
        if(!dur) dur=30;
        const tz=uc.timezone||rules.timezone||"UTC";
        const from=range.range_from||"", to=range.range_to||from;
        const out={
          mode:"find_free_slots",
          busy_intervals:JSON.parse(process.env.BUSY||"[]")||[],
          duration_minutes:dur,
          working_hours:rules.working_hours||{start:"09:00",end:"18:00"},
          buffer_minutes:rules.buffer_minutes||0,
          timezone:tz,
          no_meeting_days:rules.no_meeting_days||[],
          max_results:10
        };
        if(from) out.range_start=from+"T00:00:00Z";
        if(to) out.range_end=to+"T23:59:59Z";
        process.stdout.write(JSON.stringify(out));'
    env:
      RULES: $load-rules.json
      EVENT: $load-event.json.event
      UC: $load-user-calendar.json
      RANGE: $load-range.json
      BUSY: $merge-range.json.busy_intervals

  - id: find-new-slots
    condition: $load-event.json.found && $gate.json.action_type == "reschedule"
    command: node ora-availability-merger.js
    stdin: $slots-input.stdout

  # 10. Resolve each slot into per-participant local times (DST-aware) then rank.
  - id: tz-input
    condition: $find-new-slots.json.count > 0
    command: >
      node -e 'const slots=(JSON.parse(process.env.SLOTS||"{}").free_slots)||[];
        const ev=JSON.parse(process.env.EVENT||"{}");
        const att=(ev.attendees||[]).map(a=>({email:(a.email||a),timezone:a.timezone||a.tz||null}));
        process.stdout.write(JSON.stringify({mode:"batch_convert",slots,participants:att}));'
    env:
      SLOTS: $find-new-slots.json
      EVENT: $load-event.json.event

  - id: resolve-tz
    condition: $find-new-slots.json.count > 0
    command: node ora-datetime-engine.js
    stdin: $tz-input.stdout

  - id: rank-input
    condition: $find-new-slots.json.count > 0
    command: >
      node -e 'const slots=(JSON.parse(process.env.TZ||"{}").slots)||[];
        const ev=JSON.parse(process.env.EVENT||"{}");
        const rules=JSON.parse(process.env.RULES||"null")||{};
        const att=(ev.attendees||[]).map(a=>({email:(a.email||a),name:a.name||null,
          working_hours:a.working_hours||null}));
        process.stdout.write(JSON.stringify({slots,participants:att,user_preferences:rules}));'
    env:
      TZ: $resolve-tz.json
      EVENT: $load-event.json.event
      RULES: $load-rules.json

  - id: rank-new
    condition: $find-new-slots.json.count > 0
    command: node ora-slot-ranker.js
    stdin: $rank-input.stdout

  # ── Surface the proposal + dependencies to the agent loop (no Slack here) ─────────

  # 11. Queue a "draft + present" intent for the agent loop. The agent reads ora/notifications,
  #     drafts the adaptive-tone message (the 1 LLM call this workflow needs), and presents it.
  #     We carry the deterministic facts so the agent doesn't re-fetch.
  - id: notify-load
    condition: $load-event.json.found
    pipeline: state.get ora/notifications

  - id: notify-next
    condition: $load-event.json.found
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const ev=JSON.parse(process.env.EVENT||"{}");
        const dep=JSON.parse(process.env.DEPS||"null")||{};
        const ranked=JSON.parse(process.env.RANKED||"null");
        n.push({
          kind:"reschedule_cancel_proposal",
          action:process.env.LOBSTER_ARG_ACTION_TYPE||"",
          event_id:ev.event_id||process.env.LOBSTER_ARG_EVENT_ID||"",
          event_title:ev.title||ev.summary||"(untitled)",
          attendees:(ev.attendees||[]).map(a=>(a.email||a)),
          has_dependencies:!!dep.has_dependencies,
          dependencies:dep.dependencies||[],
          proposed_slots:(ranked&&ranked.ranked)||[],
          slot_scenario:(ranked&&ranked.scenario)||null,
          needs_agent_loop:!!(ranked&&ranked.needs_agent_loop),
          requires_user_confirmation:true,
          created_at:new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      EVENT: $load-event.json.event
      DEPS: $check-dependencies.json
      RANKED: $rank-new.json

  - id: notify
    condition: $load-event.json.found
    pipeline: state.set ora/notifications
    stdin: $notify-next.stdout

  # 12. Surface the click-to-connect / unsupported-provider notice when the gate blocked us.
  - id: connect-notice-load
    condition: "!$gate.json.proceed"
    pipeline: state.get ora/notifications

  - id: connect-notice-next
    condition: "!$gate.json.proceed"
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const reason=process.env.REASON||"";
        const msg = reason==="unsupported_provider"
          ? "This action needs a Google Calendar connection to run automatically. ms365/Apple/CalDAV reschedule + cancel are handled in-session by Ora for now — ask Ora directly."
          : "To reschedule or cancel, connect your calendar in settings. Supported automatically: Google Calendar.";
        n.push({kind:"connect_required", reason, action:process.env.LOBSTER_ARG_ACTION_TYPE||"",
          event_id:process.env.LOBSTER_ARG_EVENT_ID||"", message:msg, created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $connect-notice-load.stdout
      REASON: $gate.json.reason

  - id: connect-notice
    condition: "!$gate.json.proceed"
    pipeline: state.set ora/notifications
    stdin: $connect-notice-next.stdout

  # ── TIER 2 GATE: real human approval BEFORE any calendar write ───────────────────

  # 13. Halt for user confirmation. The run pauses with a resumeToken; the agent loop resumes
  #     with {approve:true, selected_slot:<one of proposed_slots>} after the user signs off.
  - id: confirm
    condition: $load-event.json.found
    approval:
      prompt: "Confirm ${action_type} of this meeting? Reschedules use the slot you select; nothing is sent until you approve."

  # ── Build the deterministic write payload (only after approval) ───────────────────

  # 14a. Reschedule payload. selected_slot comes from the approval resume payload; fall back
  #      to the top-ranked slot if the resumer didn't pin one.
  - id: prepare-reschedule
    condition: $confirm.approved && $gate.json.action_type == "reschedule"
    command: >
      node -e 'const ev=JSON.parse(process.env.EVENT||"{}");
        const uc=JSON.parse(process.env.UC||"null")||{};
        let slot=null;
        try { slot=JSON.parse(process.env.SELECTED||"null"); } catch(_) { slot=null; }
        if(!slot){ const r=JSON.parse(process.env.RANKED||"null"); slot=(r&&r.ranked&&r.ranked[0])||null; }
        process.stdout.write(JSON.stringify({mode:"prepare_reschedule",existing_event:ev,
          new_slot:slot,user_calendar:uc}));'
    env:
      EVENT: $load-event.json.event
      UC: $load-user-calendar.json
      RANKED: $rank-new.json
      SELECTED: $confirm.json.selected_slot

  - id: build-reschedule
    condition: $confirm.approved && $gate.json.action_type == "reschedule"
    command: node ora-booking-engine.js
    stdin: $prepare-reschedule.stdout

  # 14b. Cancel payload (no slot needed).
  - id: prepare-cancel
    condition: $confirm.approved && $gate.json.action_type == "cancel"
    command: >
      node -e 'const ev=JSON.parse(process.env.EVENT||"{}");
        const uc=JSON.parse(process.env.UC||"null")||{};
        process.stdout.write(JSON.stringify({mode:"prepare_cancel",existing_event:ev,user_calendar:uc}));'
    env:
      EVENT: $load-event.json.event
      UC: $load-user-calendar.json

  - id: build-cancel
    condition: $confirm.approved && $gate.json.action_type == "cancel"
    command: node ora-booking-engine.js
    stdin: $prepare-cancel.stdout

  # ── Execute the write via gog (Google only; payload's provider must be "gog") ────

  # 15a. Reschedule = update event start/end. The engine emits args as a JSON object; pass it
  #      through env and translate to gog flags in a node wrapper (gog has no --args-json).
  - id: execute-reschedule
    condition: $build-reschedule.json.ok == true && $build-reschedule.json.provider == "gog"
    command: >
      node -e 'const {execSync}=require("child_process");
        const a=JSON.parse(process.env.ARGS||"{}");
        const parts=["gog","calendar","events","update","--event-id",JSON.stringify(a.event_id)];
        if(a.calendar_id) parts.push("--calendar",JSON.stringify(a.calendar_id));
        if(a.start) parts.push("--start",JSON.stringify(a.start));
        if(a.end) parts.push("--end",JSON.stringify(a.end));
        if(a.send_updates) parts.push("--send-updates",JSON.stringify(a.send_updates));
        parts.push("--json");
        let out="",ok=true;
        try { out=execSync(parts.join(" "),{encoding:"utf8",timeout:45000}); }
        catch(e){ ok=false; out=String(e&&e.message||e); }
        process.stdout.write(JSON.stringify({ok, executed:ok, raw:out.slice(0,2000)}));'
    env:
      ARGS: $build-reschedule.json.args

  # 15b. Cancel = delete event.
  - id: execute-cancel
    condition: $build-cancel.json.ok == true && $build-cancel.json.provider == "gog"
    command: >
      node -e 'const {execSync}=require("child_process");
        const a=JSON.parse(process.env.ARGS||"{}");
        const parts=["gog","calendar","events","delete","--event-id",JSON.stringify(a.event_id)];
        if(a.calendar_id) parts.push("--calendar",JSON.stringify(a.calendar_id));
        if(a.send_updates) parts.push("--send-updates",JSON.stringify(a.send_updates));
        let out="",ok=true;
        try { out=execSync(parts.join(" "),{encoding:"utf8",timeout:45000}); }
        catch(e){ ok=false; out=String(e&&e.message||e); }
        process.stdout.write(JSON.stringify({ok, executed:ok, raw:out.slice(0,2000)}));'
    env:
      ARGS: $build-cancel.json.args

  # 16. Queue a "send messages + log" intent for the agent loop (Gmail/Slack delivery + the
  #     final attendee message are agent-side; the write already landed above).
  - id: done-load
    condition: $confirm.approved
    pipeline: state.get ora/notifications

  - id: done-next
    condition: $confirm.approved
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const ev=JSON.parse(process.env.EVENT||"{}");
        const resched=JSON.parse(process.env.RESCHED||"null");
        const cancel=JSON.parse(process.env.CANCEL||"null");
        const action=process.env.LOBSTER_ARG_ACTION_TYPE||"";
        const executed = (resched&&resched.executed)||(cancel&&cancel.executed)||false;
        n.push({
          kind:"reschedule_cancel_done",
          action,
          event_id:ev.event_id||process.env.LOBSTER_ARG_EVENT_ID||"",
          event_title:ev.title||ev.summary||"(untitled)",
          attendees:(ev.attendees||[]).map(a=>(a.email||a)),
          executed,
          needs_attendee_message:true,
          authorized_by:"user_confirmed",
          created_at:new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $done-load.stdout
      EVENT: $load-event.json.event
      RESCHED: $execute-reschedule.json
      CANCEL: $execute-cancel.json

  - id: done
    condition: $confirm.approved
    pipeline: state.set ora/notifications
    stdin: $done-next.stdout

  # 17. Append a durable audit record (single-key index array).
  - id: audit-load
    condition: $confirm.approved
    pipeline: state.get ora/audit-log

  - id: audit-next
    condition: $confirm.approved
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const ev=JSON.parse(process.env.EVENT||"{}");
        const resched=JSON.parse(process.env.RESCHED||"null");
        const cancel=JSON.parse(process.env.CANCEL||"null");
        const dep=JSON.parse(process.env.DEPS||"null")||{};
        log.push({
          action:process.env.LOBSTER_ARG_ACTION_TYPE||"",
          event_id:ev.event_id||process.env.LOBSTER_ARG_EVENT_ID||"",
          event_title:ev.title||ev.summary||"(untitled)",
          attendees:(ev.attendees||[]).map(a=>(a.email||a)),
          dependencies:(dep.dependencies||[]).length,
          executed:((resched&&resched.executed)||(cancel&&cancel.executed))||false,
          authorized_by:"user_confirmed",
          timestamp:new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-load.stdout
      EVENT: $load-event.json.event
      RESCHED: $execute-reschedule.json
      CANCEL: $execute-cancel.json
      DEPS: $check-dependencies.json

  - id: audit
    condition: $confirm.approved
    pipeline: state.set ora/audit-log
    stdin: $audit-next.stdout
