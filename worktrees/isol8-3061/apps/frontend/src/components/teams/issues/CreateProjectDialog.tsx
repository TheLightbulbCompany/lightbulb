"use client";

// The "New project" dialog, lifted out of ProjectsListPanel for the same
// reason CreateGoalDialog left GoalsPanel: projects became a grouping on
// Tasks, that panel stopped mounting, and this was the only way to make one.

import { useState } from "react";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

export function CreateProjectDialog({
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
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
            rows={3}
          />
        </label>
        {err && (
          <div
            className="text-[var(--err)] text-sm rounded-sm px-3 py-2"
            style={{
              border:
                "1px solid var(--err)",
              background:
                "var(--cream)",
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
