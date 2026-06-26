#!/usr/bin/env node
// atlas-determinism-audit.js
//
// S9.5 hard gate. After S9 architecture sign-off, before Step 6 build.
// For every requirement whose final classification is `deterministic`:
//   - assert triggers.length > 0
//   - for every trigger with a non-null pipeline, assert the pipeline file
//     exists in the staging dir's workspace/
//
// Final classification = decision.classification_override (if present) else
// the matrix's original classification. Decisions with decision == 'drop'
// are excluded from the audit (a dropped req has no runtime obligation).
//
// No agentgate override. Matches Atlas's existing hard-rule posture
// (ClawHavoc denylist, file-write-gate).
//
// Gateway-free: this tool is a pure transform. The requirement matrix arrives via
// the MATRIX env var and the per-requirement decisions via the DECISIONS env var
// (the caller does `state.get atlas/requirement-matrix` + `state.get
// atlas/decisions` and passes map[build_id] for each). The staging-dir pipeline
// existence check uses the local filesystem (genuine fs, not gateway state).
// It NEVER halts the pipeline on failure — pass/fail is reported on stdout so the
// caller can surface the failure (mirrors how build.lobster surfaces verify
// failures). No gateway tool calls.

const fs = require('fs');
const os = require('os');
const path = require('path');

function parseArgs(argv) {
  const args = { buildId: null, stagingDir: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--staging-dir=')) args.stagingDir = a.split('=')[1];
  }
  return args;
}

function _readMatrixEnv() {
  try { return JSON.parse(process.env.MATRIX || 'null'); } catch (e) { return null; }
}

function _readDecisionsEnv() {
  try {
    const d = JSON.parse(process.env.DECISIONS || 'null') || {};
    // decisions is a map req_id -> row; the audit consumes a list.
    return Array.isArray(d) ? d : Object.values(d);
  } catch (e) { return []; }
}

function _defaultIoAdapter() {
  return {
    readMatrix: () => _readMatrixEnv(),
    listDecisions: () => _readDecisionsEnv()
  };
}

function _defaultStagingDir(buildId) {
  const agentsRoot = process.env.ISOL8_AGENTS_DIR || path.join(os.homedir(), '.openclaw', 'agents');
  return path.join(agentsRoot, `.atlas-staging-${buildId}`);
}

function audit({ buildId, stagingDir, _ioAdapter }) {
  if (!buildId) {
    return { pass: false, error: 'build_id_missing', failures: [] };
  }
  const io = _ioAdapter || _defaultIoAdapter();
  const staging = stagingDir || _defaultStagingDir(buildId);
  const workspaceDir = path.join(staging, 'workspace');

  const matrix = io.readMatrix();
  if (!matrix) {
    return { pass: false, error: 'matrix_not_found', build_id: buildId, failures: [] };
  }
  const rows = matrix.user_stories || matrix.classifications || [];
  const decisionList = io.listDecisions() || [];
  const decisionsByReqId = {};
  for (const d of decisionList) {
    const reqId = d.req_id || (d.key || '').split('/').pop();
    decisionsByReqId[reqId] = d;
  }
  // Classification + triggers live in requirements_with_triggers (architect-
  // proposal Step 3.6), the same field the walker reads. Index by req_id and
  // fall back to the row's own inline fields for legacy matrices that carried
  // classification/triggers directly on the requirement row.
  const triggersByReqId = {};
  for (const rt of (matrix.requirements_with_triggers || [])) {
    if (rt && rt.req_id) triggersByReqId[rt.req_id] = rt;
  }

  const failures = [];

  for (const row of rows) {
    const reqId = row.req_id || row.id;
    const decision = decisionsByReqId[reqId] || {};
    if (decision.decision === 'drop') continue;

    const triggerRow = triggersByReqId[reqId] || {};
    const baseClassification = triggerRow.classification != null ? triggerRow.classification : row.classification;
    const finalClassification = decision.classification_override || baseClassification;
    if (finalClassification !== 'deterministic') continue;

    const triggers = (triggerRow.triggers != null ? triggerRow.triggers : row.triggers) || [];
    if (triggers.length === 0) {
      failures.push({
        req_id: reqId,
        reason: 'no_triggers',
        message: `deterministic requirement ${reqId} has no triggers. Re-vet to bind a trigger OR reclassify to llm/hybrid.`
      });
      continue;
    }

    for (const t of triggers) {
      // !t.pipeline catches null/undefined AND "" — an empty path would
      // path.join to workspaceDir itself (which exists), silently passing the
      // existsSync check below even though no pipeline file was actually bound.
      if (!t.pipeline) continue;
      const pipelinePath = path.join(workspaceDir, t.pipeline);
      if (!fs.existsSync(pipelinePath)) {
        failures.push({
          req_id: reqId,
          reason: 'pipeline_missing',
          pipeline: t.pipeline,
          kind: t.kind,
          message: `trigger references pipeline ${t.pipeline} but it is not in the staging workspace.`
        });
      }
    }
  }

  return {
    pass: failures.length === 0,
    build_id: buildId,
    staging_dir: staging,
    audited_count: rows.length,
    failures
  };
}

function main() {
  const args = parseArgs(process.argv);
  const result = audit({ buildId: args.buildId, stagingDir: args.stagingDir });
  // Report pass/fail on stdout — NEVER process.exit(1). A non-zero exit would
  // halt the pipeline before the caller's failure-notice step (conditioned on
  // pass == false) could run, hiding the very failure this gate must surface.
  // The hard-gate semantics are preserved by the caller refusing to advance the
  // build while `pass` is false (re-vet to bind triggers OR reclassify).
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main();

module.exports = { audit };
