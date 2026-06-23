name: message-outbound
# Route an outbound request to the right channel, draft it (agent loop), gate it
# behind explicit human approval for external contacts, then hand the approved item
# to the agent loop for delivery + record-keeping. Intent preserved from the original
# (Reqs 12,15,16,17,20,21,11); rebuilt to run on the embedded OpenClaw runner.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - There is no `fast-io` and no gateway: the LLM draft (llm-task) and the Slack/
#    WhatsApp/LinkedIn/SMS *delivery* steps would all 401 here, so this pipeline stays
#    deterministic and writes an APPROVED outbox intent to `thread/outbox` for the
#    agent loop to (a) draft with channel-appropriate tone and (b) actually send.
#  - SECURITY (AGENTS.md "Send Architecture — The Hard Gates"): every EXTERNAL send
#    requires the user to see + confirm. Modeled here with a real step-level
#    `approval: required` BEFORE the outbox is enqueued. Only designated internal Slack
#    channels are auto-send-eligible (router decides `requires_confirmation`).
#  - Durable state via native state.get/set on the per-agent EFS mount. Anything that
#    was a fast-io prefix becomes a SINGLE-KEY INDEX ARRAY (state.* has no prefix-list):
#    thread/outbox, thread/outbound-tracker, thread/audit, and per-contact history rows
#    are appended in a node step then re-written. Connected-channels / overrides /
#    contacts-by-name are read-only consumer config the user/agent maintains.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  intent:
    required: true    # "follow up with Marcus about the proposal, tell him we're ready to sign"
  contact_name:
    required: true
  explicit_channel:
    default: ""       # User can override the routed channel

