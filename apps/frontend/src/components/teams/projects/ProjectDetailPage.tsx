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

import { useMemo, useState } from "react";
import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { useUser } from "@clerk/nextjs";
import { PanelTabs } from "@/components/teams/shell";
import { ProjectHeader } from "./ProjectHeader";
import { ProjectProperties } from "./ProjectProperties";
import { ProjectDescription } from "./ProjectDescription";
import { TasksList } from "@/components/teams/issues/TasksList";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import { useIssueExternalObjectSummaries } from "@/components/teams/issues/external-objects/useIssueExternalObjects";
import { useProjectDetail } from "./hooks/useProjectDetail";
import { useProjectMutations } from "./hooks/useProjectMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsPanelUrl } from "@/lib/shellNav";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { paperclipUserIdOf } from "@/components/teams/shared/lib/assignees";
import type {
  CompanyAgent,
  CompanyMember,
  Goal,
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
  const { read, mutate } = useTeamsApi();
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: membersResp } = read<{ members: CompanyMember[] }>("/members");
  const { data: projectsResp } = read<{ projects: IssueProject[] }>("/projects");
  // Company goals back the Goals row's "add" picker (id + title). Read
  // unconditionally per useTeamsApi's rules-of-hooks contract.
  const { data: goalsResp } = read<{ goals: Goal[] }>("/goals");
  // Live-agent-runs feed → ids of this project's tasks with an active run,
  // lighting the board "Live" badge + list-row live dot (same derivation as
  // the dashboard / IssuesPanel).
  const { runs: liveRuns } = useLiveRuns();
  const { user } = useUser();
  const agents = agentsResp?.agents ?? [];
  const projects = projectsResp?.projects ?? [];
  // Goal type carries `title`; ProjectProperties wants `{ id, title }` refs.
  const goalOptions = useMemo(
    () => (goalsResp?.goals ?? []).map((g) => ({ id: g.id, title: g.title })),
    [goalsResp],
  );

  const liveIssueIds = useMemo(() => {
    const ids = new Set<string>();
    for (const run of liveRuns) {
      if (run.issueId && (run.status === "queued" || run.status === "running")) {
        ids.add(run.issueId);
      }
    }
    return ids;
  }, [liveRuns]);

  const [tab, setTab] = useState<ProjectTab>("overview");
  const [creating, setCreating] = useState(false);
  // GitHub PR / issue rollups for the task rows: one batch request, and only
  // while the Tasks tab is the one showing.
  const externalObjectSummaries = useIssueExternalObjectSummaries(
    tab === "issues" ? issues.map((i) => i.id) : [],
  );

  if (isLoading && !project) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-ui italic"
        style={{
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
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
        className="flex h-full flex-col items-center justify-center p-8 text-ui"
        style={{ color: "var(--err)" }}
      >
        <p>Failed to load project.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--midnight-muted)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  if (!project) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-ui italic"
        style={{
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
        }}
      >
        Project not found.
      </div>
    );
  }

  // TasksList's inline status circle + board drag patch the issue. The patch
  // lands in this project's cached issues read at once (keyed by the resolved
  // uuid, as useProjectDetail reads it), rolls back on failure, then
  // revalidates. Returned so the board can report a failed move.
  const handleUpdateIssue = (id: string, patch: Record<string, unknown>) =>
    issueMutations.updateInList(
      teamsQueryKeys.issuesByProject(project.id),
      id,
      patch as IssueUpdateInput,
    );

  const tabs = [
    { value: "overview", label: "Overview" },
    { value: "issues", label: "Issues", count: issues.length },
  ];

  return (
    <div
      className="flex flex-col gap-4 p-4 sm:p-5 lg:p-6 max-w-[1600px] mx-auto w-full"
      style={{ background: "var(--cream)", color: "var(--midnight)" }}
    >
      <nav
        className="flex items-center gap-1 text-caption"
        style={{ color: "var(--midnight-muted)" }}
        aria-label="Breadcrumb"
      >
        <Link
          href={teamsPanelUrl("projects")}
          prefetch={false}
          onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("projects"))}
          className="hover:underline"
        >
          Projects
        </Link>
        <ChevronRight size={12} strokeWidth={1.75} />
        <span className="truncate" style={{ color: "var(--midnight-soft)" }}>
          {project.name}
        </span>
      </nav>

      <PanelTabs
        tabs={tabs}
        value={tab}
        onValueChange={(v) => setTab(v as ProjectTab)}
      />

      {tab === "overview" && (
        <div className="grid grid-cols-1 @3xl:grid-cols-[minmax(0,1fr)_320px] gap-6">
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
          <aside className="@3xl:sticky @3xl:top-4 @3xl:self-start">
            <ProjectProperties
              project={project}
              issueCount={issues.length}
              goals={goalOptions}
              onStatusChange={async (status) => {
                await mutations.update(projectId, { status });
              }}
              onGoalsChange={async (goalIds) => {
                // Replace-semantics: send the whole desired set. The BFF
                // whitelists `goal_ids` (snake_case) and aliases it to
                // `goalIds` on the wire to upstream's updateProjectSchema.
                await mutations.update(projectId, { goal_ids: goalIds });
              }}
            />
          </aside>
        </div>
      )}

      {tab === "issues" && (
        <TasksList
          issues={issues}
          agents={agents}
          members={membersResp?.members ?? []}
          liveIssueIds={liveIssueIds}
          externalObjectSummaries={externalObjectSummaries}
          currentUserId={paperclipUserIdOf(membersResp?.members, user?.id)}
          projectId={projectId}
          isLoading={issuesLoading}
          onUpdateIssue={handleUpdateIssue}
          onNewTask={() => setCreating(true)}
        />
      )}

      <NewIssueDialog
        open={creating}
        onOpenChange={setCreating}
        agents={agents}
        members={membersResp?.members ?? []}
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
