"use client";

import { useState, useMemo } from "react";
import { FolderKanban } from "lucide-react";
import {
  EditorialButton,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { CreateProjectDialog } from "@/components/teams/issues/CreateProjectDialog";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";

// The /projects envelope proxies the raw upstream project rows verbatim. (No
// budget column: upstream has no budget field on the project row — see the
// note in shared/types.ts.)
interface Project {
  id: string;
  name: string;
  description?: string;
}

export function ProjectsListPanel() {
  const { read } = useTeamsApi();
  // In-shell (provider present) → open the detail view inline; standalone
  // under /teams (no provider) → fall back to <a>.
  const nav = useInlineTeamsNav();
  const { data, isLoading, error, mutate } = read<{ projects: Project[] }>(
    "/projects",
  );
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const projects = data?.projects;
  const filtered = useMemo(() => {
    const list = projects ?? [];
    return search
      ? list.filter((p) =>
          (p.name + (p.description ?? ""))
            .toLowerCase()
            .includes(search.toLowerCase()),
        )
      : list;
  }, [projects, search]);

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--err)]">
        Error: {String(error)}
      </div>
    );
  }

  const columns: PanelDataColumn<Project>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      cell: (p) => (
        <div className="font-medium text-[var(--midnight)]">{p.name}</div>
      ),
    },
    {
      key: "description",
      header: "Description",
      cell: (p) => (
        <span className="text-sm text-[var(--midnight-soft)]">
          {p.description ?? "—"}
        </span>
      ),
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (p) =>
        nav ? (
          <button
            type="button"
            onClick={() => nav.openDetail("project-detail", p.id)}
            className="text-sm hover:underline"
            style={{ color: "var(--midnight)" }}
          >
            Open →
          </button>
        ) : (
          <a
            href={teamsDetailUrl("project", p.id)}
            onClick={(e) =>
              handleShellLinkClick(e, teamsDetailUrl("project", p.id))
            }
            className="text-sm hover:underline"
            style={{ color: "var(--midnight)" }}
          >
            Open →
          </a>
        ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-4 p-4 sm:p-5 lg:p-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Work"
        title="Projects"
        actions={
          <EditorialButton variant="primary" onClick={() => setCreating(true)}>
            New project
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find project..."
        searchAriaLabel="Search projects"
      />
      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--midnight-muted)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={FolderKanban}
          headline={search ? "No projects match your search." : "No projects yet."}
          supporting={
            search
              ? undefined
              : "Create a project to group related issues + measure progress on a deliverable."
          }
          action={{ label: "New project", onClick: () => setCreating(true) }}
        />
      ) : (
        <PanelDataTable<Project>
          rows={filtered}
          columns={columns}
          rowKey={(p) => p.id}
        />
      )}

      {creating && (
        <CreateProjectDialog
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            mutate();
          }}
        />
      )}
    </div>
  );
}
