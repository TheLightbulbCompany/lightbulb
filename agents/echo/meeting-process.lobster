name: meeting-process
# Echo's transcript→reviewed-record pipeline, rebuilt to RUN on the embedded
# OpenClaw runner (gateway-free, deterministic). Intent preserved from the
# original (Reqs 5-31): ingest a meeting transcript from the connected platform,
# preprocess (Cornell silence/confidence/attribution fix), classify commitments,
# extract attributed action items, format per meeting-type template, gate on a
# real human approval, then distribute + route + archive.
#
# Contract mechanics (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - There is NO fast-io. Durable state is native state.get/set, namespaced `echo/*`.
#    Config the user click-connects (consent, templates, platforms, PM, CRM,
#    thresholds) is read-only CONSUMER state under `echo-config/*` — the settings
#    UI writes it. The agent archive/audit are single-key INDEX ARRAYS we append
#    to (state.* has no prefix-list), so the weekly-digest can enumerate them.
#  - The .js scripts are FLAT in the agent dir: `node echo-X.js` (NOT scripts/).
#    They read stdin-JSON and write stdout-JSON; we assemble + feed their stdin.
#  - LLM judgment (ambiguous-commitment classification, summary narrative,
#    follow-up draft) and Slack delivery CANNOT run here (401). They are written
#    to `echo/notifications` as typed intents for the agent loop to fulfil.
#  - Human review is a real step-level `approval: required` gate BEFORE any
#    side effect; every distribution/routing step gates on `$await-review.approved`.
#  - Fictional integrations dropped: fast-io, meeting-prep, ms365, llm-task,
#    slack, capability-evolver, lobster await-approval, attio-enhanced.
#    Attio CRM goes through the baked `mcporter` CLI; HubSpot/Salesforce/Pipedrive
#    and PM tools (Asana/Linear/Jira) and Zoom are direct curl with env API keys.
#  - Scripts always exit 0; the run branches via `condition:`.

args:
  meeting_id:
    required: true

