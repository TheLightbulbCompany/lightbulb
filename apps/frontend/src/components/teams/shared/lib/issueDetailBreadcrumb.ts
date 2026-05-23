// apps/frontend/src/components/teams/shared/lib/issueDetailBreadcrumb.ts

// Ported from upstream Paperclip's issueDetailBreadcrumb.ts
// (paperclip/ui/src/lib/issueDetailBreadcrumb.ts) (MIT, (c) 2025 Paperclip AI).
// Upstream relies on React Router location state; in Next App Router route
// state is not a primitive, so the breadcrumb / header-seed helpers degrade
// to no-op stubs that preserve call-site signatures.

import type { Issue } from "@/components/teams/shared/types";

type IssueDetailBreadcrumb = {
  label: string;
  href: string;
};

export type IssueDetailHeaderSeed = {
  id: string;
  identifier: string | null;
  title: string;
  status: Issue["status"];
  blockerAttention?: Issue["blockerAttention"];
  priority: Issue["priority"];
  projectId: string | null;
  projectName: string | null;
};

export type IssueDetailLocationState = {
  issueDetailBreadcrumb?: IssueDetailBreadcrumb;
  issueDetailHeaderSeed?: IssueDetailHeaderSeed;
  issueDetailInboxQuickArchiveArmed?: boolean;
};

// Translate upstream `/issues/:id` to Isol8's `/teams/issues/:id`.
export function createIssueDetailPath(issuePathId: string): string {
  return `/teams/issues/${issuePathId}`;
}

// No-op stub: Next App Router has no Link `state` prop, so we just return
// the input shape so call sites compile. Header-seed warming was a React
// Router optimization that does not apply here.
export function withIssueDetailHeaderSeed(state: unknown, _issue: Issue): IssueDetailLocationState {
  if (typeof state !== "object" || state === null) return {};
  return { ...(state as IssueDetailLocationState) };
}

// No-op stub: upstream wrote breadcrumb/source state to sessionStorage on
// navigation. Next App Router instead recomputes breadcrumbs from the
// current pathname, so persistence here is unnecessary.
export function rememberIssueDetailLocationState(
  _issuePathId: string,
  _state: unknown,
  _search?: string,
): void {
  // intentionally empty
}
