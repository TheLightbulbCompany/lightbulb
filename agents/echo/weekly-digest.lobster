name: weekly-digest
# Monday action-item status digest with deadline alerts (Requirements 33, 34).
# Runs Monday 8 AM on cron. Fully deterministic on the embedded runner (0 LLM,
# 0 gateway). The LLM digest narrative + Slack delivery are routed to an
# `echo/notifications` queue for the agent loop to handle — neither can run
# inside an embedded pipeline (llm-task + slack both 401).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node, curl)
#    under `command:`. `exec --json --shell` is FICTIONAL on this runner.
#  - Durable state via state.get/set. `fast-io` does not exist. The archived
#    action items (formerly the `echo-archive/` prefix) are a single-key INDEX
#    ARRAY `echo/archive`, appended by meeting-process and read here
#    (state.* has no prefix-list). Connected-PM config is read-only consumer
#    state written by the settings UI.
#  - PM-tool status is fetched with raw `curl` under `command:` (Asana REST,
#    Linear GraphQL, Jira REST) using the per-user tokens from process.env.
#    Each fetch step exits 0 and carries its payload (or an error marker) in
#    JSON; branches gate on `condition:`, never on a thrown error.
#  - Counts are emitted explicitly and gated numerically (never `.length`).
#  - All node scripts exit 0; the workflow branches via `condition:`.

