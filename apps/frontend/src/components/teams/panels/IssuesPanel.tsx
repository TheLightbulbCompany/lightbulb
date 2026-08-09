"use client";

import { useMemo, useState } from "react";
import { Repeat, X } from "lucide-react";
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
import type {
  CompanyAgent,
  Goal,
  Issue,
  IssueProject,
  IssueUpdateInput,
} from "@/components/teams/shared/types";

export interface IssuesPanelProps {
  /** Agent-scope filter (from the roster's "See her work →"). null = no filter. */
  agentFilter?: string | null;
  /** One-shot intent to auto-open the create dialog (from the "New task" pill). */
  newTask?: boolean;
  /** Fired when the create dialog closes — the shell strips its one-shot param. */
  onNewTaskConsumed?: () => void;
  /** Fired when the filter chip is dismissed — the shell clears its durable param. */
  onAgentFilterCleared?: () => void;
}

// Route stays /teams/issues; the surface is relabelled "Tasks" everywhere the
// user reads it. Internally Paperclip still calls these rows "issues" — the BFF
// and detail routes are unchanged.
//
// The panel is prop-driven: each shell (mobile /chat page, desktop dock) reads
// its own channel (URL vs dockview params) and feeds `agentFilter` / `newTask`
// down, and strips its channel via the consume callbacks. With all props
// absent the panel renders its unfiltered, no-dialog default (standalone/test).
export function IssuesPanel({
  agentFilter: agentFilterProp = null,
  newTask = false,
  onNewTaskConsumed,
  onAgentFilterCleared,
}: IssuesPanelProps = {}) {
  const { read } = useTeamsApi();
  const { update } = useIssueMutations();
  // The /issues BFF takes no server agent filter for this surface, so we filter
  // client-side on `assigneeAgentId` (the inbox does the same).
  const agentFilter = agentFilterProp ?? null;

  // Self-healing backstop for a board that can otherwise go silently stale.
  // The event-driven fast path stays primary: TeamsEventsProvider maps
  // `teams.activity.logged` (the BFF emits it with action "issue.created")
  // onto this `/teams/issues` key. But that signal rides a replay-less,
  // multi-hop Paperclip→gateway bridge — an agent creating tasks during a run
  // (e.g. Friday at onboarding) can emit its `issue.created` events before this
  // client is subscribed, and Paperclip has no cursor to replay them. With
  // `revalidateOnFocus` off globally (SwrProvider) and dock panels hidden (not
  // unmounted) on switch, nothing else revalidates this key — the board stayed
  // "No tasks yet" for minutes while GET /issues already returned the rows.
  // A 30s poll + focus revalidation heals it cheaply, mirroring the
  // ProvisioningStepper's event-fast-path-plus-polling backstop.
  // Routine-spawned tasks are excluded by default. A routine files a fresh
  // task every time it fires and upstream's dedup only holds while that task
  // is open with a live run, so an hourly routine leaves a finished row per
  // tick — they crowd out hand-filed work within days. The filter is applied
  // upstream (the list is capped by `limit`, so dropping rows client-side
  // would just return fewer real tasks), which makes it part of the SWR key.
  const [showRoutineTasks, setShowRoutineTasks] = useState(false);
  const { data, isLoading, error, mutate } = read<{ issues: Issue[] }>(
    showRoutineTasks ? "/issues" : "/issues?excludeRoutineExecutions=true",
    { refreshInterval: 30_000, revalidateOnFocus: true },
  );
  // NewIssueDialog renders Project + Assignee pickers only when these arrays
  // are non-empty (its defaults are []). TasksList also resolves assignee /
  // status-circle names from the roster, and feeds agents to the board.
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsData } = read<{ projects: IssueProject[] }>(
    "/projects",
  );
  const { data: goalsData } = read<{ goals: Goal[] }>("/goals");
  // Live-agent-runs feed → the set of task ids with an active run, which lights
  // the board "Live" badge + the list-row live dot. Derived the same way the
  // dashboard does (useLiveRuns → isRunActive); SWR self-refreshes the feed.
  const { runs: liveRuns } = useLiveRuns();
  const { user } = useUser();
  // Seed from the one-shot so a mount that already carries the intent opens the
  // dialog on first render (no effect); the prev-compare below covers re-delivery.
  const [creating, setCreating] = useState(newTask);
  const [search, setSearch] = useState("");

  // The sidebar create pill's "New task" delivers a one-shot `newTask` prop.
  // Seed the dialog-open from it and, so a second command to an already-mounted
  // panel re-opens the dialog, re-open on a false→true transition using the
  // render-time prev-compare pattern (React "adjust state when a prop changes")
  // rather than a setState-in-effect that would cascade renders. Closing reports
  // consumption so the shell strips its channel (URL param on mobile).
  const [prevNewTask, setPrevNewTask] = useState(newTask);
  if (newTask !== prevNewTask) {
    setPrevNewTask(newTask);
    if (newTask) setCreating(true);
  }
  const newTaskOpen = creating;
  const closeNewTask = () => {
    setCreating(false);
    onNewTaskConsumed?.();
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

  // Dismiss chip: report the clear to the shell, which drops its durable
  // channel (the ?agent URL param on mobile, the dock param on desktop).
  const clearAgentFilter = () => {
    onAgentFilterCleared?.();
  };

  // TasksList's inline status circle + board drag both patch the issue; route
  // them through the same mutation hook the rest of the issues surface uses,
  // then revalidate this panel's /issues read so the change shows immediately.
  // Returns the promise so TasksList's Hide can wait for the write before
  // showing its Undo toast (and see a failure if the PATCH rejects).
  const handleUpdateIssue = (id: string, dataPatch: Record<string, unknown>) =>
    update(id, dataPatch as IssueUpdateInput).then(() => mutate());

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
        filters={
          <button
            type="button"
            role="switch"
            aria-checked={showRoutineTasks}
            onClick={() => setShowRoutineTasks((v) => !v)}
            title="Tasks filed automatically by a routine on each run"
            className="inline-flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)]"
            style={{
              border: "1px solid var(--edge)",
              background: showRoutineTasks
                ? "var(--cream-deeper)"
                : "transparent",
              color: showRoutineTasks ? "var(--ink)" : "var(--mute)",
            }}
          >
            <Repeat className="h-3 w-3" aria-hidden="true" />
            Routine tasks
          </button>
        }
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
        goals={goalsData?.goals ?? []}
        onCreated={() => {
          closeNewTask();
          mutate();
        }}
      />
    </div>
  );
}
