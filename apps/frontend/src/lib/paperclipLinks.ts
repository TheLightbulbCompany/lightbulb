// Agents used to write internal links as native Paperclip paths (`/ISOA/issues/ISOA-171`,
// `/ISOA/approvals/<id>`, `/ISOA/agents/<key>/runs/<id>`, `/ISOA/goals/<id>`) and as
// `/teams/tasks/<id>`. None of those pages exist, so such a link opens the Lightbulb place.
// The company prefix is optional only for issues and approvals: a bare `/agents/<key>` or
// `/projects/<key>` is as likely to be someone else's route.
const PAPERCLIP_LINK =
  /^\/(?:([A-Z][A-Z0-9]{0,9})\/)?(issues|approvals|projects|agents|goals)\/([^/?#]+)(?:\/runs\/([^/?#]+))?\/?(?:\?[^#]*)?(#[^?]*)?$/;
const RETIRED_TASK_LINK = /^\/teams\/(?:tasks|issues)\//;

export function paperclipLinkTarget(href?: string | null): string | null {
  const m = href ? PAPERCLIP_LINK.exec(href.replace(RETIRED_TASK_LINK, "/issues/")) : null;
  if (!m) return null;
  const [, prefix, section] = m;
  if (!prefix && section !== "issues" && section !== "approvals") return null;
  return "/voice/lightbulb";
}
