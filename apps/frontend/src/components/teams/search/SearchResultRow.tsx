// apps/frontend/src/components/teams/search/SearchResultRow.tsx
//
// Ported + adapted from upstream Paperclip (ui/src/components/search/
// SearchResultRow.tsx, MIT © 2025 Paperclip AI). Renders one cross-entity
// search result (issue / artifact / agent / project) with matched-field
// snippets and highlights. Navigation is rewired to Isol8's in-shell panel
// router: rows prefer `nav.openDetail(...)` when rendered under /chat and fall
// back to a real anchor href (teamsDetailUrl / agentInspectorUrl) so native
// new-tab / middle-click still works.

import { Bot, FileText, Hexagon, MessageSquare, Paperclip, Quote } from "lucide-react";
import Link from "next/link";
import type { ComponentType } from "react";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import type { CompanySearchResult } from "@/components/teams/shared/types";
import { HighlightedText } from "./HighlightedText";

const SNIPPET_STYLES: Record<string, { Icon: ComponentType<{ size?: number; className?: string }>; label: string }> = {
  comment: { Icon: MessageSquare, label: "Comment" },
  document: { Icon: FileText, label: "Doc" },
  artifact: { Icon: Paperclip, label: "Artifact" },
  description: { Icon: Quote, label: "Description" },
};

function snippetStyle(field: string, fallbackLabel: string) {
  return SNIPPET_STYLES[field] ?? { Icon: Quote, label: fallbackLabel };
}

// Resolve a result to its in-shell nav target: the anchor href (for native
// new-tab) and the openDetail(panelKey, id) pair (for the client-side swap).
// `agent` has no teams detail panel — it opens the agent inspector, which is a
// plain ?panel URL committed via handleShellLinkClick (no openDetail path).
function navTargetFor(
  result: CompanySearchResult,
): { href: string; detail?: { panelKey: string; id: string } } | null {
  if (result.type === "issue") {
    const id = result.issue?.id ?? result.id;
    return { href: teamsDetailUrl("issue", id), detail: { panelKey: "issue-detail", id } };
  }
  if (result.type === "artifact") {
    const id = result.artifact?.issueId;
    if (!id) return null;
    return { href: teamsDetailUrl("issue", id), detail: { panelKey: "issue-detail", id } };
  }
  if (result.type === "project") {
    return {
      href: teamsDetailUrl("project", result.id),
      detail: { panelKey: "project-detail", id: result.id },
    };
  }
  // agent
  return { href: agentInspectorUrl(result.id) };
}

export interface SearchResultRowProps {
  result: CompanySearchResult;
  agentsById?: ReadonlyMap<string, string>;
}

export function SearchResultRow({ result, agentsById }: SearchResultRowProps) {
  const nav = useInlineTeamsNav();
  const target = navTargetFor(result);
  if (!target) return null;

  const handleClick = (e: React.MouseEvent) => {
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
    if (nav && target.detail) {
      e.preventDefault();
      nav.openDetail(target.detail.panelKey, target.detail.id);
      return;
    }
    handleShellLinkClick(e, target.href);
  };

  const TypeIcon =
    result.type === "agent"
      ? Bot
      : result.type === "project"
        ? Hexagon
        : result.type === "artifact"
          ? Paperclip
          : null;

  const issue = result.issue;
  const artifact = result.artifact;
  const assigneeName =
    issue?.assigneeAgentId && agentsById ? agentsById.get(issue.assigneeAgentId) ?? null : null;
  const updatedRaw = result.updatedAt ?? issue?.updatedAt ?? artifact?.updatedAt ?? null;
  const updated = updatedRaw ? timeAgo(updatedRaw) : "";

  const titleHighlights = result.snippets.find((s) => s.field === "title")?.highlights;
  const bodySnippets = result.snippets.filter((s) => s.field !== "title").slice(0, 2);
  // For issue rows `result.title` is the display string that already prefixes
  // the identifier (e.g. "PAP-80 Design auth"), and that identifier is rendered
  // separately above. The `title`-field highlight ranges, however, are measured
  // upstream against the RAW issue title (`issue.title`) — so highlight that,
  // not `result.title`, which would both duplicate the identifier AND shift the
  // ranges onto the wrong characters. Non-issue rows carry no identifier prefix,
  // so their `result.title` already matches the string the ranges were cut from.
  const titleText = result.type === "issue" && issue ? issue.title : result.title;

  return (
    <Link
      href={target.href}
      onClick={handleClick}
      data-result-type={result.type}
      className="group flex items-start gap-3 rounded-md border px-3 py-3 no-underline transition-colors hover:bg-[var(--cream-deep)]"
      style={{ borderColor: "var(--edge)", color: "var(--midnight)" }}
    >
      <span className="mt-0.5 shrink-0" style={{ color: "var(--midnight-muted)" }}>
        {TypeIcon ? <TypeIcon size={16} /> : <Quote size={16} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
          {issue?.identifier ? (
            <span className="shrink-0 font-mono text-caption tabular-nums" style={{ color: "var(--midnight-muted)" }}>
              {issue.identifier}
            </span>
          ) : null}
          {artifact?.issueIdentifier ? (
            <span className="shrink-0 font-mono text-caption tabular-nums" style={{ color: "var(--midnight-muted)" }}>
              {artifact.issueIdentifier}
            </span>
          ) : null}
          <HighlightedText
            text={titleText}
            highlights={titleHighlights}
            className="min-w-0 flex-1 text-ui font-medium leading-snug"
          />
        </div>
        {bodySnippets.map((snippet, i) => {
          const { Icon, label } = snippetStyle(snippet.field, snippet.label);
          return (
            <div
              key={`${snippet.field}-${i}`}
              className="mt-1.5 flex min-w-0 items-start gap-1.5 text-caption"
              style={{ color: "var(--midnight-muted)" }}
            >
              <Icon size={12} className="mt-0.5 shrink-0" />
              <span
                className="shrink-0 rounded-sm border px-1 py-0.5 label-caps"
                style={{ borderColor: "var(--edge)", color: "var(--midnight-muted)" }}
              >
                {label}
              </span>
              <HighlightedText
                text={snippet.text}
                highlights={snippet.highlights}
                className="line-clamp-2 leading-relaxed"
              />
            </div>
          );
        })}
      </div>
      <div className="ml-2 hidden shrink-0 flex-col items-end gap-1 text-caption sm:flex" style={{ color: "var(--midnight-muted)" }}>
        {assigneeName ? <span className="truncate">{assigneeName}</span> : null}
        {updated ? <span className="tabular-nums">{updated}</span> : null}
      </div>
    </Link>
  );
}
