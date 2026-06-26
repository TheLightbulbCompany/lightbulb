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
  requirements_with_triggers:[], pipelines:[{name:"intake"}], scripts:[{name:"helper"}] };
const draft=g.draftToFastIo(spec);
const w=draft.pass ? g.writeDisk("b1","lex-legal",draft.artifacts_to_stage) : {};
const v=draft.pass ? g.verifyWrite("b1","lex-legal") : {};
const w2=draft.pass ? g.writeDisk("b1","lex-legal",draft.artifacts_to_stage) : {};
const ws=path.join(tmp,"lex-legal","workspace");
const out={
  draft_pass: !!draft.pass,
  write_success: !!w.success,
  write_files: w.file_count||0,
  verify_success: !!v.success,
  has_openclaw: fs.existsSync(path.join(ws,"openclaw.json")),
  has_agents_md: fs.existsSync(path.join(ws,"AGENTS.md")),
  has_manifest: fs.existsSync(path.join(tmp,"lex-legal","build-manifest.json")),
  second_write_refused: w2.error==="destination_exists",
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
