// apps/frontend/src/lib/externalContentMarkers.ts
//
// OpenClaw wraps untrusted tool output (web_search / web_fetch / email /
// webhook bodies) in injection-defense sentinels before handing it to the
// LLM (agent src/security/external-content.ts):
//
//   SECURITY NOTICE: The following content is from an EXTERNAL, UNTRUSTED source …
//   <<<EXTERNAL_UNTRUSTED_CONTENT id="abc123">>>
//   Source: Web Search
//   ---
//   …content…
//   <<<END_EXTERNAL_UNTRUSTED_CONTENT id="abc123">>>
//
// The markers and the security-notice block are model-facing plumbing,
// never user-facing — strip them at the presentation layer (both the live
// stream and history restore render through the call sites in
// MessageList). Only the marker lines and the notice block are removed; the
// wrapped content itself (including its Source: provenance line) is kept.

const MARKER_SUBSTRING = "EXTERNAL_UNTRUSTED_CONTENT";
const WARNING_SUBSTRING =
  "SECURITY NOTICE: The following content is from an EXTERNAL, UNTRUSTED source";

/** A whole-line `<<<EXTERNAL_UNTRUSTED_CONTENT id="…">>>` or
 * `<<<END_EXTERNAL_UNTRUSTED_CONTENT id="…">>>` marker. */
const MARKER_LINE_RE = /^\s*<<<(?:END_)?EXTERNAL_UNTRUSTED_CONTENT\b[^\n]*>>>\s*$/;

export function stripExternalContentMarkers(text: string): string {
  // Fast path: the overwhelmingly common transcript chunk carries neither.
  if (!text.includes(MARKER_SUBSTRING) && !text.includes(WARNING_SUBSTRING)) {
    return text;
  }
  const out: string[] = [];
  let inWarning = false;
  for (const line of text.split("\n")) {
    if (MARKER_LINE_RE.test(line)) {
      // A start marker also terminates the warning block that precedes it.
      inWarning = false;
      continue;
    }
    if (line.includes(WARNING_SUBSTRING)) {
      inWarning = true;
      continue;
    }
    if (inWarning) {
      // The warning is a contiguous non-blank block (see the source module's
      // EXTERNAL_CONTENT_WARNING); a blank line ends it.
      if (line.trim() === "") inWarning = false;
      continue;
    }
    out.push(line);
  }
  return out.join("\n");
}