steps:
  # 1. Load the contact-by-name lookup index (read-only consumer config the agent maintains).
  #    Native state replaces the fictional fast-io "thread-contacts/by-name/<name>" read;
  #    state.* has no prefix-list, so this is one index object keyed by lowercased name.
  - id: contacts-index
    pipeline: state.get thread/contacts-by-name

  # 2. Resolve the named contact out of the index (exit 0; carries found flag + contact).
  - id: resolve-contact
    command: >
      node -e 'const idx=JSON.parse(process.env.IDX||"null")||{};
        const name=(process.env.LOBSTER_ARG_CONTACT_NAME||"").trim();
        const key=name.toLowerCase();
        const c=idx[key]||idx[name]||null;
        process.stdout.write(JSON.stringify({found:!!c, contact:c||{}, query:name}));'
    env:
      IDX: $contacts-index.stdout

  # 3. Per-contact relationship history (single-key index array, agent-namespaced).
  - id: history
    condition: $resolve-contact.json.found
    pipeline: state.get thread/contact-history

  # 4. Per-contact channel-preference model (behavioral; learned nightly by the cron
  #    under thread/preferences — match that producer key, not a contact-* alias).
  - id: preferences
    condition: $resolve-contact.json.found
    pipeline: state.get thread/preferences

  # 5. Tracked outbound (for the pending-follow-ups slice of contact context).
  - id: outbound-tracker
    condition: $resolve-contact.json.found
    pipeline: state.get thread/outbound-tracker

  # 6. Assemble the contact-context input for thread-contact-context.js.
  #    The script expects {contact, history, preferences, pending_followups}; the original
  #    fed it nothing, so build it here. History/preferences are per-contact slices.
  - id: context-input
    condition: $resolve-contact.json.found
    command: >
      node -e 'const c=JSON.parse(process.env.CONTACT||"{}");
        const allHist=JSON.parse(process.env.HIST||"null")||{};
        const allPref=JSON.parse(process.env.PREF||"null")||{};
        const tracker=JSON.parse(process.env.TRACKER||"null")||[];
        const cid=c.id||c.email||"";
        const history=Array.isArray(allHist)?allHist:(allHist[cid]||[]);
        const preferences=allPref[cid]||{};
        const pending=tracker.filter(t=>(t.contact_id===c.id||t.contact_email===c.email)&&t.awaiting_response);
        process.stdout.write(JSON.stringify({contact:c, history, preferences, pending_followups:pending}));'
    env:
      CONTACT: $resolve-contact.json.contact
      HIST: $history.stdout
      PREF: $preferences.stdout
      TRACKER: $outbound-tracker.stdout

  # 7. Build contact context (last contact, recent topic, follow-ups, best channel).
  - id: load-context
    condition: $resolve-contact.json.found
    command: node thread-contact-context.js
    stdin: $context-input.stdout

  # 8. Compress the recent conversation into a topic line (real `summarize` CLI; no
  #    gateway). Best-effort — wrapped so a missing summarize never aborts the run.
  #    Gate also carries the parent `found` so it never references a skipped load-context.
  - id: recent-topic
    condition: $resolve-contact.json.found && $load-context.json.needs_topic_summary
    command: >
      node -e 'const {execSync}=require("child_process");
        const ctx=JSON.parse(process.env.CTX||"{}");
        const msgs=(ctx.recent_messages||[]).map(m=>(m.direction||"")+" ("+(m.channel||"")+"): "+(m.preview||"")).join("\n");
        let topic="";
        try{ topic=execSync("summarize --stdin --format executive-summary --max-length 100",
          {input:msgs,encoding:"utf8",timeout:30000}).trim(); }catch(e){ topic=""; }
        if(!topic) topic=msgs.split("\n").slice(-2).join(" | ").slice(0,200);
        process.stdout.write(JSON.stringify({topic, source_messages:(ctx.recent_messages||[]).length}));'
    env:
      CTX: $load-context.stdout

  # 9. Routing inputs: connected channels + auto-send list + per-contact overrides
  #    (all read-only consumer config the user sets in settings / the agent maintains).
  - id: connected-channels
    condition: $resolve-contact.json.found
    pipeline: state.get thread/connected-channels

  - id: auto-send-channels
    condition: $resolve-contact.json.found
    pipeline: state.get thread/auto-send-channels

  - id: user-overrides
    condition: $resolve-contact.json.found
    pipeline: state.get thread/user-overrides

  # 10. Assemble router input. The original piped two raw reads into the router with no
  #     shaping; thread-channel-router.js needs {contact, connected_channels,
  #     auto_send_channels, user_overrides}. Build that exact object here.
  - id: router-input
    condition: $resolve-contact.json.found
    command: >
      node -e 'const contact=JSON.parse(process.env.CONTACT||"{}");
        const connected=JSON.parse(process.env.CONN||"null")||[];
        const autoSend=JSON.parse(process.env.AUTO||"null")||[];
        const overrides=JSON.parse(process.env.OVR||"null")||{};
        process.stdout.write(JSON.stringify({contact, connected_channels:connected,
          auto_send_channels:autoSend, user_overrides:overrides}));'
    env:
      CONTACT: $resolve-contact.json.contact
      CONN: $connected-channels.stdout
      AUTO: $auto-send-channels.stdout
      OVR: $user-overrides.stdout

  # 11. Route to a channel via the behavioral preference model (deterministic).
  - id: route
    condition: $resolve-contact.json.found
    command: node thread-channel-router.js
    stdin: $router-input.stdout

  # 12. Apply an explicit user channel override; recompute requires_confirmation.
  #     Emits an explicit `connected` count so downstream gates on a number, not .length.
  - id: apply-override
    condition: $resolve-contact.json.found
    command: >
      node -e 'const route=JSON.parse(process.env.ROUTE||"{}");
        const connected=JSON.parse(process.env.CONN||"null")||[];
        const explicit=(process.env.LOBSTER_ARG_EXPLICIT_CHANNEL||"").trim();
        if(explicit && explicit!==route.channel){
          route.channel=explicit;
          route.reason="User specified "+explicit+" for this message.";
          route.requires_confirmation=true;
          route.confidence="user_override";
        }
        const ch=route.channel||"";
        const channel_connected=ch?connected.includes(ch):false;
        const ready = !!ch && channel_connected;
        // External sends always require confirmation; router may waive ONLY for internal Slack.
        const requires_confirmation = route.requires_confirmation!==false;
        process.stdout.write(JSON.stringify({...route, channel:ch,
          requires_confirmation, channel_connected, ready,
          ready_count: ready?1:0, needs_channel: ch?0:1}));'
    env:
      ROUTE: $route.stdout
      CONN: $connected-channels.stdout

  # 13. No channel resolved (new contact / no preference data) — queue an ask for the
  #     agent loop instead of guessing. Mirrors the router's needs_agent_loop path.
  - id: notify-load-ask
    condition: $apply-override.json.needs_channel > 0
    pipeline: state.get thread/notifications

  - id: notify-ask-next
    condition: $apply-override.json.needs_channel > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const r=JSON.parse(process.env.ROUTE||"{}"); const c=JSON.parse(process.env.CONTACT||"{}");
        n.push({kind:"ask_channel", contact_name:c.name||process.env.LOBSTER_ARG_CONTACT_NAME,
          contact_id:c.id||c.email||"", intent:process.env.LOBSTER_ARG_INTENT||"",
          reason:r.reason||"No preference data — ask the user which channel to use.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load-ask.stdout
      ROUTE: $route.stdout
      CONTACT: $resolve-contact.json.contact

  - id: notify-ask
    condition: $apply-override.json.needs_channel > 0
    pipeline: state.set thread/notifications
    stdin: $notify-ask-next.stdout

  # 14. Contact not connected for the chosen channel — surface a click-to-connect notice
  #     (AGENTS.md "Missing Integrations") rather than attempting a send.
  - id: notify-load-connect
    condition: $apply-override.json.needs_channel == 0 && $apply-override.json.channel_connected == false
    pipeline: state.get thread/notifications

  - id: notify-connect-next
    condition: $apply-override.json.needs_channel == 0 && $apply-override.json.channel_connected == false
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const r=JSON.parse(process.env.ROUTE||"{}"); const c=JSON.parse(process.env.CONTACT||"{}");
        n.push({kind:"connect_channel", channel:r.channel,
          contact_name:c.name||process.env.LOBSTER_ARG_CONTACT_NAME,
          notice:"To message on "+r.channel+", connect "+r.channel+" in your settings.",
          intent:process.env.LOBSTER_ARG_INTENT||"", created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load-connect.stdout
      ROUTE: $apply-override.stdout
      CONTACT: $resolve-contact.json.contact

  - id: notify-connect
    condition: $apply-override.json.needs_channel == 0 && $apply-override.json.channel_connected == false
    pipeline: state.set thread/notifications
    stdin: $notify-connect-next.stdout

  # 15. Build the outbox intent (the draft+send brief for the agent loop). Carries
  #     everything the loop needs to draft with channel tone and deliver: the routed
  #     channel, the contact, the recent topic, tone guidance, and the relationship
  #     history. A stable message_id makes the downstream tracker/thread/audit rows joinable.
  - id: outbox-item
    condition: $apply-override.json.ready_count > 0
    command: >
      node -e 'const route=JSON.parse(process.env.ROUTE||"{}");
        const contact=JSON.parse(process.env.CONTACT||"{}");
        const ctx=JSON.parse(process.env.CTX||"null")||{};
        const topic=JSON.parse(process.env.TOPIC||"null")||{};
        const mid="out_"+Date.now()+"_"+Math.random().toString(36).slice(2,8);
        process.stdout.write(JSON.stringify({
          message_id:mid, status:"approved_pending_delivery",
          intent:process.env.LOBSTER_ARG_INTENT||"",
          channel:route.channel, routing_reason:route.reason||"",
          requires_confirmation:route.requires_confirmation!==false,
          contact:{id:contact.id||contact.email||"", name:contact.name||"",
            email:contact.email||"", phone:contact.phone||"",
            slack_channel:contact.slack_channel||"", linkedin_id:contact.linkedin_id||"",
            relationship_tier:contact.relationship_tier||"known_contact",
            is_external:contact.is_external!==false},
          tone_guidance:route.tone_guidance||"",
          recent_topic:topic.topic||"",
          relationship_history:(ctx.recent_messages||[]),
          created_at:new Date().toISOString()}));'
    env:
      ROUTE: $apply-override.stdout
      CONTACT: $resolve-contact.json.contact
      CTX: $load-context.stdout
      TOPIC: $recent-topic.stdout

  # 16. HARD HUMAN GATE — external sends require the user to see + confirm (AGENTS.md).
  #     This is a real step-level approval, not a fictional await-approval tool call.
  #     The run halts with a resumeToken; downstream side effects gate on $confirm.approved.
  #     Auto-send-eligible internal Slack (router set requires_confirmation:false) skips it.
  - id: confirm
    condition: $apply-override.json.ready_count > 0 && $apply-override.json.requires_confirmation == true
    approval:
      required: true
      prompt: >
        Outbound to ${contact_name} via $apply-override.json.channel.
        Routing: $apply-override.json.reason.
        Intent: ${intent}.
        Approve to let Thread draft + send on this channel?

  # 17. Compute a single send-decision flag (approved OR auto-send-eligible). Avoids a
  #     compound boolean across two optional steps in every downstream condition.
  - id: send-decision
    condition: $apply-override.json.ready_count > 0
    command: >
      node -e 'const requires=JSON.parse(process.env.REQUIRES||"true");
        const approved=(process.env.APPROVED==="true");
        const go = requires ? approved : true;
        process.stdout.write(JSON.stringify({go, go_count:go?1:0,
          confirmed_by: requires ? "user_confirmed" : "auto_send"}));'
    env:
      REQUIRES: $apply-override.json.requires_confirmation
      APPROVED: $confirm.approved

  # 18. Enqueue the approved outbox item for the agent loop to draft + deliver.
  #     (Draft = adaptive LLM; delivery = Slack/WhatsApp/email/LinkedIn/SMS — none of
  #     which can run in-pipeline. gog Gmail-send IS a real CLI, but tone-drafting still
  #     needs the loop, so the whole approved item is handed off as one unit.)
  - id: outbox-load
    condition: $send-decision.json.go_count > 0
    pipeline: state.get thread/outbox

  - id: outbox-next
    condition: $send-decision.json.go_count > 0
    command: >
      node -e 'const q=JSON.parse(process.env.OUTBOX||"null")||[];
        const item=JSON.parse(process.env.ITEM||"{}");
        const dec=JSON.parse(process.env.DEC||"{}");
        item.confirmed_by=dec.confirmed_by;
        q.push(item);
        process.stdout.write(JSON.stringify(q));'
    env:
      OUTBOX: $outbox-load.stdout
      ITEM: $outbox-item.stdout
      DEC: $send-decision.stdout

  - id: outbox-set
    condition: $send-decision.json.go_count > 0
    pipeline: state.set thread/outbox
    stdin: $outbox-next.stdout

  # 19. Track the outbound for follow-up (Req 21) — append to the single-key tracker array.
  #     awaiting_response flips true now; the follow-up tracker reconciles it against inbound.
  - id: tracker-next
    condition: $send-decision.json.go_count > 0
    command: >
      node -e 'const t=JSON.parse(process.env.TRACKER||"null")||[];
        const item=JSON.parse(process.env.ITEM||"{}"); const c=item.contact||{};
        t.push({id:item.message_id, contact_id:c.id, contact_name:c.name,
          contact_email:c.email, channel:item.channel, subject:"",
          preview:item.intent, relationship_tier:c.relationship_tier,
          timestamp:item.created_at, sent_date:item.created_at, awaiting_response:true});
        process.stdout.write(JSON.stringify(t));'
    env:
      TRACKER: $outbound-tracker.stdout
      ITEM: $outbox-item.stdout

  - id: tracker-set
    condition: $send-decision.json.go_count > 0
    pipeline: state.set thread/outbound-tracker
    stdin: $tracker-next.stdout

  # 20. Append an outbound row to the per-contact relationship history (Req 19/20).
  #     History is one index object keyed by contact id; append + re-write the slice.
  - id: history-next
    condition: $send-decision.json.go_count > 0
    command: >
      node -e 'const all=JSON.parse(process.env.HIST||"null")||{};
        const item=JSON.parse(process.env.ITEM||"{}"); const c=item.contact||{};
        const cid=c.id||c.email||"";
        const map=Array.isArray(all)?{}:all;
        if(!Array.isArray(map[cid])) map[cid]=[];
        map[cid].push({timestamp:item.created_at, channel:item.channel,
          direction:"outbound", preview:item.intent, message_id:item.message_id});
        process.stdout.write(JSON.stringify(map));'
    env:
      HIST: $history.stdout
      ITEM: $outbox-item.stdout

  - id: history-set
    condition: $send-decision.json.go_count > 0
    pipeline: state.set thread/contact-history
    stdin: $history-next.stdout

  # 21. Audit log (Req 11) — append to the single-key audit array.
  - id: audit-load
    condition: $send-decision.json.go_count > 0
    pipeline: state.get thread/audit

  - id: audit-next
    condition: $send-decision.json.go_count > 0
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const item=JSON.parse(process.env.ITEM||"{}");
        const dec=JSON.parse(process.env.DEC||"{}"); const c=item.contact||{};
        a.push({action:"message_queued_for_send", contact:c.name,
          channel:item.channel, routing_reason:item.routing_reason,
          confirmed_by:dec.confirmed_by, message_id:item.message_id,
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(a));'
    env:
      AUDIT: $audit-load.stdout
      ITEM: $outbox-item.stdout
      DEC: $send-decision.stdout

  - id: audit-set
    condition: $send-decision.json.go_count > 0
    pipeline: state.set thread/audit
    stdin: $audit-next.stdout
