// Ported from upstream Paperclip's NewIssueDialog
// (paperclip/ui/src/components/NewIssueDialog.tsx) (MIT, (c) 2025 Paperclip AI).
// Title + description + priority + status + work mode + assignee + project.
// Drops markdown editor, attachments, reviewer/approver, execution-workspace,
// model overrides, labels, parent-issue picker, draft autosave. Status and
// work mode are presets the BFF forwards to upstream's createIssueSchema, so
// the composer files straight into the chosen status/mode without a PATCH.
// Wrapped in <EditorialDialog>; Cancel / Create are <EditorialButton>s.

"use client";

import { useEffect, useRef, useState } from "react";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useIssueMutations } from "./hooks/useIssueMutations";
import { assignableMembers, parseAssigneeValue } from "@/components/teams/shared/lib/assignees";
import { workModeMetaList } from "@/components/teams/shared/lib/workModeMeta";
import type {
  CompanyAgent,
  CompanyMember,
  Goal,
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

const WORK_MODE_OPTIONS = workModeMetaList();

export interface NewIssueDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agents?: CompanyAgent[];
  /** Workspace humans (`/members`). Those with a Paperclip identity are offered
   * as assignees; a human assignee wakes no agent. */
  members?: CompanyMember[];
  projects?: IssueProject[];
  /** Company goals. Like `projects`, the picker renders only when non-empty. */
  goals?: Goal[];
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
  /** Pre-fills the Title field and its reset baseline. */
  defaultTitle?: string;
  /** Pre-fills the Description field and its reset baseline. */
  defaultDescription?: string;
  /** Pre-selects the assignee and its reset baseline. */
  defaultAssigneeAgentId?: string;
  /**
   * Seeds the Status select (and its reset baseline). The onboarding first-task
   * step opens at `todo` to mirror Paperclip's onboarding task; the bare
   * composer omits this and falls back to the server-default `backlog`.
   */
  defaultStatus?: IssueStatus;
}

