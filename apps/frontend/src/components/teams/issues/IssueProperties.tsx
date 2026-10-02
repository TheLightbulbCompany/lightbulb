// apps/frontend/src/components/teams/issues/IssueProperties.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx sidebar block
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// The rail is the single editing surface for every task field — status,
// priority, assignee, and project rows are all interactive pickers — so each
// value has exactly one place it can be changed (the header stays a clean
// title-only masthead).
// The Watchdog row is ported from upstream Paperclip's IssueProperties.tsx
// (paperclip/ui/src/components/issue-properties/IssueProperties.tsx) (MIT,
// (c) 2025 Paperclip AI); the Work mode row mirrors the mode menu in upstream's
// IssueChatThread.tsx composer.

"use client";

import { useState, type ReactNode } from "react";
import { notifyApiError } from "@/lib/notify";
import Link from "next/link";
import { ArrowUpRight, Check, ChevronDown } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { EditorialButton } from "@/components/teams/shell";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { assignableMembers } from "@/components/teams/shared/lib/assignees";
import {
  workModeMetaFor,
  workModeMetaList,
} from "@/components/teams/shared/lib/workModeMeta";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import type {
  Goal,
  Issue,
  IssueStatus,
  IssuePriority,
  IssueMonitorPolicy,
  IssueMonitorState,
  IssueWatchdog,
  IssueWorkMode,
  CompanyAgent,
  CompanyMember,
  IssueProject,
} from "@/components/teams/shared/types";
import { GoalPicker } from "@/components/teams/goals/GoalPicker";
import { AssigneePicker } from "./pickers/AssigneePicker";
import { ProjectPicker } from "./pickers/ProjectPicker";
import { LabelPicker } from "./pickers/LabelPicker";
import { ParentPicker } from "./pickers/ParentPicker";
import { StageParticipantPicker } from "./pickers/StageParticipantPicker";
import { stageParticipantValues } from "./executionPolicy";
import { IssueRelations } from "./IssueRelations";

export interface IssuePropertiesProps {
  issue: Issue;
  agents?: CompanyAgent[];
  members?: CompanyMember[];
  projects?: IssueProject[];
  onStatusChange: (status: IssueStatus) => void;
  onPriorityChange: (priority: IssuePriority) => void;
  /** Assign an agent (or ``null`` to unassign). Clears any human assignee. */
  onAssigneeChange: (agentId: string | null) => void;
  /** Assign a human member by Clerk user id (or ``null`` to unassign). Clears
   * any agent assignee. Optional — without it the Assignee row stays
   * agent-only. */
  onAssigneeUserChange?: (userId: string | null) => void;
  onProjectChange: (projectId: string | null) => void;
  /** Link the task to a goal (or ``null`` to detach). Optional — without it
   * the Goal row stays a read-only link. */
  onGoalChange?: (goalId: string | null) => void;
  /** Receives the FULL desired blocker-id set (replace semantics). */
  onBlockedByChange: (ids: string[]) => void;
  /** Receives the FULL desired label-id set (replace semantics). Optional —
   * without it the Labels row stays read-only chips. */
  onLabelsChange?: (labelIds: string[]) => void;
  /** Reparent to an issue id or ``null`` to clear. Optional — without it the
   * Parent row stays a read-only link. */
  onParentChange?: (parentId: string | null) => void;
  /** Schedule (or reschedule) a monitor re-check. Receives the editor's
   * subset of the monitor policy; the host maps it onto executionPolicy.
   * Optional — without it the Monitor row stays read-only. */
  onMonitorChange?: (
    monitor: Pick<IssueMonitorPolicy, "nextCheckAt" | "notes" | "serviceName">,
  ) => void;
  /** Clear the scheduled monitor (sends executionPolicy: null). */
  onMonitorClear?: () => void;
  /** Set the FULL desired reviewer participant set (replace semantics —
   * `agent:<id>` / `user:<id>` values). The host rebuilds the review stage in
   * executionPolicy, preserving monitor + approvers. Optional — without it the
   * Reviewers row stays read-only chips. */
  onReviewersChange?: (values: string[]) => void;
  /** Set the FULL desired approver participant set (replace semantics). The
   * host rebuilds the approval stage, preserving monitor + reviewers. Optional —
   * without it the Approvers row stays read-only chips. */
  onApproversChange?: (values: string[]) => void;
  /** Trigger an immediate monitor re-check. Returns the underlying promise so
   * the row can show a transient checking/done state and tolerate errors. */
  onCheckNow?: (issueId: string) => Promise<void>;
  /** Switch how the task runs (PATCH workMode). Optional — without it the Work
   * mode row is a read-only label. */
  onWorkModeChange?: (workMode: IssueWorkMode) => void;
  /** Arm or update the task watchdog. Rejections surface inside the editor
   * (upstream 409s when the agent is not invokable). Optional — without it the
   * Watchdog row stays read-only. */
  onWatchdogSave?: (watchdog: {
    agentId: string;
    instructions: string | null;
  }) => Promise<void>;
  /** Remove the task watchdog. */
  onWatchdogRemove?: () => Promise<void>;
  className?: string;
}

