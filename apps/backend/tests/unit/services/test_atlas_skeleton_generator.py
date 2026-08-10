"""Exercises Atlas's skeleton-generator (the agent-scaffolding core) end-to-end.

The generator is JS that runs inside the container (which has node), so we drive
it via a node subprocess and skip when node isn't on PATH. Guards the structural
fix: ``draft`` returns the new agent's files as JSON, and ``write-disk`` must
materialize them into the staging dir before the atomic mv — previously nothing
populated the staging dir, so every build hit ``staging_dir_missing``.
"""

import json
import os
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
def test_skeleton_generator_draft_write_verify(tmp_path):
    # Hermetic PATH (node only): whatever `openclaw` a dev machine happens to
    # have on PATH must not leak into verify-write's schema check — a local
    # bin's schema can differ from the deployed tag's. The schema gate itself
    # is covered by the stub-bin tests below.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").symlink_to(shutil.which("node"))
    proc = subprocess.run(
        ["node", "-e", _DRIVER],
        capture_output=True,
        text=True,
        env={"GEN_PATH": str(_GEN), "PATH": str(bin_dir)},
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


# Driver for the verify-write schema gate: draft + write a minimal agent,
# optionally poison the staged openclaw.json with a non-schema agent-entry key
# (the exact class of key that bricked a prod container), then verify-write.
_SCHEMA_DRIVER = r"""
const os=require("os"),fs=require("fs"),path=require("path");
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),"atlas-schema-test-"));
process.env.ISOL8_AGENTS_DIR=tmp;
const g=require(process.env.GEN_PATH);
const spec={ build_id:"b1", intake:{name:"lex-legal", build_id:"b1"},
  user_stories:[{req_id:"US1", action:"summarize contracts", outcome:"the team saves time"}],
  never_does:["leak client PII"], what_you_are:"You are Lex.",
  requirements_with_triggers:[] };
const draft=g.draftToFastIo(spec);
g.writeDisk("b1","lex-legal",draft.artifacts_to_stage);
if (process.env.POISON==="1") {
  const cfgPath=path.join(tmp,"lex-legal","workspace","openclaw.json");
  const cfg=JSON.parse(fs.readFileSync(cfgPath,"utf8"));
  cfg.agents.list[0].archetype="general";
  fs.writeFileSync(cfgPath,JSON.stringify(cfg,null,2));
}
const v=g.verifyWrite("b1","lex-legal");
fs.rmSync(tmp,{recursive:true,force:true});
process.stdout.write(JSON.stringify({
  success:!!v.success,
  error:v.error||"",
  schema_issues:v.schema_issues===undefined?null:v.schema_issues,
  skipped:v.schema_check_skipped||""
}));
"""

# Stub `openclaw` bin: emits zod-style validate JSON, exit 1 iff the config the
# generator pointed OPENCLAW_CONFIG_PATH at carries the poison key — so it also
# proves the validator runs against the staged snapshot, not some other file.
_STUB_OPENCLAW = """#!/bin/sh
if grep -q '"archetype"' "$OPENCLAW_CONFIG_PATH" 2>/dev/null; then
  echo '{"valid":false,"issues":[{"path":"agents.list.0.archetype","message":"Unrecognized key(s) in object: archetype"}]}'
  exit 1
fi
echo '{"valid":true,"issues":[]}'
"""


def _run_schema_driver(env):
    proc = subprocess.run(
        ["node", "-e", _SCHEMA_DRIVER],
        capture_output=True,
        text=True,
        env={"GEN_PATH": str(_GEN), **env},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_verify_write_blocks_on_schema_violation(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "openclaw"
    stub.write_text(_STUB_OPENCLAW)
    stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    # Non-schema agent-entry key → verify fails with the exact issue list, so
    # the build.lobster register gate ($verify-write.json.success) blocks.
    out = _run_schema_driver({**env, "POISON": "1"})
    assert out["success"] is False
    assert out["error"] == "schema_validation_failed"
    assert out["schema_issues"]
    assert out["schema_issues"][0]["path"] == "agents.list.0.archetype"

    # Same validator present, clean config → verify passes with no issues.
    out = _run_schema_driver(env)
    assert out["success"] is True
    assert out["schema_issues"] == []
    assert out["skipped"] == ""


def _verify_fail_next_snippet():
    """Extract the real verify-fail-next node -e payload from build.lobster.

    The inline JS contains no single quotes, so the first ``'`` after
    ``node -e '`` is the closing quote of the -e argument.
    """
    text = (_GEN.parent / "build.lobster").read_text(encoding="utf-8")
    step = text.split("- id: verify-fail-next", 1)[1]
    return step.split("node -e '", 1)[1].split("'", 1)[0]


def _run_verify_fail_next(verify_result):
    proc = subprocess.run(
        ["node", "-e", _verify_fail_next_snippet()],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ["PATH"],
            "NOTIFS": "[]",
            "VERIFY": json.dumps(verify_result),
            "LOBSTER_ARG_BUILD_ID": "b1",
            "LOBSTER_ARG_AGENT_NAME": "lex-legal",
        },
    )
    assert proc.returncode == 0, proc.stderr
    (notif,) = json.loads(proc.stdout)
    return notif


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_verify_fail_notification_carries_schema_issues():
    # The queued notification must carry the error kind + the exact zod issue
    # list — Atlas's loop drains atlas/notifications, so a generic "retry"
    # summary with the issues dropped means a blind retry reproduces the
    # same schema violation. Capped at 5 issues with a "+N more" tail.
    notif = _run_verify_fail_next(
        {
            "success": False,
            "error": "schema_validation_failed",
            "schema_issues": [{"path": f"agents.list.0.k{i}", "message": f"Unrecognized key k{i}"} for i in range(7)],
        }
    )
    assert notif["kind"] == "build_verify_failed"
    assert notif["error"] == "schema_validation_failed"
    assert len(notif["schema_issues"]) == 5
    assert notif["schema_issues"][0]["path"] == "agents.list.0.k0"
    assert "agents.list.0.k0: Unrecognized key k0" in notif["summary"]
    assert "(+2 more)" in notif["summary"]

    # Non-schema verify failure keeps the original files/hash wording, now
    # with the error kind attached.
    notif = _run_verify_fail_next({"success": False, "error": "destination_dir_missing"})
    assert notif["error"] == "destination_dir_missing"
    assert notif["schema_issues"] == []
    assert "No files on disk or hash mismatch" in notif["summary"]
    assert "destination_dir_missing" in notif["summary"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_verify_write_fails_open_without_openclaw_bin(tmp_path):
    # PATH holds ONLY node — no openclaw bin (local dev / CI). Fail-open: the
    # server-side register sanitizer is the backstop there.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").symlink_to(shutil.which("node"))
    out = _run_schema_driver({"PATH": str(bin_dir)})
    assert out["success"] is True
    assert out["schema_issues"] == []
    assert out["skipped"] == "openclaw bin not found"
