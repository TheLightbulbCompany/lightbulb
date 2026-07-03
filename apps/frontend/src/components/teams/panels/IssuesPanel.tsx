"use client";

import { useMemo, useState } from "react";
import { X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useUser } from "@clerk/nextjs";
import {
  EditorialButton,
  PanelMasthead,
  PanelToolbar,
} from "@/components/teams/shell";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { TasksList } from "@/components/teams/issues/TasksList";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { replaceSearchParams } from "@/lib/shellNav";
import type {
  CompanyAgent,
  Issue,
  IssueProject,
  IssueUpdateInput,
} from "@/components/teams/shared/types";

// Route stays /teams/issues; the surface is relabelled "Tasks" everywhere the
// user reads it. Internally Paperclip still calls these rows "issues" — the BFF
// and detail routes are unchanged.
export function IssuesPanel() {
  const { read } = useTeamsApi();
  const { update } = useIssueMutations();
  // `?agent=<id>` is set by the /chat roster's "See her work →" deep link.
  // The /issues BFF takes no server agent filter for this surface, so we
  // filter client-side on `assigneeAgentId` (the inbox does the same).
  // Standalone /teams never sets this param → no filter.
  const searchParams = useSearchParams();
  const agentFilter = searchParams.get("agent");

  const { data, isLoading, error, mutate } = read<{ issues: Issue[] }>(
    "/issues",
  );
  // NewIssueDialog renders Project + Assignee pickers only when these arrays
  // are non-empty (its defaults are []). TasksList also resolves assignee /
  // status-circle names from the roster, and feeds agents to the board.
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsData } = read<{ projects: IssueProject[] }>(
    "/projects",
  );
  // Live-agent-runs feed → the set of task ids with an active run, which lights
  // the board "Live" badge + the list-row live dot. Derived the same way the
  // dashboard does (useLiveRuns → isRunActive); SWR self-refreshes the feed.
  const { runs: liveRuns } = useLiveRuns();
  const { user } = useUser();
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  // The sidebar create pill's "New task" routes here with ?new=task. Derive the
  // dialog-open from that intent OR local state — rather than a setState-in-
  // effect, which would cascade renders — and strip the param on close so a
  // refresh or Back doesn't re-open it. Mirrors the ?agent filter-read above:
  // the URL stays the single source of truth.
  const wantsNewTask = searchParams.get("new") === "task";
  const newTaskOpen = creating || wantsNewTask;
  const closeNewTask = () => {
    setCreating(false);
    if (searchParams.get("new")) {
      const params = new URLSearchParams(searchParams.toString());
      params.delete("new");
      replaceSearchParams(params);
    }
  };

  const liveIssueIds = useMemo(() => {
    const ids = new Set<string>();
    for (const run of liveRuns) {
      if (run.issueId && (run.status === "queued" || run.status === "running")) {
        ids.add(run.issueId);
      }
    }
    return ids;
  }, [liveRuns]);

  const issues = data?.issues;
  const filtered = useMemo(() => {
    const list = issues ?? [];
    const q = search.trim().toLowerCase();
    return list.filter(
      (i) =>
        (!agentFilter || i.assigneeAgentId === agentFilter) &&
        (!q ||
          i.title.toLowerCase().includes(q) ||
          (i.identifier ?? "").toLowerCase().includes(q)),
    );
  }, [issues, search, agentFilter]);

  // Resolve the filtered agent's display name for the chip; fall back to a
  // generic label when the roster hasn't loaded (or the id isn't in it).
  const agentFilterName = useMemo(() => {
    if (!agentFilter) return null;
    const match = agentsData?.agents?.find((a) => a.id === agentFilter);
    return match?.name ?? "one agent";
  }, [agentFilter, agentsData]);

  // Dismiss chip: strip ONLY the `agent` param, preserving panel/etc.
  const clearAgentFilter = () => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("agent");
    replaceSearchParams(params);
  };

  // TasksList's inline status circle + board drag both patch the issue; route
  // them through the same mutation hook the rest of the issues surface uses,
  // then revalidate this panel's /issues read so the change shows immediately.
  const handleUpdateIssue = (id: string, dataPatch: Record<string, unknown>) => {
    void update(id, dataPatch as IssueUpdateInput).then(() => mutate());
  };

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Work"
        title="Tasks"
        actions={
          <EditorialButton variant="primary" onClick={() => setCreating(true)}>
            New task
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Search tasks..."
        searchAriaLabel="Search tasks"
      />
      {agentFilter ? (
        <div className="flex items-center gap-2">
          <span
            className="inline-flex items-center gap-1.5 rounded-full py-0.5 pl-3 pr-1.5 text-xs"
            style={{
              border: "1px solid var(--edge)",
              background: "var(--cream-deeper)",
              color: "var(--ink-soft)",
            }}
          >
            Filtered to {agentFilterName}
            <button
              type="button"
              onClick={clearAgentFilter}
              aria-label="Clear agent filter"
              title="Clear agent filter"
              className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full transition-colors hover:bg-[var(--edge)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)]"
              style={{ color: "var(--mute)" }}
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        </div>
      ) : null}

      <TasksList
        issues={filtered}
        agents={agentsData?.agents ?? []}
        liveIssueIds={liveIssueIds}
        currentUserId={user?.id ?? null}
        searchActive={search.trim().length > 0 || agentFilter !== null}
        isLoading={isLoading}
        onUpdateIssue={handleUpdateIssue}
        onNewTask={() => setCreating(true)}
      />

      <NewIssueDialog
        open={newTaskOpen}
        onOpenChange={(open) => (open ? setCreating(true) : closeNewTask())}
        agents={agentsData?.agents ?? []}
        projects={projectsData?.projects ?? []}
        onCreated={() => {
          closeNewTask();
          mutate();
        }}
      />
    </div>
  );
}