function PropertyRow({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div
      className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
      style={{
        color: "var(--midnight-soft)",
        borderBottom: "1px solid var(--edge)",
      }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--midnight-muted)" } as const;

// Shared chip styling for the Labels and Related-Tasks rows so the read-only
// pills read identically across the rail.
const CHIP_STYLE = {
  background: "var(--cream-deep)",
  color: "var(--midnight-soft)",
  border: "1px solid var(--edge)",
} as const;

function EmptyDash() {
  return (
    <span data-empty="true" style={EMPTY_DASH_STYLE}>
      —
    </span>
  );
}

// The children endpoint returns either the `{ issues }` envelope (used by
// SubIssuesSection) or, on some BFF paths, a bare array — accept both so the
// count stays correct regardless of which shape the proxy forwards.
type ChildrenResponse = { issues: Issue[] } | Issue[];

// Same dual shape as the children endpoint — the goals BFF envelopes, but the
// bare-array form is accepted so a raw proxy passthrough still renders.
type GoalsResponse = { goals: Goal[] } | Goal[];

function childrenCount(data: ChildrenResponse | undefined): number {
  if (!data) return 0;
  if (Array.isArray(data)) return data.length;
  return data.issues?.length ?? 0;
}

/** Goal rail row. Self-fetches the company goal list the same way LabelPicker
 * and ParentPicker fetch theirs, so the host only has to wire the change
 * handler. Read-only (a link to the goal) when no handler is passed. */
function GoalRow({
  goalId,
  onChange,
}: {
  goalId: string | null;
  onChange?: (goalId: string | null) => void;
}) {
  const { read } = useTeamsApi();
  const { data } = read<GoalsResponse>("/goals");
  const goals = Array.isArray(data) ? data : (data?.goals ?? []);

  if (!onChange) {
    if (!goalId) return <EmptyDash />;
    const goal = goals.find((g) => g.id === goalId);
    const goalUrl = teamsDetailUrl("goal", goalId);
    return (
      <Link
        href={goalUrl}
        prefetch={false}
        onClick={(e) => handleShellLinkClick(e, goalUrl)}
        className="truncate hover:underline"
        style={{ color: "var(--midnight)" }}
      >
        {goal?.title ?? goalId.slice(0, 8)}
      </Link>
    );
  }

  return (
    <GoalPicker
      goals={goals}
      selectedGoalId={goalId}
      onChange={onChange}
      emptyLabel="No goal"
    />
  );
}

/** Read-only Parent link (used only when no onParentChange is wired). Fetches
 * the parent's identifier/title so the link reads like a task reference
 * (mirrors IssueParentBreadcrumb). */
function ParentLink({ parentId }: { parentId: string }) {
  const { read } = useTeamsApi();
  const { data: parent } = read<Issue>(`/issues/${parentId}`);
  const label = parent
    ? (parent.identifier ?? parent.id.slice(0, 8))
    : parentId.slice(0, 8);
  const title = parent?.title?.trim() ?? "";
  const parentUrl = teamsDetailUrl("issue", parentId);
  return (
    <Link
      href={parentUrl}
      prefetch={false}
      onClick={(e) => handleShellLinkClick(e, parentUrl)}
      className="inline-flex min-w-0 items-baseline gap-1.5 hover:underline"
      style={{ color: "var(--midnight)" }}
    >
      <span className="font-mono text-xs shrink-0">{label}</span>
      {title && <span className="truncate">{title}</span>}
    </Link>
  );
}

/** Read-only Sub-tasks count row. Always fetches children (cheap, and the
 * detail body fetches the same list, so SWR dedupes the request). */
function SubTasksRow({ issueId }: { issueId: string }) {
  const { read } = useTeamsApi();
  const { data } = read<ChildrenResponse>(`/issues/${issueId}/children`);
  const count = childrenCount(data);
  return (
    <PropertyRow label="Sub-tasks">
      {count > 0 ? (
        <span style={{ color: "var(--midnight-soft)" }}>
          {count} {count === 1 ? "sub-task" : "sub-tasks"}
        </span>
      ) : (
        <EmptyDash />
      )}
    </PropertyRow>
  );
}

// Convert an ISO instant to the value a <input type="datetime-local"> expects
// (local "YYYY-MM-DDTHH:mm", no timezone suffix). Returns "" for an unparseable
// or absent value so the input renders empty rather than "Invalid Date".
function isoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  // Shift by the local offset so toISOString's slice reflects local wall time.
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

/** Monitor rail row: schedule an agent re-check at a time, optionally naming an
 * external service, plus an immediate "Check now". When a monitor is set the
 * row reads "Re-check {time}" with the service/notes and Check-now/Clear
 * actions; when unset it shows a muted "Not scheduled" with a Schedule
 * affordance that opens a themed popover editor. Read-only (no editor, no
 * actions) when the host wires none of the monitor handlers. */
/** One line of live monitor state: how many times it has checked, when it last
 * fired, and when the wait gives up. Without this a monitored task reads as
 * stalled — "Re-check in 4h" alone does not say whether anything is happening.
 * Returns null when there is nothing yet worth saying. */
function monitorProgressLabel(state: IssueMonitorState | null | undefined): string | null {
  if (!state) return null;
  const parts: string[] = [];
  const attempts = state.attemptCount ?? 0;
  if (attempts > 0) {
    parts.push(`${attempts} check${attempts === 1 ? "" : "s"}`);
    if (state.maxAttempts) parts[parts.length - 1] += ` of ${state.maxAttempts}`;
  }
  if (state.lastTriggeredAt) parts.push(`last ${timeAgo(state.lastTriggeredAt)}`);
  if (state.timeoutAt) parts.push(`gives up ${timeAgo(state.timeoutAt)}`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

function MonitorRow({
  issueId,
  monitor,
  monitorState,
  onMonitorChange,
  onMonitorClear,
  onCheckNow,
}: {
  issueId: string;
  monitor: IssueMonitorPolicy | null;
  /** Live state from ``executionState.monitor``. Outlives ``monitor`` — a fired
   * monitor is stripped from the policy upstream but leaves its state behind. */
  monitorState?: IssueMonitorState | null;
  onMonitorChange?: (
    monitor: Pick<IssueMonitorPolicy, "nextCheckAt" | "notes" | "serviceName">,
  ) => void;
  onMonitorClear?: () => void;
  onCheckNow?: (issueId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  // Editor draft fields. Seeded from the current monitor when (re)opening.
  const [whenLocal, setWhenLocal] = useState("");
  const [notes, setNotes] = useState("");
  const [serviceName, setServiceName] = useState("");
  // Transient "Check now" feedback: idle → checking → done (auto-clears).
  const [checkState, setCheckState] = useState<"idle" | "checking" | "done">(
    "idle",
  );

  const openEditor = () => {
    setWhenLocal(isoToLocalInput(monitor?.nextCheckAt));
    setNotes(monitor?.notes ?? "");
    setServiceName(monitor?.serviceName ?? "");
    setOpen(true);
  };

  const schedule = () => {
    if (!whenLocal || !onMonitorChange) return;
    // datetime-local has no timezone; new Date() interprets it as local time,
    // and toISOString normalizes it to the ISO-8601 instant upstream wants.
    const iso = new Date(whenLocal).toISOString();
    onMonitorChange({
      nextCheckAt: iso,
      notes: notes.trim() || null,
      serviceName: serviceName.trim() || null,
    });
    setOpen(false);
  };

  const checkNow = async () => {
    if (!onCheckNow || checkState === "checking") return;
    setCheckState("checking");
    try {
      await onCheckNow(issueId);
      setCheckState("done");
      // Reset the label after a beat so the button reads "Check now" again.
      window.setTimeout(() => setCheckState("idle"), 2000);
    } catch (e) {
      // Surface the failure (a trial-gate 402 renders the upgrade toast) —
      // a silent drop back to idle read as "the button did nothing".
      notifyApiError(e, "Couldn't run the check");
      setCheckState("idle");
    }
  };

  const editor = (
    <PopoverContent
      align="start"
      className="w-72 p-2"
      style={{
        background: "var(--cream)",
        border: "1px solid var(--edge)",
        color: "var(--midnight)",
      }}
    >
      <label className="block text-xs mb-2" style={{ color: "var(--midnight-soft)" }}>
        Re-check at
        <input
          type="datetime-local"
          value={whenLocal}
          onChange={(e) => setWhenLocal(e.target.value)}
          aria-label="Re-check at"
          className="mt-1 w-full px-2 py-1.5 text-xs rounded-sm bg-transparent"
          style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
        />
      </label>
      <label className="block text-xs mb-2" style={{ color: "var(--midnight-soft)" }}>
        External service
        <input
          type="text"
          value={serviceName}
          onChange={(e) => setServiceName(e.target.value)}
          placeholder="e.g. GitHub Actions"
          aria-label="External service"
          maxLength={120}
          className="mt-1 w-full px-2 py-1.5 text-xs rounded-sm bg-transparent"
          style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
        />
      </label>
      <label className="block text-xs mb-2" style={{ color: "var(--midnight-soft)" }}>
        Notes
        <input
          type="text"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="What to look for"
          aria-label="Monitor notes"
          maxLength={500}
          className="mt-1 w-full px-2 py-1.5 text-xs rounded-sm bg-transparent"
          style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
        />
      </label>
      <div className="flex justify-end gap-2 pt-1">
        <EditorialButton
          variant="secondary"
          size="sm"
          onClick={() => setOpen(false)}
        >
          Cancel
        </EditorialButton>
        <EditorialButton
          variant="primary"
          size="sm"
          disabled={!whenLocal}
          onClick={schedule}
        >
          Schedule
        </EditorialButton>
      </div>
    </PopoverContent>
  );

  const progress = monitorProgressLabel(monitorState);

  if (monitor) {
    return (
      <PropertyRow label="Monitor">
        <div className="flex flex-col gap-1">
          <span style={{ color: "var(--midnight-soft)" }}>
            Re-check{" "}
            <time dateTime={monitor.nextCheckAt}>
              {timeAgo(monitor.nextCheckAt)}
            </time>
          </span>
          {monitor.serviceName && (
            <span className="text-xs" style={{ color: "var(--midnight-muted)" }}>
              {monitor.serviceName}
            </span>
          )}
          {monitor.notes && (
            <span className="text-xs" style={{ color: "var(--midnight-muted)" }}>
              {monitor.notes}
            </span>
          )}
          {progress && (
            <span className="text-xs" style={{ color: "var(--midnight-muted)" }}>
              {progress}
            </span>
          )}
          <span className="flex items-center gap-3 pt-0.5">
            {onCheckNow && (
              <button
                type="button"
                onClick={() => void checkNow()}
                disabled={checkState === "checking"}
                className="text-xs hover:underline disabled:opacity-50"
                style={{ color: "var(--midnight)" }}
              >
                {checkState === "checking"
                  ? "Checking…"
                  : checkState === "done"
                    ? "Checked"
                    : "Check now"}
              </button>
            )}
            {onMonitorChange && (
              <Popover
                open={open}
                onOpenChange={(next) => (next ? openEditor() : setOpen(false))}
              >
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    className="text-xs hover:underline"
                    style={{ color: "var(--midnight-muted)" }}
                  >
                    Edit
                  </button>
                </PopoverTrigger>
                {editor}
              </Popover>
            )}
            {onMonitorClear && (
              <button
                type="button"
                onClick={onMonitorClear}
                className="text-xs hover:underline"
                style={{ color: "var(--midnight-muted)" }}
              >
                Clear
              </button>
            )}
          </span>
        </div>
      </PropertyRow>
    );
  }

  // No live schedule, but a monitor ran here. Upstream consumes the policy entry
  // when it fires, so without this branch a task that just finished waiting
  // reads as "Not scheduled" — indistinguishable from one that never waited.
  // A fired monitor shows up two ways upstream: status "triggered", or status
  // "cleared" with clearReason "triggered" once the wake is done with it. Both
  // mean the wait paid off.
  const firedAt = monitorState?.lastTriggeredAt ?? monitorState?.clearedAt ?? null;
  const spentLabel =
    monitorState?.status === "triggered" || monitorState?.clearReason === "triggered"
      ? firedAt
        ? `Woke ${timeAgo(firedAt)}`
        : "Woke"
      : monitorState?.clearReason === "timeout_exceeded"
        ? "Timed out"
        : monitorState?.clearReason === "max_attempts_exhausted"
          ? "Gave up (out of attempts)"
          : null;

  return (
    <PropertyRow label="Monitor">
      <span className="inline-flex items-baseline gap-2">
        <span data-empty={spentLabel ? undefined : "true"} style={{ color: "var(--midnight-muted)" }}>
          {spentLabel ?? "Not scheduled"}
        </span>
        {onMonitorChange && (
          <Popover
            open={open}
            onOpenChange={(next) => (next ? openEditor() : setOpen(false))}
          >
            <PopoverTrigger asChild>
              <button
                type="button"
                className="text-xs hover:underline"
                style={{ color: "var(--midnight-muted)" }}
              >
                Schedule
              </button>
            </PopoverTrigger>
            {editor}
          </Popover>
        )}
      </span>
    </PropertyRow>
  );
}

/** Reviewers / Approvers rail row. Editable when the host wires the change
 * handler — a multi-select over agents + members whose selection drives the
 * matching executionPolicy stage. Falls back to read-only chips (or an em dash)
 * when no handler is wired. The participant labels resolve from agents/members;
 * an unresolved id reads as a truncated fallback so a not-yet-loaded roster
 * never blanks the row. */
function StageRow({
  label,
  noun,
  values,
  agents,
  members,
  onChange,
}: {
  label: string;
  noun: string;
  values: string[];
  agents: CompanyAgent[];
  members: CompanyMember[];
  onChange?: (values: string[]) => void;
}) {
  const valueLabel = (value: string): string => {
    if (value.startsWith("agent:")) {
      const id = value.slice("agent:".length);
      return agents.find((a) => a.id === id)?.name ?? id.slice(0, 8);
    }
    if (value.startsWith("user:")) {
      const id = value.slice("user:".length);
      const member = members.find((m) => m.userId === id);
      return member?.name ?? member?.email ?? "Unknown member";
    }
    return value;
  };

  return (
    <PropertyRow label={label}>
      {onChange ? (
        <StageParticipantPicker
          selected={values}
          agents={agents}
          members={members}
          onChange={onChange}
          noun={noun}
        />
      ) : values.length > 0 ? (
        <span className="flex flex-wrap gap-1">
          {values.map((value) => (
            <span
              key={value}
              className="rounded-sm px-1.5 py-0.5 text-xs"
              style={CHIP_STYLE}
            >
              {valueLabel(value)}
            </span>
          ))}
        </span>
      ) : (
        <EmptyDash />
      )}
    </PropertyRow>
  );
}

const POPOVER_STYLE = {
  background: "var(--cream)",
  border: "1px solid var(--edge)",
  color: "var(--midnight)",
} as const;

/** Work mode rail row: the mode chip opens upstream's composer mode menu (Auto /
 * Plan / Ask). Picking one PATCHes ``workMode``. Read-only when the host wires
 * no handler. */
function WorkModeRow({
  workMode,
  onChange,
}: {
  workMode: Issue["workMode"];
  onChange?: (workMode: IssueWorkMode) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = workModeMetaFor(workMode);
  const CurrentIcon = current.icon;

  if (!onChange) {
    return (
      <PropertyRow label="Work mode">
        <span className="inline-flex items-center gap-1.5">
          <CurrentIcon className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {current.label}
        </span>
      </PropertyRow>
    );
  }

  return (
    <PropertyRow label="Work mode">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={open}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold transition-colors",
              current.classes.chip,
            )}
          >
            <CurrentIcon className="h-3.5 w-3.5" aria-hidden />
            <span>{current.label}</span>
            <ChevronDown className="h-3 w-3 opacity-60" aria-hidden />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-44 p-1" style={POPOVER_STYLE}>
          {workModeMetaList().map((option) => {
            const Icon = option.icon;
            const active = option.value === current.value;
            return (
              <button
                key={option.value}
                type="button"
                aria-pressed={active}
                className={cn(
                  "flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-[var(--cream-deep)]",
                  active && "bg-[var(--cream-deeper)]",
                  option.classes.menuItem,
                )}
                onClick={() => {
                  if (!active) onChange(option.value);
                  setOpen(false);
                }}
              >
                <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
                <span>{option.label}</span>
                {active ? (
                  <Check className="h-3.5 w-3.5 shrink-0" aria-hidden />
                ) : null}
              </button>
            );
          })}
        </PopoverContent>
      </Popover>
    </PropertyRow>
  );
}

/** Watchdog rail row: name the agent that reviews this task's subtree when it
 * goes quiet, with optional instructions. When it fires, Paperclip opens a
 * "Watchdog review for …" child task for that agent; the row links to it.
 * Read-only (no editor) when the host wires no save handler. */
function WatchdogRow({
  issueId,
  watchdog,
  agents,
  onSave,
  onRemove,
}: {
  issueId: string;
  watchdog: IssueWatchdog | null;
  agents: CompanyAgent[];
  onSave?: (watchdog: {
    agentId: string;
    instructions: string | null;
  }) => Promise<void>;
  onRemove?: () => Promise<void>;
}) {
  const { read } = useTeamsApi();
  // Same list SubTasksRow reads (SWR dedupes); the watchdog's review task is a
  // child of this one, so its identifier comes from here.
  const { data: childrenData } = read<ChildrenResponse>(
    `/issues/${issueId}/children`,
  );
  const children = Array.isArray(childrenData)
    ? childrenData
    : (childrenData?.issues ?? []);
  const watchdogIssue = watchdog?.watchdogIssueId
    ? children.find((child) => child.id === watchdog.watchdogIssueId)
    : undefined;
  const watchdogIssueUrl = watchdogIssue
    ? teamsDetailUrl("issue", watchdogIssue.id)
    : null;

  const [open, setOpen] = useState(false);
  // Editor draft fields. Seeded from the current watchdog when (re)opening.
  const [agentInput, setAgentInput] = useState("");
  const [instructionsInput, setInstructionsInput] = useState("");
  const [pending, setPending] = useState<"save" | "remove" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const openEditor = () => {
    setAgentInput(watchdog?.watchdogAgentId ?? "");
    setInstructionsInput(watchdog?.instructions ?? "");
    setError(null);
    setOpen(true);
  };

  const run = async (kind: "save" | "remove", action: () => Promise<void>) => {
    setPending(kind);
    setError(null);
    try {
      await action();
      setOpen(false);
    } catch (e) {
      setError(
        e instanceof Error && e.message
          ? e.message
          : "Couldn't update the watchdog.",
      );
    } finally {
      setPending(null);
    }
  };

  const save = () => {
    if (!agentInput || !onSave) return;
    void run("save", () =>
      onSave({
        agentId: agentInput,
        instructions: instructionsInput.trim() || null,
      }),
    );
  };

  const remove = () => {
    if (watchdog && onRemove) void run("remove", onRemove);
    else setOpen(false);
  };

  const agentName = (id: string) =>
    agents.find((a) => a.id === id)?.name ?? id.slice(0, 8);
  const instructions = watchdog?.instructions?.trim();

  const summary = watchdog ? (
    <span
      className="inline-flex min-w-0 max-w-full items-center gap-1.5"
      title={instructions || undefined}
    >
      <span className="shrink-0 max-w-40 truncate">
        {agentName(watchdog.watchdogAgentId)}
      </span>
      {instructions ? (
        <span className="min-w-0 flex-1 truncate" style={EMPTY_DASH_STYLE}>
          · {instructions}
        </span>
      ) : null}
      {watchdog.status === "disabled" ? (
        <span className="shrink-0 text-xs" style={EMPTY_DASH_STYLE}>
          (disabled)
        </span>
      ) : null}
    </span>
  ) : (
    <span data-empty="true" style={EMPTY_DASH_STYLE}>
      None
    </span>
  );

  return (
    <PropertyRow label="Watchdog">
      <span className="flex min-w-0 items-center gap-1.5">
        {onSave ? (
          <Popover
            open={open}
            onOpenChange={(next) => (next ? openEditor() : setOpen(false))}
          >
            <PopoverTrigger asChild>
              <button
                type="button"
                className="min-w-0 max-w-full text-left hover:underline"
              >
                {summary}
              </button>
            </PopoverTrigger>
            <PopoverContent
              align="start"
              className="w-80 space-y-3 p-2"
              style={POPOVER_STYLE}
            >
              <label
                className="block text-xs font-medium"
                style={{ color: "var(--midnight)" }}
              >
                Watchdog agent
                <select
                  value={agentInput}
                  onChange={(e) => setAgentInput(e.target.value)}
                  className="mt-1.5 w-full px-2 py-1.5 text-xs font-normal rounded-sm bg-transparent"
                  style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
                >
                  <option value="">Select agent</option>
                  {agents
                    .filter(
                      (a) => a.status !== "terminated" || a.id === agentInput,
                    )
                    .map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                </select>
              </label>
              <label
                className="block text-xs font-medium"
                style={{ color: "var(--midnight)" }}
              >
                Instructions{" "}
                <span className="font-normal" style={EMPTY_DASH_STYLE}>
                  (optional)
                </span>
                <textarea
                  value={instructionsInput}
                  onChange={(e) => setInstructionsInput(e.target.value)}
                  placeholder="What should the watchdog watch for and how should it keep work moving?"
                  rows={4}
                  className="mt-1.5 w-full px-2 py-1.5 text-xs font-normal rounded-sm bg-transparent"
                  style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
                />
              </label>
              {watchdogIssue && watchdogIssueUrl ? (
                <div className="text-xs" style={EMPTY_DASH_STYLE}>
                  Watchdog task:{" "}
                  <Link
                    href={watchdogIssueUrl}
                    prefetch={false}
                    onClick={(e) => handleShellLinkClick(e, watchdogIssueUrl)}
                    className="hover:underline"
                    style={{ color: "var(--midnight)" }}
                  >
                    {watchdogIssue.identifier ?? "View task"}
                  </Link>
                </div>
              ) : null}
              {error ? (
                <div
                  role="alert"
                  className="rounded-sm px-2 py-1 text-xs"
                  style={{ border: "1px solid var(--err)", color: "var(--err)" }}
                >
                  {error}
                </div>
              ) : null}
              <div className="flex items-center justify-between">
                <button
                  type="button"
                  className="text-xs hover:underline disabled:opacity-50"
                  style={EMPTY_DASH_STYLE}
                  disabled={pending !== null || (!watchdog && !agentInput)}
                  onClick={remove}
                >
                  {pending === "remove" ? "Removing…" : "Remove"}
                </button>
                <EditorialButton
                  variant="primary"
                  size="sm"
                  disabled={!agentInput || pending !== null}
                  onClick={save}
                >
                  {pending === "save"
                    ? "Saving…"
                    : watchdog
                      ? "Update"
                      : "Set watchdog"}
                </EditorialButton>
              </div>
            </PopoverContent>
          </Popover>
        ) : (
          summary
        )}
        {watchdogIssueUrl ? (
          <Link
            href={watchdogIssueUrl}
            prefetch={false}
            onClick={(e) => handleShellLinkClick(e, watchdogIssueUrl)}
            className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded hover:bg-[var(--cream-deep)]"
            style={EMPTY_DASH_STYLE}
            title="Open watchdog task"
            aria-label="Open watchdog task"
          >
            <ArrowUpRight className="h-3 w-3" />
          </Link>
        ) : null}
      </span>
    </PropertyRow>
  );
}

export function IssueProperties({
  issue,
  agents = [],
  members = [],
  projects = [],
  onStatusChange,
  onPriorityChange,
  onAssigneeChange,
  onAssigneeUserChange,
  onProjectChange,
  onGoalChange,
  onBlockedByChange,
  onLabelsChange,
  onParentChange,
  onMonitorChange,
  onMonitorClear,
  onReviewersChange,
  onApproversChange,
  onCheckNow,
  onWorkModeChange,
  onWatchdogSave,
  onWatchdogRemove,
  className,
}: IssuePropertiesProps) {
  // Assignees and stage participants are Paperclip principals, so humans are
  // keyed by their Paperclip user id, not the Clerk id `/members` leads with.
  const people = assignableMembers(members);
  // Merge denormalized issue.project into the projects list so the trigger
  // shows the right name even before the /teams/projects fetch resolves.
  const projectsWithDenorm = (() => {
    const base = projects.slice();
    if (issue.project && !base.some((p) => p.id === issue.project!.id)) {
      base.unshift(issue.project);
    }
    return base;
  })();
  const projectId = issue.project?.id ?? null;

  const labels = issue.labels ?? [];
  const relatedIdentifiers = issue.referencedIssueIdentifiers ?? [];

  return (
    <dl className={cn("flex flex-col", className)}>
      <PropertyRow label="Status">
        <StatusIcon
          status={issue.status}
          blockerAttention={
            typeof issue.blockerAttention === "object"
              ? issue.blockerAttention
              : null
          }
          onChange={(s) => onStatusChange(s as IssueStatus)}
          showLabel
        />
      </PropertyRow>
      <PropertyRow label="Priority">
        {issue.priority ? (
          <PriorityIcon
            priority={issue.priority}
            onChange={(p) => onPriorityChange(p)}
            showLabel
          />
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      {/* Labels sits directly after Priority to match upstream's rail order
          (Status, Priority, Labels, Assignee, Project, …). Editable when the
          host wires onLabelsChange; falls back to read-only chips otherwise. */}
      <PropertyRow label="Labels">
        {onLabelsChange ? (
          <LabelPicker selected={labels} onChange={onLabelsChange} />
        ) : labels.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {labels.map((l) => (
              <span
                key={l.id}
                className="rounded-sm px-1.5 py-0.5 text-xs"
                style={CHIP_STYLE}
              >
                {l.name}
              </span>
            ))}
          </span>
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      <PropertyRow label="Assignee">
        <AssigneePicker
          agents={agents}
          selectedAgentId={issue.assigneeAgentId ?? null}
          onChange={onAssigneeChange}
          members={people}
          selectedUserId={issue.assigneeUserId ?? null}
          onUserChange={onAssigneeUserChange}
        />
      </PropertyRow>
      <WorkModeRow workMode={issue.workMode} onChange={onWorkModeChange} />
      <PropertyRow label="Project">
        <ProjectPicker
          projects={projectsWithDenorm}
          selectedProjectId={projectId}
          onChange={onProjectChange}
        />
      </PropertyRow>
      <PropertyRow label="Goal">
        <GoalRow goalId={issue.goalId ?? null} onChange={onGoalChange} />
      </PropertyRow>
      <PropertyRow label="Parent">
        {onParentChange ? (
          <ParentPicker
            issueId={issue.id}
            parentId={issue.parentId ?? null}
            onChange={onParentChange}
          />
        ) : issue.parentId ? (
          <ParentLink parentId={issue.parentId} />
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      <IssueRelations issue={issue} onBlockedByChange={onBlockedByChange} />
      <SubTasksRow issueId={issue.id} />
      <PropertyRow label="Related Tasks">
        {relatedIdentifiers.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {relatedIdentifiers.map((identifier) => (
              <span
                key={identifier}
                className="rounded-sm px-1.5 py-0.5 text-xs font-mono"
                style={CHIP_STYLE}
              >
                {identifier}
              </span>
            ))}
          </span>
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      {/* Reviewers + Approvers sit beside Monitor: all three are facets of the
          same executionPolicy, so grouping them keeps the gate controls
          together. Each drives one stage type; the host's merge helper keeps
          them (and Monitor) from clobbering one another. */}
      <StageRow
        label="Reviewers"
        noun="reviewer"
        values={stageParticipantValues(issue.executionPolicy, "review")}
        agents={agents}
        members={people}
        onChange={onReviewersChange}
      />
      <StageRow
        label="Approvers"
        noun="approver"
        values={stageParticipantValues(issue.executionPolicy, "approval")}
        agents={agents}
        members={people}
        onChange={onApproversChange}
      />
      <MonitorRow
        issueId={issue.id}
        monitor={issue.executionPolicy?.monitor ?? null}
        monitorState={issue.executionState?.monitor ?? null}
        onMonitorChange={onMonitorChange}
        onMonitorClear={onMonitorClear}
        onCheckNow={onCheckNow}
      />
      <WatchdogRow
        issueId={issue.id}
        watchdog={issue.watchdog ?? null}
        agents={agents}
        onSave={onWatchdogSave}
        onRemove={onWatchdogRemove}
      />
      <PropertyRow label="Created">
        {issue.createdAt ? (
          <time dateTime={issue.createdAt} style={{ color: "var(--midnight-muted)" }}>
            {timeAgo(issue.createdAt)}
          </time>
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      <PropertyRow label="Updated">
        {issue.updatedAt ? (
          <time dateTime={issue.updatedAt} style={{ color: "var(--midnight-muted)" }}>
            {timeAgo(issue.updatedAt)}
          </time>
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
    </dl>
  );
}
