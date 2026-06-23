"use client";

import { GoalDetailPage } from "@/components/teams/goals/GoalDetailPage";

export interface GoalDetailPanelProps {
  goalId: string;
}

export function GoalDetailPanel({ goalId }: GoalDetailPanelProps) {
  return <GoalDetailPage goalId={goalId} />;
}
