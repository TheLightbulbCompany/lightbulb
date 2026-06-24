name: meddic-stage-check
# Req 36/37: when a deal advances a pipeline stage, surface the MEDDIC fields the
# new stage requires but that are not yet confirmed from a prospect statement.
# Stage-change webhook -> load Attio MEDDIC state -> deterministic gap check ->
# queue an alert for the rep + append an audit record. Fully deterministic (0 LLM).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    The notification queue + audit trail are single-key index arrays (state.* has
#    no prefix-list), appended in a node step then re-written.
#  - Attio is the ONLY CRM, reached gateway-free via the baked `mcporter` CLI; its
#    output is YAML-ish TEXT, so a node step parses stdout (never JSON.parse).
#  - Slack is unreachable from an embedded pipeline (401), so the gap alert is
#    written to `pitch/notifications` for the agent to deliver to #pitch-pipeline.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  prospect_domain:
    required: true
  new_stage:
    required: true
  old_stage:
    default: ""

steps:
  # 1. Load this deal's MEDDIC state from Attio. mcporter -> text -> parse.
  #    Attio stores MEDDIC confirmation as a JSON blob on a `meddic` attribute; we
  #    parse the company record by domain and JSON.parse only the meddic blob (the
  #    surrounding mcporter output is YAML-ish text, never JSON.parse the whole thing).
  #    Absent/unparseable meddic -> {} so the gap check conservatively flags every
  #    required field as a gap (a never-extracted deal has confirmed nothing).
  - id: crm-meddic
    command: >
      node -e 'const {execSync}=require("child_process");
        const d=(process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"").toLowerCase();
        let txt=""; try { txt=execSync("mcporter call attio.search-records --args "
          +JSON.stringify(JSON.stringify({object:"companies",query:d,limit:5}))+" --output text",
          {encoding:"utf8",timeout:45000}); } catch (e) { txt=""; }
        let record_id="", name="", meddicRaw="";
        for (const p of txt.split(/-\s*record_id:\s*/).slice(1)) {
          const id=(p.match(/^([0-9a-f-]{36})/)||[])[1]||"";
          const doms=[...p.matchAll(/domains\[\d+\]:\s*([^\s"]+)/g)].map(m=>m[1].toLowerCase());
          const nm=((p.match(/\n\s*name:\s*"?([^"\n]+)"?/)||[])[1]||"").trim();
          const md=((p.match(/\n\s*meddic:\s*"?(.+?)"?\s*\n/)||[])[1]||"").trim();
          if (!record_id || doms.includes(d)) { record_id=id; name=nm; meddicRaw=md; }
          if (doms.includes(d)) break;
        }
        let meddic={}; if (meddicRaw) { try { meddic=JSON.parse(meddicRaw.replace(/\\"/g,String.fromCharCode(34)))||{}; } catch(e){ meddic={}; } }
        process.stdout.write(JSON.stringify({found:!!record_id, record_id, name, meddic}));'

  # 2. Assemble the gap-check input: {deal:{domain, stage, meddic}} on the NEW stage.
  - id: gap-input
    command: >
      node -e 'const crm=JSON.parse(process.env.CRM||"{}");
        process.stdout.write(JSON.stringify({deal:{
          domain: process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"",
          name: crm.name||process.env.LOBSTER_ARG_PROSPECT_DOMAIN||"",
          stage: process.env.LOBSTER_ARG_NEW_STAGE||"",
          meddic: crm.meddic||{}}}));'
    env:
      CRM: $crm-meddic.stdout

  # 3. Deterministic MEDDIC gap check (exits 0; carries has_gaps + gaps + summary).
  #    Reads {deal} from stdin, emits the single-deal result wrapped in results[].
  - id: gap-check
    command: node pitch-meddic-gap-check.js
    stdin: $gap-input.stdout

  # 4. Normalize the single-deal result off results.0 and emit an explicit gap count
  #    to gate on (NEVER gate on .length — it silently evaluates false).
  - id: decide
    command: >
      node -e 'const r=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const res=(r.results&&r.results.length)?r.results[0]:r;
        const gaps=(res&&res.gaps)||[];
        process.stdout.write(JSON.stringify({
          has_gaps: gaps.length>0, gap_count: gaps.length,
          summary: res.summary||"", gaps,
          stage: res.stage||"", confirmed_count: res.confirmed_count||0}));'
    stdin: $gap-check.stdout

  # 5. ALERT PATH: queue a stage-gap notification for the agent to deliver to
  #    #pitch-pipeline (Slack is unreachable from the embedded pipeline -> 401).
  - id: notify-load
    condition: $decide.json.gap_count > 0
    pipeline: state.get pitch/notifications

  - id: notify-next
    condition: $decide.json.gap_count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const dec=JSON.parse(process.env.DEC||"{}");
        n.push({kind:"meddic_stage_gap", channel:"#pitch-pipeline",
          domain: process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
          old_stage: process.env.LOBSTER_ARG_OLD_STAGE||"",
          new_stage: process.env.LOBSTER_ARG_NEW_STAGE||"",
          summary: dec.summary||"",
          gaps: (dec.gaps||[]).map(g=>g.label||g.field),
          gap_detail: dec.gaps||[],
          created_at: new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      DEC: $decide.stdout

  - id: notify-save
    condition: $decide.json.gap_count > 0
    pipeline: state.set pitch/notifications
    stdin: $notify-next.stdout

  # 6. Audit log (Req 38) — append to the durable audit index array (always runs).
  - id: audit-load
    pipeline: state.get pitch/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const dec=JSON.parse(process.env.DEC||"{}");
        log.push({action:"meddic_stage_check",
          domain: process.env.LOBSTER_ARG_PROSPECT_DOMAIN,
          old_stage: process.env.LOBSTER_ARG_OLD_STAGE||"",
          new_stage: process.env.LOBSTER_ARG_NEW_STAGE||"",
          has_gaps: !!dec.has_gaps,
          gap_count: dec.gap_count||0,
          gaps: (dec.gaps||[]).map(g=>g.field||g.label),
          authorized_by: "autonomous",
          timestamp: new Date().toISOString()});
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-load.stdout
      DEC: $decide.stdout

  - id: audit-save
    pipeline: state.set pitch/audit-log
    stdin: $audit-next.stdout
