// apps/frontend/src/components/teams/shared/lib/safeHref.ts

// Shared link-safety guard for Teams-rendered hrefs. Agent/BFF-supplied URLs are
// only length-checked upstream, so a compromised or buggy agent could emit a
// non-web scheme (e.g. `data:`/`javascript:`) and turn a trusted link affordance
// into an attacker-controlled document. Callers pass any candidate href through
// this before rendering a live anchor; an unsafe scheme yields `undefined` so the
// caller can drop the link entirely.
export function safeHref(href?: string | null): string | undefined {
  if (!href) return undefined;
  const v = href.trim().toLowerCase();
  if (v.startsWith("javascript:") || v.startsWith("data:")) return undefined;
  return href;
}
