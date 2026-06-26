#!/usr/bin/env node
// atlas-recap-diff.js
//
// Step 5: verifies the requirement matrix's SHA256 and emits an original→final
// diff table. Aborts the recap with a clear error on hash mismatch (W5).
//
// Gateway-free: this tool is a pure transform. The matrix object arrives via the
// MATRIX env var (the caller does `state.get atlas/requirement-matrix` and
// passes map[build_id]); the per-requirement decisions arrive via the DECISIONS
// env var (a map req_id -> decision row, from `state.get atlas/decisions`
// map[build_id]). No gateway tool calls.
//
// W5 hash: architect-proposal stamps `matrix_sha256` = sha256(JSON.stringify(
// {skeleton, classifications, skill_recommendations, snapshot_at})) onto the
// matrix BEFORE the agent loop enriches it (user_stories / voice_tone /
// requirements_with_triggers are appended after and are NOT part of the hash).
// So we recompute over exactly that deterministic core, in that key order, and
// compare against the stamped value — a tamper check on the deterministic core.

const crypto = require('crypto');

function parseArgs(argv) {
  const args = { action: 'diff', buildId: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
  }
  return args;
}

function readMatrixEnv() {
  try { return JSON.parse(process.env.MATRIX || 'null'); } catch (e) { return null; }
}

function readDecisionsEnv() {
  try {
    const d = JSON.parse(process.env.DECISIONS || 'null') || {};
    // decisions is a map req_id -> row; tolerate an array too.
    if (Array.isArray(d)) {
      const map = {};
      for (const row of d) {
        const reqId = row.req_id || (row.key || '').split('/').pop();
        if (reqId) map[reqId] = row;
      }
      return map;
    }
    return d;
  } catch (e) { return {}; }
}

function sha256(s) {
  return crypto.createHash('sha256').update(s).digest('hex');
}

function verifyHash(buildId, matrix) {
  if (!matrix) {
    return { hash_matches: false, error: 'requirement-matrix not found in state' };
  }
  const expectedHex = matrix.matrix_sha256;
  if (!expectedHex) {
    return { hash_matches: false, error: 'matrix_sha256 missing from requirement-matrix' };
  }
  // Reconstruct the deterministic core in the exact key order architect-proposal
  // hashed it (insertion order is preserved by JSON.stringify for string keys).
  // Coerce undefined → null to match architect-proposal's compute-matrix, which
  // stamps the hash over JSON.parse(env||"null") (always null, never an omitted
  // key). Without this, a field absent here (undefined, dropped by JSON.stringify)
  // would diverge from architect's explicit null and block every recap for the build.
  const core = {
    skeleton: matrix.skeleton ?? null,
    classifications: matrix.classifications ?? null,
    skill_recommendations: matrix.skill_recommendations ?? null,
    snapshot_at: matrix.snapshot_at ?? null
  };
  const computedSha = sha256(JSON.stringify(core));
  const matches = computedSha === expectedHex;
  return {
    build_id: buildId,
    hash_matches: matches,
    computed: computedSha,
    expected: expectedHex,
    error: matches ? null : 'requirement-matrix has been modified since snapshot. Recap aborted per W5 hash-verification pattern.'
  };
}

function diff(buildId, matrix, decisions) {
  const hashCheck = verifyHash(buildId, matrix);
  if (!hashCheck.hash_matches) {
    return { error: hashCheck.error, hash_check: hashCheck };
  }

  const requirements = matrix.user_stories || matrix.classifications || [];
  const decisionMap = decisions || {};

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
    // Only explicitly approved rows are "no change". The old fallback also
    // counted unvetted (no-decision) rows whose original_status was 'Covered',
    // which double-counted them — they're already in `unvetted` below.
    no_change: rows.filter(r => r.decision === 'approve').length,
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

  const matrix = readMatrixEnv();
  const decisions = readDecisionsEnv();

  let result;
  if (args.action === 'verify-hash') {
    result = verifyHash(args.buildId, matrix);
  } else {
    result = diff(args.buildId, matrix, decisions);
  }
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main();

module.exports = { sha256, verifyHash, diff };
