import type { Href } from "expo-router";

/**
 * The two Paperclip-native link shapes with a real in-app mobile screen —
 * mirrors the `issues`/`agents/.../runs/...` branches of the web's
 * `shellUrlFromPaperclipPath` (apps/frontend/src/lib/shellNav.ts). Both take
 * their id verbatim, with no uuid-vs-url-key resolution: upstream resolves an
 * issue identifier ("ISOA-171") for every /issues/:id route, and a run id is
 * always the run's own id. Agents/projects/goals/approvals have no mobile
 * detail screen yet (the web's `/agent/[agentId]` route is a DM redirect, not
 * an agent inspector — routing a Paperclip agent link there would silently
 * land somewhere the reader didn't ask for), so those stay on the web
 * fallback in MessageMarkdown.tsx. The company prefix is optional here too —
 * agents sometimes drop it (comment-style.md still requires it).
 */
const ISSUE_OR_RUN =
  /^\/(?:[A-Z][A-Z0-9]{0,9}\/)?(?:issues\/([^/?#]+)|agents\/[^/?#]+\/runs\/([^/?#]+))\/?(?:\?[^#]*)?(?:#[^?]*)?$/;

/** The in-app screen for a Paperclip-native link, or null when it isn't one
 *  of the two mobile has a screen for (the caller falls back to the web). */
export function paperclipMobileHref(href?: string | null): Href | null {
  const m = href ? ISSUE_OR_RUN.exec(href) : null;
  if (!m) return null;
  const [, issueId, runId] = m;
  if (runId) return { pathname: "/(app)/teams/run/[runId]", params: { runId } };
  if (issueId) return { pathname: "/(app)/teams/issue/[id]", params: { id: issueId } };
  return null;
}
