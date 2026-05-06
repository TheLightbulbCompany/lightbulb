"use client";

import { useState } from "react";
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

  if (isLoading)
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  const projects = data?.projects ?? [];

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <div className="flex justify-between items-center mb-6">
        <h1 className="font-editorial-serif text-4xl text-[var(--ink)]">Projects</h1>
        <button
          onClick={() => setCreating(true)}
          className="editorial-btn editorial-btn--primary"
        >
          New project
        </button>
      </div>

      <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
        {projects.map((p) => (
          <li
            key={p.id}
            className="flex justify-between items-center p-4"
          >
            <div>
              <div className="font-medium text-[var(--ink)]">{p.name}</div>
              {p.description && (
                <div className="text-xs text-[var(--mute)]">{p.description}</div>
              )}
            </div>
            <a
              href={`/teams/projects/${p.id}`}
              className="text-sm text-[var(--ink-soft)] hover:underline"
            >
              Open →
            </a>
          </li>
        ))}
        {projects.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            No projects yet.
          </li>
        )}
      </ul>

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
    <div className="fixed inset-0 bg-[rgba(26,26,26,0.35)] flex items-center justify-center">
      <div className="editorial-card p-6 w-96">
        <h2 className="font-editorial-serif text-2xl mb-4 text-[var(--ink)]">New project</h2>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block mb-4">
          <span className="text-sm text-[var(--ink-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
            rows={3}
          />
        </label>
        {err && (
          <div className="text-[var(--danger)] text-sm mb-2 border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="editorial-btn editorial-btn--secondary editorial-btn--sm"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={submitting || !name}
            className="editorial-btn editorial-btn--primary editorial-btn--sm"
          >
            {submitting ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
