// Ported from upstream Paperclip's pages/Inbox.tsx row-renderer block
// (paperclip/ui/src/pages/Inbox.tsx:2121-2521) (MIT, (c) 2025 Paperclip AI).
// v1: flat list with today/earlier/search section dividers. Drops parent-child
// nesting (deferred per #3c plan). Only renders InboxWorkItem.kind="issue".
//
// Wrapper-level fade: upstream applies the archiving className directly to
// `IssueRow`'s root <Link>; we apply it to an outer wrapper instead so
// `data-inbox-item-id` and `onClickCapture` (for selectedId sync) live at the
// same node. Visual + pointer-events behavior is identical.
//
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

import type { JSX, ReactNode } from "react";
import { Fragment } from "react";
import type {
  InboxGroupedSection,
  InboxWorkItem,
} from "@/components/teams/shared/lib/inbox";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import { SwipeToArchive } from "@/components/teams/inbox/SwipeToArchive";
import { cn } from "@/lib/utils";

export interface InboxListProps {
  sections: InboxGroupedSection[];
  selectedIssueId: string | null;
  onSelect: (id: string) => void;
  onArchive: (id: string) => void;
  onMarkRead: (id: string) => void;
  /** Issue ids currently mid-archive (for fade-out CSS). */
  archivingIds: Set<string>;
  /** When true, wrap each row in `<SwipeToArchive>` for touch gestures. */
  isMobile?: boolean;
  /**
   * Active search query (when non-empty, the parent passes a single
   * search-kind section). Used to render `Search results for "<query>"`
   * in the section header.
   */
  searchQuery?: string;
}

function sectionHeaderLabel(
  kind: InboxGroupedSection["kind"],
  searchQuery?: string,
): string {
  if (kind === "today") return "Today";
  if (kind === "earlier") return "Earlier";
  if (kind === "search") {
    return searchQuery ? `Search results for "${searchQuery}"` : "Search results";
  }
  return "";
}

const ARCHIVING_CLASSNAME =
  "pointer-events-none -translate-x-4 scale-[0.98] opacity-0 transition-all duration-200 ease-out";

function isIssueWorkItem(
  item: InboxWorkItem,
): item is Extract<InboxWorkItem, { kind: "issue" }> {
  return item.kind === "issue";
}

export function InboxList({
  sections,
  selectedIssueId,
  onSelect,
  onArchive,
  onMarkRead,
  archivingIds,
  isMobile = false,
  searchQuery,
}: InboxListProps): JSX.Element {
  return (
    <div className="flex flex-col">
      {sections.map((section, sectionIndex) => {
        // v1: only `issue` work items render; defensively skip the rest until
        // approval/failed-run/join-request rows ship.
        const issueItems = section.items.filter(isIssueWorkItem);
        if (issueItems.length === 0) return null;
        return (
          <Fragment key={`${section.kind}-${sectionIndex}`}>
            {/* Retheme: muted-foreground section header → editorial mute mono caps. */}
            <h3
              className="section-label px-3 pt-3 pb-1 sm:px-4"
              style={{ marginBottom: 0 }}
            >
              {sectionHeaderLabel(section.kind, searchQuery)}
            </h3>
            <div className="flex flex-col">
              {issueItems.map((item) => {
                const id = item.issue.id;
                const isSelected = id === selectedIssueId;
                const isArchiving = archivingIds.has(id);
                const row: ReactNode = (
                  <IssueRow
                    issue={item.issue}
                    selected={isSelected}
                    unreadState={item.issue.unread ? "visible" : "hidden"}
                    onMarkRead={() => onMarkRead(id)}
                    onArchive={() => onArchive(id)}
                  />
                );
                return (
                  <div
                    key={id}
                    data-inbox-item-id={id}
                    onClickCapture={() => onSelect(id)}
                    className={cn(
                      "relative",
                      isArchiving && ARCHIVING_CLASSNAME,
                    )}
                  >
                    {isMobile ? (
                      <SwipeToArchive
                        selected={isSelected}
                        disabled={isArchiving}
                        onArchive={() => onArchive(id)}
                      >
                        {row}
                      </SwipeToArchive>
                    ) : (
                      row
                    )}
                  </div>
                );
              })}
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
