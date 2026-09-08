// apps/frontend/src/components/teams/inbox/InboxPage.tsx

"use client";

// The Inbox page: what needs a human, what is running, what happened.
//
// It holds NO task rows. Inbox and Tasks used to render the same issue list
// behind different filters — Inbox with five pill tabs, Tasks grouped and
// boarded — which meant two tabs answering the same question. They now split
// by KIND: this page answers "what needs me", Tasks answers "what is the
// work".

import { useCallback, useEffect, useRef } from "react";

import { InboxHappeningNow } from "@/components/teams/inbox/InboxHappeningNow";
import { InboxNeedsYou } from "@/components/teams/inbox/InboxNeedsYou";
import { InboxRecently } from "@/components/teams/inbox/InboxRecently";
import { PanelMasthead } from "@/components/teams/shell";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { teamsDetailUrl } from "@/lib/shellNav";
import type { InboxSectionId } from "@/components/workspace/panelRegistry";
import type { CompanyAgent } from "@/components/teams/shared/types";

export interface InboxPageProps {
  /** Owner id used as the localStorage namespace for dismissed failed runs. */
  companyId: string;
  /** Currently signed-in user id; passed through to the decision cards. */
  currentUserId: string;
  /** Company roster, so failed runs and activity actors read as names. */
  agents?: CompanyAgent[];
  /** ?section deep link — scrolls to that band once it is on screen. */
  section?: InboxSectionId;
}

export function InboxPage({
  companyId,
  currentUserId,
  agents = [],
  section,
}: InboxPageProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  // Inside the /chat shell a nav provider is present → open the issue detail
  // in-shell. Without one (bare mount), fall back to committing the canonical
  // in-shell URL directly — same destination, no provider hop.
  const inlineNav = useInlineTeamsNav();
  const openIssue = useCallback(
    (issueId: string) => {
      if (inlineNav) {
        inlineNav.openDetail("issue-detail", issueId);
        return;
      }
      window.history.pushState(null, "", teamsDetailUrl("issue", issueId));
    },
    [inlineNav],
  );

  // Scoped to this page's root, not the document: two Inbox panels can be open
  // in one dock, and a global lookup would scroll the wrong one. Runs after
  // the bands mount so the target exists.
  useEffect(() => {
    if (!section) return;
    rootRef.current?.querySelector(`#inbox-${section}`)?.scrollIntoView?.({ block: "start" });
  }, [section]);

  return (
    <div
      ref={rootRef}
      className="editorial-page mx-auto flex w-full max-w-4xl flex-col gap-6 p-4 sm:p-5 lg:p-6"
    >
      <PanelMasthead overline="Operations" title="Inbox" />
      <InboxNeedsYou
        companyId={companyId}
        agents={agents}
        currentUserId={currentUserId}
        onOpenIssue={openIssue}
      />
      <InboxHappeningNow />
      <InboxRecently agents={agents} />
    </div>
  );
}
