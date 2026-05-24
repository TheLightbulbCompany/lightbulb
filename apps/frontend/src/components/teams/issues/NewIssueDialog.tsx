// Ported from upstream Paperclip's NewIssueDialog
// (paperclip/ui/src/components/NewIssueDialog.tsx) (MIT, (c) 2025 Paperclip AI).
// Title + description + priority + project + assignee. Drops markdown editor,
// attachments, reviewer/approver, execution-workspace, model overrides,
// labels, parent-issue picker, draft autosave. Status is set server-side on
// create (BFF CreateIssueBody schema forbids it; use PATCH to change
// post-create). Wrapped in <EditorialDialog>; Cancel / Create are
// <EditorialButton>s.

"use client";

import { useState } from "react";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useIssueMutations } from "./hooks/useIssueMutations";
import type {
  CompanyAgent,
  IssueProject,
  IssuePriority,
} from "@/components/teams/shared/types";

const PRIORITY_OPTIONS: { value: IssuePriority; label: string }[] = [
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

export interface NewIssueDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agents?: CompanyAgent[];
  projects?: IssueProject[];
  /** Called with the created issue id on success. */
  onCreated?: (issueId: string) => void;
}

export function NewIssueDialog({
  open,
  onOpenChange,
  agents = [],
  projects = [],
  onCreated,
}: NewIssueDialogProps) {
  const { create } = useIssueMutations();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState<IssuePriority>("medium");
  const [projectId, setProjectId] = useState<string>("");
  const [assigneeAgentId, setAssigneeAgentId] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setTitle("");
    setDescription("");
    setPriority("medium");
    setProjectId("");
    setAssigneeAgentId("");
    setError(null);
  };

  const handleOpenChange = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  const submit = async () => {
    const trimmedTitle = title.trim();
    if (!trimmedTitle || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const issue = await create({
        title: trimmedTitle,
        description: description.trim() || undefined,
        priority,
        projectId: projectId || undefined,
        assigneeAgentId: assigneeAgentId || undefined,
      });
      onCreated?.(issue.id);
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create issue");
    } finally {
      setSubmitting(false);
    }
  };

  const labelStyle = { color: "var(--mute)" } as const;
  const selectStyle = {
    background: "var(--cream-deeper)",
    border: "1px solid var(--edge)",
    color: "var(--ink)",
  } as const;

  return (
    <EditorialDialog
      open={open}
      onOpenChange={handleOpenChange}
      title="New issue"
      footer={
        <>
          <EditorialButton
            variant="secondary"
            size="sm"
            onClick={() => handleOpenChange(false)}
            disabled={submitting}
          >
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={!title.trim() || submitting}
          >
            {submitting ? "Creating…" : "Create issue"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <div>
          <label
            htmlFor="new-issue-title"
            className="section-label"
            style={{ ...labelStyle, marginTop: 0 }}
          >
            <b>Title</b>
          </label>
          <Input
            id="new-issue-title"
            autoFocus
            placeholder="Issue title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            disabled={submitting}
            maxLength={200}
            style={selectStyle}
          />
        </div>
        <div>
          <label
            htmlFor="new-issue-description"
            className="section-label"
            style={{ ...labelStyle, marginTop: 0 }}
          >
            <b>Description</b>
          </label>
          <Textarea
            id="new-issue-description"
            placeholder="Optional details..."
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            disabled={submitting}
            maxLength={20000}
            style={selectStyle}
          />
        </div>
        <div>
          <label
            htmlFor="new-issue-priority"
            className="section-label"
            style={{ ...labelStyle, marginTop: 0 }}
          >
            <b>Priority</b>
          </label>
          <select
            id="new-issue-priority"
            value={priority}
            onChange={(e) => setPriority(e.target.value as IssuePriority)}
            disabled={submitting}
            className="w-full rounded px-3 py-2 text-sm"
            style={selectStyle}
          >
            {PRIORITY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        {projects.length > 0 && (
          <div>
            <label
              htmlFor="new-issue-project"
              className="section-label"
              style={{ ...labelStyle, marginTop: 0 }}
            >
              <b>Project</b>
            </label>
            <select
              id="new-issue-project"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              disabled={submitting}
              className="w-full rounded px-3 py-2 text-sm"
              style={selectStyle}
            >
              <option value="">No project</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        )}
        {agents.length > 0 && (
          <div>
            <label
              htmlFor="new-issue-assignee"
              className="section-label"
              style={{ ...labelStyle, marginTop: 0 }}
            >
              <b>Assign to agent</b>
            </label>
            <select
              id="new-issue-assignee"
              value={assigneeAgentId}
              onChange={(e) => setAssigneeAgentId(e.target.value)}
              disabled={submitting}
              className="w-full rounded px-3 py-2 text-sm"
              style={selectStyle}
            >
              <option value="">Unassigned</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </div>
        )}
        {error && (
          <p
            role="alert"
            className="text-sm"
            style={{ color: "var(--danger)" }}
          >
            {error}
          </p>
        )}
      </div>
    </EditorialDialog>
  );
}