steps:
  # 1. Load every archived meeting's action items (single-key index array).
  #    Written by meeting-process on each approved meeting; null on a fresh agent.
  - id: archive-index
    pipeline: state.get echo/archive

  # 2. Flatten the per-meeting archive rows into one action_items[] list and
  #    emit an explicit count so downstream steps gate numerically.
  - id: collect-actions
    command: >
      node -e 'const idx=JSON.parse(process.env.IDX||"null")||[];
        const items=[];
        for (const row of idx) {
          const ai=(row&&row.action_items)||[];
          for (const a of ai) {
            items.push(Object.assign({}, a, {
              meeting_title: a.meeting_title||row.title||"",
              meeting_date: a.meeting_date||row.date||"",
              meeting_organizer: a.meeting_organizer||row.organizer||""
            }));
          }
        }
        process.stdout.write(JSON.stringify({action_items:items, count:items.length}));'
    env:
      IDX: $archive-index.json

  # 3. Load the user's connected PM tool (click-to-connect, single platform).
  #    Read-only consumer config; null/empty means nothing connected — the
  #    digest then runs off meeting-date + deadline info alone.
  - id: connected-pm
    pipeline: state.get echo-config/connected-pm-tool

  # 4. Normalize the connected-PM config into an explicit platform + flags so
  #    each fetch branch gates on a strict `== "<platform>"`.
  - id: pm-target
    command: >
      node -e 'const c=JSON.parse(process.env.PM||"null")||{};
        const platform=c.platform||"none";
        process.stdout.write(JSON.stringify({
          platform,
          connected: platform==="asana"||platform==="linear"||platform==="jira",
          workspace_id: c.workspace_id||"",
          team_id: c.team_id||"",
          project_key: c.project_key||"",
          base_url: c.base_url||""
        }));'
    env:
      PM: $connected-pm.json

  # 5. Fetch task-completion status from whichever PM tool is connected.
  #    Raw curl under command:; node wraps it so a non-200 / auth error never
  #    aborts the run — the payload (or {error}) is carried in JSON.
  - id: fetch-asana
    condition: $pm-target.json.platform == "asana"
    command: >
      node -e 'const {execSync}=require("child_process");
        const ws=process.env.WORKSPACE_ID||"";
        const tok=process.env.ASANA_ACCESS_TOKEN||"";
        let out={data:[]};
        try {
          const url="https://app.asana.com/api/1.0/tasks?workspace="+encodeURIComponent(ws)
            +"&completed_since=now-7d&opt_fields=gid,completed,completed_at";
          const txt=execSync("curl -s -H "+JSON.stringify("Authorization: Bearer "+tok)+" "+JSON.stringify(url),
            {encoding:"utf8",timeout:45000});
          out=JSON.parse(txt||"{}");
        } catch(e){ out={data:[], error:String(e.message||e)}; }
        process.stdout.write(JSON.stringify(out));'
    env:
      WORKSPACE_ID: $pm-target.json.workspace_id

  - id: fetch-linear
    condition: $pm-target.json.platform == "linear"
    command: >
      node -e 'const {execSync}=require("child_process");
        const team=process.env.TEAM_ID||"";
        const key=process.env.LINEAR_API_KEY||"";
        const body=JSON.stringify({
          query:"query($teamId: String!) { issues(filter: {team: {id: {eq: $teamId}}, updatedAt: {gte: \"-P7D\"}}) { nodes { id identifier completedAt state { name } } } }",
          variables:{teamId:team}});
        let out={data:{issues:{nodes:[]}}};
        try {
          const txt=execSync("curl -s -X POST -H "+JSON.stringify("Authorization: "+key)
            +" -H "+JSON.stringify("Content-Type: application/json")
            +" --data-binary @- https://api.linear.app/graphql",
            {encoding:"utf8",timeout:45000,input:body});
          out=JSON.parse(txt||"{}");
        } catch(e){ out={data:{issues:{nodes:[]}}, error:String(e.message||e)}; }
        process.stdout.write(JSON.stringify(out));'
    env:
      TEAM_ID: $pm-target.json.team_id

  - id: fetch-jira
    condition: $pm-target.json.platform == "jira"
    command: >
      node -e 'const {execSync}=require("child_process");
        const base=process.env.BASE_URL||"";
        const proj=process.env.PROJECT_KEY||"";
        const email=process.env.JIRA_EMAIL||"";
        const tok=process.env.JIRA_API_TOKEN||"";
        let out={issues:[]};
        try {
          const jql="project="+proj+" AND updated>-7d";
          const url=base+"/rest/api/3/search?jql="+encodeURIComponent(jql)+"&fields=resolutiondate,status";
          const txt=execSync("curl -s -u "+JSON.stringify(email+":"+tok)+" "+JSON.stringify(url),
            {encoding:"utf8",timeout:45000});
          out=JSON.parse(txt||"{}");
        } catch(e){ out={issues:[], error:String(e.message||e)}; }
        process.stdout.write(JSON.stringify(out));'
    env:
      BASE_URL: $pm-target.json.base_url
      PROJECT_KEY: $pm-target.json.project_key

  # 6. Normalize whichever PM tool responded into a unified status map keyed by
  #    the action item's external id. Deterministic, zero-LLM. Tolerates the
  #    fetch steps being skipped (their refs resolve to null when unconnected).
  - id: normalize-pm-status
    command: >
      node -e 'const j=s=>{try{return JSON.parse(s||"null")}catch{return null}};
        const asana=j(process.env.ASANA), linear=j(process.env.LINEAR), jira=j(process.env.JIRA);
        const statusMap={};
        const asanaTasks=(asana&&asana.data)||[];
        for (const t of asanaTasks) statusMap[t.gid]={completed:!!t.completed, completed_date:t.completed_at||null};
        const linearIssues=(linear&&linear.data&&linear.data.issues&&linear.data.issues.nodes)||[];
        for (const i of linearIssues) statusMap[i.id]={completed:!!i.completedAt, completed_date:i.completedAt||null};
        const jiraIssues=(jira&&jira.issues)||[];
        for (const i of jiraIssues) {
          const nm=i.fields&&i.fields.status&&i.fields.status.name;
          const done=nm==="Done"||nm==="Closed";
          statusMap[i.id]={completed:!!done, completed_date:(i.fields&&i.fields.resolutiondate)||null};
        }
        const source=asana?"asana":linear?"linear":jira?"jira":"none";
        process.stdout.write(JSON.stringify({status_map:statusMap, source}));'
    env:
      ASANA: $fetch-asana.stdout
      LINEAR: $fetch-linear.stdout
      JIRA: $fetch-jira.stdout

  # 7. Assemble the deadline-tracker input (action items + PM status map + alert
  #    window). Keeps the script fed even when no PM tool is connected.
  - id: tracker-input
    command: >
      node -e 'const ai=(JSON.parse(process.env.ACTIONS||"{}").action_items)||[];
        const sm=JSON.parse(process.env.STATUS||"{}").status_map||{};
        process.stdout.write(JSON.stringify({action_items:ai, pm_tool_status:sm, alert_days_before:2}));'
    env:
      ACTIONS: $collect-actions.stdout
      STATUS: $normalize-pm-status.stdout

  # 8. Deadline tracker: deterministic date comparison → overdue / approaching /
  #    stale classification + follow-through rate. Zero LLM. Flat script path.
  - id: track-deadlines
    command: node echo-deadline-tracker.js
    stdin: $tracker-input.stdout

  # 9. Build the digest payload the agent loop needs (LLM narrative + Slack
  #    delivery happen there — both are unreachable from the embedded runner).
  #    Carry a `has_items` count so the enqueue step gates numerically.
  - id: digest-payload
    command: >
      node -e 'const t=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const tracked=t.tracked||[];
        const digestItems=t.digest_items||[];
        const payload={
          kind:"weekly_digest",
          generated_at:new Date().toISOString(),
          channel:"#echo-meetings",
          overdue:t.overdue||0,
          approaching:t.approaching||0,
          completed:t.completed||0,
          in_progress:t.in_progress||0,
          stale_no_deadline:t.stale_no_deadline||0,
          follow_through_rate:(t.follow_through_rate===null||t.follow_through_rate===undefined)?null:t.follow_through_rate,
          alerts:t.alerts||[],
          digest_items:digestItems,
          pm_source:process.env.PM_SOURCE||"none",
          pm_platform:process.env.PM_PLATFORM||"none",
          // The agent loop generates the adaptive narrative from these inputs,
          // leading with overdue, then approaching, then no-status items, and
          // ends with the follow-through rate. Anti-templated per AGENTS.md.
          narrative_instructions:"Lead with overdue items, then approaching deadlines, then items with no status update. Each line: task, owner, deadline, source meeting. End with the follow-through rate. Concise accountability check, not a report.",
          // Weekly-learning signal (formerly capability-evolver, which is
          // fictional): the agent loop inspects reviewer-edit / override state
          // keys and adjusts thresholds. Captured here as a deferred intent.
          learning_focus:"Which extracted action items do reviewers consistently remove (threshold too aggressive)? Which tentative items consistently become definitive (too conservative for this team)? Which summary sections get heavily edited (template needs adjustment)?"
        };
        payload.item_count=digestItems.length;
        process.stdout.write(JSON.stringify(payload));'
    stdin: $track-deadlines.stdout
    env:
      PM_SOURCE: $normalize-pm-status.json.source
      PM_PLATFORM: $pm-target.json.platform

  # 10. Load the notifications queue (single-key index array) and append the
  #     digest intent. The agent loop drains this: writes the LLM narrative and
  #     posts to Slack. Always enqueue (the digest runs even with zero items so
  #     the follow-through rate / "all clear" is still surfaced).
  - id: notify-load
    pipeline: state.get echo/notifications

  - id: notify-next
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const d=JSON.parse(require("fs").readFileSync(0,"utf8"));
        q.push(d);
        process.stdout.write(JSON.stringify(q));'
    stdin: $digest-payload.stdout
    env:
      QUEUE: $notify-load.json

  - id: notify
    pipeline: state.set echo/notifications
    stdin: $notify-next.stdout

  # 11. Append a deterministic audit row (single-key index array). Replaces the
  #     fast-io echo-audit write; {{date}}/{{timestamp}} were inert, so the date
  #     is computed here in the user's timezone (falls back to UTC).
  - id: audit-load
    pipeline: state.get echo/digest-audit

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const p=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const tz=process.env.USER_TZ||"UTC";
        let date;
        try { date=new Date().toLocaleDateString("en-CA",{timeZone:tz}); }
        catch { date=new Date().toISOString().slice(0,10); }
        log.push({
          action:"weekly_digest",
          date,
          overdue:p.overdue,
          approaching:p.approaching,
          completed:p.completed,
          in_progress:p.in_progress,
          stale_no_deadline:p.stale_no_deadline,
          follow_through_rate:p.follow_through_rate,
          pm_tool:p.pm_platform,
          item_count:p.item_count,
          authorized_by:"autonomous",
          timestamp:new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(log));'
    stdin: $digest-payload.stdout
    env:
      LOG: $audit-load.json

  - id: audit
    pipeline: state.set echo/digest-audit
    stdin: $audit-next.stdout
