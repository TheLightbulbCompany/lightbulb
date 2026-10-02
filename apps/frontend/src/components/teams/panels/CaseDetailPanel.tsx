"use client";

import { CaseDetailPage } from "@/components/teams/cases/CaseDetailPage";

export interface CaseDetailPanelProps {
  /** The case uuid or its identifier (`LB-C7`). */
  caseId: string;
}

export function CaseDetailPanel({ caseId }: CaseDetailPanelProps) {
  return <CaseDetailPage caseId={caseId} />;
}
