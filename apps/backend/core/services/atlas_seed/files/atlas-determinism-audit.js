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

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync } = require('child_process');

function parseArgs(argv) {
  const args = { buildId: null, stagingDir: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--staging-dir=')) args.stagingDir = a.split('=')[1];
  }
  return args;
}

function _fastIoRead(key) {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action read --args-json '{"key":"${key}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) { return null; }
}

function _fastIoList(prefix) {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action list --args-json '{"prefix":"${prefix}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) { return []; }
}

function _defaultIoAdapter(buildId) {
  return {
    readMatrix: () => _fastIoRead(`atlas-builds/${buildId}/requirement-matrix`),
    listDecisions: () => _fastIoList(`atlas-builds/${buildId}/decisions/`)
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
  const io = _ioAdapter || _defaultIoAdapter(buildId);
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

  const failures = [];

  for (const row of rows) {
    const reqId = row.req_id || row.id;
    const decision = decisionsByReqId[reqId] || {};
    if (decision.decision === 'drop') continue;

    const finalClassification = decision.classification_override || row.classification;
    if (finalClassification !== 'deterministic') continue;

    const triggers = row.triggers || [];
    if (triggers.length === 0) {
      failures.push({
        req_id: reqId,
        reason: 'no_triggers',
        message: `deterministic requirement ${reqId} has no triggers. Re-vet to bind a trigger OR reclassify to llm/hybrid.`
      });
      continue;
    }

    for (const t of triggers) {
      if (t.pipeline === null || t.pipeline === undefined) continue;
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
  process.stdout.write(JSON.stringify(result));
  if (!result.pass) process.exit(1);
}

if (require.main === module) main();

module.exports = { audit };
