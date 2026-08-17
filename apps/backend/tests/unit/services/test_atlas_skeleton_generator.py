"""Exercises Atlas's skeleton-generator (the agent-scaffolding core) end-to-end.

The generator is JS that runs inside the container (which has node), so we drive
it via a node subprocess and skip when node isn't on PATH. Guards the structural
fix: ``draft`` returns the new agent's files as JSON, and ``write-disk`` must
materialize them into the staging dir before the atomic mv — previously nothing
populated the staging dir, so every build hit ``staging_dir_missing``.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

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
const w=draft.pass ? g.writeDisk("b1",draft.artifacts_to_stage) : {};
const v=draft.pass ? g.verifyWrite("b1") : {};
const w2=draft.pass ? g.writeDisk("b1",draft.artifacts_to_stage) : {};
const ws=path.join(tmp,"workspace-atlas-staging-b1","workspace");
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
let entry={}, defaults={};
try {
  const cfg=JSON.parse(fs.readFileSync(path.join(ws,"openclaw.json"),"utf8"));
  entry=(cfg.agents||{}).list[0]||{};
  defaults=(cfg.agents||{}).defaults||{};
} catch(e){}
let manifest={};
try { manifest=JSON.parse(fs.readFileSync(path.join(tmp,"workspace-atlas-staging-b1","build-manifest.json"),"utf8")); } catch(e){}
const out={
  draft_pass: !!draft.pass,
  write_success: !!w.success,
  write_files: w.file_count||0,
  verify_success: !!v.success,
  staging_id: g.stagingIdFor("b1"),
  write_staging_id: w.staging_agent_id||"",
  verify_staging_id: v.staging_agent_id||"",
  manifest_staging_id: manifest.staging_agent_id||"",
  manifest_agent_name: manifest.agent_name||"",
  entry_id: entry.id||"",
  entry_has_workspace: Object.prototype.hasOwnProperty.call(entry,"workspace"),
  defaults_have_workspace: Object.prototype.hasOwnProperty.call(defaults,"workspace"),
  artifact_paths: Object.keys(draft.artifacts_to_stage||{}),
  destination_dir: w.destination_dir||"",
  has_openclaw: fs.existsSync(path.join(ws,"openclaw.json")),
  has_agents_md: fs.existsSync(path.join(ws,"AGENTS.md")),
  has_manifest: fs.existsSync(path.join(tmp,"workspace-atlas-staging-b1","build-manifest.json")),
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

    # THE FIX: the bundle carries no agent-chosen id. The entry's ``id`` is the
    # build's staging token (the schema requires the field), the staging dir is
    # keyed by the build, and the display name appears nowhere as an identifier.
    assert out["staging_id"] == "atlas-staging-b1"
    assert out["entry_id"] == "atlas-staging-b1"
    assert out["write_staging_id"] == "atlas-staging-b1"
    assert out["verify_staging_id"] == "atlas-staging-b1"
    assert out["manifest_staging_id"] == "atlas-staging-b1"
    # The NAME still travels — as a name, on the manifest and the entry.
    assert out["manifest_agent_name"] == "lex-legal"
    # No name-derived paths anywhere: not the destination, not the workspace
    # pointers (the platform sets those to .openclaw/workspace-{assigned id}),
    # not the staged artifact paths.
    assert out["destination_dir"].endswith("/workspace-atlas-staging-b1")
    assert "lex-legal" not in out["destination_dir"]
    assert out["entry_has_workspace"] is False
    assert out["defaults_have_workspace"] is False
    assert not [p for p in out["artifact_paths"] if "lex-legal" in p], out["artifact_paths"]


_TOKEN_DRIVER = r"""
process.env.ISOL8_AGENTS_DIR="/home/node/.openclaw";
const g=require(process.env.GEN_PATH);
const r=require(process.env.REG_PATH);
process.stdout.write(JSON.stringify({
  plain: g.stagingIdFor("b1"),
  long: g.stagingIdFor("x".repeat(300)),
  traversal: g.stagingIdFor("../../etc/passwd"),
  empty: g.stagingIdFor(""),
  dir: g.stagingWorkspaceDir("b1"),
  // The registrar must resolve the staging workspace through the generator's
  // export, not a second copy of the derivation.
  registrar_shares_derivation: r._stagingWorkspaceDir === g.stagingWorkspaceDir
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_staging_token_is_always_a_legal_staging_agent_id(tmp_path):
    # Build ids come from the agent loop and are free-form. The token derived
    # from one still has to satisfy the bridge: agent_export._AGENT_ID_RE and
    # RegisterBuiltAgentRequest.staging_agent_id (3-64 chars, [A-Za-z0-9._-],
    # alphanumeric first char). An over-long or path-y build id must not turn a
    # good build into a 422 — or a traversal.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").symlink_to(shutil.which("node"))
    proc = subprocess.run(
        ["node", "-e", _TOKEN_DRIVER],
        capture_output=True,
        text=True,
        env={
            "GEN_PATH": str(_GEN),
            "REG_PATH": str(_GEN.parent / "atlas-trigger-registrar.js"),
            "PATH": str(bin_dir),
        },
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    from core.services.agent_export import _AGENT_ID_RE

    assert out["plain"] == "atlas-staging-b1"
    assert out["empty"] == "atlas-staging-unknown"
    assert "/" not in out["traversal"] and ".." not in out["traversal"]
    assert len(out["long"]) == 64
    for key in ("plain", "long", "traversal", "empty"):
        assert _AGENT_ID_RE.match(out[key]), (key, out[key])
    assert out["dir"] == "/home/node/.openclaw/workspace-atlas-staging-b1"
    assert out["registrar_shares_derivation"] is True


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
g.writeDisk("b1",draft.artifacts_to_stage);
if (process.env.POISON==="1") {
  const cfgPath=path.join(tmp,"workspace-atlas-staging-b1","workspace","openclaw.json");
  const cfg=JSON.parse(fs.readFileSync(cfgPath,"utf8"));
  cfg.agents.list[0].archetype="general";
  fs.writeFileSync(cfgPath,JSON.stringify(cfg,null,2));
}
const v=g.verifyWrite("b1");
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


_BUILD_LOBSTER = _GEN.parent / "build.lobster"

# The EFS user root as the container mounts it (container-stack.ts mounts the
# per-user EFS access point at /home/node/.openclaw), which is the same root the
# backend's Workspace.user_path resolves `workspace-{id}` under.
_EFS_USER_ROOT = "/home/node/.openclaw"


def test_build_lobster_stages_under_the_efs_user_root():
    """The pipeline and the bridge must agree on WHERE the build is staged.

    They used to disagree: the pipeline wrote `~/.openclaw/workspaces/{name}/`
    while the bridge read `{user root}/workspace-{staging_agent_id}` — so every
    register 404'd, which is what pushed Atlas into installing the agent itself.
    `~/.openclaw/workspaces` is also the exact path `openclaw doctor --fix`
    recursively deletes on every boot, so the staging root must not go back there.
    """
    text = _BUILD_LOBSTER.read_text(encoding="utf-8")
    roots = re.findall(r"ISOL8_AGENTS_DIR:\s*(\S+)", text)
    assert roots, "build.lobster must pin the staging root explicitly"
    assert set(roots) == {_EFS_USER_ROOT}, roots
    # The bridge call takes the staging TOKEN verify-write reports, not a name.
    assert "STAGING_ID: $verify-write.json.staging_agent_id" in text
    # No tool is keyed by the display name any more (the name still travels as
    # a NAME on the notifications — that is what Atlas legitimately owns).
    assert "--agent-name=" not in text, "no step may key the build by the agent's display name"
    # Cron rows are keyed to the id the BRIDGE assigned.
    assert '--agent-id="$register-built-agent.json.agent_id"' in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_generated_staging_dir_is_what_the_bridge_reads(tmp_path):
    """End-to-end on the path contract: run the real generator against a fake
    EFS user root, then have the real backend export read that tree back."""
    from core.containers import workspace as workspace_module
    from core.services import agent_export

    owner_id = "org_owner_e2e"
    user_root = tmp_path / "users" / owner_id
    user_root.mkdir(parents=True)

    driver = (
        "const g=require(process.env.GEN_PATH);"
        'const spec={build_id:"b7",intake:{name:"Lex Legal",build_id:"b7",emoji:"\\u2696"},'
        'user_stories:[{req_id:"US1",action:"summarize contracts",outcome:"the team saves time"}],'
        'never_does:["leak client PII"],what_you_are:"You are Lex.",requirements_with_triggers:[]};'
        "const d=g.draftToFastIo(spec);"
        'process.stdout.write(JSON.stringify(g.writeDisk("b7",d.artifacts_to_stage)));'
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").symlink_to(shutil.which("node"))
    proc = subprocess.run(
        ["node", "-e", driver],
        capture_output=True,
        text=True,
        env={"GEN_PATH": str(_GEN), "PATH": str(bin_dir), "ISOL8_AGENTS_DIR": str(user_root)},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    written = json.loads(proc.stdout)
    assert written["success"] is True, written
    staging_agent_id = written["staging_agent_id"]

    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    with patch.object(agent_export, "get_workspace", return_value=fake_ws):
        pkg = agent_export.export_built_agent_from_staging(owner_id=owner_id, staging_agent_id=staging_agent_id)

    # The bridge found the dir the generator wrote, and assigns the identity:
    # the manifest keys off the staging token, the slice carries no id, and the
    # display name is the only thing the build contributed.
    assert pkg.manifest["agent_id"] == staging_agent_id == "atlas-staging-b7"
    assert pkg.manifest["name"] == "Lex Legal"
    assert "id" not in pkg.openclaw_slice["agent"]
    assert "./openclaw.json" in [m.name for m in _tar_members(pkg.tarball_bytes)]


def _tar_members(tar_bytes: bytes):
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        return tf.getmembers()


def _verify_fail_next_snippet():
    """Extract the real verify-fail-next node -e payload from build.lobster.

    The inline JS contains no single quotes, so the first ``'`` after
    ``node -e '`` is the closing quote of the -e argument.
    """
    text = (_GEN.parent / "build.lobster").read_text(encoding="utf-8")
    step = text.split("- id: verify-fail-next", 1)[1]
    return step.split("node -e '", 1)[1].split("'", 1)[0]


def _run_verify_fail_next(verify_result, write_result=None):
    proc = subprocess.run(
        ["node", "-e", _verify_fail_next_snippet()],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ["PATH"],
            "NOTIFS": "[]",
            "VERIFY": json.dumps(verify_result),
            "WRITE": json.dumps(write_result or {}),
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

    # write-disk schema-gates the promote, so a refusal reaches verify as
    # destination_dir_missing — the notification must pull the issue list
    # (and the schema error kind) from file-generate's stdout instead.
    notif = _run_verify_fail_next(
        {"success": False, "error": "destination_dir_missing"},
        {
            "success": False,
            "error": "schema_validation_failed",
            "schema_issues": [{"path": "agents.list.0.archetype", "message": "Unrecognized key"}],
        },
    )
    assert notif["error"] == "schema_validation_failed"
    assert notif["schema_issues"][0]["path"] == "agents.list.0.archetype"
    assert "agents.list.0.archetype: Unrecognized key" in notif["summary"]


# Driver for the write-disk promote gate: poison the DRAFTED artifacts (before
# any write), prove write-disk refuses the rename — destination never created,
# staging cleaned — then prove a corrected retry promotes cleanly (no
# destination_exists wedge from the rejected snapshot).
_PROMOTE_GATE_DRIVER = r"""
const os=require("os"),fs=require("fs"),path=require("path");
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),"atlas-gate-test-"));
process.env.ISOL8_AGENTS_DIR=tmp;
const g=require(process.env.GEN_PATH);
const spec={ build_id:"b1", intake:{name:"lex-legal", build_id:"b1"},
  user_stories:[{req_id:"US1", action:"summarize contracts", outcome:"the team saves time"}],
  never_does:["leak client PII"], what_you_are:"You are Lex.",
  requirements_with_triggers:[] };
const draft=g.draftToFastIo(spec);
const bad={...draft.artifacts_to_stage};
const cfg=JSON.parse(bad["workspace/openclaw.json"]);
cfg.agents.list[0].archetype="general";
bad["workspace/openclaw.json"]=JSON.stringify(cfg,null,2);
const w1=g.writeDisk("b1",bad);
const destAfterRefusal=fs.existsSync(path.join(tmp,"workspace-atlas-staging-b1"));
const stagingAfterRefusal=fs.existsSync(path.join(tmp,".atlas-staging-b1"));
const w2=g.writeDisk("b1",draft.artifacts_to_stage);
const v=g.verifyWrite("b1");
fs.rmSync(tmp,{recursive:true,force:true});
process.stdout.write(JSON.stringify({
  first_error:w1.error||"",
  first_issues:w1.schema_issues===undefined?null:w1.schema_issues,
  dest_created_on_refusal:destAfterRefusal,
  staging_left_on_refusal:stagingAfterRefusal,
  retry_success:!!w2.success,
  verify_success:!!v.success
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_write_disk_refuses_promote_on_schema_violation(tmp_path):
    # Validation must gate the COMMIT: catching the violation only after the
    # atomic mv leaves the rejected snapshot occupying the destination, and
    # every corrected retry then dies on destination_exists.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "openclaw"
    stub.write_text(_STUB_OPENCLAW)
    stub.chmod(0o755)
    proc = subprocess.run(
        ["node", "-e", _PROMOTE_GATE_DRIVER],
        capture_output=True,
        text=True,
        env={"GEN_PATH": str(_GEN), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["first_error"] == "schema_validation_failed"
    assert out["first_issues"]
    assert out["first_issues"][0]["path"] == "agents.list.0.archetype"
    # The rename never happened and staging was cleaned for the retry.
    assert out["dest_created_on_refusal"] is False
    assert out["staging_left_on_refusal"] is False
    # A corrected retry promotes cleanly — no destination_exists wedge.
    assert out["retry_success"] is True
    assert out["verify_success"] is True


# Driver for the promote-time IDENTITY gate: hand write-disk a bundle whose
# agents.list[0].id was swapped for a display-name slug (what an improvising
# Atlas would write), and prove the rename never happens.
_ID_GATE_DRIVER = r"""
const os=require("os"),fs=require("fs"),path=require("path");
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),"atlas-id-test-"));
process.env.ISOL8_AGENTS_DIR=tmp;
const g=require(process.env.GEN_PATH);
const spec={ build_id:"b1", intake:{name:"lex-legal", build_id:"b1"},
  user_stories:[{req_id:"US1", action:"summarize contracts", outcome:"the team saves time"}],
  never_does:["leak client PII"], what_you_are:"You are Lex.",
  requirements_with_triggers:[] };
const draft=g.draftToFastIo(spec);
const minted={...draft.artifacts_to_stage};
const cfg=JSON.parse(minted["workspace/openclaw.json"]);
cfg.agents.list[0].id="product-analytics-scorecard";
minted["workspace/openclaw.json"]=JSON.stringify(cfg,null,2);
const w1=g.writeDisk("b1",minted);
const out={
  error:w1.error||"",
  found_agent_id:w1.found_agent_id||"",
  expected:w1.staged_agent_id||"",
  slug_dir_created:fs.existsSync(path.join(tmp,"workspace-product-analytics-scorecard")),
  dest_created:fs.existsSync(path.join(tmp,"workspace-atlas-staging-b1")),
  staging_left:fs.existsSync(path.join(tmp,".atlas-staging-b1")),
  no_build_id:(g.writeDisk("",draft.artifacts_to_stage)||{}).error||"",
  no_build_id_verify:(g.verifyWrite("")||{}).error||"",
  hand_picked_message:w1.message||"",
  // A bundle drafted under a DIFFERENT build id also mismatches — but that is
  // an argument disagreement, not an agent picking an id, and must say so.
  wrong_build_message:(function(){
    const other={...draft.artifacts_to_stage};
    const c=JSON.parse(other["workspace/openclaw.json"]);
    c.agents.list[0].id=g.stagingIdFor("b2");
    other["workspace/openclaw.json"]=JSON.stringify(c,null,2);
    return (g.writeDisk("b1",other)||{}).message||"";
  })(),
};
fs.rmSync(tmp,{recursive:true,force:true});
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_write_disk_refuses_a_build_chosen_agent_id(tmp_path):
    # Atlas names agents; the platform assigns ids. A bundle whose entry id was
    # hand-picked never reaches disk — so there is no slug-named agent bundle to
    # improvise an install from when the bridge is unreachable, which is exactly
    # how ``product-analytics-scorecard`` went live.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").symlink_to(shutil.which("node"))
    proc = subprocess.run(
        ["node", "-e", _ID_GATE_DRIVER],
        capture_output=True,
        text=True,
        env={"GEN_PATH": str(_GEN), "PATH": str(bin_dir)},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["error"] == "staging_id_mismatch"
    assert out["found_agent_id"] == "product-analytics-scorecard"
    assert out["expected"] == "atlas-staging-b1"
    # Nothing was promoted under either name, and staging was cleaned so a
    # regenerated retry isn't wedged by destination_exists.
    assert out["slug_dir_created"] is False
    assert out["dest_created"] is False
    assert out["staging_left"] is False
    # And without a build id there is no staging identity at all — both actions
    # refuse rather than fall back to a name (verify-write reporting
    # destination_dir_missing instead would send the build-failure notification
    # chasing missing files that were never supposed to exist).
    assert out["no_build_id"] == "build_id_missing"
    assert out["no_build_id_verify"] == "build_id_missing"

    # The two ways to land on staging_id_mismatch need different advice: an
    # agent-chosen id vs. a draft/promote build-id disagreement. Telling the
    # second one "you don't choose ids" sends it hunting for an id it never set.
    assert "does not choose agent ids" in out["hand_picked_message"]
    assert "different build ids" in out["wrong_build_message"]
    assert "does not choose agent ids" not in out["wrong_build_message"]


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
