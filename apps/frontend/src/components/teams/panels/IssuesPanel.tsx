"use client";

import { useState, useMemo } from "react";
import { ListChecks, X } from "lucide-react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  EditorialButton,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { IssueStatusMenu } from "@/components/teams/issues/IssueStatusMenu";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type {
  CompanyAgent,
  Issue,
  IssueProject,
} from "@/components/teams/shared/types";

export function IssuesPanel() {
  const { read } = useTeamsApi();
  // Inside the /chat shell a nav provider is present → open the detail view
  // in-shell. Standalone under /teams there's no provider → fall back to <a>.
  const nav = useInlineTeamsNav();
  // `?agent=<id>` is set by the /chat roster's "See her work →" deep link.
  // The /issues BFF takes no server agent filter, so we filter client-side on
  // `assigneeAgentId` (the inbox does the same). Standalone /teams never sets
  // this param → no filter.
  const searchParams = useSearchParams();
  const router = useRouter();
  const agentFilter = searchParams.get("agent");
  const { data, isLoading, error, mutate } = read<{ issues: Issue[] }>(
    "/issues",
  );
  // NewIssueDialog renders Project + Assignee pickers only when these arrays
  // are non-empty (its defaults are []). Without these reads, users creating
  // issues from this panel can't set projectId / assigneeAgentId.
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsData } = read<{ projects: IssueProject[] }>(
    "/projects",
  );
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const issues = data?.issues;
  const filtered = useMemo(() => {
    const list = issues ?? [];
    return list.filter(
      (i) =>
        (!agentFilter || i.assigneeAgentId === agentFilter) &&
        (!search || i.title.toLowerCase().includes(search.toLowerCase())),
    );
  }, [issues, search, agentFilter]);

  // Resolve the filtered agent's display name for the chip; fall back to a
  // generic label when the roster hasn't loaded (or the id isn't in it).
  const agentFilterName = useMemo(() => {
    if (!agentFilter) return null;
    const match = agentsData?.agents?.find((a) => a.id === agentFilter);
    return match?.name ?? "one agent";
  }, [agentFilter, agentsData]);

  // Dismiss chip: strip ONLY the `agent` param, preserving panel/view/etc.
  const clearAgentFilter = () => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("agent");
    const qs = params.toString();
    router.replace(qs ? `?${qs}` : "?", { scroll: false });
  };

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  const columns: PanelDataColumn<Issue>[] = [
    {
      key: "title",
      header: "Title",
      cell: (i) => (
        <div className="font-medium text-[var(--ink)]">{i.title}</div>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (i) => (
        <span className="text-sm text-[var(--ink-soft)]">
          {i.status ?? "open"}
        </span>
      ),
    },
    {
      key: "priority",
      header: "Priority",
      cell: (i) => (
        <span className="text-sm text-[var(--ink-soft)]">
          {i.priority ?? "—"}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (i) => <IssueStatusMenu issue={i} onChanged={mutate} />,
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (i) =>
        nav ? (
          <button
            type="button"
            onClick={() => nav.openDetail("issue-detail", i.id)}
            className="text-sm text-[var(--forest)] hover:underline"
          >
            Open →
          </button>
        ) : (
          <a
            href={`/teams/issues/${i.id}`}
            className="text-sm text-[var(--forest)] hover:underline"
          >
            Open →
          </a>
        ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Work"
        title="Issues"
        actions={
          <EditorialButton variant="primary" onClick={() => setCreating(true)}>
            New issue
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find issue..."
        searchAriaLabel="Search issues"
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
              className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full transition-colors hover:bg-[var(--edge)] focus-visible:outline-none"
              style={{ color: "var(--mute)" }}
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        </div>
      ) : null}
      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={ListChecks}
          headline="No issues yet."
          supporting={
            search
              ? "No issues match your search."
              : "Create an issue to track a piece of work — assign it to an agent and follow it through to done."
          }
          action={{ label: "New issue", onClick: () => setCreating(true) }}
        />
      ) : (
        <PanelDataTable<Issue>
          rows={filtered}
          columns={columns}
          rowKey={(i) => i.id}
        />
      )}

      <NewIssueDialog
        open={creating}
        onOpenChange={setCreating}
        agents={agentsData?.agents ?? []}
        projects={projectsData?.projects ?? []}
        onCreated={() => {
          setCreating(false);
          mutate();
        }}
      />
    </div>
  );
}
