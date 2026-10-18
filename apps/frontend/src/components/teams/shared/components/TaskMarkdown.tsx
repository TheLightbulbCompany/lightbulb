// apps/frontend/src/components/teams/shared/components/TaskMarkdown.tsx

// Ported from upstream Paperclip's MarkdownBody issue-reference handling
// (paperclip/ui/src/components/MarkdownBody.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI): MarkdownIssueLink, the knownPrefixes gate and
// the `agent://` / `user://` mention chips (upstream lib/mention-chips.ts).
//
// Adaptations vs upstream (only what had to change):
//   - MarkdownBody                       -> shared MarkdownView, opted in here only
//                                           (room messages share MarkdownView and
//                                           must stay unlinked)
//   - useOptionalCompany().issuePrefix  -> useTeamsApi.read("/settings").issuePrefix;
//                                           with no prefix loaded nothing links
//                                           (upstream stays permissive)
//   - react-query issue detail          -> useTeamsApi.read("/issues/{id}"), keyed
//                                           null until hover/focus
//   - router Link to /issues/{id}       -> anchor to teamsDetailUrl("issue", id),
//                                           committed via handleShellLinkClick
//   - mention chips                     -> the inert "@Name" chip TaskChatMarkdown had
//   - no hover quicklook / locked chip  -> the fork has neither

"use client";

import { useMemo, useState, type ComponentProps, type ComponentType, type ReactNode } from "react";
import { defaultUrlTransform, type Components, type Options, type UrlTransform } from "react-markdown";
import { MarkdownView, markdownComponents } from "@/components/teams/shared/components/MarkdownView";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import {
  parseIssueReferenceFromHref,
  remarkLinkIssueReferences,
} from "@/components/teams/shared/lib/issue-reference";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";

const MENTION_RE = /^(agent|user):\/\//;

const mentionUrlTransform: UrlTransform = (value) =>
  MENTION_RE.test(value) ? value : defaultUrlTransform(value);

const BaseLink = markdownComponents.a as ComponentType<ComponentProps<"a">>;

function MarkdownIssueLink({ issuePathId, children }: { issuePathId: string; children: ReactNode }) {
  const { read } = useTeamsApi();
  const [engaged, setEngaged] = useState(false);
  // A transcript can mention dozens of tasks; their detail is hover information.
  const { data } = read<{ identifier?: string; title?: string; status?: string }>(
    engaged ? `/issues/${encodeURIComponent(issuePathId)}` : null,
  );
  const identifier = data?.identifier ?? issuePathId;
  const title = data?.title ?? identifier;
  const issueLabel = title !== identifier ? `Issue ${identifier}: ${title}` : `Issue ${identifier}`;
  const href = teamsDetailUrl("issue", issuePathId);
  return (
    <a
      href={href}
      data-mention-kind="issue"
      onPointerEnter={() => setEngaged(true)}
      onFocus={() => setEngaged(true)}
      onClick={(e) => {
        if (window.location.pathname === "/chat") handleShellLinkClick(e, href);
      }}
      className="font-normal underline"
      style={{ color: "var(--midnight)" }}
      title={title}
      aria-label={issueLabel}
    >
      {data?.status ? (
        <StatusIcon status={data.status} className="relative -top-px mr-1 inline-block h-4 w-4 align-middle" />
      ) : null}
      {children}
    </a>
  );
}

const COMPONENTS: Partial<Components> = {
  a: (props) => {
    const issueRef = parseIssueReferenceFromHref(props.href);
    if (issueRef) {
      return <MarkdownIssueLink issuePathId={issueRef.issuePathId}>{props.children}</MarkdownIssueLink>;
    }
    if (props.href && MENTION_RE.test(props.href)) {
      return (
        <span
          className="rounded-sm px-1 font-medium"
          style={{ background: "var(--cream-deeper)", color: "var(--midnight)" }}
          data-mention-href={props.href}
        >
          {props.children}
        </span>
      );
    }
    return <BaseLink {...(props as ComponentProps<"a">)} />;
  },
};

/** Markdown for task surfaces: MarkdownView plus issue-identifier links and mention chips. */
/** `skipHtml` drops raw HTML (task chat comments and descriptions, as before);
 *  without it raw HTML shows as escaped text, so an approval shows all it says. */
export function TaskMarkdown({ body, skipHtml = false }: { body: string; skipHtml?: boolean }) {
  const { read } = useTeamsApi();
  const issuePrefix = read<{ issuePrefix?: string }>("/settings").data?.issuePrefix;
  const remarkPlugins = useMemo<Options["remarkPlugins"]>(
    () => (issuePrefix ? [[remarkLinkIssueReferences, { knownPrefixes: [issuePrefix] }]] : undefined),
    [issuePrefix],
  );
  return (
    <MarkdownView
      skipHtml={skipHtml}
      components={COMPONENTS}
      urlTransform={mentionUrlTransform}
      remarkPlugins={remarkPlugins ?? undefined}
    >
      {body}
    </MarkdownView>
  );
}
