"use client";

import { ProjectDetailPage } from "@/components/teams/projects/ProjectDetailPage";

export interface ProjectDetailPanelProps {
  projectId: string;
}

export function ProjectDetailPanel({ projectId }: ProjectDetailPanelProps) {
  return <ProjectDetailPage projectId={projectId} />;
}
