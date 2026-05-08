#!/usr/bin/env node
// atlas-recap-diff.js
//
// Phase 5: reads original requirement matrix (snapshotted at end of Phase 3, immutable
// via SHA256 hash verification per W5) and final decisions/* set; emits original→final
// diff table. Aborts recap with clear error on hash mismatch.

const crypto = require('crypto');
const { execSync } = require('child_process');

function parseArgs(argv) {
  const args = { action: 'diff', buildId: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
  }
  return args;
}

function fastIoRead(key) {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action read --args-json '{"key":"${key}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) { return null; }
}

function fastIoList(prefix) {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action list --args-json '{"prefix":"${prefix}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) { return []; }
}

function sha256(s) {
  return crypto.createHash('sha256').update(s).digest('hex');
}

function verifyHash(buildId) {
  const matrix = fastIoRead(`atlas-builds/${buildId}/requirement-matrix`);
  const expectedSha = fastIoRead(`atlas-builds/${buildId}/requirement-matrix.sha256`);
  if (!matrix || !expectedSha) {
    return { hash_matches: false, error: 'matrix or sha256 not found in fast-io' };
  }
  // Re-serialize deterministically
  const computedSha = sha256(JSON.stringify(matrix, Object.keys(matrix).sort()));
  // expectedSha may be stored as raw string or wrapped in an object
  const expectedHex = typeof expectedSha === 'string' ? expectedSha : (expectedSha.value || expectedSha.sha256 || '');
  return {
    hash_matches: computedSha === expectedHex,
    computed: computedSha,
    expected: expectedHex,
    error: computedSha === expectedHex ? null : 'requirement-matrix has been modified since snapshot. Recap aborted per W5 hash-verification pattern. The original matrix is no longer trustable; re-vet may be required.'
  };
}

function diff(buildId) {
  // Verify hash first per W5
  const hashCheck = verifyHash(buildId);
  if (!hashCheck.hash_matches) {
    return { error: hashCheck.error, hash_check: hashCheck };
  }

  const original = fastIoRead(`atlas-builds/${buildId}/requirement-matrix`);
  const decisions = fastIoList(`atlas-builds/${buildId}/decisions/`);

  const requirements = original.user_stories || original.classifications || [];
  const decisionMap = {};
  for (const d of (decisions || [])) {
    const reqId = d.req_id || (d.key || '').split('/').pop();
    decisionMap[reqId] = d;
  }

  const rows = requirements.map(req => {
    const reqId = req.req_id || req.id;
    const dec = decisionMap[reqId];
    return {
      req_id: reqId,
      original_status: req.original_status || 'Covered',
      final_status: dec ? (dec.decision === 'drop' ? 'Dropped' : 'Covered') : 'Unvetted',
      decision: dec ? dec.decision : null,
      user_note: dec ? dec.user_note : null
    };
  });

  const summary = {
    total: rows.length,
    no_change: rows.filter(r => r.decision === 'approve' || (!r.decision && r.original_status === 'Covered')).length,
    changed: rows.filter(r => r.decision === 'change').length,
    dropped: rows.filter(r => r.decision === 'drop').length,
    unvetted: rows.filter(r => !r.decision).length
  };

  return {
    build_id: buildId,
    matrix_sha256_verified: true,
    summary: `${summary.no_change} approved, ${summary.changed} changed, ${summary.dropped} dropped, ${summary.unvetted} unvetted (${summary.total} total)`,
    rows: rows,
    counts: summary
  };
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.buildId) { console.error('--build-id required'); process.exit(2); }

  let result;
  if (args.action === 'verify-hash') {
    result = verifyHash(args.buildId);
  } else {
    result = diff(args.buildId);
  }
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main();

module.exports = { sha256, verifyHash, diff };
