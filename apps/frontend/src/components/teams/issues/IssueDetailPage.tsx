// apps/frontend/src/components/teams/issues/IssueDetailPage.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx top-level layout
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: header + description + comments thread + read-only properties sidebar.
// Drops chat/activity/related-work tabs, run ledger, sub-issues, plugin slots,
// documents, feedback, file uploads, interactions, continuation handoff.
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

"use client";

import { IssueHeader } from "./IssueHeader";
import { IssueComments } from "./IssueComments";
import { IssueProperties } from "./IssueProperties";
import { useIssueDetail } from "./hooks/useIssueDetail";
import { useIssueMutations } from "./hooks/useIssueMutations";

export interface IssueDetailPageProps {
  issueId: string;
}

export function IssueDetailPage({ issueId }: IssueDetailPageProps) {
  const { issue, comments, isLoading, isError, error } = useIssueDetail(issueId);
  const mutations = useIssueMutations();

  if (isLoading && !issue) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Loading…
      </div>
    );
  }

  if (isError && !issue) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        <p>Failed to load issue.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--mute)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  if (!issue) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Issue not found.
      </div>
    );
  }

  return (
    <div
      className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--ink)" }}
    >
      <main className="flex flex-col gap-6 min-w-0">
        <IssueHeader
          issue={issue}
          onTitleSave={async (title) => {
            await mutations.update(issueId, { title });
          }}
          onStatusChange={async (status) => {
            await mutations.update(issueId, { status });
          }}
          onPriorityChange={async (priority) => {
            await mutations.update(issueId, { priority });
          }}
        />
        {issue.description && (
          <div
            className="text-sm whitespace-pre-wrap pl-3"
            style={{
              borderLeft: "2px solid var(--edge)",
              color: "var(--ink-soft)",
            }}
          >
            {issue.description}
          </div>
        )}
        <div className="pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
          <h2 className="section-label mb-3">
            <b>Conversation</b>
          </h2>
          <IssueComments
            comments={comments}
            isLoading={isLoading}
            onSubmit={async (body) => {
              await mutations.addComment(issueId, body);
            }}
          />
        </div>
      </main>
      <aside className="lg:sticky lg:top-4 lg:self-start">
        <IssueProperties issue={issue} />
      </aside>
    </div>
  );
}
