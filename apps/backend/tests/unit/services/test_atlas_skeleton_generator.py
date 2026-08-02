"""Exercises Atlas's skeleton-generator (the agent-scaffolding core) end-to-end.

The generator is JS that runs inside the container (which has node), so we drive
it via a node subprocess and skip when node isn't on PATH. Guards the structural
fix: ``draft`` returns the new agent's files as JSON, and ``write-disk`` must
materialize them into the staging dir before the atomic mv — previously nothing
populated the staging dir, so every build hit ``staging_dir_missing``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_GEN = (
    Path(__file__).resolve().parents[3] / "core" / "services" / "atlas_seed" / "files" / "atlas-skeleton-generator.js"
)

# Driver: draft a minimal-but-valid agent, write it to disk, verify, and prove
# the second write refuses (idempotent). Prints a JSON result line.
_DRIVER = r"""
const os=require("os"),fs=require("fs"),path=require("path");
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),"atlas-test-"));
process.env.ISOL8_AGENTS_DIR=tmp;
const g=require(process.env.GEN_PATH);
const spec={ build_id:"b1", intake:{name:"lex-legal", build_id:"b1", archetype:"general"},
  user_stories:[{req_id:"US1", action:"summarize contracts", outcome:"the team saves time"}],
  never_does:["leak client PII"], what_you_are:"You are Lex.",
  requirements_with_triggers:[],
  pipelines:[{name:"intake", purpose:"collect a contract to summarize"}],
  scripts:[{name:"helper", purpose:"normalize a contract payload", archetype:"transform"}] };
const draft=g.draftToFastIo(spec);
const w=draft.pass ? g.writeDisk("b1","lex-legal",draft.artifacts_to_stage) : {};
const v=draft.pass ? g.verifyWrite("b1","lex-legal") : {};
const w2=draft.pass ? g.writeDisk("b1","lex-legal",draft.artifacts_to_stage) : {};
const ws=path.join(tmp,"lex-legal","workspace");
// The generated helper.js must actually RUN — it previously defined a local
// `process` that shadowed Node's global, crashing main() at process.argv.
let scriptRuns=false, scriptShadows=true;
try {
  const helperSrc=fs.readFileSync(path.join(ws,"helper.js"),"utf8");
  scriptShadows=/function process\(/.test(helperSrc);
  const cp=require("child_process");
  const r=cp.spawnSync(process.execPath,[path.join(ws,"helper.js")],{input:"{}",encoding:"utf8"});
  scriptRuns = r.status===0 && /"pass":true/.test(r.stdout||"");
} catch(e){}
let entry={};
try { entry=(JSON.parse(fs.readFileSync(path.join(ws,"openclaw.json"),"utf8")).agents||{}).list[0]||{}; } catch(e){}
const out={
  draft_pass: !!draft.pass,
  write_success: !!w.success,
  write_files: w.file_count||0,
  verify_success: !!v.success,
  has_openclaw: fs.existsSync(path.join(ws,"openclaw.json")),
  has_agents_md: fs.existsSync(path.join(ws,"AGENTS.md")),
  has_manifest: fs.existsSync(path.join(tmp,"lex-legal","build-manifest.json")),
  second_write_refused: w2.error==="destination_exists",
  script_runs: scriptRuns,
  script_shadows_process: scriptShadows,
  entry_name: entry.name||"",
  entry_description: entry.description||"",
  entry_identity: entry.identity||null,
  entry_has_archetype: Object.prototype.hasOwnProperty.call(entry,"archetype"),
};
fs.rmSync(tmp,{recursive:true,force:true});
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_skeleton_generator_draft_write_verify():
    proc = subprocess.run(
        ["node", "-e", _DRIVER],
        capture_output=True,
        text=True,
        env={"GEN_PATH": str(_GEN), "PATH": __import__("os").environ.get("PATH", "")},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["draft_pass"] is True
    # The fix: write-disk materializes the drafted artifacts onto disk.
    assert out["write_success"] is True
    assert out["write_files"] >= 8
    assert out["verify_success"] is True
    assert out["has_openclaw"] is True
    assert out["has_agents_md"] is True
    assert out["has_manifest"] is True
    # Idempotency: a second write to the same dest refuses rather than clobbers.
    assert out["second_write_refused"] is True
    # The generated Node script must run — no `process` shadow crashing main().
    assert out["script_shadows_process"] is False
    assert out["script_runs"] is True
    # The entry carries its display identity (name + identity block) and a
    # description (vibe → what_you_are fallback) — the description feeds the
    # Paperclip mint's capabilities via the export manifest, and an entry
    # without ``name`` gets its display name clobbered by the deploy-time
    # file merge. ``archetype`` is a non-schema key that belongs only in
    # IDENTITY.md — on the entry the deploy sanitizer had to strip it.
    assert out["entry_name"] == "lex-legal"
    assert out["entry_description"] == "You are Lex."
    assert out["entry_identity"] == {"name": "lex-legal", "emoji": "🛠"}
    assert out["entry_has_archetype"] is False
