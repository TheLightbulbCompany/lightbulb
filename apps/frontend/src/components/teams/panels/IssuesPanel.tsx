"use client";

import { useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { IssuePriority } from "@/components/teams/shared/types";

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

  if (isLoading)
    return (
      <div
        className="p-8 italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Loading…
      </div>
    );
  if (error)
    return (
      <div className="p-8" style={{ color: "var(--danger)" }}>
        Error: {String(error)}
      </div>
    );

  return (
    <div className="p-8 max-w-4xl" style={{ color: "var(--ink)" }}>
      <div className="flex justify-between items-center mb-6">
        <h1
          className="font-editorial-serif text-3xl"
          style={{ color: "var(--ink)" }}
        >
          Issues
        </h1>
        <button
          onClick={() => setCreating(true)}
          className="editorial-btn editorial-btn--primary editorial-btn--sm"
        >
          New issue
        </button>
      </div>

      <ul
        className="rounded"
        style={{
          border: "1px solid var(--edge)",
          background: "var(--cream)",
        }}
      >
        {(data?.issues ?? []).map((it, idx, arr) => (
          <li
            key={it.id}
            className="flex justify-between items-center p-4"
            style={
              idx < arr.length - 1
                ? { borderBottom: "1px solid var(--edge)" }
                : undefined
            }
          >
            <div>
              <div className="font-medium" style={{ color: "var(--ink)" }}>
                {it.title}
              </div>
              <div className="text-xs" style={{ color: "var(--mute)" }}>
                {it.status ?? "open"}
                {it.priority ? ` · ${it.priority}` : ""}
              </div>
            </div>
            <a
              href={`/teams/issues/${it.id}`}
              className="text-sm hover:underline"
              style={{ color: "var(--forest)" }}
            >
              Open →
            </a>
          </li>
        ))}
        {(data?.issues ?? []).length === 0 && (
          <li
            className="p-4 text-sm italic"
            style={{
              fontFamily: "var(--font-lora-serif), serif",
              color: "var(--mute)",
            }}
          >
            No issues yet.
          </li>
        )}
      </ul>

      {creating && (
        <CreateIssueDialog
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

function CreateIssueDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [projectId, setProjectId] = useState("");
  const [assigneeAgentId, setAssigneeAgentId] = useState("");
  const [priority, setPriority] = useState<IssuePriority | "">("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // Body fields match CreateIssueBody (Pydantic, extra="forbid"):
      // title (req), description?, project_id?, assignee_agent_id?, priority?
      const body: Record<string, unknown> = { title };
      if (description) body.description = description;
      if (projectId) body.project_id = projectId;
      if (assigneeAgentId) body.assignee_agent_id = assigneeAgentId;
      if (priority) body.priority = priority;
      await post("/issues", body);
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  const inputStyle = {
    background: "var(--cream-deeper)",
    border: "1px solid var(--edge)",
    color: "var(--ink)",
  } as const;

  return (
    <div
      className="fixed inset-0 flex items-center justify-center"
      style={{ background: "rgba(26, 26, 26, 0.45)" }}
    >
      <div
        className="rounded-lg p-6 w-[28rem]"
        style={{
          background: "var(--cream)",
          border: "1px solid var(--edge)",
          color: "var(--ink)",
        }}
      >
        <h2
          className="font-editorial-serif text-2xl mb-4"
          style={{ color: "var(--ink)" }}
        >
          New issue
        </h2>
        <label className="block mb-3">
          <span className="text-sm" style={{ color: "var(--ink-soft)" }}>
            Title
          </span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full rounded px-3 py-2 mt-1"
            style={inputStyle}
          />
        </label>
        <label className="block mb-3">
          <span className="text-sm" style={{ color: "var(--ink-soft)" }}>
            Description
          </span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full rounded px-3 py-2 mt-1"
            rows={3}
            style={inputStyle}
          />
        </label>
        <label className="block mb-3">
          <span className="text-sm" style={{ color: "var(--ink-soft)" }}>
            Project ID (optional)
          </span>
          <input
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="w-full rounded px-3 py-2 mt-1"
            style={inputStyle}
          />
        </label>
        <label className="block mb-3">
          <span className="text-sm" style={{ color: "var(--ink-soft)" }}>
            Assignee agent ID (optional)
          </span>
          <input
            value={assigneeAgentId}
            onChange={(e) => setAssigneeAgentId(e.target.value)}
            className="w-full rounded px-3 py-2 mt-1"
            style={inputStyle}
          />
        </label>
        <label className="block mb-4">
          <span className="text-sm" style={{ color: "var(--ink-soft)" }}>
            Priority
          </span>
          <select
            value={priority}
            onChange={(e) => setPriority(e.target.value as IssuePriority | "")}
            className="w-full rounded px-3 py-2 mt-1"
            style={inputStyle}
          >
            <option value="">None</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="critical">Critical</option>
          </select>
        </label>
        {err && (
          <div
            className="text-sm mb-2"
            style={{ color: "var(--danger)" }}
          >
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
