"use client";

// apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx
//
// The company Artifacts gallery — every deliverable an agent produced across
// the whole company, GROUPED BY TASK (ported shape from upstream Paperclip's
// pages/Artifacts.tsx + components/artifacts/ArtifactGroupCard.tsx, MIT (c)
// 2025 Paperclip AI). Each card is one issue's bundle of artifacts (documents +
// attachments + work-products); clicking opens that issue's detail panel, where
// the per-issue "Output" section (IssueWorkProductsSection) lives.
//
// v1 renders the grouped cards + preview titles and links back into the issue.
// Deferred (follow-up): inline media thumbnails (needs the /teams/attachments
// content-path rewrite + CSP handling), the flat/ungrouped view, kind facets,
// and cursor pagination — the BFF route already forwards all those params.

import { Images } from "lucide-react";
import Link from "next/link";
import { PanelMasthead } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type {
  CompanyArtifactGroup,
  CompanyArtifactsResponse,
} from "@/components/teams/shared/types";

function GroupCard({ group }: { group: CompanyArtifactGroup }) {
  const nav = useInlineTeamsNav();
  const url = teamsDetailUrl("issue", group.issue.id);

  const handleClick = (e: React.MouseEvent) => {
    if (!nav) return;
    if (
      e.defaultPrevented ||
      e.button !== 0 ||
      e.metaKey ||
      e.ctrlKey ||
      e.shiftKey ||
      e.altKey
    ) {
      return;
    }
    e.preventDefault();
    nav.openDetail("issue-detail", group.issue.id);
  };

  const previewTitles = group.previewArtifacts
    .map((a) => a.title)
    .filter((t): t is string => !!t && t.trim().length > 0);

  return (
    <Link
      href={url}
      onClick={handleClick}
      className="editorial-card flex flex-col gap-2 p-4 transition-colors hover:bg-[var(--cream-deeper)]"
      style={{ borderColor: "var(--edge)" }}
    >
      <div className="flex items-center gap-2">
        <span
          className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
          style={{ background: "var(--edge)", color: "var(--ink)" }}
        >
          {group.issue.identifier}
        </span>
        <span className="flex-1 truncate text-sm font-medium text-[var(--ink)]" title={group.title}>
          {group.title}
        </span>
        <span className="text-xs flex-shrink-0" style={{ color: "var(--mute)" }}>
          {group.count} {group.count === 1 ? "item" : "items"}
        </span>
      </div>
      {previewTitles.length > 0 ? (
        <ul className="flex flex-col gap-1 pl-1">
          {previewTitles.slice(0, 4).map((title, i) => (
            <li key={i} className="truncate text-xs" style={{ color: "var(--ink-soft)" }}>
              · {title}
            </li>
          ))}
        </ul>
      ) : null}
      <span className="text-xs" style={{ color: "var(--mute)" }}>
        Updated {timeAgo(group.updatedAt)}
      </span>
    </Link>
  );
}

export function ArtifactsPanel() {
  const { read } = useTeamsApi();
  // groupBy=task clusters deliverables under the issue that produced them —
  // the ported grouped shape, not a flat firehose.
  const { data, error, isLoading } = read<CompanyArtifactsResponse>("/artifacts?groupBy=task");
  const groups = data?.groups ?? [];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-4xl mx-auto w-full">
      <PanelMasthead overline="Company" title="Artifacts" />

      <section aria-label="Artifacts gallery" className="min-w-0">
        {isLoading && groups.length === 0 ? (
          <p className="font-editorial-serif text-base italic" style={{ color: "var(--mute)" }}>
            Loading artifacts…
          </p>
        ) : error && groups.length === 0 ? (
          <p
            role="alert"
            className="font-editorial-serif text-base italic"
            style={{ color: "var(--warn)" }}
          >
            Failed to load artifacts.
          </p>
        ) : groups.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-12 text-center">
            <Images size={28} style={{ color: "var(--mute)" }} />
            <p className="font-editorial-serif text-base italic" style={{ color: "var(--mute)" }}>
              No artifacts yet.
            </p>
            <p className="text-xs" style={{ color: "var(--mute)" }}>
              Deliverables your agents produce show up here.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {groups.map((group) => (
              <GroupCard key={group.id} group={group} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