export function NewIssueDialog({
  open,
  onOpenChange,
  agents = [],
  members = [],
  projects = [],
  goals = [],
  onCreated,
  parentId,
  parentLabel,
  defaultProjectId,
  defaultTitle,
  defaultDescription,
  defaultAssigneeAgentId,
  defaultStatus,
}: NewIssueDialogProps) {
  const { create, createChild } = useIssueMutations();
  const [title, setTitle] = useState(defaultTitle ?? "");
  const [description, setDescription] = useState(defaultDescription ?? "");
  const [priority, setPriority] = useState<IssuePriority>("medium");
  // Resting choice mirrors the server default (backlog/standard) so leaving the
  // controls untouched produces the same task the bare composer always did.
  const [status, setStatus] = useState<IssueStatus>(defaultStatus ?? "backlog");
  const [workMode, setWorkMode] = useState<IssueWorkMode>("standard");
  const [projectId, setProjectId] = useState<string>(defaultProjectId ?? "");
  const [goalId, setGoalId] = useState<string>("");
  // A raw agent id, or `user:<paperclipUserId>` for a human (upstream's
  // encoding — parseAssigneeValue reads both).
  const [assignee, setAssignee] = useState<string>(
    defaultAssigneeAgentId ?? "",
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // On a cold first-run the roster (and thus the resolved CEO) can arrive after
  // the dialog has already opened. Seed the assignee the first time a non-empty
  // default appears, without remounting — so in-progress title/description edits
  // survive. The transition guard (prev was empty) means we never re-apply over a
  // choice the user made afterward.
  const prevDefaultAssignee = useRef(defaultAssigneeAgentId);
  useEffect(() => {
    const prev = prevDefaultAssignee.current;
    prevDefaultAssignee.current = defaultAssigneeAgentId;
    if (!prev && defaultAssigneeAgentId && !assignee) {
      setAssignee(defaultAssigneeAgentId);
    }
  }, [defaultAssigneeAgentId, assignee]);
  const people = assignableMembers(members);

  const reset = () => {
    setTitle(defaultTitle ?? "");
    setDescription(defaultDescription ?? "");
    setPriority("medium");
    setStatus(defaultStatus ?? "backlog");
    setWorkMode("standard");
    setProjectId(defaultProjectId ?? "");
    setGoalId("");
    setAssignee(defaultAssigneeAgentId ?? "");
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
      const { assigneeAgentId, assigneeUserId } = parseAssigneeValue(assignee);
      const payload = {
        title: trimmedTitle,
        description: description.trim() || undefined,
        priority,
        status,
        workMode,
        projectId: projectId || undefined,
        goalId: goalId || undefined,
        assigneeAgentId: assigneeAgentId || undefined,
        assigneeUserId: assigneeUserId || undefined,
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

  const labelStyle = { color: "var(--midnight-muted)" } as const;
  const selectStyle = {
    background: "var(--cream-deep)",
    border: "1px solid var(--edge)",
    color: "var(--midnight)",
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
          <p className="text-ui" style={{ color: "var(--midnight-muted)" }}>
            Parent: <span style={{ color: "var(--midnight)" }}>{parentLabel ?? parentId}</span>
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
            className="w-full rounded-sm px-3 py-2 text-ui"
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
            className="w-full rounded-sm px-3 py-2 text-ui"
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
            className="flex gap-1 rounded-sm p-1"
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
                  className="flex-1 rounded-md px-3 py-1.5 text-ui font-medium transition-colors"
                  style={{
                    background: active ? "var(--midnight)" : "transparent",
                    color: active ? "var(--cream)" : "var(--midnight)",
                  }}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
          <p className="mt-1 text-caption" style={labelStyle}>
            Auto does the work; Plan writes a plan for you to accept first; Ask answers without doing the work.
          </p>
        </div>
        {(agents.length > 0 || people.length > 0) && (
          <div>
            <label
              htmlFor="new-issue-assignee"
              className="section-label"
              style={{ ...labelStyle, marginTop: 0 }}
            >
              <b>Assignee</b>
            </label>
            <select
              id="new-issue-assignee"
              value={assignee}
              onChange={(e) => setAssignee(e.target.value)}
              disabled={submitting}
              className="w-full rounded-sm px-3 py-2 text-ui"
              style={selectStyle}
            >
              <option value="">Unassigned</option>
              {agents.length > 0 && (
                <optgroup label="Agents">
                  {agents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </optgroup>
              )}
              {people.length > 0 && (
                <optgroup label="People">
                  {people.map((m) => (
                    <option key={m.userId} value={`user:${m.userId}`}>
                      {m.name || m.email || "Workspace member"}
                    </option>
                  ))}
                </optgroup>
              )}
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
              className="w-full rounded-sm px-3 py-2 text-ui"
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
        {/* Top-level creates only. The children endpoint inherits the
            parent's goal when `goalId` is omitted (upstream
            `goalId: issueData.goalId ?? parent.goalId`), and
            `exclude_none` drops an explicit null too — so on a sub-task
            this control could not honour the "No goal" it displays.
            The rail's Goal row can change it right after creation.
            (`projectId` inherits the same way; that predates this and is
            left as-is rather than changed from under people.) */}
        {goals.length > 0 && !parentId && (
          <div>
            <label
              htmlFor="new-issue-goal"
              className="section-label"
              style={{ ...labelStyle, marginTop: 0 }}
            >
              <b>Goal</b>
            </label>
            <select
              id="new-issue-goal"
              value={goalId}
              onChange={(e) => setGoalId(e.target.value)}
              disabled={submitting}
              className="w-full rounded-sm px-3 py-2 text-ui"
              style={selectStyle}
            >
              <option value="">No goal</option>
              {goals.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.title}
                </option>
              ))}
            </select>
          </div>
        )}
        {error && (
          <p
            role="alert"
            className="text-ui"
            style={{ color: "var(--err)" }}
          >
            {error}
          </p>
        )}
      </div>
    </EditorialDialog>
  );
}
