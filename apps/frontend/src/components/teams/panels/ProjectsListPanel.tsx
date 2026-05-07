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
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Project {
  id: string;
  name: string;
  description?: string;
  budget_monthly_cents?: number;
}

export function ProjectsListPanel() {
  const { read } = useTeamsApi();
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
      <div className="editorial-page p-8 text-[var(--danger)]">
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
        <div className="font-medium text-[var(--ink)]">{p.name}</div>
      ),
    },
    {
      key: "description",
      header: "Description",
      cell: (p) => (
        <span className="text-sm text-[var(--ink-soft)]">
          {p.description ?? "—"}
        </span>
      ),
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (p) => (
        <a
          href={`/teams/projects/${p.id}`}
          className="text-sm hover:underline"
          style={{ color: "var(--forest)" }}
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
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
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
          <span className="text-sm text-[var(--ink-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
            rows={3}
          />
        </label>
        {err && (
          <div className="text-[var(--danger)] text-sm border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
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
