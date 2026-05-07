"use client";

import { useState, useMemo } from "react";
import { Target } from "lucide-react";
import {
  EditorialButton,
  EditorialDialog,
  PanelEmptyState,
  PanelMasthead,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Goal {
  id: string;
  title: string;
  description?: string;
  parent_id?: string | null;
  status?: string;
}

interface GoalNode extends Goal {
  children: GoalNode[];
}

function buildTree(goals: Goal[]): GoalNode[] {
  const byId: Record<string, GoalNode> = {};
  goals.forEach((g) => {
    byId[g.id] = { ...g, children: [] };
  });
  const roots: GoalNode[] = [];
  goals.forEach((g) => {
    const node = byId[g.id];
    if (g.parent_id && byId[g.parent_id]) {
      byId[g.parent_id].children.push(node);
    } else {
      roots.push(node);
    }
  });
  return roots;
}

function GoalRow({ node, depth }: { node: GoalNode; depth: number }) {
  return (
    <>
      <li
        className="p-3 border-b border-[var(--edge)] last:border-b-0"
        style={{ paddingLeft: `${depth * 1.5 + 0.75}rem` }}
      >
        <div className="font-medium text-[var(--ink)]">{node.title}</div>
        {node.description && (
          <div className="text-xs text-[var(--mute)] mt-0.5">{node.description}</div>
        )}
      </li>
      {node.children.map((c) => (
        <GoalRow key={c.id} node={c} depth={depth + 1} />
      ))}
    </>
  );
}

export function GoalsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error, mutate } = read<{ goals: Goal[] }>("/goals");
  const [creating, setCreating] = useState(false);

  const tree = useMemo(() => buildTree(data?.goals ?? []), [data]);

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Strategy"
        title="Goals"
        actions={
          <EditorialButton variant="primary" onClick={() => setCreating(true)}>
            New goal
          </EditorialButton>
        }
      />

      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : tree.length === 0 ? (
        <PanelEmptyState
          icon={Target}
          headline="No goals yet."
          supporting="Define a goal to give your agents direction — break it into nested sub-goals as needed."
          action={{ label: "New goal", onClick: () => setCreating(true) }}
        />
      ) : (
        <ul className="editorial-card overflow-hidden">
          {tree.map((root) => (
            <GoalRow key={root.id} node={root} depth={0} />
          ))}
        </ul>
      )}

      {creating && (
        <CreateGoalDialog
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

function CreateGoalDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [parentId, setParentId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // CreateGoalBody fields (Task 3): title, description?, parent_id?
      const body: Record<string, unknown> = { title };
      if (description) body.description = description;
      if (parentId) body.parent_id = parentId;
      await post("/goals", body);
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
      title="New goal"
    >
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
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
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Parent goal ID (optional)</span>
          <input
            value={parentId}
            onChange={(e) => setParentId(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
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
            disabled={submitting || !title}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
