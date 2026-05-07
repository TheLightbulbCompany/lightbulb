"use client";

import { useState, useMemo } from "react";
import { ListChecks } from "lucide-react";
import {
  EditorialButton,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Issue {
  id: string;
  title: string;
  status?: string;
  priority?: string;
  project_id?: string;
  assignee_agent_id?: string;
}

export function IssuesPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error, mutate } = read<{ issues: Issue[] }>(
    "/issues",
  );
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const issues = data?.issues ?? [];
  const filtered = useMemo(
    () =>
      search
        ? issues.filter((i) =>
            i.title.toLowerCase().includes(search.toLowerCase()),
          )
        : issues,
    [issues, search],
  );

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
      key: "open",
      header: "",
      align: "right",
      cell: (i) => (
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
        onCreated={() => {
          setCreating(false);
          mutate();
        }}
      />
    </div>
  );
}
