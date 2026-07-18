// Ported from upstream Paperclip `ui/src/lib/line-diff.ts` (A3).
// LCS-based line diff used by the document revision diff modal.

// "truncated" is a single summary/notice row the oversized fallback appends in
// place of the lines it dropped; it carries no line numbers and renders as a
// full-width notice in the modal.
export type DiffRowKind = "context" | "removed" | "added" | "truncated";

export type DiffRow = {
  kind: DiffRowKind;
  oldLineNumber: number | null;
  newLineNumber: number | null;
  text: string;
};

// Quadratic-cost guard. The LCS diff below allocates + fills an
// (oldLines+1) × (newLines+1) DP matrix. Under the new plan doctrine a plan doc
// can be hundreds of KiB / tens of thousands of lines, and that matrix will
// freeze or crash the tab. Above either bound we skip the DP entirely and fall
// back to a capped presence diff, which is coarse but keeps "Compare revisions"
// responsive.
export const LINE_DIFF_MAX_LINES = 5000;
export const LINE_DIFF_MAX_CELLS = 4_000_000;

// Row cap for the oversized (presence) fallback. Skipping the DP matrix is not
// enough on its own: a ~512 KiB body that is mostly blank lines is >1,000,000
// lines, and materializing one row per old + per new line still yields millions
// of rows that freeze the tab and the modal that renders them. So the fallback
// shows at most this many content rows (split across the two sides) and appends
// a single "truncated" summary row instead of every line.
export const LINE_DIFF_FALLBACK_MAX_ROWS = 2000;

/** True when the LCS DP matrix for these docs would exceed the cost budget. */
export function isLineDiffTooLarge(oldCount: number, newCount: number): boolean {
  return (
    oldCount > LINE_DIFF_MAX_LINES
    || newCount > LINE_DIFF_MAX_LINES
    || oldCount * newCount > LINE_DIFF_MAX_CELLS
  );
}

/** O(n+m) fallback used when the docs are too large for the LCS diff: show a
 * bounded window of both sides (old lines removed, new lines added) without
 * pairing unchanged lines, capped at {@link LINE_DIFF_FALLBACK_MAX_ROWS} so it
 * never materializes a row per line. Any dropped lines are summarized in a
 * single trailing "truncated" row. Never allocates the DP matrix. */
function buildPresenceDiff(oldLines: string[], newLines: string[]): DiffRow[] {
  const rows: DiffRow[] = [];
  const budget = LINE_DIFF_FALLBACK_MAX_ROWS;
  // Split the budget across both sides so neither side is entirely hidden; if
  // one side is short, the other reclaims the remainder.
  const removedShown = Math.min(oldLines.length, Math.ceil(budget / 2));
  const addedShown = Math.min(newLines.length, budget - removedShown);

  for (let i = 0; i < removedShown; i += 1) {
    rows.push({ kind: "removed", oldLineNumber: i + 1, newLineNumber: null, text: oldLines[i] });
  }
  for (let j = 0; j < addedShown; j += 1) {
    rows.push({ kind: "added", oldLineNumber: null, newLineNumber: j + 1, text: newLines[j] });
  }

  const omitted = (oldLines.length - removedShown) + (newLines.length - addedShown);
  if (omitted > 0) {
    rows.push({
      kind: "truncated",
      oldLineNumber: null,
      newLineNumber: null,
      text:
        `Diff truncated — document too large to show a full comparison. `
        + `${oldLines.length} lines removed, ${newLines.length} added; `
        + `showing the first ${removedShown + addedShown}.`,
    });
  }
  return rows;
}

export function buildLineDiff(oldText: string, newText: string): DiffRow[] {
  const oldLines = oldText.split("\n");
  const newLines = newText.split("\n");
  const oldCount = oldLines.length;
  const newCount = newLines.length;

  if (isLineDiffTooLarge(oldCount, newCount)) {
    return buildPresenceDiff(oldLines, newLines);
  }

  const dp = Array.from({ length: oldCount + 1 }, () => Array<number>(newCount + 1).fill(0));

  for (let i = oldCount - 1; i >= 0; i -= 1) {
    for (let j = newCount - 1; j >= 0; j -= 1) {
      dp[i][j] = oldLines[i] === newLines[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  let oldLineNumber = 1;
  let newLineNumber = 1;

  while (i < oldCount && j < newCount) {
    if (oldLines[i] === newLines[j]) {
      rows.push({ kind: "context", oldLineNumber, newLineNumber, text: oldLines[i] });
      i += 1;
      j += 1;
      oldLineNumber += 1;
      newLineNumber += 1;
      continue;
    }

    if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: "removed", oldLineNumber, newLineNumber: null, text: oldLines[i] });
      i += 1;
      oldLineNumber += 1;
      continue;
    }

    rows.push({ kind: "added", oldLineNumber: null, newLineNumber, text: newLines[j] });
    j += 1;
    newLineNumber += 1;
  }

  while (i < oldCount) {
    rows.push({ kind: "removed", oldLineNumber, newLineNumber: null, text: oldLines[i] });
    i += 1;
    oldLineNumber += 1;
  }

  while (j < newCount) {
    rows.push({ kind: "added", oldLineNumber: null, newLineNumber, text: newLines[j] });
    j += 1;
    newLineNumber += 1;
  }

  return rows;
}
