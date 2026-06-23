name: deposit-lead
# Deposit a qualified, enriched lead into Attio + the Scout->Pitch lead-queue.
# Fully deterministic (0 LLM). The Scout agent runs this after enrich-and-score.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    The Scout queue + Pitch sequences are single-key index arrays (state.* has no
#    prefix-list), appended in a node step then re-written.
#  - Attio is the CRM, reached gateway-free via the baked `mcporter` CLI; its output
#    is YAML-ish TEXT, so a node step parses stdout (never JSON.parse).
#  - Notifications can't post to Slack from an embedded pipeline (401), so the lead
#    summary is written to a `scout/notifications` queue for the agent to deliver.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  lead_domain:
    required: true
  dossier:
    required: true        # full lead dossier (JSON object)
  scoring:
    required: true        # { score, tier }

steps:
  # 1. Threshold gate — archive-tier leads are not deposited.
  - id: gate
    command: >
      node -e 'const s=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({pass: s.tier!=="archive", score: s.score||0, tier: s.tier}));'
    stdin: $scoring

  # 2. Search Attio for this domain (CRM dedup source). mcporter -> text -> parse.
  - id: crm-search
    condition: $gate.json.pass
    command: >
      node -e 'const {execSync}=require("child_process");
        const d=process.env.LOBSTER_ARG_LEAD_DOMAIN||"";
        let txt=""; try { txt=execSync("mcporter call attio.search-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",query:d,limit:5}))+" --output text",
          {encoding:"utf8",timeout:45000}); } catch (e) { txt=""; }
        const records=[];
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const id=(p.match(/^([0-9a-f-]{36})/)||[])[1]||"";
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const name=((p.match(/\n\s*name:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          records.push({record_id:id, domain:doms[0]||"", domains:doms, name, status:"contact"});
        }
        process.stdout.write(JSON.stringify({records, count:records.length}));'

  # 3. Scout's own queue (single-key index array) — already-deposited dedup.
  - id: queue
    condition: $gate.json.pass
    pipeline: state.get scout/lead-queue

  # 4. Pitch's active sequences (single-key index array) — in-flight dedup.
  #    Reads Pitch's producer key (Scout + Pitch share one per-owner state store).
  - id: sequences
    condition: $gate.json.pass
    pipeline: state.get pitch/sequences

  # 5. Assemble the dedup input ({lead, crm_records, scout_queue, pitch_sequences}).
  - id: dedup-input
    condition: $gate.json.pass
    command: >
      node -e 'const d=JSON.parse(process.env.DOSSIER||"{}");
        const crm=(JSON.parse(process.env.CRM||"{}").records)||[];
        const q=JSON.parse(process.env.QUEUE||"null")||[];
        const seq=JSON.parse(process.env.SEQ||"null")||[];
        const c=d.contact||{}, co=d.company||{}, sig=d.trigger_signal||{};
        const lead={email:c.email||"", domain:(process.env.LOBSTER_ARG_LEAD_DOMAIN||co.domain||"").toLowerCase(),
          linkedin_url:c.linkedin_url||"", signal_type:sig.type||""};
        process.stdout.write(JSON.stringify({lead, crm_records:crm, scout_queue:q, pitch_sequences:seq}));'
    env:
      DOSSIER: $dossier
      CRM: $crm-search.stdout
      QUEUE: $queue.stdout
      SEQ: $sequences.stdout

  # 6. Deterministic dedup (exits 0; carries action + hard_block).
  - id: dedup
    condition: $gate.json.pass
    command: node scout-dedup-check.js
    stdin: $dedup-input.stdout

  # 7. Decide: deposit only when dedup says "proceed".
  - id: decide
    condition: $gate.json.pass
    command: >
      node -e 'const r=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({deposit: r.action==="proceed", action:r.action, duplicates:r.duplicates||[]}));'
    stdin: $dedup.stdout

  # 8. Today's deposit count (for the volume limit).
  - id: today
    condition: $decide.json.deposit
    pipeline: state.get scout/today-deposits

  # 9. Volume limit (50/day). Wrap the single lead as leads[] for the limiter.
  - id: volume-input
    condition: $decide.json.deposit
    command: >
      node -e 'const today=(JSON.parse(process.env.TODAY||"null")||{}).count||0;
        const s=JSON.parse(process.env.SCORING||"{}");
        process.stdout.write(JSON.stringify({leads:[{score:s.score||0, domain:process.env.LOBSTER_ARG_LEAD_DOMAIN}], today_deposits:today, daily_limit:50}));'
    env:
      TODAY: $today.stdout
      SCORING: $scoring

  - id: volume
    condition: $decide.json.deposit
    command: node scout-volume-limiter.js
    stdin: $volume-input.stdout

  # 10. Deposit to Attio: upsert the company by domain, attach the signal as a note.
  - id: deposit-attio
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const {execSync}=require("child_process");
        const d=JSON.parse(process.env.DOSSIER||"{}"); const s=JSON.parse(process.env.SCORING||"{}");
        const domain=process.env.LOBSTER_ARG_LEAD_DOMAIN; const co=d.company||{}, sig=d.trigger_signal||{};
        const up={object:"companies", matching_attribute:"domains",
          values:{domains:[domain], name:co.name||domain}};
        let rec=""; try { rec=execSync("mcporter call attio.upsert-record --args "
          +JSON.stringify(JSON.stringify(up))+" --output text",{encoding:"utf8",timeout:45000}); } catch(e){ rec="ERR:"+e.message; }
        const rid=(rec.match(/record_id:\s*([0-9a-f-]{36})/)||[])[1]||"";
        let note="skipped";
        if (rid) { const n={parent_object:"companies", parent_record_id:rid,
            title:"Scout signal: "+(sig.type||"lead"),
            content:"ICP score "+(s.score||0)+" ("+(s.tier||"")+"). "+(sig.summary||"")};
          try { execSync("mcporter call attio.create-note --args "+JSON.stringify(JSON.stringify(n))+" --output text",{encoding:"utf8",timeout:45000}); note="created"; } catch(e){ note="ERR:"+e.message; } }
        process.stdout.write(JSON.stringify({deposited:!!rid, record_id:rid, note}));'
    env:
      DOSSIER: $dossier
      SCORING: $scoring

  # 11. Enqueue the lead for Pitch (append to the single-key queue array).
  - id: queue-next
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const d=JSON.parse(process.env.DOSSIER||"{}"); const s=JSON.parse(process.env.SCORING||"{}");
        q.push({domain:process.env.LOBSTER_ARG_LEAD_DOMAIN, score:s.score||0, tier:s.tier,
          dossier:d, queued_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: $queue.stdout
      DOSSIER: $dossier
      SCORING: $scoring

  - id: enqueue
    condition: $volume.json.deposited_count > 0
    pipeline: state.set scout/lead-queue
    stdin: $queue-next.stdout

  # 12. Update the daily deposit counter.
  - id: counter-next
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8"));
        process.stdout.write(JSON.stringify({count:v.today_total||0, date:new Date().toISOString().slice(0,10)}));'
    stdin: $volume.stdout

  - id: counter
    condition: $volume.json.deposited_count > 0
    pipeline: state.set scout/today-deposits
    stdin: $counter-next.stdout

  # 13. Buffer overflow leads (when the daily limit is reached) for the next day.
  - id: buffer-load
    condition: $decide.json.deposit
    pipeline: state.get scout/buffer

  - id: buffer-next
    condition: $volume.json.at_limit
    command: >
      node -e 'const b=JSON.parse(process.env.BUFFER||"null")||[];
        const d=JSON.parse(process.env.DOSSIER||"{}"); const s=JSON.parse(process.env.SCORING||"{}");
        b.push({domain:process.env.LOBSTER_ARG_LEAD_DOMAIN, score:s.score||0, dossier:d, buffered_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(b));'
    env:
      BUFFER: $buffer-load.stdout
      DOSSIER: $dossier
      SCORING: $scoring

  - id: buffer
    condition: $volume.json.at_limit
    pipeline: state.set scout/buffer
    stdin: $buffer-next.stdout

  # 14. Queue a notification for the agent to deliver (Slack is unreachable here).
  - id: notify-load
    condition: $volume.json.deposited_count > 0
    pipeline: state.get scout/notifications

  - id: notify-next
    condition: $volume.json.deposited_count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const d=JSON.parse(process.env.DOSSIER||"{}"); const s=JSON.parse(process.env.SCORING||"{}");
        const sig=d.trigger_signal||{}, co=d.company||{}, c=d.contact||{};
        n.push({kind:"new_lead", urgent: s.score>=90 || !!d.urgency,
          domain:process.env.LOBSTER_ARG_LEAD_DOMAIN, score:s.score||0, tier:s.tier,
          summary:sig.summary||"", contact:(c.title||"")+" at "+(co.name||""), angle:d.outreach_angle||"",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      DOSSIER: $dossier
      SCORING: $scoring

  - id: notify
    condition: $volume.json.deposited_count > 0
    pipeline: state.set scout/notifications
    stdin: $notify-next.stdout
