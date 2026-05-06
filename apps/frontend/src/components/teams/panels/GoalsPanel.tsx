"use client";

import { useState, useMemo } from "react";
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

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <div className="flex justify-between items-center mb-6">
        <h1 className="font-editorial-serif text-4xl text-[var(--ink)]">Goals</h1>
        <button
          onClick={() => setCreating(true)}
          className="editorial-btn editorial-btn--primary"
        >
          New goal
        </button>
      </div>

      <ul className="editorial-card overflow-hidden">
        {tree.map((root) => (
          <GoalRow key={root.id} node={root} depth={0} />
        ))}
        {tree.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            No goals yet.
          </li>
        )}
      </ul>

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
    <div className="fixed inset-0 bg-[rgba(26,26,26,0.35)] flex items-center justify-center">
      <div className="editorial-card p-6 w-[28rem]">
        <h2 className="font-editorial-serif text-2xl mb-4 text-[var(--ink)]">New goal</h2>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
            rows={3}
          />
        </label>
        <label className="block mb-4">
          <span className="text-sm text-[var(--ink-soft)]">Parent goal ID (optional)</span>
          <input
            value={parentId}
            onChange={(e) => setParentId(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
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
            disabled={submitting || !title}
            className="editorial-btn editorial-btn--primary editorial-btn--sm"
          >
            {submitting ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
