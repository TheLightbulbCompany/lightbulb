"use client";

import { useState, useMemo } from "react";
import { FolderKanban } from "lucide-react";
import {
  EditorialButton,
  EditorialDialog,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
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
          headline="No projects yet."
          supporting={
            search
              ? "No projects match your search."
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

function CreateProjectDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // CreateProjectBody (Task 3): name (req), description?
      const body: Record<string, unknown> = { name };
      if (description) body.description = description;
      await post("/projects", body);
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New project"
    >
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--midnight)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--midnight)]"
            rows={3}
          />
        </label>
        {err && (
          <div
            className="text-[var(--err)] text-sm rounded px-3 py-2"
            style={{
              border:
                "1px solid color-mix(in srgb, var(--err) 20%, transparent)",
              background:
                "color-mix(in srgb, var(--err) 6%, transparent)",
            }}
          >
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 mt-2">
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            size="sm"
            onClick={submit}
            disabled={submitting || !name}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