steps:
  # ── Step 1: Consent gate (Tier 2 hard-block) ───────────────────────────────
  # Read the user-set consent config. The ORIGINAL used `process.exit(1)` to
  # hard-block — that aborts the whole run under on_error:stop (self-DoS). We
  # exit 0, carry {pass} in JSON, and gate every downstream step on it.
  - id: load-consent
    pipeline: state.get echo-config/consent

  - id: consent-gate
    command: >
      node -e 'const c=JSON.parse(process.env.CONSENT||"null");
        process.stdout.write(JSON.stringify({pass: !!(c && c.confirmed)}));'
    env:
      CONSENT: $load-consent.stdout

  # If consent is missing/unconfirmed, queue a notice for the agent and stop here.
  - id: consent-block-load
    condition: "!$consent-gate.json.pass"
    pipeline: state.get echo/notifications

  - id: consent-block-next
    condition: "!$consent-gate.json.pass"
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"blocked", reason:"consent_not_confirmed",
          meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          text:"HARD BLOCK: Recording consent not confirmed. Echo cannot process this meeting until consent practices are confirmed in settings.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $consent-block-load.stdout

  - id: consent-block-save
    condition: "!$consent-gate.json.pass"
    pipeline: state.set echo/notifications
    stdin: $consent-block-next.stdout

  # ── Step 2: Load meeting metadata from calendar (real CLI: gog) ─────────────
  # gog emits JSON; downstream node steps JSON.parse process.env.MEETING.
  - id: load-meeting
    condition: $consent-gate.json.pass
    command: >
      gog calendar events get --event-id "$LOBSTER_ARG_MEETING_ID" --json

  # ── Step 3: Read the connected meeting platforms (consumer config) ──────────
  - id: load-platforms
    condition: $consent-gate.json.pass
    pipeline: state.get echo-config/connected-meeting-platforms

  # Emit an explicit connection summary so later steps gate on booleans, not
  # nested `.connected` reads that may be undefined.
  - id: platform-flags
    condition: $consent-gate.json.pass
    command: >
      node -e 'const p=JSON.parse(process.env.PLATFORMS||"null")||{};
        const zoom=!!(p.zoom&&p.zoom.connected), gmeet=!!(p.gmeet&&p.gmeet.connected), teams=!!(p.teams&&p.teams.connected);
        process.stdout.write(JSON.stringify({zoom, gmeet, teams, any: zoom||gmeet||teams,
          connected_count:[zoom,gmeet,teams].filter(Boolean).length}));'
    env:
      PLATFORMS: $load-platforms.stdout

  # If nothing is connected, queue the click-to-connect notice and stop.
  # Gate also on consent.pass: platform-flags only runs on the consent-passed
  # path, so this condition must match that gate or it would reference a
  # skipped step on the consent-blocked path.
  - id: no-platform-load
    condition: $consent-gate.json.pass && !$platform-flags.json.any
    pipeline: state.get echo/notifications

  - id: no-platform-next
    condition: $consent-gate.json.pass && !$platform-flags.json.any
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"connect_required", service:"meeting_platform",
          meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          text:"To process meeting transcripts, connect a meeting platform in your settings. Supported: Zoom, Google Meet, Microsoft Teams. You can connect more than one — Echo pulls from whichever hosted the meeting.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $no-platform-load.stdout

  - id: no-platform-save
    condition: $consent-gate.json.pass && !$platform-flags.json.any
    pipeline: state.set echo/notifications
    stdin: $no-platform-next.stdout

  # ── Step 4: Pull the transcript from whichever platform hosted the meeting ──
  # Zoom: direct curl to the Cloud Recording Transcript API (token from env).
  - id: pull-zoom
    condition: $platform-flags.json.zoom
    command: >
      curl -s -H "Authorization: Bearer $ZOOM_OAUTH_TOKEN"
      "https://api.zoom.us/v2/meetings/$LOBSTER_ARG_MEETING_ID/recordings/transcript"

  # Google Meet: recordings + captions live in Drive, pulled via gog.
  - id: pull-gmeet
    condition: $platform-flags.json.gmeet
    command: >
      gog drive meet-captions --meeting-id "$LOBSTER_ARG_MEETING_ID" --json

  # Microsoft Teams: the ms365/Graph path is NOT wired in this build (ms365 skill
  # is disabled, no gateway-free CLI). Queue a connect-notice instead of a fake
  # fetch, so a Teams-only meeting surfaces honestly rather than silently empty.
  - id: teams-unavailable-load
    condition: $platform-flags.json.teams && !$platform-flags.json.zoom && !$platform-flags.json.gmeet
    pipeline: state.get echo/notifications

  - id: teams-unavailable-next
    condition: $platform-flags.json.teams && !$platform-flags.json.zoom && !$platform-flags.json.gmeet
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"connect_required", service:"teams_transcript_path",
          meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          text:"Microsoft Teams transcript retrieval (Graph API) is not available in this Echo build. Use Zoom or Google Meet for automatic transcripts, or paste the Teams transcript manually.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $teams-unavailable-load.stdout

  - id: teams-unavailable-save
    condition: $platform-flags.json.teams && !$platform-flags.json.zoom && !$platform-flags.json.gmeet
    pipeline: state.set echo/notifications
    stdin: $teams-unavailable-next.stdout

  # ── Step 5: Normalize the provider payload into canonical segments ──────────
  # echo-transcript-normalizer.js picks the first populated provider. We pass
  # zoom/gmeet (teams omitted — unwired). Refs are null when their branch didn't
  # run, which the script's isPopulated() guard treats as "absent".
  - id: normalize-input
    condition: $platform-flags.json.zoom || $platform-flags.json.gmeet
    command: >
      node -e 'process.stdout.write(JSON.stringify({
        zoom: JSON.parse(process.env.ZOOM||"null"),
        gmeet: JSON.parse(process.env.GMEET||"null")
      }));'
    env:
      ZOOM: $pull-zoom.stdout
      GMEET: $pull-gmeet.stdout

  - id: normalize
    condition: $platform-flags.json.zoom || $platform-flags.json.gmeet
    command: node echo-transcript-normalizer.js
    stdin: $normalize-input.stdout

  # Explicit segment count → gate the rest on real data, never on .length.
  - id: transcript-count
    condition: $platform-flags.json.zoom || $platform-flags.json.gmeet
    command: >
      node -e 'const r=JSON.parse(process.env.NORM||"null")||{};
        process.stdout.write(JSON.stringify({count:(r.segments||[]).length, source:r.source||null}));'
    env:
      NORM: $normalize.stdout

  # ── Step 6: Audio preprocessing (Cornell fix: silence/confidence/attribution) ─
  - id: preprocess-input
    condition: $transcript-count.json.count > 0
    command: >
      node -e 'const norm=JSON.parse(process.env.NORM||"{}");
        const meeting=JSON.parse(process.env.MEETING||"{}");
        process.stdout.write(JSON.stringify({
          segments: norm.segments||[],
          confidence_threshold: 0.85,
          attendees: meeting.attendees||[]
        }));'
    env:
      NORM: $normalize.stdout
      MEETING: $load-meeting.stdout

  - id: preprocess
    condition: $transcript-count.json.count > 0
    command: node echo-audio-preprocessor.js
    stdin: $preprocess-input.stdout

  # ── Step 7: Load custom commitment thresholds (consumer config) ─────────────
  - id: load-thresholds
    condition: $transcript-count.json.count > 0
    pipeline: state.get echo-config/commitment-thresholds

  # ── Step 8: Deterministic commitment classification (~60% of cases) ─────────
  - id: classify-input
    condition: $transcript-count.json.count > 0
    command: >
      node -e 'const pre=JSON.parse(process.env.PRE||"{}");
        const thr=JSON.parse(process.env.THRESHOLDS||"null")||{};
        process.stdout.write(JSON.stringify({
          statements: pre.processed_segments||[],
          custom_thresholds: thr
        }));'
    env:
      PRE: $preprocess.stdout
      THRESHOLDS: $load-thresholds.stdout

  - id: classify-commitments
    condition: $transcript-count.json.count > 0
    command: node echo-commitment-classifier.js
    stdin: $classify-input.stdout

  # ── Step 9: Ambiguous classifications need LLM judgment → defer to agent loop ─
  # llm-task is 401 here. Queue the ambiguous segments to echo/notifications so
  # the agent loop classifies them conservatively (default: tentative) and folds
  # the result back in on the next pass. Gated on an explicit count, not .length.
  - id: ambiguous-load
    condition: $classify-commitments.json.needs_llm_count > 0
    pipeline: state.get echo/notifications

  - id: ambiguous-next
    condition: $classify-commitments.json.needs_llm_count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const c=JSON.parse(process.env.CLASSIFIED||"{}");
        const ambiguous=(c.classified||[]).filter(x=>x.needs_llm);
        n.push({kind:"llm_classify_ambiguous", meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          count:ambiguous.length, segments:ambiguous,
          instruction:"Classify each ambiguous statement as definitive commitment, tentative proposal, or not an action item. Be conservative — when in doubt, classify as tentative.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $ambiguous-load.stdout
      CLASSIFIED: $classify-commitments.stdout

  - id: ambiguous-save
    condition: $classify-commitments.json.needs_llm_count > 0
    pipeline: state.set echo/notifications
    stdin: $ambiguous-next.stdout

  # ── Step 10: Extract action items with speaker-verified attribution ─────────
  - id: extract-input
    condition: $transcript-count.json.count > 0
    command: >
      node -e 'const c=JSON.parse(process.env.CLASSIFIED||"{}");
        const m=JSON.parse(process.env.MEETING||"{}");
        process.stdout.write(JSON.stringify({
          classified: c.classified||[],
          attendees: m.attendees||[],
          meeting_context: {
            meeting_id: process.env.LOBSTER_ARG_MEETING_ID,
            title: m.title||"",
            date: m.date||"",
            recording_url: m.recording_url||""
          }
        }));'
    env:
      CLASSIFIED: $classify-commitments.stdout
      MEETING: $load-meeting.stdout

  - id: extract-actions
    condition: $transcript-count.json.count > 0
    command: node echo-action-extractor.js
    stdin: $extract-input.stdout

  # ── Step 11: Format for review per meeting type ─────────────────────────────
  - id: format-input
    condition: $transcript-count.json.count > 0
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        const pre=JSON.parse(process.env.PRE||"{}");
        process.stdout.write(JSON.stringify({
          meeting_type: m.meeting_type||"standup",
          action_items: ex.action_items||[],
          tentative_items: ex.tentative_items||[],
          flagged_segments: pre.processed_segments||[],
          meeting_meta: {
            title: m.title||"", date: m.date||"", attendees: m.attendees||[],
            duration_minutes: m.duration_minutes||null,
            reviewer: m.reviewer||null, secretary: m.secretary||null
          },
          custom_template: null
        }));'
    env:
      MEETING: $load-meeting.stdout
      EXTRACT: $extract-actions.stdout
      PRE: $preprocess.stdout

  - id: format-review
    condition: $transcript-count.json.count > 0
    command: node echo-synthesis-formatter.js
    stdin: $format-input.stdout

  # ── Step 12: Summary narrative needs the LLM → queue for the agent loop ─────
  # The adaptive, anti-seniority-bias summary is model work (401 here). Write a
  # typed intent carrying everything the agent needs; the agent loop generates
  # the narrative and resumes review delivery. Deterministic data (segments,
  # actions, template instructions) travels with it.
  - id: summary-intent-load
    condition: $transcript-count.json.count > 0
    pipeline: state.get echo/notifications

  - id: summary-intent-next
    condition: $transcript-count.json.count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const fmt=JSON.parse(process.env.FORMAT||"{}");
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        const pre=JSON.parse(process.env.PRE||"{}");
        const m=JSON.parse(process.env.MEETING||"{}");
        n.push({kind:"generate_summary", meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          meeting_type:fmt.meeting_type, title:m.title||"",
          llm_instructions:fmt.llm_instructions,
          reviewer:fmt.reviewer, requires_specific_reviewer:fmt.requires_specific_reviewer,
          flags:fmt.flags,
          action_items:ex.action_items||[], tentative_items:ex.tentative_items||[],
          transcript_segments:pre.processed_segments||[],
          instruction:"Generate the meeting summary constrained by llm_instructions (capture/omit per template). Attribute ideas to the ORIGINATOR, not the senior person who agreed. Then present summary + action items + flags to the configured reviewer for approval.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $summary-intent-load.stdout
      FORMAT: $format-review.stdout
      EXTRACT: $extract-actions.stdout
      PRE: $preprocess.stdout
      MEETING: $load-meeting.stdout

  - id: summary-intent-save
    condition: $transcript-count.json.count > 0
    pipeline: state.set echo/notifications
    stdin: $summary-intent-next.stdout

  # ── Step 13: HUMAN REVIEW — real step-level approval gate ───────────────────
  # Echo's non-negotiable: a human approves the record before ANY distribution.
  # This halts the run with a resumeToken; the reviewer resumes with approve:true
  # and the step exposes $await-review.approved. The summary narrative the
  # reviewer reads is produced by the agent loop from the intent queued above.
  - id: await-review
    condition: $transcript-count.json.count > 0
    approval:
      required: true
      prompt: "Echo meeting summary requires reviewer approval before distribution. Approve to distribute the summary, route action items, and update the CRM."

  # ── Step 14: Distribute approved summary to attendees (gog gmail) ───────────
  # The agent loop, when it fulfils the generate_summary intent, appends the
  # narrative to the `echo/summaries` index array (state.* has no per-key
  # interpolation, so we use a single static key and pick by meeting_id here).
  # Board template ("configured_recipients_only") is excluded from the broad send.
  - id: load-summaries
    condition: $await-review.approved
    pipeline: state.get echo/summaries

  - id: load-summary
    condition: $await-review.approved
    command: >
      node -e 'const all=JSON.parse(process.env.SUMMARIES||"null")||[];
        const mid=process.env.LOBSTER_ARG_MEETING_ID;
        const hit=all.filter(s=>s&&s.meeting_id===mid).slice(-1)[0]||{};
        process.stdout.write(JSON.stringify({summary:hit.summary||"", found:!!hit.summary}));'
    env:
      SUMMARIES: $load-summaries.stdout

  - id: distribute-input
    condition: $await-review.approved && $format-review.json.distribution_rule != "configured_recipients_only"
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        const to=(m.attendees||[]).map(a=>a.email||a).filter(Boolean).join(",");
        process.stdout.write(JSON.stringify({to, subject:"Meeting Summary: "+(m.title||""),
          body:s.summary||"(summary pending agent generation)"}));'
    env:
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout

  - id: distribute-email
    condition: $await-review.approved && $format-review.json.distribution_rule != "configured_recipients_only"
    command: >
      gog gmail send --to "$DIST_TO" --subject "$DIST_SUBJECT" --body "$DIST_BODY"
    env:
      DIST_TO: $distribute-input.json.to
      DIST_SUBJECT: $distribute-input.json.subject
      DIST_BODY: $distribute-input.json.body

  # Slack delivery of the approved summary is 401 here → queue it.
  - id: notify-approved-load
    condition: $await-review.approved
    pipeline: state.get echo/notifications

  - id: notify-approved-next
    condition: $await-review.approved
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        n.push({kind:"approved_summary", meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          title:m.title||"", summary:s.summary||"",
          text:"APPROVED: "+(m.title||""),
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-approved-load.stdout
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout

  - id: notify-approved-save
    condition: $await-review.approved
    pipeline: state.set echo/notifications
    stdin: $notify-approved-next.stdout

  # ── Step 15: Route action items to the connected PM tool ────────────────────
  - id: load-pm
    condition: $await-review.approved
    pipeline: state.get echo-config/connected-pm-tool

  - id: pm-flags
    condition: $await-review.approved
    command: >
      node -e 'const p=JSON.parse(process.env.PM||"null")||{};
        const plat=p.platform||"";
        process.stdout.write(JSON.stringify({platform:plat,
          asana:plat==="asana", linear:plat==="linear", jira:plat==="jira",
          connected:plat==="asana"||plat==="linear"||plat==="jira"}));'
    env:
      PM: $load-pm.stdout

  # No PM tool → queue the connect notice (summary still distributed + archived).
  - id: no-pm-load
    condition: $await-review.approved && !$pm-flags.json.connected
    pipeline: state.get echo/notifications

  - id: no-pm-next
    condition: $await-review.approved && !$pm-flags.json.connected
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"connect_required", service:"pm_tool",
          meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          text:"Action items for this meeting were not routed to a project tracker. To auto-route approved action items, connect Asana, Linear, or Jira in settings.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $no-pm-load.stdout

  - id: no-pm-save
    condition: $await-review.approved && !$pm-flags.json.connected
    pipeline: state.set echo/notifications
    stdin: $no-pm-next.stdout

  # Asana (direct REST). Body assembled by node, fed to curl via stdin.
  - id: asana-body
    condition: $await-review.approved && $pm-flags.json.asana
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        const pm=JSON.parse(process.env.PM||"{}");
        process.stdout.write(JSON.stringify({data:{
          workspace:pm.workspace_id, projects:[pm.default_project_id],
          name:"Echo action items from "+(m.title||""),
          notes:JSON.stringify(ex.action_items||[]),
          assignee:pm.default_assignee}}));'
    env:
      MEETING: $load-meeting.stdout
      EXTRACT: $extract-actions.stdout
      PM: $load-pm.stdout

  - id: push-asana
    condition: $await-review.approved && $pm-flags.json.asana
    command: >
      curl -s -X POST -H "Authorization: Bearer $ASANA_ACCESS_TOKEN"
      -H "Content-Type: application/json" -d @- "https://app.asana.com/api/1.0/tasks"
    stdin: $asana-body.stdout

  # Linear (GraphQL).
  - id: linear-body
    condition: $await-review.approved && $pm-flags.json.linear
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        const pm=JSON.parse(process.env.PM||"{}");
        process.stdout.write(JSON.stringify({
          query:"mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier } } }",
          variables:{input:{teamId:pm.team_id,
            title:"Echo action items from "+(m.title||""),
            description:JSON.stringify(ex.action_items||[])}}}));'
    env:
      MEETING: $load-meeting.stdout
      EXTRACT: $extract-actions.stdout
      PM: $load-pm.stdout

  - id: push-linear
    condition: $await-review.approved && $pm-flags.json.linear
    command: >
      curl -s -X POST -H "Authorization: $LINEAR_API_KEY"
      -H "Content-Type: application/json" -d @- "https://api.linear.app/graphql"
    stdin: $linear-body.stdout

  # Jira (REST). base_url + project_key come from the connected-pm config.
  - id: jira-body
    condition: $await-review.approved && $pm-flags.json.jira
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        const pm=JSON.parse(process.env.PM||"{}");
        process.stdout.write(JSON.stringify({fields:{
          project:{key:pm.project_key},
          summary:"Echo action items from "+(m.title||""),
          description:JSON.stringify(ex.action_items||[]),
          issuetype:{name:"Task"}}}));'
    env:
      MEETING: $load-meeting.stdout
      EXTRACT: $extract-actions.stdout
      PM: $load-pm.stdout

  - id: push-jira
    condition: $await-review.approved && $pm-flags.json.jira
    command: >
      curl -s -X POST -u "$JIRA_EMAIL:$JIRA_API_TOKEN"
      -H "Content-Type: application/json" -d @- "$JIRA_BASE_URL/rest/api/3/issue"
    stdin: $jira-body.stdout
    env:
      JIRA_BASE_URL: $load-pm.json.base_url

  # ── Step 16: Update the connected CRM (customer meetings only) ──────────────
  - id: load-crm
    condition: $await-review.approved && $format-review.json.crm_update
    pipeline: state.get echo-config/connected-crm

  - id: crm-flags
    condition: $await-review.approved && $format-review.json.crm_update
    command: >
      node -e 'const c=JSON.parse(process.env.CRM||"null")||{};
        const plat=c.platform||"";
        process.stdout.write(JSON.stringify({platform:plat,
          hubspot:plat==="hubspot", attio:plat==="attio",
          salesforce:plat==="salesforce", pipedrive:plat==="pipedrive",
          connected:["hubspot","attio","salesforce","pipedrive"].includes(plat)}));'
    env:
      CRM: $load-crm.stdout

  - id: no-crm-load
    condition: $await-review.approved && $format-review.json.crm_update && !$crm-flags.json.connected
    pipeline: state.get echo/notifications

  - id: no-crm-next
    condition: $await-review.approved && $format-review.json.crm_update && !$crm-flags.json.connected
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"connect_required", service:"crm",
          meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          text:"To write meeting notes to your CRM, connect it in settings. Supported: HubSpot, Salesforce, Attio, Pipedrive.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $no-crm-load.stdout

  - id: no-crm-save
    condition: $await-review.approved && $format-review.json.crm_update && !$crm-flags.json.connected
    pipeline: state.set echo/notifications
    stdin: $no-crm-next.stdout

  # Attio: gateway-free via the baked mcporter CLI. Resolve the company by the
  # client contact's email domain, then attach the summary as a note. mcporter
  # output is YAML-ish TEXT, so the node step parses stdout (never JSON.parse).
  - id: attio-note
    condition: $await-review.approved && $format-review.json.crm_update && $crm-flags.json.attio
    command: >
      node -e 'const {execSync}=require("child_process");
        const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        const email=(m.client_contact||""); const domain=(email.split("@")[1]||"").toLowerCase();
        let rid=""; if (domain) { let txt="";
          try { txt=execSync("mcporter call attio.search-records --args "
            +JSON.stringify(JSON.stringify({object:"companies",query:domain,limit:1}))+" --output text",
            {encoding:"utf8",timeout:45000}); } catch(e){ txt=""; }
          rid=(txt.match(/record_id:\s*([0-9a-f-]{36})/)||[])[1]||""; }
        let note="skipped";
        if (rid) { const body="Meeting "+(m.title||"")+" ("+(m.date||"")+"). "+(s.summary||"")
            +" Next steps: "+JSON.stringify(ex.action_items||[]);
          const n={parent_object:"companies", parent_record_id:rid,
            title:"Echo meeting note: "+(m.title||""), content:body};
          try { execSync("mcporter call attio.create-note --args "+JSON.stringify(JSON.stringify(n))+" --output text",
            {encoding:"utf8",timeout:45000}); note="created"; } catch(e){ note="ERR:"+e.message; } }
        process.stdout.write(JSON.stringify({record_id:rid, note}));'
    env:
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout
      EXTRACT: $extract-actions.stdout

  # HubSpot: direct REST (engagement). Token from env.
  - id: hubspot-body
    condition: $await-review.approved && $format-review.json.crm_update && $crm-flags.json.hubspot
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        process.stdout.write(JSON.stringify({properties:{
          hs_engagement_type:"MEETING",
          hs_meeting_title:"Meeting: "+(m.title||""),
          hs_meeting_body:s.summary||""}}));'
    env:
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout

  - id: push-hubspot
    condition: $await-review.approved && $format-review.json.crm_update && $crm-flags.json.hubspot
    command: >
      curl -s -X POST -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN"
      -H "Content-Type: application/json" -d @- "https://api.hubapi.com/crm/v3/objects/meetings"
    stdin: $hubspot-body.stdout

  # Salesforce: direct REST (Task). instance_url from the connected-crm config.
  - id: salesforce-body
    condition: $await-review.approved && $format-review.json.crm_update && $crm-flags.json.salesforce
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        process.stdout.write(JSON.stringify({
          WhoId:m.client_contact_id||"", Subject:"Meeting: "+(m.title||""),
          ActivityDate:m.date||"", Description:s.summary||""}));'
    env:
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout

  - id: push-salesforce
    condition: $await-review.approved && $format-review.json.crm_update && $crm-flags.json.salesforce
    command: >
      curl -s -X POST -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"
      -H "Content-Type: application/json" -d @- "$SF_INSTANCE_URL/services/data/v60.0/sobjects/Task"
    stdin: $salesforce-body.stdout
    env:
      SF_INSTANCE_URL: $load-crm.json.instance_url

  # Pipedrive: direct REST (note). API token in the query string from env.
  - id: pipedrive-body
    condition: $await-review.approved && $format-review.json.crm_update && $crm-flags.json.pipedrive
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        process.stdout.write(JSON.stringify({
          person_id:m.client_contact_id||"",
          content:"Meeting: "+(m.title||"")+"\n\n"+(s.summary||"")}));'
    env:
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout

  - id: push-pipedrive
    condition: $await-review.approved && $format-review.json.crm_update && $crm-flags.json.pipedrive
    command: >
      curl -s -X POST -H "Content-Type: application/json" -d @-
      "https://api.pipedrive.com/v1/notes?api_token=$PIPEDRIVE_API_TOKEN"
    stdin: $pipedrive-body.stdout

  # ── Step 17: Customer follow-up email DRAFT → agent loop (LLM + rep review) ──
  # Drafting is model work, and Echo never auto-sends client email. Queue a
  # draft_followup intent; the agent loop writes the draft and presents it to
  # the rep for review before anything is sent.
  - id: followup-load
    condition: $await-review.approved && $format-review.json.crm_update
    pipeline: state.get echo/notifications

  - id: followup-next
    condition: $await-review.approved && $format-review.json.crm_update
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        n.push({kind:"draft_followup", meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          client:m.client_contact||"", title:m.title||"",
          summary:s.summary||"", action_items:ex.action_items||[],
          instruction:"Draft a client follow-up email (thanks, what was discussed, commitments by each party, next steps with timelines) in the rep's voice. Present to the rep for review — NEVER auto-send.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $followup-load.stdout
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout
      EXTRACT: $extract-actions.stdout

  - id: followup-save
    condition: $await-review.approved && $format-review.json.crm_update
    pipeline: state.set echo/notifications
    stdin: $followup-next.stdout

  # ── Step 18: Institutional memory — append to the archive INDEX ARRAY ───────
  # state.* has no prefix-list, so the archive is a single-key array the
  # weekly-digest enumerates. Append this meeting's reviewed record.
  - id: archive-load
    condition: $await-review.approved
    pipeline: state.get echo/archive

  - id: archive-next
    condition: $await-review.approved
    command: >
      node -e 'const a=JSON.parse(process.env.ARCHIVE||"null")||[];
        const m=JSON.parse(process.env.MEETING||"{}");
        const fmt=JSON.parse(process.env.FORMAT||"{}");
        const ex=JSON.parse(process.env.EXTRACT||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        a.push({meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          title:m.title||"", date:m.date||"", attendees:m.attendees||[],
          meeting_type:fmt.meeting_type, summary:s.summary||"",
          action_items:ex.action_items||[], tentative_items:ex.tentative_items||[],
          confidential:fmt.confidential||false,
          reviewed:true, archived_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(a));'
    env:
      ARCHIVE: $archive-load.stdout
      MEETING: $load-meeting.stdout
      FORMAT: $format-review.stdout
      EXTRACT: $extract-actions.stdout
      SUMMARY: $load-summary.stdout

  - id: archive-save
    condition: $await-review.approved
    pipeline: state.set echo/archive
    stdin: $archive-next.stdout

  # Also write a human-readable copy to Drive (real CLI: gog).
  - id: archive-drive
    condition: $await-review.approved
    command: >
      node -e 'const m=JSON.parse(process.env.MEETING||"{}");
        const s=JSON.parse(process.env.SUMMARY||"null")||{};
        const path="Echo Meeting Archive/"+(m.date||"")+" - "+(m.title||"untitled");
        const {execSync}=require("child_process");
        try { execSync("gog drive write --path "+JSON.stringify(path)+" --content "+JSON.stringify(s.summary||""),
          {encoding:"utf8",timeout:45000,stdio:"ignore"}); process.stdout.write(JSON.stringify({drive:"written"})); }
        catch(e){ process.stdout.write(JSON.stringify({drive:"ERR:"+e.message})); }'
    env:
      MEETING: $load-meeting.stdout
      SUMMARY: $load-summary.stdout

  # ── Step 19: Audit log — append to the audit INDEX ARRAY ────────────────────
  # Gated on the transcript branch (count > 0) so every step it references
  # (format-review, transcript-count, pm-flags, await-review) is guaranteed to
  # have run — refs to a skipped step do not resolve. Early-exit paths
  # (consent-blocked, no-platform) already recorded their own echo/notifications.
  - id: audit-load
    condition: $transcript-count.json.count > 0
    pipeline: state.get echo/audit

  # $await-review.approved is exposed DIRECTLY by the approval step (not as
  # stdout JSON), so it is threaded in as a scalar env value.
  - id: audit-next
    condition: $transcript-count.json.count > 0
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const m=JSON.parse(process.env.MEETING||"null")||{};
        const fmt=JSON.parse(process.env.FORMAT||"null")||{};
        const tc=JSON.parse(process.env.TC||"null")||{};
        const pm=JSON.parse(process.env.PMF||"null")||{};
        const approved=process.env.APPROVED==="true";
        log.push({action:"meeting_processed",
          meeting_id:process.env.LOBSTER_ARG_MEETING_ID,
          title:m.title||"", meeting_type:fmt.meeting_type||null,
          transcript_source:tc.source||null, transcript_segments:tc.count||0,
          flags_count:(fmt.flags&&fmt.flags.count)||0,
          pm_tool:pm.platform||null,
          distributed: approved,
          reviewed_by:(fmt.reviewer)||null,
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      MEETING: $load-meeting.stdout
      FORMAT: $format-review.stdout
      TC: $transcript-count.stdout
      PMF: $pm-flags.stdout
      APPROVED: $await-review.approved

  - id: audit-save
    condition: $transcript-count.json.count > 0
    pipeline: state.set echo/audit
    stdin: $audit-next.stdout
