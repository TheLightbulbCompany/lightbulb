"use client";

// The panel is mounted via TeamsLayout when ?panel=issue-detail&issueId=<id>;
// see apps/frontend/src/components/teams/TeamsPanelRouter.tsx for routing.

import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";

export interface IssueDetailPanelProps {
  issueId: string;
}

export function IssueDetailPanel({ issueId }: IssueDetailPanelProps) {
  return <IssueDetailPage issueId={issueId} />;
}
