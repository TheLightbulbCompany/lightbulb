"use client";

// Home's "Needs you": the top three decisions waiting on this human, read from
// the same server-ranked feed the Inbox badge uses (useAttentionFeed), so Home
// and Inbox can never disagree about the count. Rows link to the task when
// there is one; the section link opens the Inbox, where the actions live.

import { sourceLabel } from "@/components/teams/inbox/AttentionList";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { AttentionItem } from "@/components/teams/shared/types";
import { teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { HomeRow, HomeRowsEmpty, HomeSection } from "./HomeSection";

const LIMIT = 3;

export function needsYouTitle(item: AttentionItem): string {
  const title = item.subject.title?.trim();
  // sourceLabel's own fallback ("Needs you") already covers an unknown kind.
  return title || sourceLabel(item);
}

export function needsYouSub(item: AttentionItem): string {
  return [item.originAgentName, item.whyNow].filter(Boolean).join(" · ");
}

export function HomeNeedsYou() {
  const { items, count, isLoading, isError } = useAttentionFeed();
  const inbox = teamsPanelUrl("inbox");
  const settled = !isLoading || items.length > 0;

  return (
    <HomeSection
      label="Needs you"
      href={inbox}
      linkLabel={settled && count > 0 ? `Open Inbox · ${count}` : "Open Inbox"}
    >
      {!settled ? (
        <HomeRowsEmpty>Checking what needs you…</HomeRowsEmpty>
      ) : isError && items.length === 0 ? (
        <HomeRowsEmpty role="alert">Couldn&apos;t read what needs you just now.</HomeRowsEmpty>
      ) : items.length === 0 ? (
        <HomeRowsEmpty>Nothing is waiting on you.</HomeRowsEmpty>
      ) : (
        items.slice(0, LIMIT).map((item) => (
          <HomeRow
            key={item.id}
            who={item.originAgentName ?? "Your team"}
            title={needsYouTitle(item)}
            sub={needsYouSub(item)}
            right={timeAgo(item.createdAt)}
            href={item.relatedIssue?.id ? teamsDetailUrl("issue", item.relatedIssue.id) : inbox}
          />
        ))
      )}
    </HomeSection>
  );
}
