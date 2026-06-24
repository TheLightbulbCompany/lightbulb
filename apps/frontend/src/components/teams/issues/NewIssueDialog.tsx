// Ported from upstream Paperclip's NewIssueDialog
// (paperclip/ui/src/components/NewIssueDialog.tsx) (MIT, (c) 2025 Paperclip AI).
// Title + description + priority + status + work mode + assignee + project.
// Drops markdown editor, attachments, reviewer/approver, execution-workspace,
// model overrides, labels, parent-issue picker, draft autosave. Status and
// work mode are presets the BFF forwards to upstream's createIssueSchema, so
// the composer files straight into the chosen status/mode without a PATCH.
// Wrapped in <EditorialDialog>; Cancel / Create are <EditorialButton>s.

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
  IssueStatus,
  IssueWorkMode,
} from "@/components/teams/shared/types";

const PRIORITY_OPTIONS: { value: IssuePriority; label: string }[] = [
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

// Only the subset of IssueStatus the BFF CreateIssueBody whitelists for a
// fresh task — drops upstream's terminal/legacy aliases (pending, review,
// won_t_do, duplicate, open, closed) that aren't valid starting statuses.
const STATUS_OPTIONS: { value: IssueStatus; label: string }[] = [
  { value: "backlog", label: "Backlog" },
  { value: "todo", label: "Todo" },
  { value: "in_progress", label: "In progress" },
  { value: "in_review", label: "In review" },
  { value: "done", label: "Done" },
  { value: "blocked", label: "Blocked" },
  { value: "cancelled", label: "Cancelled" },
];

const WORK_MODE_OPTIONS: { value: IssueWorkMode; label: string }[] = [
  { value: "standard", label: "Standard" },
  { value: "ask", label: "Ask" },
  { value: "planning", label: "Planning" },
];

export interface NewIssueDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agents?: CompanyAgent[];
  projects?: IssueProject[];
  /** Called with the created issue id on success. */
  onCreated?: (issueId: string) => void;
  /** When set, the dialog creates a sub-issue under this parent. */
  parentId?: string;
  /** Display label for the parent (e.g. "ISO-12 Title"). */
  parentLabel?: string;
  /**
   * Pre-selects (and seeds the reset baseline for) the Project picker — used
   * by the Project-detail Issues tab so a new task lands in that project.
   */
  defaultProjectId?: string;
}

export function NewIssueDialog({
  open,
  onOpenChange,
  agents = [],
  projects = [],
  onCreated,
  parentId,
  parentLabel,
  defaultProjectId,
}: NewIssueDialogProps) {
  const { create, createChild } = useIssueMutations();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState<IssuePriority>("medium");
  // Resting choice mirrors the server default (backlog/standard) so leaving the
  // controls untouched produces the same task the bare composer always did.
  const [status, setStatus] = useState<IssueStatus>("backlog");
  const [workMode, setWorkMode] = useState<IssueWorkMode>("standard");
  const [projectId, setProjectId] = useState<string>(defaultProjectId ?? "");
  const [assigneeAgentId, setAssigneeAgentId] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setTitle("");
    setDescription("");
    setPriority("medium");
    setStatus("backlog");
    setWorkMode("standard");
    setProjectId(defaultProjectId ?? "");
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
      const payload = {
        title: trimmedTitle,
        description: description.trim() || undefined,
        priority,
        status,
        workMode,
        projectId: projectId || undefined,
        assigneeAgentId: assigneeAgentId || undefined,
      };
      const issue = parentId ? await createChild(parentId, payload) : await create(payload);
      onCreated?.(issue.id);
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create task");
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
      title={parentId ? "New sub-task" : "New task"}
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
            {submitting ? "Creating…" : "Create task"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {parentId && (
          <p className="text-sm" style={{ color: "var(--mute)" }}>
            Parent: <span style={{ color: "var(--ink)" }}>{parentLabel ?? parentId}</span>
          </p>
        )}
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
            placeholder="Task title"
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
        <div>
          <label
            htmlFor="new-issue-status"
            className="section-label"
            style={{ ...labelStyle, marginTop: 0 }}
          >
            <b>Status</b>
          </label>
          <select
            id="new-issue-status"
            value={status}
            onChange={(e) => setStatus(e.target.value as IssueStatus)}
            disabled={submitting}
            className="w-full rounded px-3 py-2 text-sm"
            style={selectStyle}
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label
            id="new-issue-workmode-label"
            className="section-label"
            style={{ ...labelStyle, marginTop: 0 }}
          >
            <b>Work mode</b>
          </label>
          {/* Minimal themed segmented control — there is no shared
              segmented/toggle in teams/ (PanelTabs is an underline tab strip),
              so the three modes render as an aria-labelled radiogroup of pills
              built from theme tokens. */}
          <div
            role="radiogroup"
            aria-labelledby="new-issue-workmode-label"
            className="flex gap-1 rounded p-1"
            style={selectStyle}
          >
            {WORK_MODE_OPTIONS.map((opt) => {
              const active = workMode === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => setWorkMode(opt.value)}
                  disabled={submitting}
                  className="flex-1 rounded px-3 py-1.5 text-sm font-medium transition-colors"
                  style={{
                    background: active ? "var(--forest)" : "transparent",
                    color: active ? "var(--cream)" : "var(--ink)",
                  }}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
          <p className="mt-1 text-xs" style={labelStyle}>
            Standard runs autonomously; Ask pauses for input; Planning plans first.
          </p>
        </div>
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
