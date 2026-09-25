"use client";

import { useEffect, useId, useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

type Issue = { id: string; identifier?: string | null; title?: string | null };
type Project = { id: string; name?: string | null; workspaces?: { id: string; name?: string | null }[] };
export type PickedWork = { kind: "issue"; issueId: string } | { kind: "workspace"; projectId: string; workspaceId: string };

/**
 * Search the company's tasks and project workspaces instead of typing ids. Both reads
 * are the existing company-fenced Teams routes; the server re-verifies the pick on save.
 */
export function WorkPicker({ disabled, onPick }: { disabled: boolean; onPick: (link: PickedWork) => void }) {
  const api = useTeamsApi();
  const id = useId();
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQ(text.trim()), 250);
    return () => clearTimeout(t);
  }, [text]);
  const issues = api.read<{ issues: Issue[] }>(
    `/issues?${new URLSearchParams({ ...(q && { q }), limit: "8", sortField: "updated", sortDir: "desc", excludeRoutineExecutions: "true" })}`,
    { shouldRetryOnError: false },
  );
  const projects = api.read<{ projects: Project[] }>("/projects", { shouldRetryOnError: false });
  const needle = q.toLowerCase();
  const workspaces = (projects.data?.projects ?? []).flatMap(project => (project.workspaces ?? []).map(workspace => ({
    projectId: project.id, workspaceId: workspace.id, label: `${project.name ?? "Project"} · ${workspace.name ?? "Workspace"}`,
  }))).filter(w => !needle || w.label.toLowerCase().includes(needle)).slice(0, 8);
  const tasks = issues.data?.issues ?? [];
  const loading = issues.isLoading || projects.isLoading;
  return <div className="space-y-2">
    <label htmlFor={id} className="block">Find a task or project</label>
    <input id={id} type="search" className="w-full rounded border bg-background p-2" autoFocus maxLength={200}
      placeholder="Search by title or project" value={text} onChange={event => setText(event.target.value)} />
    {loading && <p role="status">Searching…</p>}
    {(issues.error || projects.error) && <p role="status">Some work could not be loaded.</p>}
    {tasks.length > 0 && <section aria-label="Tasks"><p className="text-muted-foreground">Tasks</p>
      <ul className="space-y-1">{tasks.map(issue => <li key={issue.id}>
        <button type="button" className="w-full rounded p-1 text-left hover:bg-accent" disabled={disabled}
          onClick={() => onPick({ kind: "issue", issueId: issue.id })}>
          {issue.identifier && <span className="text-muted-foreground">{issue.identifier} · </span>}{issue.title ?? "Untitled task"}
        </button>
      </li>)}</ul></section>}
    {workspaces.length > 0 && <section aria-label="Project workspaces"><p className="text-muted-foreground">Project workspaces</p>
      <ul className="space-y-1">{workspaces.map(w => <li key={w.workspaceId}>
        <button type="button" className="w-full rounded p-1 text-left hover:bg-accent" disabled={disabled}
          onClick={() => onPick({ kind: "workspace", projectId: w.projectId, workspaceId: w.workspaceId })}>{w.label}</button>
      </li>)}</ul></section>}
    {!loading && !tasks.length && !workspaces.length && <p>No matching work.</p>}
  </div>;
}
