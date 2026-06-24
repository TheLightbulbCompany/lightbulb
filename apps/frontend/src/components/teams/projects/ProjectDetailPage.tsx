// apps/frontend/src/components/teams/projects/ProjectDetailPage.tsx

// Ported from upstream Paperclip's pages/ProjectDetail.tsx
// (paperclip/ui/src/pages/ProjectDetail.tsx) (MIT, © 2025 Paperclip AI).
// Two-tab Project-detail surface reskinned onto the editorial cream/forest
// palette, modelled on the Issue/Goal detail siblings:
//   - Overview: inline-editable name (ProjectHeader) + inline-editable
//     description + a sticky read-only Properties aside (issue count /
//     timestamps). Mirrors upstream's OverviewContent + the issue/goal layout.
//   - Issues: the project's issues via the BFF `?project=` filter (PR #1006),
//     rendered with the shared TasksList (rows navigate to the task-detail).
//
// No Budget tab: upstream has no project-budget field on the project row
// (budgets live behind a separate budget_policies API the slim BFF does not
// proxy), so a budget editor here was a silent no-op — writes stripped, reads
// always empty.
//
// Deferred vs upstream: icon/color tile, plugin-operations + workspaces +
// configuration tabs, membership affordances, and hard delete/archive (the
// slim BFF exposes no DELETE /projects route). Noted as clean follow-ups —
// none block v1.

"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { PanelTabs } from "@/components/teams/shell";
import { ProjectHeader } from "./ProjectHeader";
import { ProjectProperties } from "./ProjectProperties";
import { ProjectDescription } from "./ProjectDescription";
import { TasksList } from "@/components/teams/issues/TasksList";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import { useProjectDetail } from "./hooks/useProjectDetail";
import { useProjectMutations } from "./hooks/useProjectMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { useSWRConfig } from "swr";
import type {
  CompanyAgent,
  IssueProject,
  IssueUpdateInput,
} from "@/components/teams/shared/types";

export interface ProjectDetailPageProps {
  projectId: string;
}

type ProjectTab = "overview" | "issues";

export function ProjectDetailPage({ projectId }: ProjectDetailPageProps) {
  const { project, issues, isLoading, issuesLoading, isError, error } =
    useProjectDetail(projectId);
  const mutations = useProjectMutations();
  const issueMutations = useIssueMutations();
  const { read } = useTeamsApi();
  const { mutate } = useSWRConfig();
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsResp } = read<{ projects: IssueProject[] }>("/projects");
  const agents = agentsResp?.agents ?? [];
  const projects = projectsResp?.projects ?? [];

  const [tab, setTab] = useState<ProjectTab>("overview");
  const [creating, setCreating] = useState(false);

  if (isLoading && !project) {
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

  if (isError && !project) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        <p>Failed to load project.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--mute)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  if (!project) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Project not found.
      </div>
    );
  }

  // TasksList's inline status circle + board drag patch the issue; route them
  // through the issues mutation hook, then revalidate this project's scoped
  // issues read so the change shows immediately.
  const handleUpdateIssue = (id: string, patch: Record<string, unknown>) => {
    void issueMutations
      .update(id, patch as IssueUpdateInput)
      .then(() =>
        mutate(`/teams${teamsQueryKeys.issuesByProject(projectId)}`),
      );
  };

  const tabs = [
    { value: "overview", label: "Overview" },
    { value: "issues", label: "Issues", count: issues.length },
  ];

  return (
    <div
      className="flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full"
      style={{ background: "var(--cream)", color: "var(--ink)" }}
    >
      <nav
        className="flex items-center gap-1 text-xs"
        style={{ color: "var(--mute)" }}
        aria-label="Breadcrumb"
      >
        <Link href="/teams/projects" className="hover:underline">
          Projects
        </Link>
        <ChevronRight className="h-3 w-3" />
        <span className="truncate" style={{ color: "var(--ink-soft)" }}>
          {project.name}
        </span>
      </nav>

      <PanelTabs
        tabs={tabs}
        value={tab}
        onValueChange={(v) => setTab(v as ProjectTab)}
      />

      {tab === "overview" && (
        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-6">
          <main className="flex flex-col gap-6 min-w-0">
            <ProjectHeader
              project={project}
              onNameSave={async (name) => {
                await mutations.update(projectId, { name });
              }}
            />
            <ProjectDescription
              description={project.description ?? ""}
              onSave={async (description) => {
                await mutations.update(projectId, {
                  description: description || null,
                });
              }}
            />
          </main>
          <aside className="lg:sticky lg:top-4 lg:self-start">
            <ProjectProperties project={project} issueCount={issues.length} />
          </aside>
        </div>
      )}

      {tab === "issues" && (
        <TasksList
          issues={issues}
          agents={agents}
          isLoading={issuesLoading}
          onUpdateIssue={handleUpdateIssue}
          onNewTask={() => setCreating(true)}
        />
      )}

      <NewIssueDialog
        open={creating}
        onOpenChange={setCreating}
        agents={agents}
        projects={projects}
        defaultProjectId={projectId}
        onCreated={() => {
          setCreating(false);
          mutate(`/teams${teamsQueryKeys.issuesByProject(projectId)}`);
        }}
      />
    </div>
  );
}
